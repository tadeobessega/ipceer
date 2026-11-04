"""Asignación de sucursales a las regiones del IPC del INDEC."""

from __future__ import annotations

import math
import unicodedata

import polars as pl

from .config import Config


def normalizar(texto: str | None) -> str:
    if texto is None:
        return ""
    t = unicodedata.normalize("NFKD", str(texto).lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.replace(".", " ").split())


def _tablas(cfg: Config):
    prov = pl.read_csv(cfg.dir_config / "provincias.csv")
    por_codigo = dict(zip(prov["codigo"], prov["region"]))
    por_nombre = {normalizar(n): r for n, r in zip(prov["nombre"], prov["region"])}
    # alias frecuentes
    por_nombre.update({"capital federal": "GBA", "caba": "GBA", "ciudad de buenos aires": "GBA",
                       "tierra del fuego antartida e islas del atlantico sur": "Patagonia"})
    gba = set(normalizar(x) for x in pl.read_csv(cfg.dir_config / "gba_localidades.csv")["localidad"])
    return por_codigo, por_nombre, gba


def _distancia_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def asignar_regiones(sucursales: pl.DataFrame, cfg: Config) -> pl.DataFrame:
    """Agrega la columna `region` a un DataFrame de sucursales SEPA.

    Usa sucursales_provincia (código ISO "AR-B" o nombre), y para la provincia de
    Buenos Aires decide entre GBA y Pampeana por distancia al centro o por localidad.
    """
    por_codigo, por_nombre, gba = _tablas(cfg)
    radio = cfg["regiones"]["radio_gba_km"]
    clat, clon = cfg["regiones"]["centro_gba"]

    def region(prov, lat, lon, loc):
        p = (prov or "").strip().upper()
        reg = por_codigo.get(p) or por_nombre.get(normalizar(prov))
        if reg is None:
            return None
        if p == "AR-B" or normalizar(prov) == "buenos aires":
            try:
                la, lo = float(str(lat).replace(",", ".")), float(str(lon).replace(",", "."))
                if -56 < la < -21 and -74 < lo < -53:  # coordenadas plausibles en Argentina
                    return "GBA" if _distancia_km(la, lo, clat, clon) <= radio else "Pampeana"
            except (TypeError, ValueError):
                pass
            return "GBA" if normalizar(loc) in gba else "Pampeana"
        return reg

    cols = ["sucursales_provincia", "sucursales_latitud", "sucursales_longitud", "sucursales_localidad"]
    for c in cols:
        if c not in sucursales.columns:
            sucursales = sucursales.with_columns(pl.lit(None, dtype=pl.Utf8).alias(c))
    regiones = [region(*fila) for fila in sucursales.select(cols).iter_rows()]
    return sucursales.with_columns(pl.Series("region", regiones, dtype=pl.Utf8))
