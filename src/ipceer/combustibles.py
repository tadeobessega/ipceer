"""Combustibles: precios en surtidor de la Secretaría de Energía.

Fuente principal: dataset "Precios y volúmenes a partir de 2018" (Resolución SE 1104/2004),
con precio y volumen vendido por estación de servicio, producto y mes, y las coordenadas de
cada estación. Se usa para:

  1. un índice mensual de combustibles (clase 07.2.2 del IPC, "Combustibles y lubricantes
     para vehículos de uso del hogar") por región del IPC y nacional, que se suma al IPCEER
     con su ponderador del INDEC y se valida contra la apertura 07.2.2 del INDEC;
  2. niveles de precio y variaciones por provincia y por departamento/partido, para el mapa.

La geometría de provincias y departamentos es la de Georef (IGN, datos.gob.ar).

También se archiva todos los días el dataset "Precios en surtidor" (precios vigentes por
estación), para poder armar más adelante un índice semanal de combustibles.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import math
import re
import time
import unicodedata
from datetime import date
from pathlib import Path

import polars as pl
import requests

from .config import Config, ponderadores_clase, ponderadores_region
from .indice import NACIONAL

log = logging.getLogger(__name__)

CODIGO = "07.2.2"
CKAN = "http://datos.energia.gob.ar/api/3/action/"
DATASET_VOLUMENES = "708f9ab4-829b-4f02-b507-f303c5bc4800"
URL_VOLUMENES = ("http://datos.energia.gob.ar/dataset/708f9ab4-829b-4f02-b507-f303c5bc4800/resource/"
                 "d6ecf302-049b-459d-bca2-b638ea70518b/download/precios-y-volumenes-a-partir-de-2018.csv")
GEOREF = "https://apis.datos.gob.ar/georef/api/"
UA = {"User-Agent": "Mozilla/5.0 (IPCEER)"}

PRODUCTOS = {
    "nafta_super": "Nafta súper (grado 2)",
    "nafta_premium": "Nafta premium (grado 3)",
    "gasoil": "Gasoil (grado 2)",
    "gasoil_premium": "Gasoil premium (grado 3)",
    "gnc": "GNC",
}
# productos que se muestran a nivel de departamento (el GNC tiene pocas estaciones por departamento)
PRODUCTOS_DEPTO = ["nafta_super", "nafta_premium", "gasoil", "gasoil_premium"]

# provincias de Georef -> región del IPC (la provincia de Buenos Aires se reparte entre GBA y Pampeana)
REGION_PROVINCIA = {
    "02": "GBA", "06": "Pampeana", "14": "Pampeana", "30": "Pampeana", "42": "Pampeana", "82": "Pampeana",
    "18": "Noreste", "22": "Noreste", "34": "Noreste", "54": "Noreste",
    "10": "Noroeste", "38": "Noroeste", "46": "Noroeste", "66": "Noroeste", "86": "Noroeste", "90": "Noroeste",
    "50": "Cuyo", "70": "Cuyo", "74": "Cuyo",
    "26": "Patagonia", "58": "Patagonia", "62": "Patagonia", "78": "Patagonia", "94": "Patagonia",
}

ALIAS = {
    "anio": ["anio", "ano", "year"],
    "mes": ["mes", "month"],
    "periodo": ["indice_tiempo", "periodo", "fecha"],  # el primero que exista
    "provincia": ["provincia"],
    "localidad": ["localidad"],
    "direccion": ["direccion"],
    "cuit": ["cuit"],
    "estacion": ["idempresa", "id_empresa", "nro_inscripcion", "nro_de_inscripcion"],
    "bandera": ["empresabandera", "bandera"],
    "producto": ["producto", "descripcion_producto"],
    "horario": ["tipohorario", "tipo_horario"],
    "precio": ["precio", "precio_surtidor"],
    "canal": ["canal_de_comercializacion", "canal", "destino"],
    "volumen": ["volumen", "volumen_m3"],
    "lat": ["latitud", "lat"],
    "lon": ["longitud", "lon", "lng"],
}


# ---------------------------------------------------------------- utilidades

def directorio(cfg: Config) -> Path:
    d = cfg.ruta("panel_diario").parent.parent / "combustibles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower().strip()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _texto(s) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore")
                  .decode().lower()).strip()


def clasificar_producto(txt: str | None) -> str | None:
    t = _texto(txt)
    if "gnc" in t or "gas natural" in t:
        return "gnc"
    premium = any(k in t for k in ("grado 3", "premium", "ultra", "euro", "infinia", "v-power", "vpower",
                                   "mas de 95", "> 95", "+95"))
    if any(k in t for k in ("gas oil", "gasoil", "diesel")):
        return "gasoil_premium" if premium else "gasoil"
    if any(k in t for k in ("nafta", "gasolina")):
        return "nafta_premium" if premium else "nafta_super"
    return None


def _numero(col: str) -> pl.Expr:
    """Número con punto o coma decimal ("1.234,5" o "-58.805")."""
    c = pl.col(col).cast(pl.Utf8).str.strip_chars()
    ambos = c.str.contains(",", literal=True) & c.str.contains(".", literal=True)
    c = pl.when(ambos).then(c.str.replace_all(".", "", literal=True)).otherwise(c)
    return c.str.replace(",", ".", literal=True).cast(pl.Float64, strict=False)


def _get(url: str, timeout: int = 300, **kw) -> requests.Response:
    r = requests.get(url, timeout=timeout, headers=UA, **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- descargas

def _recurso_ckan(dataset: str, patron: str) -> str | None:
    try:
        d = _get(CKAN + "package_show", params={"id": dataset}, timeout=60).json()["result"]
    except Exception as e:
        log.warning("Combustibles: no se pudo consultar %s: %s", dataset, e)
        return None
    for r in d.get("resources", []):
        if re.search(patron, (r.get("name") or "") + " " + (r.get("url") or ""), re.I):
            return r.get("url")
    return None


def _recursos_volumenes() -> list[dict]:
    """Recursos CSV del dataset de precios y volúmenes (Energía publica un archivo por período)."""
    try:
        d = _get(CKAN + "package_show", params={"id": DATASET_VOLUMENES}, timeout=60).json()["result"]
    except Exception as e:
        log.warning("Combustibles: no se pudo consultar el catálogo: %s", e)
        return []
    out = []
    for r in d.get("resources", []):
        url, nombre = r.get("url") or "", r.get("name") or ""
        if url.lower().endswith(".csv") or (r.get("format") or "").lower() == "csv":
            out.append({"id": r.get("id"), "nombre": nombre, "url": url,
                        "modificado": r.get("last_modified") or r.get("metadata_modified") or r.get("created") or ""})
    return out


def descargar(cfg: Config, max_dias: float = 6) -> Path | None:
    """Baja todos los CSV de precios y volúmenes por estación y mes (uno por recurso del catálogo).
    Un archivo se vuelve a bajar si el catálogo dice que cambió o si tiene más de `max_dias`."""
    d = directorio(cfg)
    dv = d / "volumenes"
    dv.mkdir(exist_ok=True)
    recursos = _recursos_volumenes() or [{"id": "a-partir-de-2018", "nombre": "precios-y-volumenes-a-partir-de-2018",
                                          "url": URL_VOLUMENES, "modificado": ""}]
    (d / "recursos.json").write_text(json.dumps(recursos, ensure_ascii=False, indent=1), encoding="utf-8")
    previo = {}
    reg = dv / "registro.json"
    if reg.exists():
        previo = json.loads(reg.read_text(encoding="utf-8"))
    for r in recursos:
        destino = dv / f"{r['id']}.csv"
        viejo = destino.exists() and time.time() - destino.stat().st_mtime > max_dias * 86400
        if destino.exists() and previo.get(r["id"]) == r["modificado"] and not (viejo and not r["modificado"]):
            continue
        url = r["url"].replace("https://datos.energia.gob.ar", "http://datos.energia.gob.ar")
        tmp = destino.with_suffix(".part")
        try:
            with requests.get(url, stream=True, timeout=600, headers=UA) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for parte in resp.iter_content(1 << 20):
                        f.write(parte)
            tmp.replace(destino)
            previo[r["id"]] = r["modificado"]
            log.info("Combustibles: %s (%.0f MB)", r["nombre"] or destino.name, destino.stat().st_size / 1e6)
        except Exception as e:
            log.warning("Combustibles: no se pudo bajar %s: %s", url, e)
            if tmp.exists():
                tmp.unlink()
    reg.write_text(json.dumps(previo, indent=1), encoding="utf-8")
    archivos = sorted(dv.glob("*.csv"))
    return dv if archivos else None


def _coordenadas_surtidor(cfg: Config) -> pl.DataFrame:
    """Latitud y longitud de cada estación según los archivos diarios de "Precios en surtidor"
    (el CSV de volúmenes no trae coordenadas). Clave: número de inscripción = idempresa."""
    filas = []
    for a in sorted((directorio(cfg) / "surtidor").glob("*.csv.gz")):
        try:
            df = pl.read_csv(a, infer_schema=False, encoding="utf8-lossy", ignore_errors=True, truncate_ragged_lines=True)
        except Exception as e:
            log.warning("Surtidor %s: %s", a.name, e)
            continue
        cols = {_norm(c): c for c in df.columns}
        if not all(k in cols for k in ("idempresa", "latitud", "longitud")):
            continue
        filas.append(df.select(pl.col(cols["idempresa"]).str.strip_chars().alias("est"),
                               pl.col(cols["latitud"]).alias("lat_s"), pl.col(cols["longitud"]).alias("lon_s")))
    if not filas:
        return pl.DataFrame(schema={"est": pl.Utf8, "lat_s": pl.Float64, "lon_s": pl.Float64})
    t = pl.concat(filas).with_columns(
        pl.col("lat_s").str.replace(",", ".").cast(pl.Float64, strict=False),
        pl.col("lon_s").str.replace(",", ".").cast(pl.Float64, strict=False)).drop_nulls()
    return t.group_by("est").agg(pl.col("lat_s").last(), pl.col("lon_s").last())


def archivar_surtidor(cfg: Config) -> Path | None:
    """Guarda una copia diaria de "Precios en surtidor" (precios vigentes por estación)."""
    d = directorio(cfg) / "surtidor"
    d.mkdir(exist_ok=True)
    destino = d / f"{date.today().isoformat()}.csv.gz"
    if destino.exists():
        return destino
    url = None
    try:
        res = _get(CKAN + "package_search", params={"q": "precios surtidor", "rows": 20}, timeout=60).json()
        for p in res["result"]["results"]:
            for r in p.get("resources", []):
                nombre = f"{r.get('name', '')} {r.get('url', '')}".lower()
                if "surtidor" in nombre and (r.get("format", "").lower() == "csv" or nombre.endswith(".csv")):
                    url = r["url"].replace("https://", "http://")
                    break
            if url:
                break
    except Exception as e:
        log.warning("Surtidor: no se pudo consultar el catálogo: %s", e)
    if not url:
        return None
    try:
        contenido = _get(url, timeout=300).content
        destino.write_bytes(gzip.compress(contenido))
        log.info("Surtidor: %s (%.1f MB)", destino.name, len(contenido) / 1e6)
        return destino
    except Exception as e:
        log.warning("Surtidor: no se pudo bajar %s: %s", url, e)
        return None


def descargar_geometria(cfg: Config) -> None:
    """Provincias y departamentos de Georef (una sola vez)."""
    d = directorio(cfg) / "geo"
    d.mkdir(exist_ok=True)
    for nivel, maximo in (("provincias", 30), ("departamentos", 600)):
        destino = d / f"{nivel}.geojson"
        if destino.exists():
            continue
        try:
            r = _get(f"{GEOREF}{nivel}.geojson", params={"max": maximo}, timeout=300)
            destino.write_bytes(r.content)
            log.info("Georef: %s (%.1f MB)", destino.name, len(r.content) / 1e6)
        except Exception as e:
            log.warning("Georef: no se pudo bajar %s: %s", nivel, e)


# ---------------------------------------------------------------- geometría

def _cargar_geo(cfg: Config) -> tuple[list[dict], list[dict]]:
    d = directorio(cfg) / "geo"
    out = []
    for nivel in ("provincias", "departamentos"):
        ruta = d / f"{nivel}.geojson"
        out.append(json.loads(ruta.read_text(encoding="utf-8"))["features"] if ruta.exists() else [])
    return out[0], out[1]


def _recortar(geom):
    """Saca de la geometría las partes fuera del continente e islas cercanas (Antártida, Georgias)."""
    from shapely.geometry import MultiPolygon, Polygon
    partes = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    ok = [p for p in partes if isinstance(p, Polygon) and -74.5 < p.centroid.x < -53 and -56 < p.centroid.y < -21]
    if not ok:
        return None
    return ok[0] if len(ok) == 1 else MultiPolygon(ok)


def geometria_simple(cfg: Config, tolerancia_prov: float = 0.02, tolerancia_depto: float = 0.008) -> dict:
    """Polígonos simplificados para el mapa del tablero: anillos como listas planas de enteros
    (lon, lat en centésimos de grado)."""
    from shapely.geometry import MultiPolygon, shape
    provs, deptos = _cargar_geo(cfg)

    def anillos(geom, tol):
        g = _recortar(shape(geom))
        if g is None:
            return []
        g = g.simplify(tol, preserve_topology=True)
        partes = list(g.geoms) if isinstance(g, MultiPolygon) else [g]
        out = []
        for p in partes:
            if p.is_empty or p.area < tol * tol * 2:
                continue
            xy = []
            for x, y in p.exterior.coords:
                xy += [round(x * 100), round(y * 100)]
            out.append(xy)
        return out

    res = {"provincias": [], "departamentos": []}
    for f in provs:
        p = f["properties"]
        a = anillos(f["geometry"], tolerancia_prov)
        if a:
            res["provincias"].append({"id": p["id"], "n": p["nombre"], "a": a})
    for f in deptos:
        p = f["properties"]
        a = anillos(f["geometry"], tolerancia_depto)
        if a:
            res["departamentos"].append({"id": p["id"], "n": p["nombre"], "p": p["provincia"]["id"], "a": a})
    return res


def asignar_departamentos(est: pl.DataFrame, cfg: Config) -> pl.DataFrame:
    """Agrega depto_id y prov_id a las estaciones (punto en polígono; si no hay coordenadas,
    el departamento más frecuente de su provincia y localidad)."""
    import numpy as np
    import shapely
    from shapely.geometry import shape
    _, deptos = _cargar_geo(cfg)
    if not deptos:
        return est.with_columns(pl.lit(None, dtype=pl.Utf8).alias("depto_id"), pl.lit(None, dtype=pl.Utf8).alias("prov_id"))
    geoms = [shape(f["geometry"]) for f in deptos]
    ids = [f["properties"]["id"] for f in deptos]
    arbol = shapely.STRtree(geoms)
    lat, lon = est["lat"].to_numpy(), est["lon"].to_numpy()
    validos = np.isfinite(lat) & np.isfinite(lon) & (lat < -21) & (lat > -56) & (lon > -74) & (lon < -53)
    asign = [None] * est.height
    idx = np.where(validos)[0]
    if len(idx):
        pts = shapely.points(lon[idx], lat[idx])
        pi, gi = arbol.query(pts, predicate="within")
        for a, b in zip(pi, gi):
            asign[idx[a]] = ids[b]
    est = est.with_columns(pl.Series("depto_id", asign, dtype=pl.Utf8))
    # respaldo: departamento más frecuente de la misma provincia y localidad
    moda = (est.filter(pl.col("depto_id").is_not_null()).group_by("prov_txt", "loc_txt")
            .agg(pl.col("depto_id").mode().first().alias("_d")))
    est = (est.join(moda, on=["prov_txt", "loc_txt"], how="left")
           .with_columns(pl.coalesce("depto_id", "_d").alias("depto_id")).drop("_d")
           .with_columns(pl.col("depto_id").str.slice(0, 2).alias("prov_id")))
    sin = est.filter(pl.col("depto_id").is_null()).height
    log.info("Combustibles: %d estaciones, %d sin departamento", est.height, sin)
    return est


def _region(prov_id: str | None, lat: float | None, lon: float | None, cfg: Config) -> str | None:
    if prov_id is None:
        return None
    if prov_id == "06" and lat is not None and lon is not None and math.isfinite(lat) and math.isfinite(lon):
        clat, clon = cfg["regiones"]["centro_gba"]
        r = 6371.0
        p1, p2 = math.radians(lat), math.radians(clat)
        dp, dl = p2 - p1, math.radians(clon - lon)
        h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return "GBA" if 2 * r * math.asin(math.sqrt(h)) <= cfg["regiones"]["radio_gba_km"] else "Pampeana"
    return REGION_PROVINCIA.get(prov_id)


# ---------------------------------------------------------------- lectura

def leer(ruta: Path) -> pl.DataFrame:
    """Lee el CSV de Energía y devuelve una fila por estación, producto y mes."""
    with open(ruta, "rb") as f:
        cab = f.readline().decode("utf-8-sig", "ignore")
    sep = ";" if cab.count(";") > cab.count(",") else ","
    lf = pl.scan_csv(ruta, separator=sep, infer_schema=False, encoding="utf8-lossy", ignore_errors=True,
                     truncate_ragged_lines=True)
    cols = lf.collect_schema().names()
    n = {_norm(c): c for c in cols}
    m = {}
    for k, alts in ALIAS.items():
        for a in alts:
            if a in n:
                m[k] = n[a]
                break
    faltan = [k for k in ("provincia", "producto", "precio") if k not in m]
    if faltan or ("anio" not in m and "periodo" not in m):
        raise ValueError(f"Combustibles: faltan columnas {faltan}. Columnas: {cols}")
    sel = [pl.col(v).alias(k) for k, v in m.items()]
    df = lf.select(sel).collect()
    for k in ALIAS:
        if k not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias(k))
    if df["anio"].null_count() == df.height:
        # "2024/12", "2024-12", "2024-12-31 00:00:00" o "2024-12-01"
        am = df["periodo"].str.extract_groups(r"^\s*(\d{4})[/-](\d{1,2})")
        df = df.with_columns(am.struct.field("1").alias("anio"), am.struct.field("2").alias("mes"))
    df = df.with_columns(
        _numero("precio").alias("precio"), _numero("volumen").alias("volumen"),
        _numero("lat").alias("lat"), _numero("lon").alias("lon"),
        _numero("anio").cast(pl.Int32, strict=False).alias("anio"), _numero("mes").cast(pl.Int32, strict=False).alias("mes"),
        pl.col("producto").map_elements(clasificar_producto, return_dtype=pl.Utf8).alias("prod"),
        pl.col("horario").map_elements(_texto, return_dtype=pl.Utf8).alias("horario"),
        pl.col("provincia").map_elements(_texto, return_dtype=pl.Utf8).alias("prov_txt"),
        pl.col("localidad").map_elements(_texto, return_dtype=pl.Utf8).alias("loc_txt"),
    ).filter(pl.col("prod").is_not_null() & (pl.col("precio") > 0) & pl.col("anio").is_not_null() & pl.col("mes").is_between(1, 12))
    # solo venta al público: los archivos de Energía traen también otros canales (mayorista, agro,
    # transporte...), con otros precios, y mezclarlos movía el índice cuando cambiaba la composición
    if df["canal"].drop_nulls().len():
        df = df.filter(pl.col("canal").map_elements(_texto, return_dtype=pl.Utf8).fill_null("").str.contains("publico"))
    # precio diurno cuando la estación informa ambos turnos
    if df["horario"].drop_nulls().str.contains("diurn").any():
        df = df.filter(~pl.col("horario").str.contains("noct").fill_null(False))
    clave = pl.when(pl.col("estacion").is_not_null() & (pl.col("estacion").str.len_chars() > 0)).then(pl.col("estacion")) \
        .otherwise(pl.concat_str([pl.col("cuit"), pl.col("direccion").map_elements(_texto, return_dtype=pl.Utf8),
                                  pl.col("loc_txt")], separator="|", ignore_nulls=True))
    df = df.with_columns(clave.alias("est"), (pl.col("anio").cast(pl.Utf8) + "-" + pl.col("mes").cast(pl.Utf8).str.zfill(2)).alias("mes_id"))
    # una fila por estación, producto y mes (precio promedio ponderado por volumen)
    df = df.with_columns(pl.col("volumen").fill_null(0).clip(lower_bound=0).alias("vol")).group_by("est", "prod", "mes_id").agg(
        pl.when(pl.col("vol").sum() > 0).then((pl.col("precio") * pl.col("vol")).sum() / pl.col("vol").sum())
        .otherwise(pl.col("precio").mean()).alias("precio"),
        pl.col("vol").sum(), pl.col("lat").drop_nulls().last(), pl.col("lon").drop_nulls().last(),
        pl.col("prov_txt").drop_nulls().last(), pl.col("loc_txt").drop_nulls().last(), pl.col("bandera").drop_nulls().last())
    # descarta precios absurdos (fuera de un tercio a tres veces la mediana del producto y mes)
    df = df.with_columns(pl.col("precio").median().over("prod", "mes_id").alias("_m")).filter(
        pl.col("precio").is_between(pl.col("_m") / 3, pl.col("_m") * 3)).drop("_m")
    return df


def procesar(cfg: Config) -> Path | None:
    """CSV -> panel mensual por estación con departamento, provincia y región del IPC."""
    d = directorio(cfg)
    preferidos = ("d6ecf302", "3cbdd364")  # "a partir de 2018" y "desde diciembre 2024"
    archivos = sorted((d / "volumenes").glob("*.csv"), key=lambda a: (a.stem.startswith(preferidos), a.stem)) or ([d / "precios-y-volumenes.csv"] if (d / "precios-y-volumenes.csv").exists() else [])
    if not archivos:
        return None
    destino = d / "panel.parquet"
    surt = sorted((d / "surtidor").glob("*.csv.gz"))
    ultimo = max([a.stat().st_mtime for a in archivos] + [s.stat().st_mtime for s in surt[-1:]])
    if destino.exists() and destino.stat().st_mtime > ultimo:
        return destino
    partes = []
    for a in archivos:
        try:
            partes.append(leer(a))
        except Exception as e:
            log.warning("Combustibles: no se pudo leer %s: %s", a.name, e)
    if not partes:
        return None
    df = pl.concat(partes, how="diagonal_relaxed")
    # si dos archivos traen el mismo mes de la misma estación, queda el último
    df = df.unique(["est", "prod", "mes_id"], keep="last")
    est = df.group_by("est").agg(pl.col("lat").drop_nulls().last(), pl.col("lon").drop_nulls().last(),
                                 pl.col("prov_txt").drop_nulls().last(), pl.col("loc_txt").drop_nulls().last())
    coord = _coordenadas_surtidor(cfg)
    est = (est.join(coord, on="est", how="left")
           .with_columns(pl.coalesce("lat", "lat_s").alias("lat"), pl.coalesce("lon", "lon_s").alias("lon"))
           .drop("lat_s", "lon_s"))
    log.info("Combustibles: %d de %d estaciones con coordenadas", est["lat"].drop_nulls().len(), est.height)
    est = asignar_departamentos(est, cfg)
    est = est.with_columns(pl.Series("region", [_region(p, la, lo, cfg) for p, la, lo in
                                                est.select("prov_id", "lat", "lon").iter_rows()], dtype=pl.Utf8))
    df = df.drop("lat", "lon").join(est.select("est", "depto_id", "prov_id", "region"), on="est", how="left")
    df.write_parquet(destino, compression="zstd")
    log.info("Combustibles: panel con %d filas (%s a %s)", df.height, df["mes_id"].min(), df["mes_id"].max())
    return destino


# ---------------------------------------------------------------- índices y niveles

def _meses(a: str, b: str) -> int:
    a1, m1 = map(int, a.split("-"))
    a2, m2 = map(int, b.split("-"))
    return (a2 - a1) * 12 + (m2 - m1)


def relativos(panel: pl.DataFrame) -> pl.DataFrame:
    """Relativos de precio de cada estación y producto entre meses consecutivos.
    Peso: volumen promedio de los dos meses (cada estación pesa por lo que vende)."""
    p = panel.sort("est", "prod", "mes_id").with_columns(
        pl.col("mes_id").shift(1).over("est", "prod").alias("mes0"),
        pl.col("precio").shift(1).over("est", "prod").alias("p0"),
        pl.col("vol").shift(1).over("est", "prod").alias("v0"))
    p = p.filter(pl.col("mes0").is_not_null())
    p = p.filter(pl.Series([_meses(a, b) == 1 for a, b in p.select("mes0", "mes_id").iter_rows()]))
    return p.with_columns((pl.col("precio") / pl.col("p0")).log().alias("lr"),
                          ((pl.col("vol") + pl.col("v0")) / 2 + 1e-9).alias("w")).filter(
        pl.col("lr").is_between(math.log(0.5), math.log(2)))


def _encadenar(rel: pl.DataFrame, por: list[str]) -> pl.DataFrame:
    """Índice encadenado (100 = primer mes) por grupo `por` + prod."""
    g = rel.group_by(*por, "prod", "mes_id").agg(((pl.col("lr") * pl.col("w")).sum() / pl.col("w").sum()).alias("lr"),
                                                 pl.len().alias("n"))
    return g.sort(*por, "prod", "mes_id").with_columns((pl.col("lr").cum_sum().over(*por, "prod").exp() * 100).alias("indice"))


def indice_mensual(cfg: Config, panel: pl.DataFrame) -> pl.DataFrame:
    """Índice de combustibles (07.2.2) por región del IPC y nacional, con el formato de
    resultados/indices_mensual.csv para sumarse al resto de las series."""
    rel = relativos(panel.filter(pl.col("region").is_not_null()))
    # pesos de cada producto en cada región: volumen de todo el período
    w_prod = panel.filter(pl.col("region").is_not_null()).group_by("region", "prod").agg(pl.col("vol").sum().alias("wp"))
    g = rel.group_by("region", "prod", "mes_id").agg(((pl.col("lr") * pl.col("w")).sum() / pl.col("w").sum()).alias("lr"),
                                                     pl.len().alias("n"))
    g = g.join(w_prod, on=["region", "prod"]).group_by("region", "mes_id").agg(
        ((pl.col("lr") * pl.col("wp")).sum() / pl.col("wp").sum()).alias("lr"), pl.col("n").sum())
    meses = sorted(panel["mes_id"].unique().to_list())
    base = meses[0]
    filas = []
    pond = ponderadores_clase(cfg).filter(pl.col("codigo") == CODIGO)
    w_cl = dict(zip(pond["region"], pond["ponderacion"])) or {r: 1.0 for r in ponderadores_region(cfg)}
    w_reg = ponderadores_region(cfg)
    niveles: dict[str, dict[str, float]] = {}
    for reg in sorted(g["region"].unique().to_list()):
        s = g.filter(pl.col("region") == reg).sort("mes_id")
        nivel, prev = 100.0, base
        niveles[reg] = {base: 100.0}
        for mes, lr, n in s.select("mes_id", "lr", "n").iter_rows():
            if mes <= base:
                continue
            if _meses(prev, mes) != 1:
                # falta un mes en la región: la serie se corta (no se supone que no hubo cambio)
                log.warning("Combustibles %s: falta %s, la serie se corta", reg, prev)
                break
            nivel *= math.exp(lr)
            niveles[reg][mes] = nivel
            prev = mes
            filas.append({"periodo": mes, "region": reg, "indice": nivel, "n_productos": n})
        filas.append({"periodo": base, "region": reg, "indice": 100.0, "n_productos": 0})
    df = pl.DataFrame(filas)
    # nacional: regiones ponderadas por su peso en el gasto y el peso de la clase en cada región
    nac = df.with_columns((pl.col("region").replace_strict(w_reg, default=0.0, return_dtype=pl.Float64)
                           * pl.col("region").replace_strict(w_cl, default=0.0, return_dtype=pl.Float64)).alias("w")) \
        .group_by("periodo").agg(((pl.col("indice") * pl.col("w")).sum() / pl.col("w").sum()).alias("indice"),
                                 pl.col("n_productos").sum(), pl.len().alias("_r"))
    nac = nac.filter(pl.col("_r") == df["region"].n_unique()).drop("_r").with_columns(pl.lit(NACIONAL).alias("region"))
    df = pl.concat([df, nac.select(df.columns)]).sort("region", "periodo")
    df = df.with_columns(
        pl.lit("clase").alias("nivel"), pl.lit(CODIGO).alias("codigo"),
        pl.lit("Combustibles y lubricantes para vehículos").alias("nombre"),
        ((pl.col("indice") / pl.col("indice").shift(1).over("region") - 1) * 100).alias("var_pct"),
        ((pl.col("indice") / pl.col("indice").shift(12).over("region") - 1) * 100).alias("var_interanual_pct"),
        pl.lit(False).alias("parcial"), pl.lit(False).alias("salto"),
        pl.col("periodo").shift(1).over("region").alias("periodo_anterior"),
        pl.lit(None, dtype=pl.Float64).alias("var_salto_pct"), pl.lit(0, dtype=pl.Int32).alias("tramo"))
    return df


def niveles(panel: pl.DataFrame) -> dict:
    """Precio promedio (ponderado por volumen) e índice encadenado por país, provincia y
    departamento, producto y mes."""
    meses = sorted(panel["mes_id"].unique().to_list())
    pos = {m: i for i, m in enumerate(meses)}
    rel = relativos(panel)
    out = {"meses": meses, "productos": PRODUCTOS, "pais": {}, "provincias": {}, "departamentos": {}}

    def precio(df, por):
        return df.group_by(*por, "prod", "mes_id").agg(
            pl.when(pl.col("vol").sum() > 0).then((pl.col("precio") * pl.col("vol")).sum() / pl.col("vol").sum())
            .otherwise(pl.col("precio").mean()).alias("p"), pl.col("est").n_unique().alias("n"))

    def volcar(destino, pr, ix, por, productos):
        series = {}
        for fila in pr.iter_rows(named=True):
            k = tuple(fila[c] for c in por) if por else ("AR",)
            if fila["prod"] not in productos:
                continue
            s = series.setdefault(k, {}).setdefault(fila["prod"], {"p": [None] * len(meses), "i": [None] * len(meses),
                                                                   "n": [0] * len(meses)})
            s["p"][pos[fila["mes_id"]]] = round(fila["p"], 2)
            s["n"][pos[fila["mes_id"]]] = fila["n"]
        for fila in ix.iter_rows(named=True):
            k = tuple(fila[c] for c in por) if por else ("AR",)
            if k in series and fila["prod"] in series[k]:
                series[k][fila["prod"]]["i"][pos[fila["mes_id"]]] = round(fila["indice"], 2)
        # el índice arranca en 100 en el primer mes con precio
        for k, prods in series.items():
            for s in prods.values():
                i0 = next((i for i, v in enumerate(s["p"]) if v is not None), None)
                if i0 is not None and s["i"][i0] is None:
                    s["i"][i0] = 100.0
        for k, v in series.items():
            destino[k[0] if len(k) == 1 else "|".join(k)] = v

    volcar(out["pais"], precio(panel, []), _encadenar(rel, []), [], list(PRODUCTOS))
    pp = panel.filter(pl.col("prov_id").is_not_null())
    volcar(out["provincias"], precio(pp, ["prov_id"]), _encadenar(rel.filter(pl.col("prov_id").is_not_null()), ["prov_id"]),
           ["prov_id"], list(PRODUCTOS))
    pd_ = panel.filter(pl.col("depto_id").is_not_null())
    volcar(out["departamentos"], precio(pd_, ["depto_id"]),
           _encadenar(rel.filter(pl.col("depto_id").is_not_null()), ["depto_id"]), ["depto_id"], PRODUCTOS_DEPTO)
    # un mes sin relativos queda vacío: no se arrastra el nivel del mes anterior
    return out


def total_con_combustibles(cfg: Config, mensual: pl.DataFrame, comb: pl.DataFrame,
                           cobertura: pl.DataFrame | None) -> list[dict]:
    """IPCEER con combustibles: el total de bienes y el índice de combustibles, ponderados por
    la parte de la canasta que cubre cada uno, en los meses que tienen los dos."""
    if mensual is None or comb is None or comb.is_empty():
        return []
    tot = mensual.filter(pl.col("nivel") == "total", pl.col("codigo") == "TOTAL")
    if "tramo" in tot.columns:
        tot = tot.filter(pl.col("tramo") == tot["tramo"].max())
    pond = ponderadores_clase(cfg).filter(pl.col("codigo") == CODIGO)
    w_f = dict(zip(pond["region"], pond["ponderacion"]))
    w_b = dict(zip(cobertura["region"], cobertura["ponderacion_cubierta_pct"])) if cobertura is not None else {}
    w_reg = ponderadores_region(cfg)
    out = []
    for reg in [NACIONAL, *w_reg]:
        b = dict(tot.filter(pl.col("region") == reg).select("periodo", "indice").iter_rows())
        f = dict(comb.filter(pl.col("region") == reg).select("periodo", "indice").iter_rows())
        meses = sorted(set(b) & set(f))
        if len(meses) < 2:
            continue
        if reg == NACIONAL:
            wb = sum(w_reg[r] * w_b.get(r, 0) for r in w_reg)
            wf = sum(w_reg[r] * w_f.get(r, 0) for r in w_reg)
        else:
            wb, wf = w_b.get(reg, 0), w_f.get(reg, 0)
        if wb + wf <= 0:
            continue
        nivel = 100.0
        prev = None
        for m in meses:
            if prev is not None and _meses(prev, m) == 1:
                vb, vf = b[m] / b[prev], f[m] / f[prev]
                nivel *= (wb * vb + wf * vf) / (wb + wf)
                out.append({"mes": m, "region": reg, "indice": nivel, "var": ((wb * vb + wf * vf) / (wb + wf) - 1) * 100,
                            "var_bienes": (vb - 1) * 100, "var_combustibles": (vf - 1) * 100,
                            "peso_combustibles": 100 * wf / (wb + wf)})
            prev = m
    return out


def construir(cfg: Config) -> tuple[dict, pl.DataFrame | None]:
    """Todo lo de combustibles para el tablero y el índice 07.2.2 (o ({}, None) si no hay datos)."""
    ruta = directorio(cfg) / "panel.parquet"
    if not ruta.exists():
        return {}, None
    panel = pl.read_parquet(ruta)
    comb = indice_mensual(cfg, panel)
    datos = niveles(panel)
    datos["indice"] = [{"mes": r["periodo"], "region": r["region"], "indice": r["indice"], "var": r["var_pct"],
                        "var_ia": r["var_interanual_pct"]} for r in comb.iter_rows(named=True)]
    try:
        datos["geo"] = geometria_simple(cfg)
    except Exception as e:
        log.warning("Combustibles: geometría: %s", e)
        datos["geo"] = {}
    datos["fuente"] = "Secretaría de Energía, Precios y volúmenes por estación de servicio (Res. 1104/2004); Georef (IGN)"
    datos["n_estaciones"] = int(panel["est"].n_unique())
    datos["sin_departamento_pct"] = round(100 * panel.filter(pl.col("depto_id").is_null())["vol"].sum()
                                          / max(panel["vol"].sum(), 1), 2)
    comb.write_csv(cfg.ruta("resultados") / "combustibles_mensual.csv")
    return datos, comb


def actualizar(cfg: Config) -> None:
    """Descarga y procesa (lo llama `ipceer diario`)."""
    descargar_geometria(cfg)
    archivar_surtidor(cfg)
    if descargar(cfg):
        try:
            procesar(cfg)
        except Exception as e:
            log.warning("Combustibles: no se pudo procesar el CSV: %s", e)
