"""Cálculo del índice (mensual o semanal).

Esquema (el mismo que usa el INDEC, adaptado a la fuente):

1. Agregado elemental (código de ponderador x región): media geométrica no ponderada
   de los relativos de precio entre dos períodos consecutivos de los productos
   apareados (mismo producto, cadena, presentación y región en ambos períodos)
   -> índice de Jevons encadenado. Si en una región hay pocos productos apareados,
   se usa el relativo nacional de ese código.
2. Agregación superior: suma ponderada de los índices elementales con los
   ponderadores de la canasta INDEC (base dic-2016, ENGHo 2004/05) de cada región,
   tipo Laspeyres.
3. Índice nacional: suma ponderada de las regiones por su peso en el gasto de
   consumo (Cuadro 6 de la Metodología INDEC 32) y por el peso cubierto en cada una.
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from .clasificador import SIN_CLASIFICAR
from .config import REGIONES, Config, ponderadores_clase, ponderadores_region
from .lectura import CLAVE

log = logging.getLogger(__name__)

NACIONAL = "Nacional"
REZAGO_INTERANUAL = {"mensual": 12, "semanal": 52}


def _siguiente(periodo: str, frecuencia: str) -> str:
    if frecuencia == "mensual":
        a, m = map(int, periodo.split("-"))
        return f"{a + (m == 12)}-{1 if m == 12 else m + 1:02d}"
    a, s = periodo.split("-W")
    lunes = date.fromisocalendar(int(a), int(s), 1) + timedelta(days=7)
    a2, s2, _ = lunes.isocalendar()
    return f"{a2}-W{s2:02d}"


def _inicio(periodo: str, frecuencia: str) -> date:
    if frecuencia == "mensual":
        a, m = map(int, periodo.split("-"))
        return date(a, m, 1)
    a, w = periodo.split("-W")
    return date.fromisocalendar(int(a), int(w), 1)


def tramos(periodos: list[str], frecuencia: str, max_meses: float) -> dict[str, int]:
    """Numera los tramos de la serie: se corta cuando entre dos períodos con datos pasan más
    de `max_meses` (ej. de 2018 a 2025). Dentro de un tramo se encadena aunque haya huecos."""
    out, t = {}, 0
    for i, p in enumerate(periodos):
        if i and (_inicio(p, frecuencia) - _inicio(periodos[i - 1], frecuencia)).days / 30.4375 > max_meses:
            t += 1
        out[p] = t
    return out


def resolver_codigo(clase: str, codigos: set[str], min_niveles: int = 2) -> str | None:
    """Código con ponderador más específico que contiene a la clase.

    No sube más allá del nivel de grupo (2 niveles, ej. "09.3") para no asignarle a
    un puñado de productos el peso de una división entera.
    """
    partes = clase.split(".")
    for i in range(len(partes), min_niveles - 1, -1):
        c = ".".join(partes[:i])
        if c in codigos:
            return c
    return None


def _cargar(ruta: Path, col_precio: str, mapa: dict[str, str | None]) -> pl.DataFrame:
    df = pl.read_parquet(ruta).filter(pl.col("clase") != SIN_CLASIFICAR)
    return df.select(
        *CLAVE, "clase", pl.col(col_precio).alias("precio"), "n_dias", "dias_con_datos", "dias_periodo"
    ).with_columns(
        pl.col("clase").replace_strict(mapa, default=None, return_dtype=pl.Utf8).alias("codigo")
    ).filter(pl.col("codigo").is_not_null())


def _info(ruta: Path) -> tuple[int, int]:
    d = pl.read_parquet(ruta, columns=["dias_con_datos", "dias_periodo"]).row(0)
    return int(d[0]), int(d[1])


def _min_dias(dias_con_datos: int, minimo: int) -> int:
    return max(1, min(minimo, math.ceil(dias_con_datos / 2)))


def relativos(previo: pl.DataFrame, actual: pl.DataFrame, min_p: int, min_a: int,
              cfg: Config) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Relativos de Jevons por código y región (y nacional por código) entre dos períodos."""
    lim = cfg["limpieza"]
    pares = (
        previo.filter(pl.col("n_dias") >= min_p).select(*CLAVE, pl.col("precio").alias("p0"))
        .join(actual.filter(pl.col("n_dias") >= min_a).select(*CLAVE, "codigo", pl.col("precio").alias("p1")),
              on=CLAVE, how="inner")
        .with_columns((pl.col("p1") / pl.col("p0")).alias("rel"))
        .filter(pl.col("rel").is_between(lim["relativo_min"], lim["relativo_max"]))
        .with_columns(pl.col("rel").log().alias("logrel"))
    )
    regional = pares.group_by("codigo", "region").agg(
        pl.col("logrel").mean().alias("logrel_reg"), pl.len().alias("n_reg"))
    nacional = pares.group_by("codigo").agg(
        pl.col("logrel").mean().alias("logrel_nac"), pl.len().alias("n_nac"))
    return regional, nacional


def calcular(cfg: Config, frecuencia: str = "mensual") -> dict[str, pl.DataFrame]:
    ic = cfg["indice"]
    col_precio = "precio_lista" if ic["precio"] == "lista" else "precio_efectivo"
    min_dias = ic["min_dias_mes"] if frecuencia == "mensual" else ic["min_dias_semana"]
    pond = ponderadores_clase(cfg)
    codigos = set(pond["codigo"].unique().to_list())
    nombres = dict(pond.select("codigo", "nombre").unique("codigo").iter_rows())
    w_region = ponderadores_region(cfg)

    rutas = sorted(cfg.ruta(f"panel_{frecuencia}").glob("*.parquet"))
    if len(rutas) < 2:
        raise ValueError(f"Se necesitan al menos dos períodos ({frecuencia}) consolidados")
    periodos = [r.stem for r in rutas]

    clases = set()
    for r in rutas:
        clases |= set(pl.read_parquet(r, columns=["clase"])["clase"].unique().to_list())
    clases.discard(SIN_CLASIFICAR)
    mapa = {c: resolver_codigo(c, codigos) for c in sorted(clases)}
    sin_pond = sorted(c for c, v in mapa.items() if v is None)
    if sin_pond:
        log.warning("Clases sin ponderador (quedan fuera del índice): %s", sin_pond)
    usados = sorted({v for v in mapa.values() if v})
    for c in usados:
        hijos = [d for d in usados if d != c and d.startswith(c + ".")]
        if hijos:
            log.warning("El código %s y sus subcódigos %s se usan a la vez: revisar el clasificador", c, hijos)

    # 1. relativos elementales período a período
    minp = ic["min_productos_clase"]
    tramo = tramos(periodos, frecuencia, ic.get("max_meses_salto", 24))
    grilla = pl.DataFrame({"codigo": usados}).join(pl.DataFrame({"region": REGIONES}), how="cross")
    filas = []
    previo, info_p = _cargar(rutas[0], col_precio, mapa), _info(rutas[0])
    for ruta, per, per_prev in zip(rutas[1:], periodos[1:], periodos[:-1]):
        actual, info_a = _cargar(ruta, col_precio, mapa), _info(ruta)
        if tramo[per] != tramo[per_prev]:  # comienza un tramo nuevo: no se compara con el anterior
            filas.append(grilla.with_columns(
                pl.lit(per).alias("periodo"), pl.lit(0.0).alias("logrel"), pl.lit("inicio_tramo").alias("origen"),
                pl.lit(0, dtype=pl.UInt32).alias("n_reg"), pl.lit(info_a[0] < info_a[1]).alias("parcial"),
            ).select("periodo", "codigo", "region", "logrel", "origen", "n_reg", "parcial"))
            previo, info_p = actual, info_a
            continue
        reg, nac = relativos(previo, actual, _min_dias(info_p[0], min_dias), _min_dias(info_a[0], min_dias), cfg)
        r = (
            grilla.join(reg, on=["codigo", "region"], how="left")
            .join(nac, on="codigo", how="left")
            .with_columns(
                pl.when(pl.col("n_reg") >= minp).then(pl.col("logrel_reg"))
                .when(pl.col("n_nac") >= minp).then(pl.col("logrel_nac"))
                .otherwise(None).alias("logrel"),  # sin datos: no se supone que el precio no cambió
                pl.when(pl.col("n_reg") >= minp).then(pl.lit("regional"))
                .when(pl.col("n_nac") >= minp).then(pl.lit("nacional"))
                .otherwise(pl.lit("sin_datos")).alias("origen"),
                pl.col("n_reg").fill_null(0).cast(pl.UInt32),
                pl.lit(per).alias("periodo"),
                pl.lit(info_a[0] < info_a[1]).alias("parcial"),
            )
        )
        filas.append(r.select("periodo", "codigo", "region", "logrel", "origen", "n_reg", "parcial"))
        previo, info_p = actual, info_a

    info0 = _info(rutas[0])
    base0 = grilla.with_columns(
        pl.lit(periodos[0]).alias("periodo"), pl.lit(0.0).alias("logrel"), pl.lit("base").alias("origen"),
        pl.lit(0, dtype=pl.UInt32).alias("n_reg"), pl.lit(info0[0] < info0[1]).alias("parcial"),
    ).select("periodo", "codigo", "region", "logrel", "origen", "n_reg", "parcial")
    elem = (
        pl.concat([base0, *filas])
        .with_columns(pl.col("periodo").replace_strict(tramo, return_dtype=pl.Int32).alias("tramo"))
        .sort("codigo", "region", "periodo")
        .with_columns(pl.col("logrel").fill_null(0.0).cum_sum().over("codigo", "region", "tramo").alias("_acum"))
        .with_columns(
            # nivel de la clase: vacío en los períodos sin datos (no se arrastra el anterior)
            pl.when(pl.col("logrel").is_null()).then(None).otherwise(pl.col("_acum").exp() * 100).alias("indice"),
            # nivel previo, solo para ponderar el eslabón siguiente (no entra como variación de precio)
            pl.col("_acum").shift(1).over("codigo", "region", "tramo").fill_null(0.0).exp().alias("_nivel_prev"))
        .drop("_acum")
    )
    # base = 100: el mes_base configurado en su tramo; en los demás, el primer período del tramo
    base = ic.get("mes_base") if frecuencia == "mensual" else None
    if base and base not in periodos:
        raise ValueError(f"Período base {base} no está entre los períodos con datos")
    if base:
        elem = elem.with_columns(
            pl.when(pl.col("tramo") == tramo[base])
            .then(pl.col("indice") / pl.col("indice").filter(pl.col("periodo") == base).first().over("codigo", "region", "tramo") * 100)
            .otherwise(pl.col("indice")).alias("indice"))

    # 2. agregación con ponderadores de la canasta regional
    elem = elem.join(pond.select("codigo", "region", "ponderacion"), on=["codigo", "region"], how="left").with_columns(
        pl.col("ponderacion").fill_null(0.0), pl.col("codigo").str.slice(0, 2).alias("division"),
        pl.col("codigo").str.slice(0, 4).alias("grupo"))
    parcial = elem.group_by("periodo").agg(pl.col("parcial").any(), pl.col("tramo").first())

    def agregar(df: pl.DataFrame, por: list[str]) -> pl.DataFrame:
        """Laspeyres encadenado: en cada período, promedio de los relativos de las clases CON datos,
        ponderados por su peso en la canasta y su nivel del período anterior. Una clase sin datos en
        un período queda afuera de ese eslabón: no se le supone variación cero ni se arrastra nada."""
        w = pl.col("ponderacion") * pl.col("_nivel_prev")
        con = pl.col("logrel").is_not_null()
        eslabon = df.group_by(*por, "tramo").agg(
            ((w * pl.col("logrel").exp()).filter(con).sum() / w.filter(con).sum()).alias("_rel"),
            pl.col("ponderacion").sum().alias("ponderacion"),
            pl.col("n_reg").sum().alias("n_productos"),
        )
        claves = [c for c in por if c != "periodo"]
        eslabon = eslabon.sort(*claves, "periodo").with_columns(
            (pl.col("_rel").log().fill_null(0.0).cum_sum().over(*claves, "tramo").exp() * 100).alias("indice"))
        # sin ninguna clase con datos en el período: el agregado queda vacío ese período
        return eslabon.with_columns(pl.when(pl.col("_rel").is_null()).then(None).otherwise(pl.col("indice"))
                                    .alias("indice")).drop("_rel", "tramo")

    regional = pl.concat([
        agregar(elem, ["periodo", "region", "codigo"]).with_columns(pl.lit("clase").alias("nivel")),
        agregar(elem, ["periodo", "region", "division"]).rename({"division": "codigo"})
        .with_columns(pl.lit("division").alias("nivel")),
        agregar(elem, ["periodo", "region", "grupo"]).rename({"grupo": "codigo"})
        .with_columns(pl.lit("grupo").alias("nivel")),
        agregar(elem, ["periodo", "region"]).with_columns(pl.lit("TOTAL").alias("codigo"), pl.lit("total").alias("nivel")),
    ], how="diagonal")

    if base:  # el agregado encadenado arranca en 100 al inicio del tramo: se lleva a 100 en el mes base
        regional = regional.join(parcial.select("periodo", "tramo"), on="periodo").with_columns(
            pl.when(pl.col("tramo") == tramo[base])
            .then(pl.col("indice") / pl.col("indice").filter(pl.col("periodo") == base).first()
                  .over("region", "nivel", "codigo", "tramo") * 100)
            .otherwise(pl.col("indice")).alias("indice")).drop("tramo")

    # 3. nacional
    nac = (
        regional.with_columns(pl.col("region").replace_strict(w_region, return_dtype=pl.Float64).alias("w_region"))
        .with_columns((pl.col("w_region") * pl.col("ponderacion")).alias("w"))
        .group_by("periodo", "codigo", "nivel")
        .agg(pl.when(pl.col("indice").null_count() == 0)  # si falta una región, el nacional queda vacío
             .then((pl.col("indice") * pl.col("w")).sum() / pl.col("w").sum()).otherwise(None).alias("indice"),
             pl.col("w").sum().alias("ponderacion"), pl.col("n_productos").sum())
        .with_columns(pl.lit(NACIONAL).alias("region"))
    )

    nombres_div = dict(pl.read_csv(cfg.dir_config / "ponderadores_division.csv", schema_overrides={"codigo": pl.Utf8})
                       .select("codigo", "nombre").iter_rows())
    todos = (
        pl.concat([regional, nac.select(regional.columns)])
        .join(parcial, on="periodo", how="left")
        .sort("region", "nivel", "codigo", "periodo")
        .with_columns(
            pl.col("periodo").shift(1).over("region", "nivel", "codigo", "tramo").alias("periodo_anterior"),
            ((pl.col("indice") / pl.col("indice").shift(1).over("region", "nivel", "codigo", "tramo") - 1) * 100)
            .alias("var_pct"),
            (pl.col("periodo").str.slice(0, 4).cast(pl.Int32) - 1).cast(pl.Utf8)
            .add(pl.col("periodo").str.slice(4)).alias("_periodo_ia"),
            pl.when(pl.col("nivel") == "total").then(pl.lit("Bienes relevados (total)"))
            .when(pl.col("nivel") == "division").then(pl.col("codigo").replace_strict(nombres_div, default=""))
            .when(pl.col("nivel") == "grupo").then(pl.col("codigo").replace_strict(nombres, default=""))
            .otherwise(pl.col("codigo").replace_strict(nombres, default="")).alias("nombre"),
        )
        .join(
            pl.concat([regional, nac.select(regional.columns)]).join(parcial.select("periodo", "tramo"), on="periodo")
            .select("region", "nivel", "codigo", "tramo", pl.col("periodo").alias("_periodo_ia"),
                    pl.col("indice").alias("_indice_ia")),
            on=["region", "nivel", "codigo", "tramo", "_periodo_ia"], how="left",
        )
        .with_columns(((pl.col("indice") / pl.col("_indice_ia") - 1) * 100).alias("var_interanual_pct"))
        .with_columns(
            # salto: el período anterior con datos no es el inmediato anterior (ej. la copia de la
            # Wayback Machine de abril de 2025). La variación de ese tramo no es mensual ni semanal.
            (pl.col("periodo_anterior").is_not_null()
             & (pl.col("periodo_anterior").map_elements(lambda p: _siguiente(p, frecuencia), return_dtype=pl.Utf8)
                != pl.col("periodo"))).alias("salto"),
        )
        .with_columns(
            pl.when(pl.col("salto")).then(pl.col("var_pct")).otherwise(None).alias("var_salto_pct"),
            pl.when(pl.col("salto")).then(None).otherwise(pl.col("var_pct")).alias("var_pct"),
        )
        .select("periodo", "region", "nivel", "codigo", "nombre", "indice", "var_pct",
                "var_interanual_pct", "n_productos", "parcial", "salto", "periodo_anterior", "var_salto_pct", "tramo")
        .with_columns(pl.col("indice", "var_pct", "var_interanual_pct", "var_salto_pct").round(4))
    )

    ult = elem.filter(pl.col("periodo") == periodos[-1])
    cob = ult.group_by("region").agg(pl.col("ponderacion").sum().alias("ponderacion_cubierta_pct")).with_columns(
        pl.col("region").replace_strict(w_region, return_dtype=pl.Float64).alias("w"))
    cobertura = pl.concat([
        cob.select("region", "ponderacion_cubierta_pct"),
        cob.select(pl.lit(NACIONAL).alias("region"),
                   ((pl.col("ponderacion_cubierta_pct") * pl.col("w")).sum() / pl.col("w").sum())
                   .alias("ponderacion_cubierta_pct")),
    ]).with_columns(pl.col("ponderacion_cubierta_pct").round(2))
    mapeo = pl.DataFrame({"clase": list(mapa), "codigo_ponderador": list(mapa.values())},
                         schema={"clase": pl.Utf8, "codigo_ponderador": pl.Utf8})
    calidad = elem.group_by("periodo", "origen").agg(pl.len().alias("n_agregados")).sort("periodo", "origen")

    return {"indices": todos, "cobertura": cobertura, "mapeo_clases": mapeo, "calidad": calidad}


def guardar(res: dict[str, pl.DataFrame], cfg: Config, frecuencia: str) -> list[Path]:
    dir_res = cfg.ruta("resultados")
    rutas = []
    for nombre, df in res.items():
        p = dir_res / f"{nombre}_{frecuencia}.csv"
        df.write_csv(p)
        rutas.append(p)
    return rutas
