"""Pasa un dato de consultora cargado desde el tablero (issue de GitHub) a config/externos_manual.csv (D-69).

Lee el cuerpo del issue (variable ISSUE_BODY), con un bloque de líneas "campo: valor". Valida todo y,
si algo no cierra, termina con error y no escribe nada. Un dato nuevo reemplaza al del mismo período,
medida, rubro y región; "borrar" deja una fila marcada que tapa también a la carga automática.
"""
from __future__ import annotations

import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

RUTA = Path("config/externos_manual.csv")
COLS = ["fuente", "tipo", "periodo", "medida", "rubro", "region", "valor", "publicado", "url", "nota", "borrado"]
MEDIDAS = {"semanal", "mensual", "proyeccion_mensual", "promedio_4_semanas"}
RUBROS = {"total", "alimentos"}
REGIONES = {"", "Nacional", "GBA", "CABA"}


def leer_campos(cuerpo: str) -> dict:
    m = re.search(r"```\s*\n(.*?)```", cuerpo, re.S)
    texto = m.group(1) if m else cuerpo
    out = {}
    for linea in texto.splitlines():
        if ":" in linea:
            k, v = linea.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def validar(c: dict) -> dict:
    err = []
    if c.get("accion") not in ("guardar", "borrar"):
        err.append("accion tiene que ser guardar o borrar")
    if not c.get("fuente") or len(c["fuente"]) > 60:
        err.append("falta la consultora")
    med = c.get("medida", "")
    if med not in MEDIDAS:
        err.append(f"medida desconocida: {med}")
    per = c.get("periodo", "")
    if med in ("semanal", "promedio_4_semanas") and not re.fullmatch(r"20\d\d-W\d\d", per):
        err.append(f"período semanal inválido: {per}")
    if med in ("mensual", "proyeccion_mensual") and not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", per):
        err.append(f"período mensual inválido: {per}")
    if c.get("rubro") not in RUBROS:
        err.append(f"rubro desconocido: {c.get('rubro')}")
    if c.get("region", "") not in REGIONES:
        err.append(f"región desconocida: {c.get('region')}")
    if c.get("accion") == "guardar":
        try:
            v = float(c.get("valor", "").replace(",", "."))
            if not -20 < v < 50:
                err.append("valor fuera de rango")
            c["valor"] = str(v)
        except ValueError:
            err.append("valor no es un número")
        if c.get("url") and not re.match(r"https?://", c["url"]):
            err.append("el link tiene que empezar con http")
    if err:
        raise ValueError("; ".join(err))
    return c


def aplicar(c: dict, filas: list[dict]) -> list[dict]:
    clave = lambda f: (f["fuente"], f["periodo"], f["medida"], f["rubro"], f.get("region") or "")
    k = (c["fuente"], c["periodo"], c["medida"], c["rubro"], c.get("region", ""))
    filas = [f for f in filas if clave(f) != k]
    hoy = date.today().isoformat()
    if c["accion"] == "borrar":
        filas.append({"fuente": c["fuente"], "tipo": "consultora", "periodo": c["periodo"], "medida": c["medida"],
                      "rubro": c["rubro"], "region": c.get("region", ""), "valor": "", "publicado": hoy, "url": "",
                      "nota": "borrado desde el tablero", "borrado": "1"})
    else:
        filas.append({"fuente": c["fuente"], "tipo": "consultora", "periodo": c["periodo"], "medida": c["medida"],
                      "rubro": c["rubro"], "region": c.get("region", ""), "valor": c["valor"], "publicado": hoy,
                      "url": c.get("url", ""), "nota": c.get("nota", "") or "cargado desde el tablero", "borrado": ""})
        if c.get("reemplaza"):  # cambió la medida o la región: se tapa el dato anterior
            med0, reg0 = (c["reemplaza"].split("|") + [""])[:2]
            k0 = (c["fuente"], c["periodo"], med0, c["rubro"], reg0)
            if med0 in MEDIDAS and k0 != k:
                filas = [f for f in filas if clave(f) != k0]
                filas.append({"fuente": c["fuente"], "tipo": "consultora", "periodo": c["periodo"], "medida": med0,
                              "rubro": c["rubro"], "region": reg0, "valor": "", "publicado": hoy, "url": "",
                              "nota": "reemplazado desde el tablero", "borrado": "1"})
    return sorted(filas, key=lambda f: (f["fuente"], f["periodo"], f["medida"], f["rubro"]))


def main() -> int:
    c = validar(leer_campos(os.environ.get("ISSUE_BODY", "")))
    filas = list(csv.DictReader(RUTA.open(encoding="utf-8"))) if RUTA.exists() else []
    filas = aplicar(c, filas)
    with RUTA.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows({k: f.get(k, "") for k in COLS} for f in filas)
    print(f"{c['accion']}: {c['fuente']} {c['periodo']} {c['medida']} {c['rubro']} {c.get('region','')} {c.get('valor','')}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as e:
        print(f"Dato rechazado: {e}")
        sys.exit(2)
