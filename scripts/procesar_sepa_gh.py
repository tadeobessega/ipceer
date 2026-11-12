"""Plan A (METODOLOGIA.md, D-33): procesa en GitHub Actions los zips de SEPA del release.

Para cada sepa_AAAA-MM-DD.zip del release que todavía no tiene su proc_AAAA-MM-DD.tar, lo baja, lo
procesa con el mismo código que la PC (lectura.procesar_zip_diario: panel diario, mapa y municipios)
y empaqueta el resultado. La PC trae el .tar y no tiene que descomprimir ni procesar el zip.

Uso: python scripts/procesar_sepa_gh.py assets.txt salida/ [--max 4] [--desde AAAA-MM-DD]
  assets.txt: nombres de los archivos del release (uno por línea).
Necesita data/combustibles/geo/departamentos.geojson (el flujo lo baja del release) para el mapa y
los municipios; sin él procesa igual el panel diario.
"""
from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
import tarfile
from datetime import date
from pathlib import Path

from ipceer import lectura
from ipceer.config import cargar_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("procesar_gh")


def pendientes(assets: set[str], desde: str | None) -> list[str]:
    zips = sorted({m.group(1) for a in assets if (m := re.fullmatch(r"sepa_(\d{4}-\d{2}-\d{2})\.zip", a))}, reverse=True)
    return [f for f in zips if f"proc_{f}.tar" not in assets and (desde is None or f >= desde)]


def empaquetar(dir_data: Path, fecha: str, destino: Path) -> list[str]:
    miembros = [p for p in [dir_data / "panel" / "diario" / f"{fecha}.parquet",
                            dir_data / "geo" / "diario" / f"{fecha}.parquet",
                            *sorted((dir_data / "municipios").glob(f"*/panel/diario/{fecha}.parquet")),
                            dir_data / "panel" / "cadenas.csv"] if p.exists()]
    with tarfile.open(destino, "w") as tf:   # los parquet ya vienen comprimidos
        for p in miembros:
            tf.add(p, arcname=p.relative_to(dir_data).as_posix())
    return [p.relative_to(dir_data).as_posix() for p in miembros]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("assets")
    ap.add_argument("salida")
    ap.add_argument("--max", type=int, default=4)
    ap.add_argument("--desde", default=None)
    ap.add_argument("--tag", default="sepa-archivo")
    a = ap.parse_args()
    assets = set(Path(a.assets).read_text().split())
    cola = pendientes(assets, a.desde)[: a.max]
    log.info("Días a procesar: %s", cola)
    cfg = cargar_config()
    dir_crudos = cfg.ruta("crudos")
    dir_data = cfg.ruta("panel_diario").parent.parent
    salida = Path(a.salida)
    salida.mkdir(parents=True, exist_ok=True)
    errores = 0
    for fecha in cola:
        z = dir_crudos / f"sepa_{fecha}.zip"
        try:
            subprocess.run(["gh", "release", "download", a.tag, "-p", z.name, "-D", str(dir_crudos), "--clobber"], check=True)
            (dir_data / "panel" / "cadenas.csv").unlink(missing_ok=True)   # solo las cadenas de este día
            lectura.procesar_zip_diario(z, date.fromisoformat(fecha), cfg)
            m = empaquetar(dir_data, fecha, salida / f"proc_{fecha}.tar")
            log.info("%s: %s", fecha, m)
        except Exception as e:
            errores += 1
            log.error("%s: %s", fecha, e)
        finally:
            z.unlink(missing_ok=True)
    print(f"procesados={len(cola) - errores} errores={errores}")
    return 1 if errores and errores == len(cola) else 0


if __name__ == "__main__":
    sys.exit(main())
