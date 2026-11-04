"""Respaldo diario de SEPA en GitHub Actions.

Baja los zips diarios que el portal tiene publicados y que todavía no están en el release
`sepa-archivo` (la lista viene en respaldados.txt). Si el portal no responde desde GitHub, prueba
con las capturas recientes de la Wayback Machine. Deja los zips nuevos en la carpeta `archivo/`
con el nombre sepa_AAAA-MM-DD.zip; el flujo los sube al release.

Uso: python scripts/respaldo_sepa.py respaldados.txt archivo/
Sale con código 2 si no pudo conseguir nada nuevo y el día de ayer no está respaldado.
"""
import logging
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

from ipceer import descarga
from ipceer.config import cargar_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("respaldo")


def main() -> int:
    hechos = set(Path(sys.argv[1]).read_text().split()) if Path(sys.argv[1]).exists() else set()
    dest = Path(sys.argv[2])
    dest.mkdir(parents=True, exist_ok=True)
    cfg = cargar_config()
    d = cfg["descarga"]
    nuevos = []
    try:
        for rec in descarga.listar_recursos(cfg):
            nombre = f"sepa_{rec.fecha.isoformat()}.zip"
            if nombre in hechos:
                continue
            ruta = dest / nombre
            descarga._bajar(rec.url, ruta, d["timeout_segundos"] * 5, d["reintentos"])
            if zipfile.is_zipfile(ruta):
                nuevos.append(nombre)
            else:
                ruta.unlink()
        log.info("Portal: %d días nuevos", len(nuevos))
    except Exception as e:
        log.warning("Portal de SEPA no disponible desde GitHub: %s", e)
        try:
            limite = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
            for ts, orig, _ in sorted(descarga.listar_wayback(d["timeout_segundos"]), reverse=True):
                if ts[:8] < limite:
                    continue
                tmp = dest / f"wayback_{ts}.zip"
                try:
                    descarga._bajar(f"https://web.archive.org/web/{ts}id_/{orig}", tmp, d["timeout_segundos"] * 5, 1)
                except Exception as e2:
                    log.warning("Wayback %s: %s", ts, e2)
                    if "429" in str(e2):
                        break
                    continue
                f = descarga._fecha_dentro_del_zip(tmp) if zipfile.is_zipfile(tmp) else None
                nombre = f"sepa_{f.isoformat()}.zip" if f else None
                if nombre and nombre not in hechos and nombre not in nuevos:
                    tmp.rename(dest / nombre)
                    nuevos.append(nombre)
                else:
                    tmp.unlink(missing_ok=True)
            log.info("Wayback: %d días nuevos", len(nuevos))
        except Exception as e2:
            log.warning("Wayback no disponible: %s", e2)
    ayer = f"sepa_{(date.today() - timedelta(days=1)).isoformat()}.zip"
    hay = hechos | set(nuevos)
    print(f"nuevos={len(nuevos)}")
    return 0 if (nuevos or ayer in hay or f"sepa_{date.today().isoformat()}.zip" in hay) else 2


if __name__ == "__main__":
    sys.exit(main())
