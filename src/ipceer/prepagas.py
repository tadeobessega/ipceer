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


def bajar_periodo(periodo: int, rnemps: list[int], pausa: float = 1.0) -> pl.DataFrame:
    filas = []
    for rn in rnemps:
        pagina, ultima = 1, 1
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


def actualizar(cfg: Config, max_periodos: int = 3) -> dict:
    """Baja los períodos que faltan (los más recientes primero) y vuelve a bajar los dos últimos,
    que las entidades pueden corregir. `max_periodos` limita cuántos se bajan por corrida."""
    ents = entidades(cfg)
    if not ents:
        return {"error": "sin entidades en config/regulados/prepagas.yaml"}
    d = _dir(cfg)
    try:
        periodos = sorted(_get("getPeriodo", {}).get("data") or [], reverse=True)
    except RuntimeError as e:
        return {"error": str(e)}
    tengo = {int(p.stem) for p in d.glob("*.parquet")}
    cola = [p for p in periodos[:2]] + [p for p in periodos[2:] if p not in tengo]
    hechos, errores = [], []
    for p in cola[:max_periodos]:
        try:
            df = bajar_periodo(p, [e["rnemp"] for e in ents])
            if df.is_empty():
                errores.append(f"{p}: vacío")
                continue
            df.write_parquet(d / f"{p}.parquet", compression="zstd")
            hechos.append(p)
            log.info("Prepagas %s: %d filas", p, df.height)
        except Exception as e:
            errores.append(f"{p}: {e}")
    return {"periodos_publicados": periodos, "bajados": hechos, "errores": errores,
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
        tasa = (x["tasa"].fill_null(float("nan")) * x["w"]).filter(x["tasa"].is_not_null()).sum() / \
            x["w"].filter(x["tasa"].is_not_null()).sum() if x["tasa"].is_not_null().any() else None
        filas.append({"periodo": f"{pb // 100}-{pb % 100:02d}", "var_pct": (math.exp(lr) - 1) * 100,
                      "n_celdas": x.height, "capitas": int(x["w"].sum()),
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
