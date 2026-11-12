"""Variedades dentro de cada clase (config/variedades.yaml).

El INDEC calcula su nivel elemental por VARIEDAD, con ponderadores de gasto (Metodología n° 32,
apartados 5.1 y 5.2). En SEPA una clase tiene miles de artículos y las variedades con muchas marcas
y presentaciones pesarían de más si se promedia todo junto. Con la estratificación se promedia
primero dentro de cada variedad y después entre variedades, con los pesos del archivo.
Los artículos que no matchean ninguna variedad quedan en "otros" (peso PESO_OTROS).
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import polars as pl
import yaml

from .clasificador import normalizar_descripcion

OTROS = "otros"
PESO_OTROS = 5.0
MIN_ARTICULOS = 3   # una variedad con menos artículos apareados no entra en el promedio de su clase


@lru_cache(maxsize=4)
def _reglas(ruta: str) -> dict[str, list[tuple[str, float, re.Pattern, re.Pattern | None]]]:
    p = Path(ruta)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        datos = yaml.safe_load(f) or {}
    return {str(clase): [(x["id"], float(x["peso"]), re.compile(x["incluir"]),
                          re.compile(x["excluir"]) if x.get("excluir") else None) for x in lista]
            for clase, lista in datos.items()}


def reglas(dir_config: Path) -> dict:
    return _reglas(str(dir_config / "variedades.yaml"))


def pesos(dir_config: Path) -> pl.DataFrame:
    filas = [{"codigo": c, "variedad": v, "peso_var": w} for c, lista in reglas(dir_config).items() for v, w, _i, _e in lista]
    return pl.DataFrame(filas, schema={"codigo": pl.Utf8, "variedad": pl.Utf8, "peso_var": pl.Float64})


_cache: dict[tuple[str, str], str] = {}


def asignar(desc: str | None, codigo: str, r: dict) -> str:
    clave = (desc or "", codigo)
    if clave in _cache:
        return _cache[clave]
    t = normalizar_descripcion(desc)
    out = OTROS
    for vid, _w, inc, exc in r.get(codigo, []):
        if inc.search(t) and not (exc and exc.search(t)):
            out = vid
            break
    _cache[clave] = out
    return out


def con_variedad(df: pl.DataFrame, dir_config: Path) -> pl.DataFrame:
    """Agrega la columna `variedad` (según `codigo` y `descripcion`)."""
    r = reglas(dir_config)
    if "descripcion" not in df.columns or not r:
        return df.with_columns(pl.lit(OTROS).alias("variedad"))
    df = df.with_columns(pl.col("descripcion").fill_null(""))
    pares = df.select("descripcion", "codigo").unique()
    mapa = pares.with_columns(pl.Series("variedad", [asignar(d, c, r) for d, c in pares.iter_rows()], dtype=pl.Utf8))
    return df.join(mapa, on=["descripcion", "codigo"], how="left").with_columns(
        pl.col("variedad").fill_null(OTROS))
