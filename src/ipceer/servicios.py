"""Servicios que entran al índice publicado sin precios relevados (D-53): tarifas reguladas (cuadros y
normas), alquileres con el ICL del BCRA y cuotas de medicina prepaga (Superintendencia de Servicios
de Salud).

Devuelve variaciones por clase y región en el formato `extra` de `indice.calcular` (columnas
periodo, codigo, region, logrel). Solo se usan en las clases sin precios propios.

Mensual: la variación del mes de cada fuente.
Semanal:
  - tarifas y prepagas: el aumento del mes entra entero en la semana que contiene el día 1 (los
    cuadros y las cuotas rigen desde el primer día del mes); las otras semanas, sin cambio.
  - alquiler: el ICL es diario, así que la semana sale del promedio de sus días contra el de la
    semana anterior.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta

import polars as pl

from .config import REGIONES, Config

log = logging.getLogger(__name__)

ESQUEMA = {"periodo": pl.Utf8, "codigo": pl.Utf8, "region": pl.Utf8, "logrel": pl.Float64, "fuente": pl.Utf8}
FUENTES = ("tarifas", "alquiler_icl", "prepagas")


def _vacio() -> pl.DataFrame:
    return pl.DataFrame(schema=ESQUEMA)


def _semana_del_dia1(mes: str) -> str:
    a, m = map(int, mes.split("-"))
    y, w, _ = date(a, m, 1).isocalendar()
    return f"{y}-W{w:02d}"


def tarifas_mensual(cfg: Config) -> pl.DataFrame:
    from . import externos
    reg = externos.indice_regulados(cfg)
    if reg is None or reg.is_empty():
        return _vacio()
    return reg.filter(pl.col("region").is_in(REGIONES), pl.col("var_pct").is_not_null()).select(
        "periodo", "codigo", "region", (1 + pl.col("var_pct") / 100).log().alias("logrel"),
        pl.lit("tarifas").alias("fuente")).cast(ESQUEMA)


def prepagas_mensual(cfg: Config) -> pl.DataFrame:
    from . import prepagas
    p = prepagas.relativos_externos(cfg)
    return _vacio() if p.is_empty() else p.select(list(ESQUEMA)).cast(ESQUEMA)


def _icl_diario(cfg: Config) -> pl.DataFrame:
    ruta = cfg.ruta("panel_diario").parent.parent / "otras" / "bcra" / "icl.csv"
    if not ruta.exists():
        return pl.DataFrame(schema={"fecha": pl.Date, "valor": pl.Float64})
    return (pl.read_csv(ruta, infer_schema_length=None)
            .select(pl.col("fecha").cast(pl.Utf8).str.slice(0, 10).str.to_date("%Y-%m-%d"), pl.col("valor").cast(pl.Float64))
            .drop_nulls().sort("fecha"))


def alquiler_icl(cfg: Config, frecuencia: str) -> pl.DataFrame:
    """04.1.1 en todas las regiones: variación del promedio del ICL en el período contra el anterior.
    Solo períodos completos (todos sus días con dato)."""
    d = _icl_diario(cfg)
    if d.is_empty():
        return _vacio()
    if frecuencia == "semanal":
        d = d.with_columns(pl.col("fecha").map_elements(lambda f: "%d-W%02d" % f.isocalendar()[:2], return_dtype=pl.Utf8).alias("p"))
        completo = 7
    else:
        d = d.with_columns(pl.col("fecha").dt.strftime("%Y-%m").alias("p"))
        completo = None
    g = d.group_by("p").agg(pl.col("valor").mean().alias("v"), pl.len().alias("n"),
                            pl.col("fecha").max().alias("fin")).sort("p")
    if completo:
        g = g.filter(pl.col("n") >= completo)
    else:  # mes completo: el último día del mes tiene dato
        g = g.filter(pl.col("fin") == pl.col("fin").dt.month_end())
    filas, prev = [], None
    for p, v, _, _ in g.iter_rows():
        if prev is not None and v and prev[1]:
            filas += [{"periodo": p, "codigo": "04.1.1", "region": r, "logrel": math.log(v / prev[1]), "fuente": "alquiler_icl"}
                      for r in REGIONES]
        prev = (p, v)
    return pl.DataFrame(filas, schema=ESQUEMA) if filas else _vacio()


def _a_semanas(m: pl.DataFrame) -> pl.DataFrame:
    """Saltos mensuales (tarifas, prepagas) a la semana que contiene el día 1 del mes."""
    if m.is_empty():
        return m
    return m.with_columns(pl.col("periodo").map_elements(_semana_del_dia1, return_dtype=pl.Utf8))


def relativos(cfg: Config, frecuencia: str, fuentes: tuple[str, ...] = FUENTES) -> pl.DataFrame:
    partes = []
    for f in fuentes:
        try:
            if f == "tarifas":
                x = tarifas_mensual(cfg)
                partes.append(_a_semanas(x) if frecuencia == "semanal" else x)
            elif f == "prepagas":
                x = prepagas_mensual(cfg)
                partes.append(_a_semanas(x) if frecuencia == "semanal" else x)
            elif f == "alquiler_icl":
                partes.append(alquiler_icl(cfg, frecuencia))
            else:
                log.warning("Servicios: fuente desconocida %s", f)
        except Exception as e:
            log.warning("Servicios, %s: %s", f, e)
    partes = [p for p in partes if not p.is_empty()]
    if not partes:
        return _vacio()
    out = pl.concat(partes)
    log.info("Servicios en el índice (%s): %s", frecuencia,
             dict(out.group_by("fuente").agg(pl.col("periodo").max()).iter_rows()))
    return out
