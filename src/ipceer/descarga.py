"""Descarga de los zips diarios de SEPA desde el portal CKAN de datos.produccion.gob.ar.

El dataset `sepa-precios` mantiene un recurso por día de la semana (sepa_lunes.zip, ...),
que se pisa cada semana. O sea: el portal solo guarda los últimos 7 días.
Cada día que no se descarga se pierde, por eso este paso tiene que correr todos los días.

Nota: el portal responde 403 a los servidores de GitHub Actions; hay que correr esto
desde una máquina en Argentina (servidor propio o runner self-hosted).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import requests

from .config import Config

log = logging.getLogger(__name__)

_FECHA = re.compile(r"(\d{4}-\d{2}-\d{2})")


@dataclass
class Recurso:
    fecha: date
    url: str
    nombre: str


def listar_recursos(cfg: Config) -> list[Recurso]:
    d = cfg["descarga"]
    r = requests.get(d["ckan_api"], params={"id": d["dataset"]}, timeout=d["timeout_segundos"])
    r.raise_for_status()
    recursos = []
    for res in r.json()["result"]["resources"]:
        texto = " ".join(str(res.get(k) or "") for k in ("description", "name", "url"))
        m = _FECHA.search(texto)
        if not m or not str(res.get("url", "")).lower().endswith(".zip"):
            continue
        recursos.append(Recurso(date.fromisoformat(m.group(1)), res["url"], res.get("name", "")))
    return sorted(recursos, key=lambda x: x.fecha)


def _bajar(url: str, destino: Path, timeout: int, reintentos: int) -> None:
    tmp = destino.with_suffix(".parcial")
    for intento in range(1, reintentos + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for bloque in r.iter_content(chunk_size=1 << 20):
                        f.write(bloque)
            tmp.rename(destino)
            return
        except (requests.RequestException, OSError) as e:
            log.warning("Fallo descargando %s (intento %d/%d): %s", url, intento, reintentos, e)
            tmp.unlink(missing_ok=True)
            if intento == reintentos:
                raise
            time.sleep(10 * intento)


def _registrar(dir_crudos: Path, rec: Recurso, archivo: Path) -> None:
    """Agrega una línea al manifiesto del archivo (fecha, origen, tamaño, hash)."""
    h = hashlib.sha256()
    with open(archivo, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    manifiesto = dir_crudos / "manifiesto.csv"
    nuevo = not manifiesto.exists()
    with open(manifiesto, "a", encoding="utf-8") as f:
        if nuevo:
            f.write("fecha,archivo,bytes,sha256,url,descargado\n")
        f.write(f"{rec.fecha},{archivo.name},{archivo.stat().st_size},{h.hexdigest()},{rec.url},"
                f"{datetime.now().isoformat(timespec='seconds')}\n")


def descargar_pendientes(cfg: Config) -> list[Path]:
    """Archiva los días publicados en el portal que todavía no están en la carpeta local.

    Devuelve todos los zips archivados que aún no tienen panel diario procesado.
    """
    d = cfg["descarga"]
    dir_crudos = cfg.ruta("crudos")
    for rec in listar_recursos(cfg):
        destino = dir_crudos / f"sepa_{rec.fecha.isoformat()}.zip"
        if destino.exists():
            continue
        log.info("Descargando %s (%s)", rec.fecha, rec.url)
        _bajar(rec.url, destino, d["timeout_segundos"], d["reintentos"])
        if not zipfile.is_zipfile(destino):
            destino.rename(destino.with_suffix(".invalido"))
            log.error("%s no es un zip válido; se dejó como .invalido", destino.name)
            continue
        _registrar(dir_crudos, rec, destino)
    return zips_sin_procesar(cfg)


def zips_sin_procesar(cfg: Config) -> list[Path]:
    dir_panel = cfg.ruta("panel_diario")
    pendientes = []
    for z in sorted(cfg.ruta("crudos").glob("sepa_*.zip")):
        fecha = z.stem.removeprefix("sepa_")
        if not (dir_panel / f"{fecha}.parquet").exists():
            pendientes.append(z)
    return pendientes


def fecha_de_zip(ruta: Path) -> date:
    return date.fromisoformat(ruta.stem.removeprefix("sepa_"))
