"""Comparación contra el IPC del INDEC.

Usa la serie pública por divisiones del INDEC (serie_ipc_divisiones.csv), que hay que
bajar en la máquina que corre el pipeline:
    https://www.indec.gob.ar/ftp/cuadros/economia/serie_ipc_divisiones.csv

Compara la variación mensual de nuestras divisiones (01 alimentos y bebidas no
alcohólicas, 02 bebidas alcohólicas y tabaco) con la del INDEC, por región.
Que se parezcan no prueba que el índice esté bien, pero divergencias grandes y
sostenidas indican problemas de clasificación, cobertura o limpieza.
"""

from __future__ import annotations

import io
from pathlib import Path

import polars as pl

from .regiones import normalizar

_DIVISIONES = {
    "01": "alimentos y bebidas no alcoholicas",
    "02": "bebidas alcoholicas y tabaco",
}


def leer_serie_indec(ruta: Path) -> pl.DataFrame:
    crudo = Path(ruta).read_bytes()
    try:
        texto = crudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = crudo.decode("latin-1")
    sep = ";" if texto.splitlines()[0].count(";") > texto.splitlines()[0].count(",") else ","
    df = pl.read_csv(io.StringIO(texto), separator=sep, infer_schema=False)
    cols = {c.lower(): c for c in df.columns}

    def buscar(*claves):
        for k, v in cols.items():
            if all(c in k for c in claves):
                return v
        raise ValueError(f"No encuentro columna con {claves} en {list(df.columns)}")

    c_per, c_desc, c_reg, c_vm = buscar("periodo"), buscar("descrip"), buscar("region"), buscar("v_m")
    return df.select(
        pl.col(c_per).str.slice(0, 4).alias("anio"),
        pl.col(c_per).str.replace("-", "").str.slice(4, 2).alias("m"),
        pl.col(c_desc).map_elements(normalizar, return_dtype=pl.Utf8).alias("descripcion"),
        pl.col(c_reg).alias("region"),
        pl.col(c_vm).str.replace(",", ".").cast(pl.Float64, strict=False).alias("var_mensual_indec"),
    ).with_columns((pl.col("anio") + "-" + pl.col("m")).alias("mes")).drop("anio", "m")


def comparar(indices: pl.DataFrame, ruta_indec: Path) -> pl.DataFrame:
    indec = leer_serie_indec(ruta_indec)
    inv = {v: k for k, v in _DIVISIONES.items()}
    indec = indec.filter(pl.col("descripcion").is_in(list(inv))).with_columns(
        pl.col("descripcion").replace_strict(inv).alias("codigo")
    )
    propio = indices.filter(pl.col("nivel") == "division", pl.col("codigo").is_in(list(_DIVISIONES)))
    return (
        propio.select("mes", "region", "codigo", "nombre", "var_mensual_pct", "parcial")
        .join(indec.select("mes", "region", "codigo", "var_mensual_indec"), on=["mes", "region", "codigo"], how="inner")
        .with_columns((pl.col("var_mensual_pct") - pl.col("var_mensual_indec")).round(2).alias("diferencia_pp"))
        .sort("codigo", "region", "mes")
    )
