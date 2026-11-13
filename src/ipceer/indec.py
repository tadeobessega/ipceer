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
    # canasta básica alimentaria y total, Gran Buenos Aires, por adulto equivalente
    "serie_cba_cbt.xls": "https://www.indec.gob.ar/ftp/cuadros/sociedad/serie_cba_cbt.xls",
    # precios promedio de una selección de productos (incluye cortes de carne), GBA y regiones
    "sh_ipc_precios_promedio.xls": "https://www.indec.gob.ar/ftp/cuadros/economia/sh_ipc_precios_promedio.xls",
}
MESES_LARGOS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
                "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}

MESES_ES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "set": 9,
            "oct": 10, "nov": 11, "dic": 12}


def directorio(cfg: Config) -> Path:
    if cfg.datos.get("rutas", {}).get("indec"):
        return cfg.ruta("indec")
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


def _num(v) -> float:
    """Número de una celda: 239058, "239058.5", "239.058,5" o "2,21"."""
    if not isinstance(v, str):
        return float(v)
    t = v.strip().replace("$", "").replace(" ", "")
    import re
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", t):  # "239.058": punto de miles
        t = t.replace(".", "")
    return float(t)


def _mes_de_celda(v) -> str | None:
    """Reconoce un mes en una celda: fecha de Excel, '2026-09', 'sep-26', 'Septiembre 2026'."""
    import datetime as dt
    import re
    if isinstance(v, (dt.date, dt.datetime)):
        return f"{v.year}-{v.month:02d}"
    if not isinstance(v, str):
        return None
    t = v.strip().lower()
    m = re.match(r"^(\d{4})-(\d{2})", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"^([a-zé]{3})[a-zé]*[\s\-./]*(\d{2,4})$", t)
    if m and m.group(1) in MESES_ES:
        a = int(m.group(2))
        a = a + 2000 if a < 100 else a
        return f"{a}-{MESES_ES[m.group(1)]:02d}"
    return None


def leer_cba(dir_indec: Path) -> "pl.DataFrame":
    """CBA oficial de Gran Buenos Aires por adulto equivalente: columnas mes (AAAA-MM), cba ($),
    ice (inversa del coeficiente de Engel) y cbt ($).

    Lee data/indec/serie_cba_cbt.xls (cuadro del INDEC: mes, CBA, inversa del coeficiente de
    Engel, CBT). Si existe data/indec/cba_gba.csv (columnas mes,cba) se usa ese archivo.
    """
    import polars as pl
    manual = dir_indec / "cba_gba.csv"
    vacio = pl.DataFrame(schema={"mes": pl.Utf8, "cba": pl.Float64, "ice": pl.Float64, "cbt": pl.Float64})
    if manual.exists():
        m = pl.read_csv(manual, schema_overrides={"mes": pl.Utf8, "cba": pl.Float64})
        for c in ("ice", "cbt"):
            if c not in m.columns:
                m = m.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))
        return m.select("mes", "cba", "ice", "cbt")
    ruta = next((dir_indec / n for n in ("serie_cba_cbt.xls", "serie_cba_cbt.xlsx") if (dir_indec / n).exists()), None)
    if ruta is None:
        return vacio
    import shutil
    import tempfile
    import fastexcel
    libro = None
    # el sitio a veces sirve un .xlsx con extensión .xls: se prueba con las dos
    for suf in (ruta.suffix, ".xlsx" if ruta.suffix == ".xls" else ".xls"):
        try:
            if suf == ruta.suffix:
                libro = fastexcel.read_excel(str(ruta))
            else:
                tmp = Path(tempfile.mkdtemp()) / f"cba{suf}"
                shutil.copy(ruta, tmp)
                libro = fastexcel.read_excel(str(tmp))
            break
        except Exception as e:
            err = e
    if libro is None:
        log.warning("CBA INDEC: no se pudo leer %s: %s", ruta.name, err)
        return vacio
    filas = {}
    for nombre in libro.sheet_names:
        try:
            hoja = libro.load_sheet_by_name(nombre, header_row=None).to_polars()
        except Exception:
            continue
        for fila in hoja.iter_rows():
            mes = None
            for i, v in enumerate(fila):
                mes = _mes_de_celda(v)
                if mes:
                    resto = fila[i + 1:]
                    break
            if not mes:
                continue
            nums = []
            for v in resto:
                try:
                    nums.append(_num(v))
                except (TypeError, ValueError):
                    continue
            # orden del cuadro: CBA, inversa del coeficiente de Engel (~2 a 3), CBT
            cba = next((x for x in nums if x > 10), None)
            if cba is None or mes in filas:
                continue
            despues = nums[nums.index(cba) + 1:]
            ice = next((x for x in despues if 1 < x < 6), None)
            cbt = next((x for x in despues if x > 10), None)
            filas[mes] = (cba, ice, cbt)
        if filas:
            break
    if not filas:
        log.warning("CBA INDEC: no se encontraron datos en %s", ruta.name)
        return vacio
    return pl.DataFrame({"mes": list(filas), "cba": [v[0] for v in filas.values()],
                         "ice": [v[1] for v in filas.values()], "cbt": [v[2] for v in filas.values()]},
                        schema={"mes": pl.Utf8, "cba": pl.Float64, "ice": pl.Float64, "cbt": pl.Float64}).sort("mes")
