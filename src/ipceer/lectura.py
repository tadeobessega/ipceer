"""Lectura de los zips de SEPA y armado del panel diario.

Estructura de un zip diario de SEPA: un zip "de zips", con un zip por comercio.
Cada zip de comercio trae tres archivos separados por "|":
  - comercio.csv:   id_comercio, id_bandera, comercio_cuit, comercio_bandera_nombre, ...
  - sucursales.csv: id_comercio, id_bandera, id_sucursal, sucursales_provincia,
                    sucursales_localidad, sucursales_latitud, sucursales_longitud, ...
  - productos.csv:  id_comercio, id_bandera, id_sucursal, id_producto, productos_ean,
                    productos_descripcion, productos_marca, productos_cantidad_presentacion,
                    productos_unidad_medida_presentacion, productos_precio_lista,
                    productos_precio_unitario_promo1, ...
Los archivos traen BOM, a veces una línea final de "Última actualización" y algunas
filas mal formadas; todo eso se tolera.

Salida: un parquet por día con una fila por producto-cadena-región, con la media
geométrica del precio entre sucursales (precio de lista y precio efectivo con promo).
"""

from __future__ import annotations

import io
import logging
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path, PurePosixPath

import polars as pl

from .config import Config
from .regiones import asignar_regiones

log = logging.getLogger(__name__)

CLAVE = ["id_comercio", "id_bandera", "id_producto", "cantidad", "unidad", "region"]


def _leer_csv(contenido: bytes) -> pl.DataFrame:
    df = pl.read_csv(
        io.BytesIO(contenido),
        separator="|",
        infer_schema=False,
        quote_char=None,
        truncate_ragged_lines=True,
        ignore_errors=True,
        encoding="utf8-lossy",
    )
    df = df.rename({c: c.replace("﻿", "").strip().lower() for c in df.columns})
    if "id_comercio" in df.columns:
        # descarta pie de archivo ("Última actualización: ...") y filas basura
        df = df.filter(pl.col("id_comercio").str.strip_chars().str.contains(r"^\d+$"))
    return df.with_columns(pl.col(c).str.strip_chars() for c in df.columns)


def _numero(col: str) -> pl.Expr:
    return pl.col(col).str.replace_all(",", ".").cast(pl.Float64, strict=False)


def _iterar_comercios(ruta_zip: Path):
    """Devuelve (nombre, {archivo: bytes}) por cada comercio dentro del zip diario."""

    def recorrer(zf: zipfile.ZipFile, prefijo: str):
        grupos: dict[str, dict[str, bytes]] = defaultdict(dict)
        for nombre in zf.namelist():
            if nombre.endswith("/"):
                continue
            pp = PurePosixPath(nombre)
            if pp.suffix.lower() == ".zip":
                with zipfile.ZipFile(io.BytesIO(zf.read(nombre))) as interno:
                    yield from recorrer(interno, f"{prefijo}{pp.stem}/")
            elif pp.name.lower() in ("comercio.csv", "sucursales.csv", "productos.csv"):
                grupos[str(pp.parent)][pp.name.lower()] = zf.read(nombre)
        for carpeta, archivos in grupos.items():
            yield f"{prefijo}{carpeta}", archivos

    with zipfile.ZipFile(ruta_zip) as zf:
        yield from recorrer(zf, "")


def procesar_comercio(archivos: dict[str, bytes], cfg: Config) -> pl.DataFrame | None:
    if "productos.csv" not in archivos or "sucursales.csv" not in archivos:
        return None
    prod = _leer_csv(archivos["productos.csv"])
    suc = _leer_csv(archivos["sucursales.csv"])
    req = {"id_comercio", "id_bandera", "id_sucursal", "id_producto", "productos_precio_lista"}
    if prod.is_empty() or not req.issubset(prod.columns):
        return None

    suc = asignar_regiones(suc, cfg).select("id_comercio", "id_bandera", "id_sucursal", "region")
    opcionales = {
        "productos_descripcion": "descripcion",
        "productos_marca": "marca",
        "productos_cantidad_presentacion": "cantidad",
        "productos_unidad_medida_presentacion": "unidad",
        "productos_ean": "es_ean",
        "productos_precio_unitario_promo1": "promo1",
    }
    for orig in opcionales:
        if orig not in prod.columns:
            prod = prod.with_columns(pl.lit(None, dtype=pl.Utf8).alias(orig))

    df = (
        prod.select(
            "id_comercio", "id_bandera", "id_sucursal", "id_producto",
            *[pl.col(o).alias(n) for o, n in opcionales.items() if n not in ("promo1",)],
            _numero("productos_precio_lista").alias("precio_lista"),
            _numero("productos_precio_unitario_promo1").alias("promo1"),
        )
        .with_columns(
            pl.col("cantidad").str.replace_all(",", ".").cast(pl.Float64, strict=False).round(3).cast(pl.Utf8),
            pl.col("unidad").str.to_lowercase().fill_null(""),
        )
        .join(suc, on=["id_comercio", "id_bandera", "id_sucursal"], how="inner")
        .filter(pl.col("region").is_not_null() & (pl.col("precio_lista") > 0))
        .with_columns(
            pl.when((pl.col("promo1") > 0) & (pl.col("promo1") < pl.col("precio_lista")))
            .then(pl.col("promo1"))
            .otherwise(pl.col("precio_lista"))
            .alias("precio_efectivo"),
            pl.col("cantidad").fill_null(""),
        )
    )
    if df.is_empty():
        return None

    # descarta precios de sucursal muy alejados de la mediana del producto-cadena-región
    f = cfg["limpieza"]["factor_atipico_sucursal"]
    df = df.with_columns(pl.col("precio_lista").median().over(CLAVE).alias("_med")).filter(
        (pl.col("precio_lista") <= pl.col("_med") * f) & (pl.col("precio_lista") >= pl.col("_med") / f)
    )

    return df.group_by(CLAVE).agg(
        pl.col("precio_lista").log().mean().exp().alias("precio_lista"),
        pl.col("precio_efectivo").log().mean().exp().alias("precio_efectivo"),
        pl.col("id_sucursal").n_unique().alias("n_sucursales"),
        pl.col("descripcion").first(),
        pl.col("marca").first(),
        pl.col("es_ean").first(),
    )


def procesar_zip_diario(ruta_zip: Path, fecha: date, cfg: Config) -> Path:
    partes = []
    for nombre, archivos in _iterar_comercios(ruta_zip):
        try:
            df = procesar_comercio(archivos, cfg)
        except Exception as e:  # un comercio mal formado no debe frenar el día
            log.warning("No se pudo procesar %s: %s", nombre, e)
            continue
        if df is not None:
            partes.append(df)
    if not partes:
        raise ValueError(f"{ruta_zip}: no se encontraron precios válidos")
    panel = pl.concat(partes, how="vertical_relaxed").with_columns(pl.lit(fecha).alias("fecha"))
    destino = cfg.ruta("panel_diario") / f"{fecha.isoformat()}.parquet"
    panel.write_parquet(destino, compression="zstd")
    log.info("%s: %d productos-cadena-región, %d cadenas", fecha, panel.height,
             panel.select("id_comercio", "id_bandera").n_unique())
    return destino
