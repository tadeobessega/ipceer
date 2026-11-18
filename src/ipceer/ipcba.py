"""IPC de la Ciudad de Buenos Aires (IDECBA, base 2021 = 100), principales aperturas.

No se compara con el INDEC ni entra al índice: es un insumo de modelos (D-65, servicios médicos con
la apertura "Servicios para pacientes externos" de dos meses antes). Se baja el Excel de la página
del IPCBA; si no se puede, se usa la copia más nueva que haya (data/otras/ipcba o la semilla de
config/ipcba)."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import polars as pl
import requests

from .config import Config

log = logging.getLogger(__name__)

PAGINA = "https://www.estadisticaciudad.gob.ar/eyc/?p=144551"
ARCHIVO = "IPCBA_base_2021100-Principales_aperturas_indices.xlsx"
UA = {"User-Agent": "IPCEER/1.0 (indice de precios del CEER; +https://github.com/tadeobessega/ipceer)"}


def _dir(cfg: Config) -> Path:
    from .otras_fuentes import _dir as dir_otras
    return dir_otras(cfg, "ipcba")


def actualizar(cfg: Config) -> dict:
    """Busca en la página del IPCBA el enlace al Excel de aperturas y lo baja si cambió."""
    r = requests.get(PAGINA, headers=UA, timeout=60)
    r.raise_for_status()
    urls = sorted(set(re.findall(r'https?://[^"\'\s]+' + re.escape(ARCHIVO), r.text)))
    if not urls:
        return {"error": "no se encontró el enlace al Excel en la página del IPCBA"}
    x = requests.get(urls[-1], headers=UA, timeout=120)
    x.raise_for_status()
    destino = _dir(cfg) / ARCHIVO
    if not destino.exists() or destino.read_bytes() != x.content:
        destino.write_bytes(x.content)
    return {"url": urls[-1], "ultimo_mes": max(variaciones(cfg, "Nivel General"), default=None)}


def _ruta(cfg: Config) -> Path | None:
    candidatos = [p for p in (_dir(cfg) / ARCHIVO, cfg.dir_config / "ipcba" / ARCHIVO) if p.exists()]
    if not candidatos:
        return None
    # la que llega a un mes más reciente
    return max(candidatos, key=lambda p: (_leer(p).width, p.stat().st_mtime))


def _leer(ruta: Path) -> pl.DataFrame:
    return pl.read_excel(ruta, sheet_id=1, has_header=False, infer_schema_length=0)


def variaciones(cfg: Config, apertura: str) -> dict[str, float]:
    """Variación mensual (%) de una apertura, por mes AAAA-MM."""
    ruta = _ruta(cfg)
    if ruta is None:
        return {}
    df = _leer(ruta)
    filas = df.rows()
    fila_f = next(i for i, f in enumerate(filas) if sum(1 for x in f[1:] if x and re.match(r"\d{4}-\d{2}", str(x))) > 3)
    meses = [str(x)[:7] if x else None for x in filas[fila_f][1:]]
    for f in filas[fila_f + 1:]:
        if f[0] and str(f[0]).strip() == apertura:
            niv = {m: float(v) for m, v in zip(meses, f[1:]) if m and v not in (None, "") and re.match(r"^[\d.,-]+$", str(v))}
            ms = sorted(niv)
            return {b: (niv[b] / niv[a] - 1) * 100 for a, b in zip(ms, ms[1:]) if niv[a]}
    return {}
