"""Factura de luz de Edenor y Edesur con los cuadros mensuales del ENRE, con distintas cantidades,
contra el IPC del INDEC (GBA, 04.5 electricidad, gas y otros combustibles). (D-48)

Cantidades que se prueban (cada una es un supuesto con su fuente o marcado como supuesto):
  E1  un hogar de 265 kWh/mes sin subsidio (consumo del Observatorio de Tarifas del IIEP).
  E2  265 kWh, con el 35% de los usuarios en el nivel con subsidio y el resto sin subsidio
      (ENRE, Res. 119 y 120/2025: el Nivel 2 son el 34% de Edenor y el 35% de Edesur).
  E3  tramos: 100, 250, 450, 550, 650 y 900 kWh con 30, 30, 15, 10, 7 y 8% de los usuarios (R1 y R2
      juntos son el 60% según el sector, EconoJournal mar-2024; el reparto entre bloques es supuesto).
  E4  E3 con el 35% de los usuarios con subsidio.
Edenor y Edesur pesan por usuarios (2.98 y 2.54 millones en 2019, auditoría de la RTI del ENRE).

Como 04.5 del INDEC junta luz y gas, se compara con una regresión 04.5 = a + b·luz y se mira el
ajuste (R² y error); gana la cantidad cuya factura explica mejor el índice oficial.

Uso: python scripts/electricidad_vs_indec.py     Salida: resultados/electricidad_vs_indec.json
"""
from __future__ import annotations

import csv
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))
import polars as pl  # noqa: E402
from ipceer.config import cargar_config  # noqa: E402
from ipceer.indec import directorio  # noqa: E402
from ipceer.validacion import leer_indec  # noqa: E402

W_EMP = {"edenor": 2.98, "edesur": 2.54}


def prev(m: str) -> str:
    a, b = int(m[:4]), int(m[5:7])
    return f"{a - 1}-12" if b == 1 else f"{a}-{b - 1:02d}"


def rango(txt: str):
    t = txt.replace(" ", "")
    m = re.match(r"^(\d+)-(\d+)", t)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.match(r"^\+(\d+)", t) or re.match(r"^(?:mayora|más de)(\d+)", t, re.I)
    if m:
        return float(m.group(1)), 1e9
    return None


def es_subsidio(tabla: str) -> bool:
    return bool(re.search(r"con subsidio|Nivel 2|Nivel 3", tabla, re.I))


def es_pleno(tabla: str) -> bool:
    return bool(re.search(r"sin subsidio|Nivel 1|Usuarios Residenciales", tabla, re.I)) and not es_subsidio(tabla)


def base_subsidio(tabla: str, per: str) -> float:
    if re.search(r"Nivel 2", tabla, re.I):
        return 350.0
    if re.search(r"Nivel 3", tabla, re.I):
        return 250.0
    mes = int(per[5:7])   # 2026: 300 kWh en dic-feb y may-ago, 150 en el resto
    return 300.0 if mes in (12, 1, 2, 5, 6, 7, 8) else 150.0


def factura(filas: list[dict], q: float, emp: str, per: str, subsidio: bool) -> float | None:
    """Importe sin impuestos de un consumo q en la tabla que corresponda. None si no se puede."""
    tablas = defaultdict(list)
    for f in filas:
        tablas[f["tabla"]].append(f)
    cand = [t for t in tablas if (es_subsidio(t) if subsidio else es_pleno(t))]
    if subsidio:   # preferir la del nivel de menores ingresos o la "con subsidio"
        cand.sort(key=lambda t: (0 if re.search(r"con subsidio|Nivel 2", t, re.I) else 1))
    for t in cand:
        fs = tablas[t]
        cats = defaultdict(list)
        for f in fs:
            cats[f["categoria"]].append(f)
        for c, cf in cats.items():
            fijos = [f for f in cf if f["cargo"] == "fijo"]
            if not fijos:
                continue
            r = rango(fijos[0]["rango"])
            if not r or not (r[0] - 1 <= q <= r[1]):
                continue
            fijo = float(fijos[0][emp])
            vs = [f for f in cf if f["cargo"] == "variable"]
            if not vs:
                return None
            if len(vs) == 1:
                return fijo + float(vs[0][emp]) * q
            # dos tramos: "hasta X"/"consumo base" y "excedente"
            base_f = next((f for f in vs if re.search(r"hasta|base\b|consumo base|Primeros", f["rango"], re.I) and not re.search(r"excedente", f["rango"], re.I)), None)
            exc_f = next((f for f in vs if re.search(r"excedente", f["rango"], re.I)), None)
            if base_f is None or exc_f is None:
                # bloques por rango ("hasta 400" y "401-500"): el primero hasta su tope, el resto al segundo
                m = re.search(r"hasta\s*(\d+)", " ".join(f["rango"] for f in vs), re.I)
                if not m:
                    return fijo + float(vs[-1][emp]) * q
                X = float(m.group(1))
                v1 = next(f for f in vs if re.search(r"hasta", f["rango"], re.I))
                v2 = next(f for f in vs if f is not v1)
                return fijo + float(v1[emp]) * min(q, X) + float(v2[emp]) * max(0.0, q - X)
            m = re.search(r"(\d+)", base_f["rango"])
            X = float(m.group(1)) if m and not re.search(r"consumo base", base_f["rango"], re.I) else base_subsidio(t, per)
            return fijo + float(base_f[emp]) * min(q, X) + float(exc_f[emp]) * max(0.0, q - X)
    return None


def main() -> None:
    cfg = cargar_config(RAIZ)
    filas = list(csv.DictReader(open(cfg.ruta("resultados") / "enre_cuadros.csv", encoding="utf-8")))
    por_doc = defaultdict(list)
    for f in filas:
        por_doc[(f["periodo"], f["id"])].append(f)
    tramos = [(100, .30), (250, .30), (450, .15), (550, .10), (650, .07), (900, .08)]
    variantes = {
        "E1 265 kWh sin subsidio (IIEP)": ([(265, 1.0)], 0.0),
        "E2 265 kWh, 35% con subsidio": ([(265, 1.0)], 0.35),
        "E3 tramos sin subsidio": (tramos, 0.0),
        "E4 tramos, 35% con subsidio": (tramos, 0.35),
    }
    serie = {}
    for k, (qs, ps) in variantes.items():
        s = defaultdict(list)
        for (per, _id), fs in por_doc.items():
            hay_sub = any(es_subsidio(f["tabla"]) for f in fs)
            tot, ok = 0.0, True
            for emp, we in W_EMP.items():
                for q, wq in qs:
                    p = factura(fs, q, emp, per, False)
                    if p is None:
                        ok = False
                        break
                    if ps and hay_sub:
                        p2 = factura(fs, q, emp, per, True)
                        p = p if p2 is None else (1 - ps) * p + ps * p2
                    tot += we * wq * p
                if not ok:
                    break
            if ok:
                s[per].append(tot)
        serie[k] = {per: math.exp(sum(map(math.log, v)) / len(v)) for per, v in s.items()}
    ind = leer_indec(directorio(cfg)).filter((pl.col("region") == "GBA") & (pl.col("codigo") == "04.5"))
    I = dict(ind.select("mes", "indice").iter_rows())
    vI = {m: (I[m] / I[prev(m)] - 1) * 100 for m in I if prev(m) in I}
    out = {}
    for k, s in serie.items():
        v = {m: (s[m] / s[prev(m)] - 1) * 100 for m in s if prev(m) in s}   # solo meses consecutivos
        com = sorted(set(v) & set(vI))
        x, y = [v[m] for m in com], [vI[m] for m in com]
        n = len(com)
        mx, my = sum(x) / n, sum(y) / n
        sxx = sum((a - mx) ** 2 for a in x); syy = sum((b - my) ** 2 for b in y)
        sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
        b = sxy / sxx if sxx else 0
        a = my - b * mx
        res = [yy - (a + b * xx) for xx, yy in zip(x, y)]
        r2 = 1 - sum(e * e for e in res) / syy if syy else 0
        # cuántos meses el INDEC movió más de 1% y nosotros también (sincronía de los aumentos)
        sinc = sum(1 for xx, yy in zip(x, y) if abs(yy) > 1 and abs(xx) > 1) / max(1, sum(1 for yy in y if abs(yy) > 1))
        out[k] = {"meses": n, "desde": com[0] if com else None, "hasta": com[-1] if com else None,
                  "correlacion": round(sxy / math.sqrt(sxx * syy), 3) if sxx and syy else None, "r2": round(r2, 3),
                  "pendiente": round(b, 3), "error_residual": round(math.sqrt(sum(e * e for e in res) / n), 3),
                  "sincronia_aumentos": round(sinc, 3),
                  "acum_luz": round((math.prod(1 + xx / 100 for xx in x) - 1) * 100, 1),
                  "acum_indec_045": round((math.prod(1 + yy / 100 for yy in y) - 1) * 100, 1)}
        print(f"{k:34s} n={n} r={out[k]['correlacion']} R2={out[k]['r2']} b={out[k]['pendiente']} err={out[k]['error_residual']} sinc={out[k]['sincronia_aumentos']} acum luz {out[k]['acum_luz']}% vs 04.5 {out[k]['acum_indec_045']}%")
    (cfg.ruta("resultados") / "electricidad_vs_indec.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
