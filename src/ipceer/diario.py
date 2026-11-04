"""Índice diario por división: cada día contra un día base fijo (Jevons sobre los mismos productos).

No se encadena día a día (el encadenamiento diario acumula ruido): cada día se compara directamente
con el primer día de la ventana, usando solo los productos-cadena-región que aparecen en los dos días.
Una clase sin productos comparables ese día queda fuera y su peso se reparte entre las que tienen
datos; si una división no tiene ninguna, ese día no tiene dato. No se rellena ni se arrastra nada.
"""
from __future__ import annotations

import logging
from datetime import date

import polars as pl

from .clasificador import SIN_CLASIFICAR, Clasificador
from .config import REGIONES, Config, ponderadores_clase, ponderadores_region
from .indice import NACIONAL, resolver_codigo
from .panel import CLAVE, dias_disponibles

log = logging.getLogger(__name__)

DIVISIONES = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]


def _ventana(dias: list[date], n: int, max_hueco: int = 7) -> list[date]:
    """Últimos n días, cortando en el primer hueco mayor a max_hueco días (la base histórica no se mezcla)."""
    out = []
    for d in reversed(dias):
        if out and (out[-1] - d).days > max_hueco:
            break
        out.append(d)
        if len(out) >= n:
            break
    return list(reversed(out))


def _cargar(cfg: Config, d: date, col: str, clasif: Clasificador, mapa: dict) -> pl.DataFrame:
    df = pl.read_parquet(cfg.ruta("panel_diario") / f"{d.isoformat()}.parquet")
    df = clasif.clasificar(df).filter(pl.col("clase") != SIN_CLASIFICAR)
    df = df.with_columns(pl.col("clase").replace_strict(mapa, default=None, return_dtype=pl.Utf8).alias("codigo"))
    return df.filter(pl.col("codigo").is_not_null(), pl.col(col) > 0).select(*CLAVE, "codigo", pl.col(col).alias("p"))


def calcular(cfg: Config) -> dict:
    ic = cfg["indice"]
    col = "precio_lista" if ic["precio"] == "lista" else "precio_efectivo"
    n = int((cfg.datos.get("indicadores") or {}).get("diario_dias", 120))
    dias = _ventana(dias_disponibles(cfg), n)
    if len(dias) < 2:
        return {}
    pond = ponderadores_clase(cfg)
    codigos = set(pond["codigo"].unique().to_list())
    w_cl = {(c, r): w for c, r, w in pond.select("codigo", "region", "ponderacion").iter_rows()}
    w_reg = ponderadores_region(cfg)
    lim = cfg["limpieza"]
    minp = ic["min_productos_clase"]
    clasif = Clasificador.desde_config(cfg)

    base = None
    mapa: dict = {}
    filas = []
    for d in dias:
        if base is None:
            # el mapa clase → código se arma con las clases del día base
            tmp = clasif.clasificar(pl.read_parquet(cfg.ruta("panel_diario") / f"{d.isoformat()}.parquet"))
            mapa = {c: resolver_codigo(c, codigos) for c in tmp["clase"].unique().to_list() if c != SIN_CLASIFICAR}
            base = _cargar(cfg, d, col, clasif, mapa).rename({"p": "p0"})
            continue
        act = _cargar(cfg, d, col, clasif, mapa)
        pares = (base.join(act.select(*CLAVE, "p"), on=CLAVE, how="inner")
                 .with_columns((pl.col("p") / pl.col("p0")).alias("rel"))
                 .filter(pl.col("rel").is_between(lim["relativo_min"], lim["relativo_max"]))
                 .with_columns(pl.col("rel").log().alias("lr")))
        g = pares.group_by("codigo", "region").agg(pl.col("lr").mean(), pl.len().alias("n")).filter(pl.col("n") >= minp)
        for codigo, region, lr, _n in g.iter_rows():
            filas.append({"fecha": d.isoformat(), "codigo": codigo, "region": region, "lr": lr,
                          "w": w_cl.get((codigo, region), 0.0)})
    if not filas:
        return {}
    el = pl.DataFrame(filas, infer_schema_length=None).with_columns(pl.col("codigo").str.slice(0, 2).alias("division"))

    def _agr(df: pl.DataFrame, por: list[str]) -> pl.DataFrame:
        return (df.filter(pl.col("w") > 0).group_by(por)
                .agg(((pl.col("lr") * pl.col("w")).sum() / pl.col("w").sum()).alias("lr"), pl.col("w").sum().alias("w")))

    reg_div = _agr(el, ["fecha", "region", "division"])
    reg_tot = _agr(el.with_columns(pl.lit("TOTAL").alias("division")), ["fecha", "region", "division"])
    reg = pl.concat([reg_div, reg_tot])
    # nacional: solo si están las seis regiones ese día
    nac = (reg.with_columns(pl.col("region").replace_strict(w_reg, default=0.0).alias("wr"))
           .group_by("fecha", "division")
           .agg(((pl.col("lr") * pl.col("wr")).sum() / pl.col("wr").sum()).alias("lr"), pl.len().alias("nreg"))
           .filter(pl.col("nreg") == len(REGIONES)).drop("nreg").with_columns(pl.lit(NACIONAL).alias("region")))
    todo = pl.concat([reg.select("fecha", "region", "division", "lr"), nac.select("fecha", "region", "division", "lr")])
    todo = todo.with_columns((pl.col("lr").exp() * 100).alias("indice")).sort("fecha")
    series: dict = {}
    for (region, div), s in todo.group_by(["region", "division"], maintain_order=True):
        series.setdefault(region, {})[div] = [[f, round(i, 3)] for f, i in s.select("fecha", "indice").iter_rows()]
    for r in series.values():
        for v in r.values():
            v.insert(0, [dias[0].isoformat(), 100.0])
    nombres = dict(pond.filter(pl.col("codigo").str.len_chars() == 2).select("codigo", "nombre").unique("codigo").iter_rows())
    return {"base": dias[0].isoformat(), "hasta": dias[-1].isoformat(), "series": series,
            "nombres": {**nombres, "TOTAL": "Nivel general (bienes)"}, "eventos": eventos(cfg)}


def eventos(cfg: Config) -> list[dict]:
    ruta = cfg.dir_config / "eventos.csv"
    if not ruta.exists():
        return []
    return pl.read_csv(ruta, infer_schema_length=None).to_dicts()
