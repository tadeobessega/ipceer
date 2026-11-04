"""Base histórica de Precios Claros (SEPA) 2016-2018 publicada en Zenodo.

Fuente: "Precios Claros (2016-05 - 2018-03)", Zenodo, registro 6568295
(https://zenodo.org/records/6568295). Datos de SEPA (Ministerio de Producción) bajados
por Diego Daruich y Julián Kozlowski (NYU) entre el 9/5/2016 y el 11/3/2018 y depositados
por Lars Vilhuber para la American Economic Association. Licencia CC BY 2.5.

Formato (distinto del SEPA actual): por día, tres archivos .csv.gz separados por coma
  sucursales_AAAA-MM-DD: comercio_id, bandera_id, sucursal_id, sucursal_provincia (ISO),
                         sucursal_localidad, sucursal_latitud, sucursal_longitud, ...
  productos_AAAA-MM-DD:  producto_sepa_id, producto_ean (t/f), producto_descripcion,
                         producto_cantidad_presentacion, producto_unidad_medida_presentacion,
                         producto_marca, categoria_rubro_desc, categoria_categ_desc,
                         categoria_subcateg_desc (categorías oficiales de SEPA)
  precios_AAAA-MM-DD:    comercio_id, bandera_id, sucursal_id, producto_sepa_id, precio_lista,
                         precio_unitario_promo1, precio_unitario_promo2, precio_fecha

Cada día se baja, se convierte al mismo panel diario que el SEPA actual
(data/panel/diario/AAAA-MM-DD.parquet) y se borran los archivos bajados: Zenodo los
conserva, así que no hace falta guardar 44 GB. Es reanudable: saltea los días hechos.
"""

from __future__ import annotations

import gzip
import logging
import shutil
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path

import polars as pl
import requests

from .config import Config
from .lectura import CLAVE
from .regiones import asignar_regiones

log = logging.getLogger(__name__)

ZENODO_API = "https://zenodo.org/api/records/6568295"


def listar_dias(timeout: int = 60) -> dict[date, dict[str, str]]:
    r = requests.get(ZENODO_API, timeout=timeout)
    r.raise_for_status()
    dias: dict[date, dict[str, str]] = defaultdict(dict)
    for f in r.json()["files"]:
        clave = f["key"]  # ej. precios_2017-03-01.csv.gz
        tipo, resto = clave.split("_", 1)
        dias[date.fromisoformat(resto[:10])][tipo] = f["links"]["self"]
    return {d: v for d, v in sorted(dias.items()) if {"precios", "productos", "sucursales"} <= v.keys()}


def _bajar(url: str, destino: Path, timeout: int = 120, reintentos: int = 4) -> None:
    for i in range(1, reintentos + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                with open(destino, "wb") as f:
                    for b in r.iter_content(1 << 20):
                        f.write(b)
            return
        except requests.RequestException as e:
            log.warning("Zenodo: fallo bajando %s (intento %d): %s", url, i, e)
            if i == reintentos:
                raise


def _gunzip(origen: Path, destino: Path) -> None:
    with gzip.open(origen, "rb") as fi, open(destino, "wb") as fo:
        shutil.copyfileobj(fi, fo, 1 << 22)


def _leer_chico(ruta_gz: Path) -> pl.DataFrame:
    df = pl.read_csv(ruta_gz, infer_schema=False, truncate_ragged_lines=True, ignore_errors=True,
                     encoding="utf8-lossy")
    return df.rename({c: c.strip().lower() for c in df.columns}).with_columns(
        pl.col(c).str.strip_chars() for c in df.columns)


def procesar_dia(dia: date, urls: dict[str, str], cfg: Config, dir_tmp: Path) -> Path:
    archivos = {}
    for tipo in ("sucursales", "productos", "precios"):
        archivos[tipo] = dir_tmp / f"{tipo}.csv.gz"
        _bajar(urls[tipo], archivos[tipo])

    suc = _leer_chico(archivos["sucursales"]).rename({
        "comercio_id": "id_comercio", "bandera_id": "id_bandera", "sucursal_id": "id_sucursal",
        "sucursal_provincia": "sucursales_provincia", "sucursal_localidad": "sucursales_localidad",
        "sucursal_latitud": "sucursales_latitud", "sucursal_longitud": "sucursales_longitud",
    }).unique(["id_comercio", "id_bandera", "id_sucursal"])
    suc = asignar_regiones(suc, cfg).select("id_comercio", "id_bandera", "id_sucursal", "region")

    prod = _leer_chico(archivos["productos"])
    prod = prod.select(
        pl.col("producto_sepa_id").alias("id_producto"),
        pl.when(pl.col("producto_ean") == "t").then(pl.lit("1")).otherwise(pl.lit("0")).alias("es_ean"),
        pl.col("producto_descripcion").alias("descripcion"),
        pl.col("producto_marca").alias("marca"),
        pl.col("producto_cantidad_presentacion").str.replace_all(",", ".").cast(pl.Float64, strict=False)
        .round(3).cast(pl.Utf8).fill_null("").alias("cantidad"),
        pl.col("producto_unidad_medida_presentacion").str.to_lowercase().fill_null("").alias("unidad"),
        *[pl.col(c).alias(c.replace("categoria_", "cat_").replace("_desc", ""))
          for c in ("categoria_rubro_desc", "categoria_categ_desc", "categoria_subcateg_desc") if c in prod.columns],
    ).unique("id_producto")

    # catálogo con las categorías oficiales de SEPA (sirve para clasificar por EAN)
    dir_cat = cfg.ruta("panel_diario").parent / "catalogo_historico"
    dir_cat.mkdir(parents=True, exist_ok=True)
    prod.write_parquet(dir_cat / f"{dia.isoformat()}.parquet", compression="zstd")

    csv = dir_tmp / "precios.csv"
    _gunzip(archivos["precios"], csv)
    archivos["precios"].unlink()
    lf = (
        pl.scan_csv(csv, infer_schema=False, truncate_ragged_lines=True, ignore_errors=True)
        .select(
            pl.col("comercio_id").str.strip_chars().alias("id_comercio"),
            pl.col("bandera_id").str.strip_chars().alias("id_bandera"),
            pl.col("sucursal_id").str.strip_chars().alias("id_sucursal"),
            pl.col("producto_sepa_id").str.strip_chars().alias("id_producto"),
            pl.col("precio_lista").cast(pl.Float64, strict=False).alias("precio_lista"),
            pl.col("precio_unitario_promo1").cast(pl.Float64, strict=False).alias("promo1"),
        )
        .filter(pl.col("precio_lista") > 0)
        .join(suc.lazy(), on=["id_comercio", "id_bandera", "id_sucursal"], how="inner")
        .filter(pl.col("region").is_not_null())
        .join(prod.lazy().select("id_producto", "cantidad", "unidad"), on="id_producto", how="left")
        .with_columns(
            pl.col("cantidad").fill_null(""), pl.col("unidad").fill_null(""),
            pl.when(pl.col("promo1").is_not_nan() & (pl.col("promo1") > 0) & (pl.col("promo1") < pl.col("precio_lista")))
            .then(pl.col("promo1")).otherwise(pl.col("precio_lista")).alias("precio_efectivo"),
        )
    )
    f = cfg["limpieza"]["factor_atipico_sucursal"]
    medianas = lf.group_by(CLAVE).agg(pl.col("precio_lista").median().alias("_med"))
    panel = (
        lf.join(medianas, on=CLAVE)
        .filter((pl.col("precio_lista") <= pl.col("_med") * f) & (pl.col("precio_lista") >= pl.col("_med") / f))
        .group_by(CLAVE)
        .agg(
            pl.col("precio_lista").log().mean().exp().alias("precio_lista"),
            pl.col("precio_efectivo").log().mean().exp().alias("precio_efectivo"),
            pl.col("id_sucursal").n_unique().alias("n_sucursales"),
        )
        .collect(engine="streaming")
        .join(prod.select("id_producto", "descripcion", "marca", "es_ean"), on="id_producto", how="left")
        .with_columns(pl.lit(dia).alias("fecha"))
    )
    csv.unlink()
    for a in archivos.values():
        Path(a).unlink(missing_ok=True)
    destino = cfg.ruta("panel_diario") / f"{dia.isoformat()}.parquet"
    panel.write_parquet(destino, compression="zstd")
    log.info("Zenodo %s: %d productos-cadena-región, %d cadenas", dia, panel.height,
             panel.select("id_comercio", "id_bandera").n_unique())
    return destino


# orden de descarga: primero un día por semana en todo el período (miércoles), después el resto;
# así la comparación con el INDEC cubre todos los meses desde los primeros lotes y se va afinando
PRIORIDAD_DIA = {3: 0, 1: 1, 5: 2, 2: 3, 4: 4, 6: 5, 7: 6}  # día ISO de la semana -> orden


def construir(cfg: Config, desde: date | None = None, hasta: date | None = None,
              max_dias: int | None = None, paralelo: int | None = None) -> int:
    """Baja y procesa días de la base de Zenodo que falten. Con `max_dias`, procesa a lo sumo
    esa cantidad (un lote) y termina. Procesa `paralelo` días a la vez (por defecto el valor de
    `historico.paralelo` en config.yaml, o 4): mientras uno se procesa, los otros se bajan."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    dias = listar_dias()
    orden = sorted(dias, key=lambda d: (PRIORIDAD_DIA[d.isoweekday()], d))
    pend = [d for d in orden
            if not ((desde and d < desde) or (hasta and d > hasta))
            and not (cfg.ruta("panel_diario") / f"{d.isoformat()}.parquet").exists()]
    if max_dias is not None:
        pend = pend[:max_dias]
    n = paralelo or int((cfg.datos.get("historico") or {}).get("paralelo", 4))
    hechos = 0
    with tempfile.TemporaryDirectory(dir=cfg.ruta("panel_diario").parent) as tmp:
        def uno(dia: date) -> None:
            sub = Path(tmp) / dia.isoformat()
            sub.mkdir()
            try:
                procesar_dia(dia, dias[dia], cfg, sub)
            finally:
                shutil.rmtree(sub, ignore_errors=True)
        with ThreadPoolExecutor(max_workers=max(1, n)) as ex:
            futuros = {ex.submit(uno, d): d for d in pend}
            for f in as_completed(futuros):
                try:
                    f.result()
                    hechos += 1
                except Exception as e:  # un día malo no frena la serie
                    log.error("Zenodo %s: %s", futuros[f], e)
    return hechos
