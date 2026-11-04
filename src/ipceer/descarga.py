"""Descarga de los zips diarios de SEPA desde el portal CKAN de datos.produccion.gob.ar.

El dataset `sepa-precios` mantiene un recurso por día de la semana (sepa_lunes.zip, ...),
que se pisa cada semana. O sea: el portal solo guarda los últimos 7 días.
Cada día que no se descarga se pierde, por eso este paso tiene que correr todos los días.

Nota: el portal respondió 403 a los servidores de GitHub Actions. La descarga principal corre
en la PC; el flujo de respaldo de GitHub lo intenta igual (y si no puede, prueba la Wayback
Machine) y guarda lo que consigue en un release, de donde la PC completa los días que falten.
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


UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36"}


def listar_recursos(cfg: Config) -> list[Recurso]:
    d = cfg["descarga"]
    r = requests.get(d["ckan_api"], params={"id": d["dataset"]}, timeout=d["timeout_segundos"], headers=UA)
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
            with requests.get(url, stream=True, timeout=timeout, headers=UA) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for bloque in r.iter_content(chunk_size=1 << 20):
                        f.write(bloque)
            tmp.rename(destino)
            return
        except (requests.RequestException, OSError) as e:
            log.warning("Fallo descargando %s (intento %d/%d): %s", url, intento, reintentos, e)
            tmp.unlink(missing_ok=True)
            # 429 = el servidor pide que se baje el ritmo: reintentar enseguida empeora las cosas
            if getattr(getattr(e, "response", None), "status_code", None) == 429:
                raise
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


# --- Copias históricas en la Wayback Machine (Internet Archive) -------------------------

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
DATASET_ID = "6f47ec76-d1ce-4e34-a7e1-621fe9b1d0b5"  # sepa-precios (minoristas)


def listar_wayback(timeout: int = 60) -> list[tuple[str, str, int]]:
    """Capturas de los zips diarios de SEPA guardadas en la Wayback Machine.

    Devuelve (timestamp, url original, bytes) de las capturas con estado 200.
    """
    r = requests.get(WAYBACK_CDX, params={
        "url": f"datos.produccion.gob.ar/dataset/{DATASET_ID}/resource/*",
        "output": "json", "fl": "timestamp,original,statuscode,length",
    }, timeout=timeout)
    r.raise_for_status()
    filas = r.json()[1:] if r.text.strip() else []
    vistas, out = set(), []
    for ts, orig, estado, largo in filas:
        if estado != "200" or not orig.lower().endswith(".zip") or "sepa_" not in orig:
            continue
        clave = (ts[:8], orig)
        if clave in vistas:
            continue
        vistas.add(clave)
        out.append((ts, orig, int(largo or 0)))
    return out


def _fecha_dentro_del_zip(ruta: Path) -> date | None:
    """La fecha real de un zip SEPA es la carpeta AAAA-MM-DD/ de adentro."""
    with zipfile.ZipFile(ruta) as zf:
        for n in zf.namelist():
            m = _FECHA.match(n)
            if m:
                return date.fromisoformat(m.group(1))
    return None


def descargar_wayback(cfg: Config) -> list[Path]:
    """Agrega al archivo los días de SEPA que solo existen en la Wayback Machine.

    Se baja cada captura una vez (se recuerdan en wayback.csv), se lee la fecha de la
    carpeta interna y se guarda como sepa_AAAA-MM-DD.zip si ese día no estaba archivado.
    """
    d = cfg["descarga"]
    dir_crudos = cfg.ruta("crudos")
    registro = dir_crudos / "wayback.csv"
    hechas = set()
    if registro.exists():
        hechas = {l.split(",")[0] + "," + l.split(",")[1] for l in registro.read_text(encoding="utf-8").splitlines()[1:]}
    nuevos = []
    for ts, orig, largo in listar_wayback(d["timeout_segundos"]):
        if f"{ts},{orig}" in hechas:
            continue
        tmp = dir_crudos / f"wayback_{ts}.zip"
        log.info("Wayback: bajando captura %s de %s (%.0f MB)", ts, orig.rsplit("/", 1)[-1], largo / 1e6)
        try:
            _bajar(f"https://web.archive.org/web/{ts}id_/{orig}", tmp, d["timeout_segundos"] * 5, d["reintentos"])
        except Exception as e:
            if getattr(getattr(e, "response", None), "status_code", None) == 429:
                log.warning("Wayback: el Internet Archive limita las descargas (429). Se sigue en la próxima corrida.")
                break
            log.error("Wayback: no se pudo bajar %s: %s", ts, e)
            continue
        time.sleep(5)  # una captura cada tantos segundos, para no superar el límite del Archive
        fecha = _fecha_dentro_del_zip(tmp) if zipfile.is_zipfile(tmp) else None
        destino = dir_crudos / f"sepa_{fecha.isoformat()}.zip" if fecha else None
        if destino is None:
            tmp.rename(tmp.with_suffix(".invalido"))
            estado = "invalido"
        elif destino.exists():
            tmp.unlink()
            estado = "ya_estaba"
        else:
            tmp.rename(destino)
            _registrar(dir_crudos, Recurso(fecha, f"https://web.archive.org/web/{ts}id_/{orig}", "wayback"), destino)
            nuevos.append(destino)
            estado = "agregado"
        nuevo_registro = not registro.exists()
        with open(registro, "a", encoding="utf-8") as f:
            if nuevo_registro:
                f.write("timestamp,url,fecha,estado\n")
            f.write(f"{ts},{orig},{fecha or ''},{estado}\n")
    return nuevos


# --- Respaldo en GitHub (release "sepa-archivo" del repo) -------------------------------

def _repo_respaldo(cfg: Config) -> tuple[str, str] | None:
    r = (cfg.datos.get("descarga") or {}).get("respaldo_github") or {}
    return (r["repo"], r.get("tag", "sepa-archivo")) if r.get("repo") else None


def desde_github(cfg: Config) -> list[Path]:
    """Trae del respaldo en GitHub los zips de SEPA que falten en la carpeta local.

    El respaldo lo llena un flujo de GitHub Actions todos los días; sirve para los días en que
    la PC estuvo apagada más de lo que el portal guarda (7 días)."""
    rr = _repo_respaldo(cfg)
    if rr is None:
        return []
    repo, tag = rr
    d = cfg["descarga"]
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", timeout=d["timeout_segundos"],
                         headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 404:
            return []
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("Respaldo en GitHub no disponible: %s", e)
        return []
    dir_crudos = cfg.ruta("crudos")
    nuevos = []
    for a in r.json().get("assets", []):
        nombre = a.get("name", "")
        if not re.fullmatch(r"sepa_\d{4}-\d{2}-\d{2}\.zip", nombre):
            continue
        destino = dir_crudos / nombre
        if destino.exists():
            continue
        log.info("Respaldo en GitHub: bajando %s (%.0f MB)", nombre, a.get("size", 0) / 1e6)
        try:
            _bajar(a["browser_download_url"], destino, d["timeout_segundos"] * 5, d["reintentos"])
        except Exception as e:
            log.warning("Respaldo en GitHub: no se pudo bajar %s: %s", nombre, e)
            continue
        if not zipfile.is_zipfile(destino):
            destino.rename(destino.with_suffix(".invalido"))
            continue
        _registrar(dir_crudos, Recurso(fecha_de_zip(destino), a["browser_download_url"], "github"), destino)
        nuevos.append(destino)
    return nuevos
