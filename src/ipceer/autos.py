"""Precios de autos: Guía Oficial de Precios de la Cámara del Comercio Automotor (D-41).

La CCA publica cada mes en https://www.cca.org.ar/descargas/precios/Autos.pdf (robots.txt permite
todo) la guía de autos, pick-ups, todoterreno y utilitarios: una fila por versión y una columna por
año modelo (0 km y los usados de los últimos 14 años). El PDF se archiva una vez por mes (el mes
sale del encabezado, "Noviembre 2026") y se pasa a una tabla larga con la posición de cada número
en la página: cada valor va a la columna cuyo borde derecho tiene más cerca (los números están
alineados a la derecha bajo el año).

Estructura de la guía: la marca y las versiones están en una fuente y el modelo en otra (negrita),
así que una línea sin números en la fuente de las versiones es una marca y una en negrita es un
modelo (una línea sin números que tiene dígitos es una versión sin precios y se saltea). Unidades:
los usados en miles de pesos; el 0 km en dólares (la guía lo aclara al pie, "AGRALE 0KM EN US$", y
esos valores vienen con coma de miles), guardado en `moneda`. En la guía de noviembre de 2026: 18.038
valores, 70 marcas, 783 precios de 0 km.

La tabla queda guardada para medir 07.1.1 (compra de vehículos); todavía no entra al índice: antes
hay que comparar contra el INDEC (regla de la metodología).
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

import requests

from .config import Config
from .normas import MESES

log = logging.getLogger(__name__)
UA = {"User-Agent": "IPCEER/1.0 (+https://github.com/tadeobessega/ipceer)"}
URL = "https://www.cca.org.ar/descargas/precios/Autos.pdf"
CAMPOS = ["mes", "marca", "modelo", "version", "anio", "valor", "moneda", "pagina"]


def _dir(cfg: Config) -> Path:
    from .otras_fuentes import _dir as dir_otras
    return dir_otras(cfg, "autos")


def _num(s: str) -> float | None:
    s = s.replace(".", "").replace(",", "")
    return float(s) if s.isdigit() else None


def leer_guia(ruta: Path) -> tuple[str | None, list[dict]]:
    import pdfplumber
    filas, mes = [], None
    marca = modelo = fuente_version = None
    with pdfplumber.open(ruta) as pdf:
        for npag, p in enumerate(pdf.pages, 1):
            ws = p.extract_words(extra_attrs=["fontname"])
            if mes is None:
                txt = " ".join(w["text"] for w in ws[:30])
                m = re.search(r"(" + "|".join(MESES) + r")\s+(\d{4})", txt, re.I)
                if m:
                    mes = f"{m.group(2)}-{MESES[m.group(1).lower()]:02d}"
            km = [w for w in ws if w["text"] == "Km" and w["top"] < 200]
            if not km:
                continue
            top_cab = km[0]["top"]
            cab = [w for w in ws if abs(w["top"] - top_cab) < 3 and re.fullmatch(r"\d{4}|Km", w["text"])]
            cols = sorted([(w["x1"], "0km" if w["text"] == "Km" else w["text"]) for w in cab])
            pie = " ".join(w["text"] for w in ws)
            usd = {m.group(1).strip() for m in re.finditer(r"([A-Z][A-Z\-\. ]{1,30}?) 0KM EN US\$", pie)}
            lineas: dict[int, list] = {}
            for w in ws:
                if w["top"] <= top_cab + 3:
                    continue
                lineas.setdefault(round(w["top"]), []).append(w)
            from collections import Counter
            primeras = Counter(sorted(ls, key=lambda w: w["x0"])[0]["fontname"] for ls in lineas.values()
                               if any(w["x1"] >= cols[0][0] - 25 and _num(w["text"]) is not None for w in ls))
            if primeras:
                fuente_version = primeras.most_common(1)[0][0]
            for top in sorted(lineas):
                l = sorted(lineas[top], key=lambda w: w["x0"])
                if "0KM" in [w["text"] for w in l]:
                    continue
                texto = [w for w in l if w["x1"] < cols[0][0] - 25]
                nums = [w for w in l if w["x1"] >= cols[0][0] - 25 and _num(w["text"]) is not None]
                nombre = " ".join(w["text"] for w in texto).strip()
                if not nombre:
                    continue
                if not nums:
                    # misma fuente que las versiones: marca; otra (negrita): modelo
                    if fuente_version is None or texto[0]["fontname"] == fuente_version:
                        if not re.search(r"\d", nombre):   # con números es una versión sin precios
                            marca, modelo = nombre, None
                    else:
                        modelo = nombre
                    continue
                for w in nums:
                    x, anio = min(cols, key=lambda c: abs(c[0] - w["x1"]))
                    filas.append({"mes": mes, "marca": marca, "modelo": modelo, "version": nombre, "anio": anio,
                                  "valor": _num(w["text"]),
                                  "moneda": "US$" if anio == "0km" and ("," in w["text"] or any((marca or "").startswith(u.split()[0]) for u in usd)) else "miles de $",
                                  "pagina": npag})
    return mes, filas


def actualizar(cfg: Config) -> dict:
    d = _dir(cfg)
    tmp = d / "Autos_ultimo.pdf"
    r = requests.get(URL, headers=UA, timeout=120)
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        return {"error": f"CCA respondió {r.status_code}"}
    tmp.write_bytes(r.content)
    mes, filas = leer_guia(tmp)
    if not mes:
        return {"error": "no se encontró el mes en la guía"}
    pdf = d / f"cca_autos_{mes}.pdf"
    if not pdf.exists():
        tmp.replace(pdf)
    else:
        tmp.unlink()
    with open(d / f"cca_autos_{mes}.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        w.writeheader()
        w.writerows(filas)
    return {"mes": mes, "valores": len(filas), "versiones": len({(x["marca"], x["modelo"], x["version"]) for x in filas}),
            "marcas": len({x["marca"] for x in filas})}
