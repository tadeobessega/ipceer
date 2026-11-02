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
import tempfile
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path, PurePosixPath

import polars as pl

from .config import Config
from .regiones import asignar_regiones

log = logging.getLogger(__name__)

CLAVE = ["id_comercio", "id_bandera", "id_producto", "cantidad", "unidad", "region"]


COLUMNAS_PRODUCTOS = [
    "id_comercio", "id_bandera", "id_sucursal", "id_producto", "productos_ean", "productos_descripcion",
    "productos_cantidad_presentacion", "productos_unidad_medida_presentacion", "productos_marca",
    "productos_precio_lista", "productos_precio_unitario_promo1",
]


def _leer_csv(contenido: bytes, columnas: list[str] | None = None) -> pl.DataFrame:
    """Lee un CSV de SEPA ("|", BOM, pie de archivo, espacios de relleno).

    Los productos de una cadena grande pueden ser millones de filas: se escriben a un
    archivo temporal y se leen en modo lazy solo las columnas necesarias.
    """
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(contenido)
        ruta = tmp.name
    try:
        lf = pl.scan_csv(
            ruta, separator="|", infer_schema=False, quote_char=None,
            truncate_ragged_lines=True, ignore_errors=True, encoding="utf8-lossy",
        )
        nombres = lf.collect_schema().names()
        limpio = {c: c.replace("\ufeff", "").strip().lower() for c in nombres}
        lf = lf.rename(limpio)
        if columnas:
            lf = lf.select([c for c in columnas if c in limpio.values()])
        cols = lf.collect_schema().names()
        lf = lf.with_columns(pl.col(c).str.strip_chars() for c in cols)
        if "id_comercio" in cols:
            # descarta pie de archivo ("Última actualización: ...") y filas basura
            lf = lf.filter(pl.col("id_comercio").str.contains(r"^\d+$"))
        return lf.collect()
    finally:
        Path(ruta).unlink(missing_ok=True)


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
                try:
                    interno = zipfile.ZipFile(io.BytesIO(zf.read(nombre)))
                except zipfile.BadZipFile:
                    log.warning("Zip interno vacío o dañado, se omite: %s", nombre)
                    continue
                with interno:
                    yield from recorrer(interno, f"{prefijo}{pp.stem}/")
            elif pp.name.lower() in ("comercio.csv", "sucursales.csv", "productos.csv"):
                grupos[str(pp.parent)][pp.name.lower()] = zf.read(nombre)
        for carpeta, archivos in grupos.items():
            yield f"{prefijo}{carpeta}", archivos

    with zipfile.ZipFile(ruta_zip) as zf:
        yield from recorrer(zf, "")


_MUNI_CACHE: dict = {}


def _municipios(cfg: Config):
    """(árbol de geometrías, ids, {id: región}) de los municipios configurados, o None."""
    conf = cfg.datos.get("municipios") or []
    if not conf:
        return None
    clave = tuple(m["id"] for m in conf)
    if clave in _MUNI_CACHE:
        return _MUNI_CACHE[clave]
    import json
    ruta = cfg.ruta("panel_diario").parent.parent / "combustibles" / "geo" / "departamentos.geojson"
    if not ruta.exists():
        return None
    import shapely
    from shapely.geometry import shape
    feats = [f for f in json.loads(ruta.read_text(encoding="utf-8"))["features"] if f["properties"]["id"] in clave]
    if not feats:
        return None
    res = (shapely.STRtree([shape(f["geometry"]) for f in feats]), [f["properties"]["id"] for f in feats],
           {m["id"]: m.get("region", "GBA") for m in conf})
    _MUNI_CACHE[clave] = res
    return res


def _sucursales_municipio(suc: pl.DataFrame, cfg: Config) -> pl.DataFrame | None:
    """id_comercio, id_bandera, id_sucursal, municipio (id Georef) de las sucursales que caen
    dentro de un municipio configurado (por coordenadas)."""
    m = _municipios(cfg)
    if m is None or "sucursales_latitud" not in suc.columns:
        return None
    import numpy as np
    import shapely
    arbol, ids, _ = m
    lat = suc["sucursales_latitud"].cast(pl.Utf8).str.replace(",", ".").cast(pl.Float64, strict=False).to_numpy()
    lon = suc["sucursales_longitud"].cast(pl.Utf8).str.replace(",", ".").cast(pl.Float64, strict=False).to_numpy()
    ok = np.where(np.isfinite(lat) & np.isfinite(lon))[0]
    if not len(ok):
        return None
    pi, gi = arbol.query(shapely.points(lon[ok], lat[ok]), predicate="within")
    if not len(pi):
        return None
    filas = suc[ok[pi]].select("id_comercio", "id_bandera", "id_sucursal").with_columns(
        pl.Series("municipio", [ids[g] for g in gi], dtype=pl.Utf8))
    return filas


def procesar_comercio(archivos: dict[str, bytes], cfg: Config, municipal: list | None = None) -> pl.DataFrame | None:
    """Precios del día de un comercio por producto-cadena-región. Si se pasa `municipal`, le agrega
    los mismos precios de las sucursales de cada municipio configurado (región = id del municipio)."""
    if "productos.csv" not in archivos or "sucursales.csv" not in archivos:
        return None
    prod = _leer_csv(archivos["productos.csv"], COLUMNAS_PRODUCTOS)
    suc = _leer_csv(archivos["sucursales.csv"])
    req = {"id_comercio", "id_bandera", "id_sucursal", "id_producto", "productos_precio_lista"}
    if prod.is_empty() or not req.issubset(prod.columns):
        return None

    suc_muni = _sucursales_municipio(suc, cfg) if municipal is not None else None
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

    aggs = [
        pl.col("precio_lista").log().mean().exp().alias("precio_lista"),
        pl.col("precio_efectivo").log().mean().exp().alias("precio_efectivo"),
        pl.col("id_sucursal").n_unique().alias("n_sucursales"),
        pl.col("descripcion").first(),
        pl.col("marca").first(),
        pl.col("es_ean").first(),
    ]
    if suc_muni is not None and suc_muni.height:
        mdf = (df.drop("region").join(suc_muni, on=["id_comercio", "id_bandera", "id_sucursal"], how="inner")
               .rename({"municipio": "region"}))
        if mdf.height:
            municipal.append(mdf.group_by(CLAVE).agg(*aggs))
    return df.group_by(CLAVE).agg(*aggs)


def _nombres_cadenas(archivos: dict[str, bytes]) -> list[dict]:
    """Nombre de fantasía de cada bandera (comercio.csv), para mostrar las cadenas."""
    if "comercio.csv" not in archivos:
        return []
    try:
        com = _leer_csv(archivos["comercio.csv"])
    except Exception:
        return []
    if not {"id_comercio", "id_bandera"}.issubset(com.columns):
        return []
    col = next((c for c in ("comercio_bandera_nombre", "comercio_razon_social") if c in com.columns), None)
    if col is None:
        return []
    return [{"id_comercio": r["id_comercio"], "id_bandera": r["id_bandera"], "cadena": (r[col] or "").strip()}
            for r in com.select("id_comercio", "id_bandera", col).unique().iter_rows(named=True)]


def actualizar_cadenas(cfg: Config, filas: list[dict]) -> None:
    """Mantiene data/panel/cadenas.csv (id_comercio, id_bandera, cadena) con el último nombre visto."""
    if not filas:
        return
    ruta = cfg.ruta("panel_diario").parent / "cadenas.csv"
    nuevo = pl.DataFrame(filas, schema={"id_comercio": pl.Utf8, "id_bandera": pl.Utf8, "cadena": pl.Utf8})
    if ruta.exists():
        viejo = pl.read_csv(ruta, schema_overrides={"id_comercio": pl.Utf8, "id_bandera": pl.Utf8, "cadena": pl.Utf8})
        nuevo = pl.concat([viejo, nuevo])
    nuevo = nuevo.filter(pl.col("cadena").str.len_chars() > 0).unique(["id_comercio", "id_bandera"], keep="last")
    nuevo.sort("id_comercio", "id_bandera").write_csv(ruta)


def dir_municipio(cfg: Config, muni_id: str) -> Path:
    d = cfg.ruta("panel_diario").parent.parent / "municipios" / muni_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _guardar_municipal(municipal: list, fecha: date, cfg: Config) -> None:
    if not municipal:
        return
    _, _, region = _municipios(cfg)
    todo = pl.concat(municipal, how="vertical_relaxed").with_columns(pl.lit(fecha).alias("fecha"))
    for (mid,), g in todo.group_by(["region"]):
        d = dir_municipio(cfg, mid) / "panel" / "diario"
        d.mkdir(parents=True, exist_ok=True)
        # región del IPC del municipio, para usar sus ponderadores
        g.with_columns(pl.lit(region.get(mid, "GBA")).alias("region")).write_parquet(
            d / f"{fecha.isoformat()}.parquet", compression="zstd")


def procesar_zip_diario(ruta_zip: Path, fecha: date, cfg: Config, solo_municipios: bool = False) -> Path:
    partes, nombres, municipal = [], [], []
    for nombre, archivos in _iterar_comercios(ruta_zip):
        nombres += _nombres_cadenas(archivos)
        try:
            df = procesar_comercio(archivos, cfg, municipal)
        except Exception as e:  # un comercio mal formado no debe frenar el día
            log.warning("No se pudo procesar %s: %s", nombre, e)
            continue
        if df is not None:
            partes.append(df)
    _guardar_municipal(municipal, fecha, cfg)
    if solo_municipios:
        return ruta_zip
    if not partes:
        raise ValueError(f"{ruta_zip}: no se encontraron precios válidos")
    panel = pl.concat(partes, how="vertical_relaxed").with_columns(pl.lit(fecha).alias("fecha"))
    actualizar_cadenas(cfg, nombres)
    destino = cfg.ruta("panel_diario") / f"{fecha.isoformat()}.parquet"
    panel.write_parquet(destino, compression="zstd")
    log.info("%s: %d productos-cadena-región, %d cadenas", fecha, panel.height,
             panel.select("id_comercio", "id_bandera").n_unique())
    return destino


def catalogo_cadenas(cfg: Config, ultimos: int = 3) -> int:
    """Arma data/panel/cadenas.csv leyendo solo comercio.csv de los últimos zips archivados
    (para tener los nombres de las cadenas sin reprocesar todo)."""
    zips = sorted(cfg.ruta("crudos").glob("sepa_*.zip"))[-ultimos:]
    filas = []
    for ruta in zips:
        try:
            with zipfile.ZipFile(ruta) as z:
                for nombre in z.namelist():
                    if not nombre.lower().endswith(".zip"):
                        continue
                    try:
                        with zipfile.ZipFile(io.BytesIO(z.read(nombre))) as zi:
                            for n in zi.namelist():
                                if PurePosixPath(n).name.lower() == "comercio.csv":
                                    filas += _nombres_cadenas({"comercio.csv": zi.read(n)})
                    except zipfile.BadZipFile:
                        continue
        except zipfile.BadZipFile:
            log.warning("Zip dañado: %s", ruta)
    actualizar_cadenas(cfg, filas)
    return len({(f["id_comercio"], f["id_bandera"]) for f in filas})
