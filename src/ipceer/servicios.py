"""Servicios que entran al índice publicado sin precios relevados (D-53): tarifas reguladas (cuadros y
normas) y cuotas de medicina prepaga (Superintendencia de Servicios de Salud). Con modelo (D-55):
educación (el INDEC del mismo mes del año anterior) y restaurantes (los bienes del mismo período). El alquiler queda
afuera hasta comparar el ICL y el relevamiento de avisos contra el INDEC (fuente "alquiler_icl",
disponible para las pruebas).

Devuelve variaciones por clase y región en el formato `extra` de `indice.calcular` (columnas
periodo, codigo, region, logrel). Solo se usan en las clases sin precios propios.

Criterio del INDEC (Metodología IPC 2016, sección 8: "los cambios tarifarios ... son considerados
desde que entran en vigencia, adoptándose el criterio de devengado"; y sección 6: el precio del mes
es el promedio de lo observado a lo largo del mes). Igual acá:
  - Mensual: la variación del mes de cada fuente (el aumento rige desde el día 1).
  - Semanal: el precio de cada día es el vigente ese día; el de la semana, el promedio de sus 7 días.
    Un aumento del día 1 entra en la semana que contiene ese día en la proporción de días que rige
    (si el 1 cae martes, 6/7 en esa semana y el resto en la siguiente). Las demás semanas del mes no
    cambian: los servicios suben una vez por mes, y el índice semanal lo muestra así.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta

import polars as pl

from .config import REGIONES, Config

log = logging.getLogger(__name__)

ESQUEMA = {"periodo": pl.Utf8, "codigo": pl.Utf8, "region": pl.Utf8, "logrel": pl.Float64, "fuente": pl.Utf8}
FUENTES = ("tarifas", "prepagas", "educacion", "alquiler_icl", "combustibles")
# D-55: clases que se imputan con modelo (D-36, D-47)
EDUCACION = ("10.1", "10.2", "10.3", "10.5")
RESTAURANTES = ("11.1",)


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
    """D-56. 04.1.1 en todas las regiones con el ICL del BCRA: variación del promedio del mes contra
    el del mes anterior (solo meses completos). En semanal, por devengado: los alquileres se pagan por
    mes, así que el aumento del mes entra en la semana del día 1 (como tarifas y cuotas, D-53)."""
    if frecuencia == "semanal":
        return _a_semanas(_alquiler_icl(cfg, "mensual"))
    return _alquiler_icl(cfg, "mensual")


def combustibles_mensual(cfg: Config) -> pl.DataFrame:
    """D-56. 07.2.2 con el índice de la Secretaría de Energía (precios y volúmenes por estación, Res.
    1104/2004; resultados/combustibles_mensual.csv). Solo mensual: la fuente sale con unos dos meses de
    atraso y no hay precios diarios vigentes (la Res. 314/2016 de precios en surtidor se derogó en
    2025), así que el semanal no lo incluye y los últimos meses entran cuando Energía los publica."""
    ruta = cfg.ruta("resultados") / "combustibles_mensual.csv"
    if not ruta.exists():
        return _vacio()
    c = pl.read_csv(ruta, schema_overrides={"codigo": pl.Utf8, "periodo": pl.Utf8}, infer_schema_length=None)
    if "nivel" in c.columns:
        c = c.filter(pl.col("nivel") == "clase")
    return c.filter(pl.col("region").is_in(REGIONES), pl.col("var_pct").is_not_null()).select(
        "periodo", pl.lit("07.2.2").alias("codigo"), "region", (1 + pl.col("var_pct") / 100).log().alias("logrel"),
        pl.lit("combustibles").alias("fuente")).cast(ESQUEMA)


def _alquiler_icl(cfg: Config, frecuencia: str) -> pl.DataFrame:
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


def educacion_mensual(cfg: Config) -> pl.DataFrame:
    """D-55. Educación (10) por estacionalidad: la variación del INDEC del mismo mes del año anterior,
    por región (D-36: el mejor indicador disponible; las cuotas suben en marzo y a mitad de año).
    Se proyecta hasta 12 meses después del último publicado."""
    from .indec import directorio
    from .validacion import leer_indec
    ind = leer_indec(directorio(cfg))
    if ind.is_empty():
        return _vacio()
    filas = []
    for reg in REGIONES:
        g = dict(ind.filter((pl.col("codigo") == "10") & (pl.col("region") == reg)).select("mes", "indice").iter_rows())
        ms = sorted(g)
        for a, b in zip(ms, ms[1:]):
            y, m = map(int, b.split("-"))
            dest = f"{y + 1}-{m:02d}"
            if g[a] and g[b]:
                filas += [{"periodo": dest, "codigo": c, "region": reg, "logrel": math.log(g[b] / g[a]), "fuente": "educacion"}
                          for c in EDUCACION]
    return pl.DataFrame(filas, schema=ESQUEMA) if filas else _vacio()


def restaurantes(bienes: pl.DataFrame) -> pl.DataFrame:
    """D-55. Restaurantes (11.1) con los bienes del mismo período y región (D-36: el mejor indicador;
    mejora del 61% fuera de muestra). `bienes`: los índices de un primer cálculo sin servicios."""
    t = bienes.filter(pl.col("codigo") == "TOTAL", pl.col("region").is_in(REGIONES), pl.col("var_pct").is_not_null())
    if t.is_empty():
        return _vacio()
    return t.select("periodo", pl.lit(RESTAURANTES[0]).alias("codigo"), "region",
                    (1 + pl.col("var_pct") / 100).log().alias("logrel"), pl.lit("restaurantes").alias("fuente")).cast(ESQUEMA)


def _a_semanas(m: pl.DataFrame) -> pl.DataFrame:
    """Saltos mensuales (vigentes desde el día 1) a semanas, por devengado: nivel diario escalonado,
    promedio de los 7 días de cada semana y variación contra la semana anterior."""
    if m.is_empty():
        return m
    filas = []
    for (cod, reg, fu), g in m.group_by("codigo", "region", "fuente"):
        rel = dict(g.select("periodo", "logrel").iter_rows())
        meses = sorted(rel)
        a, mm = map(int, meses[0].split("-"))
        ini = date(a, mm, 1)
        ua, um = map(int, meses[-1].split("-"))
        fin = date(ua + (um == 12), 1 if um == 12 else um + 1, 1) + timedelta(days=40)
        ini = ini - timedelta(days=ini.weekday())  # desde el lunes de la semana del primer día
        nivel, acum, d, sem = {}, 0.0, ini, {}
        while d < fin:
            if d.day == 1:
                acum += rel.get(d.strftime("%Y-%m"), 0.0)
            y, w, _ = d.isocalendar()
            sem.setdefault(f"{y}-W{w:02d}", []).append(math.exp(acum))
            d += timedelta(days=1)
        ks = [k for k in sorted(sem) if len(sem[k]) == 7]
        for k0, k1 in zip(ks, ks[1:]):
            lr = math.log((sum(sem[k1]) / 7) / (sum(sem[k0]) / 7))
            if abs(lr) > 1e-12:
                filas.append({"periodo": k1, "codigo": cod, "region": reg, "logrel": lr, "fuente": fu})
    return pl.DataFrame(filas, schema=ESQUEMA) if filas else pl.DataFrame(schema=ESQUEMA)


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
            elif f == "educacion":
                x = educacion_mensual(cfg)
                partes.append(_a_semanas(x) if frecuencia == "semanal" else x)
            elif f == "alquiler_icl":
                partes.append(alquiler_icl(cfg, frecuencia))
            elif f == "telefonia":
                from . import telefonia
                partes.append(telefonia.relativos(cfg, frecuencia))
            elif f == "combustibles":
                if frecuencia == "mensual":
                    partes.append(combustibles_mensual(cfg))
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
