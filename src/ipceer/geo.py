"""Inflación y nivel de precios por provincia y por departamento o partido (para el mapa).

Cada día, al procesar SEPA, se guarda un panel chico con el precio de cada producto de cada cadena
en cada departamento (data/geo/diario). Por semana:

- **inflación**: Jevons de los mismos productos de la misma cadena en el mismo departamento (o
  provincia) entre una semana y la anterior, por clase del IPC, ponderado con los ponderadores de la
  región del IPC a la que pertenece. Las clases sin productos comparables quedan afuera y su peso se
  reparte; si lo medido cubre menos que `geo.cobertura_min` de la canasta, no hay dato.
- **nivel de precios**: mismo producto de la misma cadena en distintos lugares (efectos fijos de dos
  vías), 100 = promedio del país.

Para no ocupar disco, los archivos diarios de una semana se borran cuando la semana terminó y ya
está calculada; de los paneles semanales se guardan los dos últimos (hace falta el anterior para
comparar). Los resultados quedan en data/geo/resultados.parquet.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from .config import Config, ponderadores_clase
from .indicadores import _con_codigo, efectos_fijos
from .lectura import dir_geo
from .panel import id_semana, rango_semana

log = logging.getLogger(__name__)

PROD = ["id_comercio", "id_bandera", "id_producto", "cantidad", "unidad"]
COLS_RES = {"semana": pl.Utf8, "nivel": pl.Utf8, "id": pl.Utf8, "region": pl.Utf8, "var": pl.Float64,
            "pares": pl.Int64, "cobertura": pl.Float64, "nivel_precios": pl.Float64, "n_nivel": pl.Int64,
            "completa": pl.Boolean}


def semana_de(d: date) -> str:
    return id_semana(d)


def _ruta_res(cfg: Config) -> Path:
    return dir_geo(cfg) / "resultados.parquet"


def _resultados(cfg: Config) -> pl.DataFrame:
    r = _ruta_res(cfg)
    return pl.read_parquet(r) if r.exists() else pl.DataFrame(schema=COLS_RES)


def semanas_hechas(cfg: Config) -> set[str]:
    """Semanas completas ya calculadas (sus archivos diarios ya no hacen falta)."""
    r = _resultados(cfg)
    return set(r.filter(pl.col("completa"))["semana"].unique().to_list()) if r.height else set()


def _param(cfg: Config, k: str, defecto):
    return (cfg.datos.get("geo") or {}).get(k, defecto)


def _panel_semana(archivos: list[Path]) -> pl.DataFrame:
    d = pl.concat([pl.read_parquet(a) for a in archivos], how="vertical_relaxed")
    return d.group_by(*PROD, "depto").agg(
        pl.col("precio_lista").log().mean().exp().alias("p"), pl.col("region").first(),
        pl.col("n_sucursales").max().alias("n_suc"), pl.len().alias("n_dias"))


def _codigos(cfg: Config, semana: str, codigos: set[str]) -> pl.DataFrame | None:
    """Clase del IPC de cada producto de cada cadena (del panel semanal nacional ya clasificado)."""
    ruta = cfg.ruta("panel_semanal") / f"{semana}.parquet"
    if not ruta.exists():
        return None
    df = pl.read_parquet(ruta, columns=[*PROD, "clase"]).unique(PROD)
    return _con_codigo(df, codigos).select(*PROD, "codigo")


def _unidad(df: pl.DataFrame, nivel: str) -> pl.DataFrame:
    return df.with_columns((pl.col("depto") if nivel == "depto" else pl.col("depto").str.slice(0, 2)).alias("u"))


def _inflacion(prev: pl.DataFrame, act: pl.DataFrame, pond: pl.DataFrame, cfg: Config, nivel: str) -> pl.DataFrame:
    lim = cfg["limpieza"]
    minp = int(_param(cfg, "min_productos", 5))
    cob_min = float(_param(cfg, "cobertura_min", 0.2))
    pares = (_unidad(prev, nivel).select(*PROD, "depto", "u", pl.col("p").alias("p0"))
             .join(_unidad(act, nivel).select(*PROD, "depto", "codigo", "region", pl.col("p").alias("p1")), on=[*PROD, "depto"])
             .with_columns((pl.col("p1") / pl.col("p0")).alias("rel"))
             .filter(pl.col("rel").is_between(lim["relativo_min"], lim["relativo_max"]))
             .with_columns(pl.col("rel").log().alias("lr")))
    if pares.is_empty():
        return pl.DataFrame()
    reg_u = pares.group_by("u").agg(pl.col("region").mode().first().alias("region"))
    cel = (pares.group_by("u", "codigo").agg(pl.col("lr").mean(), pl.len().alias("n"))
           .filter(pl.col("n") >= minp).join(reg_u, on="u")
           .join(pond, on=["codigo", "region"], how="inner"))
    # total de la canasta de la región = suma de las divisiones (los ponderadores traen todos los niveles)
    tot = pond.filter(pl.col("codigo").str.len_chars() == 2).group_by("region").agg(pl.col("ponderacion").sum().alias("tot"))
    out = (cel.group_by("u", "region")
           .agg(((pl.col("lr") * pl.col("ponderacion")).sum() / pl.col("ponderacion").sum()).alias("lr"),
                pl.col("ponderacion").sum().alias("w"), pl.col("n").sum().alias("pares"))
           .join(tot, on="region")
           .with_columns((pl.col("w") / pl.col("tot")).alias("cobertura"))
           .filter(pl.col("cobertura") >= cob_min)
           .select(pl.col("u").alias("id"), "region", ((pl.col("lr").exp() - 1) * 100).alias("var"),
                   pl.col("pares").cast(pl.Int64), "cobertura"))
    return out


def _nivel(act: pl.DataFrame, pond: pl.DataFrame, cfg: Config, nivel: str) -> pl.DataFrame:
    df = _unidad(act, nivel).filter(pl.col("p") > 0).with_columns(pl.col("p").log().alias("y"))
    if nivel == "provincia":  # varios departamentos de la misma provincia: un precio por provincia
        df = df.group_by(*PROD, "u", "codigo").agg(pl.col("y").mean())
    df = df.with_columns(pl.n_unique("u").over(PROD).alias("nu")).filter(pl.col("nu") >= 2)
    if df.is_empty() or df["u"].n_unique() < 2:
        return pl.DataFrame()
    pn = pond.group_by("codigo").agg(pl.col("ponderacion").mean().alias("pn"))
    df = df.join(pn, on="codigo").with_columns((pl.col("pn") / pl.len().over("codigo")).alias("w"))
    ef = efectos_fijos(df, PROD, ["u"], iteraciones=25)
    ef = ef.filter(pl.col("n") >= int(_param(cfg, "min_productos_nivel", 30)))
    if ef.is_empty():
        return pl.DataFrame()
    centro = (ef["efecto"] * ef["n"]).sum() / ef["n"].sum()
    return ef.select(pl.col("u").alias("id"), ((pl.col("efecto") - centro).exp() * 100).alias("nivel_precios"),
                     pl.col("n").cast(pl.Int64).alias("n_nivel"))


def actualizar(cfg: Config) -> int:
    """Calcula las semanas con archivos diarios nuevos. Devuelve cuántas semanas calculó."""
    dg = dir_geo(cfg) / "diario"
    ds = dir_geo(cfg) / "semanal"
    ds.mkdir(parents=True, exist_ok=True)
    archivos = sorted(dg.glob("*.parquet")) if dg.exists() else []
    if not archivos:
        return 0
    por_semana: dict[str, list[Path]] = {}
    for a in archivos:
        por_semana.setdefault(id_semana(date.fromisoformat(a.stem)), []).append(a)
    dias = sorted(cfg.ruta("panel_diario").glob("*.parquet"))
    ultimo = date.fromisoformat(dias[-1].stem) if dias else date.today()
    pond = ponderadores_clase(cfg).filter(pl.col("ponderacion") > 0).select("codigo", "region", "ponderacion")
    codigos = set(pond["codigo"].unique().to_list())
    res = _resultados(cfg)
    hechas = semanas_hechas(cfg)
    n = 0
    for sem in sorted(por_semana):
        lunes, domingo = rango_semana(sem)
        completa = domingo <= ultimo
        if sem in hechas:
            if completa:
                for a in por_semana[sem]:
                    a.unlink(missing_ok=True)
            continue
        cod = _codigos(cfg, sem, codigos)
        if cod is None:
            log.info("Mapa %s: falta el panel semanal consolidado", sem)
            continue
        act = _panel_semana(por_semana[sem]).join(cod, on=PROD, how="inner")
        previa = id_semana(lunes - timedelta(days=7))
        rp = ds / f"{previa}.parquet"
        prev = pl.read_parquet(rp) if rp.exists() else (
            _panel_semana(por_semana[previa]) if previa in por_semana else None)
        filas = []
        for nivel in ("depto", "provincia"):
            inf = _inflacion(prev, act, pond, cfg, nivel) if prev is not None else pl.DataFrame()
            niv = _nivel(act, pond, cfg, nivel)
            if inf.is_empty() and niv.is_empty():
                continue
            if inf.is_empty():
                t = niv.with_columns(pl.lit(None, dtype=pl.Utf8).alias("region"), pl.lit(None, dtype=pl.Float64).alias("var"),
                                     pl.lit(None, dtype=pl.Int64).alias("pares"), pl.lit(None, dtype=pl.Float64).alias("cobertura"))
            elif niv.is_empty():
                t = inf.with_columns(pl.lit(None, dtype=pl.Float64).alias("nivel_precios"), pl.lit(None, dtype=pl.Int64).alias("n_nivel"))
            else:
                t = inf.join(niv, on="id", how="full", coalesce=True)
            filas.append(t.with_columns(pl.lit(sem).alias("semana"), pl.lit(nivel).alias("nivel"), pl.lit(completa).alias("completa")))
        if filas:
            nuevo = pl.concat(filas, how="diagonal_relaxed").select([pl.col(k).cast(v) for k, v in COLS_RES.items()])
            res = pl.concat([res.filter(pl.col("semana") != sem), nuevo])
            res.write_parquet(_ruta_res(cfg))
            n += 1
            log.info("Mapa %s: %d departamentos y %d provincias", sem,
                     nuevo.filter(pl.col("nivel") == "depto").height, nuevo.filter(pl.col("nivel") == "provincia").height)
        act.drop("codigo").write_parquet(ds / f"{sem}.parquet", compression="zstd")
        if completa:
            for a in por_semana[sem]:
                a.unlink(missing_ok=True)
    for viejo in sorted(ds.glob("*.parquet"))[:-2]:
        viejo.unlink(missing_ok=True)
    return n


def _prom4(vals: list[tuple[str, float | None]]) -> float | None:
    """Promedio geométrico de las últimas 4 semanas, mensualizado; solo con 4 semanas seguidas con dato."""
    if len(vals) < 4:
        return None
    ult = vals[-4:]
    sems = [rango_semana(s)[0] for s, _ in ult]
    if any((b - a).days != 7 for a, b in zip(sems, sems[1:])) or any(v is None for _, v in ult):
        return None
    lr = sum(math.log1p(v / 100) for _, v in ult) / 4
    return (math.exp(lr * 30.4 / 7) - 1) * 100


def datos(cfg: Config, semanas: int = 13) -> dict:
    res = _resultados(cfg)
    if res.is_empty():
        return {}
    sem = sorted(res["semana"].unique().to_list())[-semanas:]
    ruta = cfg.ruta("panel_diario").parent.parent / "combustibles" / "geo" / "departamentos.geojson"
    nombres, prov = {}, {}
    if ruta.exists():
        for f in json.loads(ruta.read_text(encoding="utf-8"))["features"]:
            p = f["properties"]
            nombres[p["id"]] = p["nombre"]
            prov[p["id"]] = p["provincia"]["id"]
            nombres.setdefault(p["provincia"]["id"], p["provincia"]["nombre"])
    out = {"semanas": sem, "depto": {}, "provincia": {}}
    for (nivel, uid), g in res.filter(pl.col("semana").is_in(sem)).sort("semana").group_by(["nivel", "id"], maintain_order=True):
        filas = list(g.select("semana", "var", "nivel_precios", "cobertura", "completa", "region").iter_rows())
        serie, hist = [], []
        for s, v, nv, cob, comp, reg in filas:
            hist.append((s, v))
            serie.append({"semana": s, "var": None if v is None else round(v, 3),
                          "prom4m": None if _prom4(hist) is None else round(_prom4(hist), 3),
                          "nivel": None if nv is None else round(nv, 2), "parcial": not comp})
        reg = next((r for *_, r in filas[::-1] if r), None)
        out[nivel][uid] = {"n": nombres.get(uid, uid), "p": prov.get(uid, uid[:2]), "r": reg, "s": serie}
    return out
