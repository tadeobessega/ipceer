"""Cuotas de medicina prepaga: cuadros tarifarios que las entidades declaran a la Superintendencia de
Servicios de Salud (Res. SSS 645/2025), publicados en https://cuadrostarifarios.sssalud.gob.ar.

La página usa una API pública (la misma que consulta el navegador): por período (AAAAMM) y entidad
(RNEMP) devuelve cada plan con su valor por rango de edad y región, la tasa de aumento del mes y la
cantidad de cápitas (afiliados). Las entidades cargan los valores dentro de los 5 días hábiles
posteriores al dato del INDEC, así que el mes siguiente se conoce antes de que el INDEC lo publique.

Método (decisión D-35 de METODOLOGIA.md):
  - Celda = entidad, plan, tipo de plan, región, rango de edad y cantidad de cápitas declaradas en el
    período anterior (la ponderación).
  - Variación de una celda = valor del período / valor del período anterior, con las mismas celdas
    en los dos períodos (se descartan relativos fuera de [0,5; 2]).
  - Variación del mes = media geométrica de los relativos ponderada por las cápitas del período
    anterior (una celda sin cápitas declaradas pesa 1).
  - Va a la clase 06.4.1 (Gastos de prepagas) del INDEC, la misma en todas las regiones.
Solo se bajan las entidades de config/regulados/prepagas.yaml (las grandes concentran casi todas las
cápitas). Nada se completa: un período que no se pudo bajar queda sin dato.
"""
from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

import polars as pl
import requests
import yaml

from .config import Config

log = logging.getLogger(__name__)

API = "https://cuadrostarifarios.sssalud.gob.ar/api"
UA = {"User-Agent": "IPCEER/1.0 (indice de precios del CEER; +https://github.com/tadeobessega/ipceer)"}
POR_PAGINA = 50   # más de 50 por pedido hace que el servidor corte por tiempo
CLAVE = ["rnemp", "codigo_plan", "tipo_plan", "region", "rango_etario_desde", "rango_etario_hasta"]


def _dir(cfg: Config) -> Path:
    from .otras_fuentes import _dir as dir_otras
    return dir_otras(cfg, "prepagas")


def entidades(cfg: Config) -> list[dict]:
    ruta = cfg.dir_config / "regulados" / "prepagas.yaml"
    if not ruta.exists():
        return []
    return yaml.safe_load(ruta.read_text(encoding="utf-8")).get("entidades", [])


def _get(ruta: str, params: dict, reintentos: int = 3) -> dict:
    for i in range(reintentos):
        try:
            r = requests.get(f"{API}/{ruta}", params=params, headers=UA, timeout=60)
            if r.status_code == 200:
                return r.json()
            log.warning("Prepagas %s %s: HTTP %s", ruta, params, r.status_code)
        except requests.RequestException as e:
            log.warning("Prepagas %s %s: %s", ruta, params, e)
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"Prepagas: sin respuesta de {ruta} {params}")


def _total(periodo: int, rn: int) -> int:
    """Cuántas filas declaró la entidad para el período (un pedido de una fila: rápido)."""
    j = _get("getCuadrosTarifarios", {"page": 1, "per_page": 1, "periodo": periodo, "rnemp": rn})
    return int((j.get("paginacion") or {}).get("total") or 0)


def bajar_entidad(periodo: int, rn: int, pausa: float = 1.0) -> pl.DataFrame:
    filas, pagina, ultima = [], 1, 1
    while pagina <= ultima:
        j = _get("getCuadrosTarifarios", {"page": pagina, "per_page": POR_PAGINA, "periodo": periodo, "rnemp": rn})
        filas += j.get("data") or []
        ultima = (j.get("paginacion") or {}).get("ultima_pagina") or 1
        pagina += 1
        time.sleep(pausa)
    if not filas:
        return pl.DataFrame()
    cols = [*CLAVE, "periodo", "nombre_comercial", "nombre_plan", "valor_capital", "cantidad_capitas",
            "tasa_aumento_mensual", "permite_copago", "comercializable"]
    return pl.DataFrame(filas, infer_schema_length=None).select([c for c in cols if c in filas[0]])


def bajar_periodo(periodo: int, rnemps: list[int], pausa: float = 1.0) -> pl.DataFrame:
    partes = [bajar_entidad(periodo, rn, pausa) for rn in rnemps]
    partes = [x for x in partes if not x.is_empty()]
    return pl.concat(partes, how="diagonal_relaxed") if partes else pl.DataFrame()


def _partes(cfg: Config) -> Path:
    d = _dir(cfg) / "partes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _migrar(cfg: Config) -> None:
    """Parte los archivos por período que se bajaron antes de D-63 en un archivo por entidad, así no
    se vuelven a bajar."""
    d, dp = _dir(cfg), _partes(cfg)
    for a in d.glob("*.parquet"):
        if any(dp.glob(f"{a.stem}_*.parquet")):
            continue
        x = pl.read_parquet(a)
        if "rnemp" not in x.columns:
            continue
        for (rn,), g in x.group_by("rnemp"):
            g.write_parquet(dp / f"{a.stem}_{rn}.parquet", compression="zstd")


def _armar(cfg: Config, periodo: int) -> int:
    """El archivo del período es la unión de lo que declaró cada entidad."""
    ps = sorted(_partes(cfg).glob(f"{periodo}_*.parquet"))
    if not ps:
        return 0
    x = pl.concat([pl.read_parquet(p) for p in ps], how="diagonal_relaxed")
    x.write_parquet(_dir(cfg) / f"{periodo}.parquet", compression="zstd")
    return x.height


def actualizar(cfg: Config, max_periodos: int = 3, revisar: int = 6) -> dict:
    """D-63. Las entidades declaran con atraso y algunas saltean meses, así que en cada corrida:
    - se revisan los últimos `revisar` períodos entidad por entidad (un pedido de una fila cada una) y
      se baja de nuevo la entidad que apareció o cambió la cantidad de filas (los dos últimos períodos
      se bajan siempre: las entidades corrigen valores);
    - de los períodos más viejos se completan hasta `max_periodos` que todavía no se tienen;
    - una entidad que no responde no tira el período: queda anotada y se reintenta en la próxima corrida.
    Nada se completa ni se estima: lo que una entidad no declaró queda sin dato."""
    ents = entidades(cfg)
    if not ents:
        return {"error": "sin entidades en config/regulados/prepagas.yaml"}
    d, dp = _dir(cfg), _partes(cfg)
    _migrar(cfg)
    try:
        periodos = sorted(_get("getPeriodo", {}).get("data") or [], reverse=True)
    except RuntimeError as e:
        return {"error": str(e)}
    ruta_cob = d / "cobertura.json"
    cob = json.loads(ruta_cob.read_text(encoding="utf-8")) if ruta_cob.exists() else {}
    tengo = {int(p.stem) for p in d.glob("*.parquet")}
    viejos = [p for p in periodos[revisar:] if p not in tengo][:max_periodos]
    hechos, errores = [], []
    for k, p in enumerate(periodos[:revisar] + viejos):
        c = cob.setdefault(str(p), {})
        cambio = False
        for e in ents:
            rn, parte = e["rnemp"], dp / f"{p}_{e['rnemp']}.parquet"
            try:
                n = _total(p, rn)
                if n == 0:
                    c[str(rn)] = 0
                    continue
                if parte.exists() and c.get(str(rn)) == n and k >= 2:
                    continue
                x = bajar_entidad(p, rn)
                if x.is_empty():
                    continue
                x.write_parquet(parte, compression="zstd")
                c[str(rn)] = n
                cambio = True
            except Exception as ex:
                errores.append(f"{p} {e['nombre']}: {ex}")
        if cambio or (p not in tengo and any(dp.glob(f"{p}_*.parquet"))):
            log.info("Prepagas %s: %d filas", p, _armar(cfg, p))
            hechos.append(p)
    ruta_cob.write_text(json.dumps(cob, indent=1, sort_keys=True), encoding="utf-8")
    nombres = {str(e["rnemp"]): e["nombre"] for e in ents}
    sin = {p: [nombres.get(rn, rn) for rn, n in sorted(cob.get(str(p), {}).items()) if n == 0]
           for p in periodos[:revisar]}
    return {"periodos_publicados": periodos, "bajados": hechos, "errores": errores,
            "sin_declarar": {p: v for p, v in sin.items() if v},
            "faltan": sorted(set(periodos) - {int(x.stem) for x in d.glob("*.parquet")})}


def indice(cfg: Config) -> pl.DataFrame:
    """Variación mensual (periodo AAAA-MM, var_pct, n_celdas, capitas, tasa_declarada_pct)."""
    d = _dir(cfg)
    archivos = sorted(d.glob("*.parquet"))
    if len(archivos) < 2:
        return pl.DataFrame()
    def leer(p: Path) -> pl.DataFrame:
        return pl.read_parquet(p).filter(pl.col("valor_capital") > 0).unique(CLAVE, keep="first")
    filas = []
    for a, b in zip(archivos[:-1], archivos[1:]):
        pa, pb = int(a.stem), int(b.stem)
        if (pb // 100 * 12 + pb % 100) - (pa // 100 * 12 + pa % 100) != 1:
            continue   # períodos no consecutivos: no se compara a través del hueco
        x = (leer(a).select(*CLAVE, pl.col("valor_capital").alias("v0"),
                            pl.col("cantidad_capitas").fill_null(0).alias("w"))
             .join(leer(b).select(*CLAVE, pl.col("valor_capital").alias("v1"),
                                  pl.col("tasa_aumento_mensual").alias("tasa")), on=CLAVE)
             .with_columns((pl.col("v1") / pl.col("v0")).alias("rel"),
                           pl.when(pl.col("w") > 0).then(pl.col("w")).otherwise(1).alias("w"))
             .filter(pl.col("rel").is_between(0.5, 2.0)))
        if x.is_empty():
            continue
        lr = (x["rel"].log() * x["w"]).sum() / x["w"].sum()
        a0 = leer(a)
        cap_a = float(a0["cantidad_capitas"].fill_null(0).sum()) or 1.0
        ent_a, ent_ab = set(a0["rnemp"].to_list()), set(x["rnemp"].to_list())
        tasa = (x["tasa"].fill_null(float("nan")) * x["w"]).filter(x["tasa"].is_not_null()).sum() / \
            x["w"].filter(x["tasa"].is_not_null()).sum() if x["tasa"].is_not_null().any() else None
        filas.append({"periodo": f"{pb // 100}-{pb % 100:02d}", "var_pct": (math.exp(lr) - 1) * 100,
                      "n_celdas": x.height, "capitas": int(x["w"].sum()),
                      "cobertura_pct": round(100 * float(x["w"].sum()) / cap_a, 1),
                      "entidades": len(ent_ab), "faltan_entidades": sorted(ent_a - ent_ab),
                      "tasa_declarada_pct": tasa * 100 if tasa is not None and math.isfinite(tasa) else None})
    return pl.DataFrame(filas)


def relativos_externos(cfg: Config) -> pl.DataFrame:
    """Para la carrera de métodos: clase 06.4.1 en todas las regiones (mismo valor)."""
    from .config import REGIONES
    i = indice(cfg)
    if i.is_empty():
        return pl.DataFrame()
    return pl.concat([i.select("periodo", pl.lit("06.4.1").alias("codigo"), pl.lit(r).alias("region"),
                               (1 + pl.col("var_pct") / 100).log().alias("logrel"), pl.lit("prepagas").alias("fuente"))
                      for r in REGIONES])


def resumen(cfg: Config) -> dict:
    i = indice(cfg)
    return {"indice": i.to_dicts() if not i.is_empty() else [],
            "fuente": "Superintendencia de Servicios de Salud, cuadros tarifarios (Res. SSS 645/2025)",
            "url": "https://cuadrostarifarios.sssalud.gob.ar/"}


if __name__ == "__main__":   # prueba manual: python -m ipceer.prepagas
    from .config import cargar_config
    logging.basicConfig(level=logging.INFO)
    c = cargar_config()
    print(json.dumps(actualizar(c, 1), default=str)[:500])
