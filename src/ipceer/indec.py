"""Series oficiales del IPC del INDEC (base diciembre 2016 = 100), para comparar.

Se bajan en la máquina que corre el proceso (el sitio del INDEC no es accesible desde
todos lados) y se guardan en data/indec/.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

from .config import Config

log = logging.getLogger(__name__)

SERIES = {
    "serie_ipc_divisiones.csv": "https://www.indec.gob.ar/ftp/cuadros/economia/serie_ipc_divisiones.csv",
    "serie_ipc_aperturas.csv": "https://www.indec.gob.ar/ftp/cuadros/economia/serie_ipc_aperturas.csv",
}


def directorio(cfg: Config) -> Path:
    d = cfg.ruta("panel_diario").parent.parent / "indec"
    d.mkdir(parents=True, exist_ok=True)
    return d


def descargar(cfg: Config, max_horas: float = 24) -> list[Path]:
    d = directorio(cfg)
    out = []
    for nombre, url in SERIES.items():
        destino = d / nombre
        if destino.exists() and (time.time() - destino.stat().st_mtime) < max_horas * 3600:
            out.append(destino)
            continue
        try:
            r = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0 (IPCEER)"})
            r.raise_for_status()
            destino.write_bytes(r.content)
            log.info("INDEC: %s (%d KB)", nombre, len(r.content) // 1024)
            out.append(destino)
        except requests.RequestException as e:
            log.warning("INDEC: no se pudo bajar %s: %s", nombre, e)
            if destino.exists():
                out.append(destino)
    return out
