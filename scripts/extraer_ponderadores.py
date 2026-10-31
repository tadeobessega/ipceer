"""Extrae los ponderadores por apertura y región del IPC del INDEC.

Fuente: INDEC, Metodología n° 32 "Índice de precios al consumidor (IPC). Base diciembre
2016 = 100" (2019), Anexo II, Cuadro 7 "Estructura de ponderaciones regionales del IPC.
En porcentaje". Cada fila del cuadro tiene la forma:

    01.1.1 Pan y cereales 4,05 5,02 6,59 6,49 5,21 4,61

(código COICOP, nombre y el peso en la canasta de GBA, Pampeana, Noreste, Noroeste,
Cuyo y Patagonia).

Uso:
    pip install pdfplumber
    python scripts/extraer_ponderadores.py                 # baja el PDF del INDEC
    python scripts/extraer_ponderadores.py --pdf metodologia_ipc_nacional_2019.pdf

Escribe config/ponderadores_clase.csv y muestra un control: la suma de las aperturas de
cada división contra los pesos por división publicados (config/ponderadores_division.csv).
Revisar el CSV a mano antes de usarlo: la extracción de tablas de PDF puede fallar.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

URL = "https://www.indec.gob.ar/ftp/cuadros/economia/metodologia_ipc_nacional_2019.pdf"
REGIONES = ["GBA", "Pampeana", "Noreste", "Noroeste", "Cuyo", "Patagonia"]
RAIZ = Path(__file__).resolve().parents[1]

NUM = r"(\d{1,3},\d{1,3}|-)"
FILA = re.compile(rf"^(\d{{2}}(?:\.\d{{1,2}}){{0,3}})\s+(.+?)\s+{NUM}\s+{NUM}\s+{NUM}\s+{NUM}\s+{NUM}\s+{NUM}\s*$")
CODIGO = re.compile(r"^\d{2}(?:\.\d{1,2}){0,3}\s+\S")


def bajar(destino: Path) -> Path:
    import requests
    destino.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(URL, timeout=120)
    r.raise_for_status()
    destino.write_bytes(r.content)
    return destino


def lineas_cuadro7(pdf: Path) -> list[str]:
    import pdfplumber
    lineas, dentro = [], False
    with pdfplumber.open(pdf) as doc:
        for pagina in doc.pages:
            texto = pagina.extract_text() or ""
            if "Estructura de ponderaciones regionales" in texto:
                dentro = True
            elif dentro and ("Cuadro 8" in texto or "Ponderadores de las categorías" in texto):
                # la última página del cuadro 7 puede compartir hoja con el 8
                lineas += texto.split("Cuadro 8")[0].splitlines()
                break
            if dentro:
                lineas += texto.splitlines()
    return lineas


def parsear(lineas: list[str]) -> list[dict]:
    filas, pendiente = [], ""
    for ln in lineas:
        ln = " ".join(ln.split())
        if pendiente:
            ln = f"{pendiente} {ln}"
            pendiente = ""
        m = FILA.match(ln)
        if m:
            cod, nombre, *vals = m.groups()
            filas.append({"codigo": cod, "nombre": nombre.strip(),
                          **{r: (None if v == "-" else float(v.replace(",", "."))) for r, v in zip(REGIONES, vals)}})
        elif CODIGO.match(ln) and len(ln) < 160:
            pendiente = ln  # nombre partido en dos líneas
    return filas


def control(filas: list[dict]) -> None:
    div = {}
    with open(RAIZ / "config" / "ponderadores_division.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            div[r["codigo"]] = {k: float(r[k]) for k in REGIONES}
    print("\nControl: suma de las aperturas de mayor nivel de cada división vs. peso publicado de la división")
    for d, pub in div.items():
        hijos = [f for f in filas if f["codigo"].startswith(d + ".") and f["codigo"].count(".") == 1]
        if not hijos:
            print(f"  {d}: sin grupos extraídos")
            continue
        sumas = {r: sum(h[r] or 0 for h in hijos) for r in REGIONES}
        dif = max(abs(sumas[r] - pub[r]) for r in REGIONES)
        estado = "ok" if dif < 0.15 else "REVISAR (los grupos publicados pueden no cubrir toda la división)"
        print(f"  {d}: dif. máx {dif:.2f} pp -> {estado}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", type=Path, help="PDF de la Metodología 32 ya descargado")
    ap.add_argument("--salida", type=Path, default=RAIZ / "config" / "ponderadores_clase.csv")
    a = ap.parse_args()
    pdf = a.pdf or bajar(RAIZ / "data" / "metodologia_ipc_nacional_2019.pdf")
    filas = parsear(lineas_cuadro7(pdf))
    if not filas:
        sys.exit("No se encontraron filas del Cuadro 7. Revisar el PDF o cargar los valores a mano.")
    with open(a.salida, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["codigo", "nombre", *REGIONES])
        w.writeheader()
        w.writerows(filas)
    print(f"{len(filas)} aperturas escritas en {a.salida}")
    for fl in filas[:8]:
        print("  ", fl["codigo"], fl["nombre"], [fl[r] for r in REGIONES])
    control(filas)


if __name__ == "__main__":
    main()
