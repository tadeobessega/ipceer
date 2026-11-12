"""Qué indicador anticipa mejor cada rubro que el IPCEER todavía no mide (sugerencias, D-36).

Para cada rubro del INDEC sin precios propios (alquiler, tarifas, prepagas, transporte público,
telefonía, servicios recreativos, educación, restaurantes, etc.) compara su variación mensual con
indicadores que el IPCEER ya tiene o que se publican antes que el IPC:
  - el propio rubro y el nivel general del INDEC 1, 2 y 3 meses antes (inercia e indexación);
  - lo que medimos en el mes (alimentos y bienes del INDEC como aproximación de lo que mide SEPA);
  - dólar mayorista (BCRA), promedio del mes, contemporáneo y 1 a 3 meses antes;
  - ICL y CER (BCRA), que son índices de actualización de contratos;
  - el mes del año (estacionalidad: educación en marzo, turismo en enero y julio).
Para cada indicador: correlación en 2017-2026 y en julio 2024 a septiembre 2026 (régimen actual), y
error de pronóstico fuera de muestra con una regresión que se reestima cada mes con los 24 meses
anteriores (sin mirar el futuro). Es diagnóstico: nada de esto entra al índice.

Uso: python scripts/correlaciones_rubros.py  (en la raíz del repo de la PC)
Salida: resultados/correlaciones_rubros.json
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import polars as pl

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))
from ipceer.config import cargar_config  # noqa: E402
from ipceer.indec import directorio  # noqa: E402
from ipceer.validacion import leer_indec  # noqa: E402

RUBROS = {"04.1.1": "Alquiler de la vivienda", "04.5": "Electricidad, gas y otros combustibles",
          "06.4.1": "Gastos de prepagas", "07.3": "Transporte público", "08.3": "Telefonía e internet",
          "09.4": "Servicios recreativos y culturales", "10": "Educación", "11.1": "Restaurantes y comidas fuera del hogar",
          "07.1": "Adquisición de vehículos", "03": "Prendas de vestir y calzado", "06.1": "Productos medicinales",
          "12": "Bienes y servicios varios", "S": "Servicios (total)"}
REGIMEN = ("2024-07", "2026-09")


def prev(m: str, k: int = 1) -> str:
    a, mm = int(m[:4]), int(m[5:])
    t = a * 12 + mm - 1 - k
    return f"{t // 12}-{t % 12 + 1:02d}"


def var(d: dict[str, float]) -> dict[str, float]:
    return {m: (d[m] / d[prev(m)] - 1) * 100 for m in d if prev(m) in d}


def mensual_bcra(ruta: Path) -> dict[str, float]:
    if not ruta.exists():
        return {}
    x = pl.read_csv(ruta).with_columns(pl.col("fecha").str.slice(0, 7).alias("mes")).group_by("mes").agg(pl.col("valor").mean())
    return var(dict(x.iter_rows()))


def corr(xs, ys):
    n = len(xs)
    if n < 8:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sxx = sum((a - mx) ** 2 for a in xs)
    syy = sum((b - my) ** 2 for b in ys)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else None


def oos(x: dict, y: dict, meses: list[str], ventana: int = 24):
    """Error fuera de muestra: y_t ≈ a + b x_t, con a y b de los `ventana` meses anteriores."""
    e, e0 = [], []
    for i, m in enumerate(meses):
        hist = [k for k in meses[:i] if k in x and k in y][-ventana:]
        if len(hist) < ventana or m not in x or m not in y:
            continue
        xs, ys = [x[k] for k in hist], [y[k] for k in hist]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((a - mx) ** 2 for a in xs)
        b = sum((a - mx) * (c - my) for a, c in zip(xs, ys)) / sxx if sxx else 0.0
        e.append(y[m] - (my + b * (x[m] - mx)))
        e0.append(y[m] - my)   # referencia: promedio de los 24 meses anteriores
    if len(e) < 6:
        return None
    rm = math.sqrt(sum(v * v for v in e) / len(e))
    r0 = math.sqrt(sum(v * v for v in e0) / len(e0))
    return {"n": len(e), "rmse": round(rm, 3), "rmse_promedio": round(r0, 3), "mejora_pct": round((1 - rm / r0) * 100, 1)}


def main() -> None:
    cfg = cargar_config(RAIZ)
    ind = leer_indec(directorio(cfg))
    out = {}
    for region in ("GBA", "Nacional"):
        g = ind.filter(pl.col("region") == region)
        serie = {c: dict(x.select("mes", "indice").iter_rows()) for (c,), x in g.group_by(["codigo"])}
        v = {c: var(s) for c, s in serie.items()}
        if "0" not in v:
            continue
        ng = v["0"]
        pred: dict[str, dict[str, float]] = {}
        for k in (1, 2, 3):
            pred[f"Nivel general, {k} mes(es) antes"] = {m: ng[prev(m, k)] for m in ng if prev(m, k) in ng}
        for c, nombre in (("01", "Alimentos"), ("B", "Bienes")):
            if c in v:
                pred[f"{nombre} del mismo mes"] = v[c]
                pred[f"{nombre}, 1 mes antes"] = {m: v[c][prev(m)] for m in v[c] if prev(m) in v[c]}
        bc = cfg.ruta("panel_diario").parent.parent / "otras" / "bcra"
        tc = mensual_bcra(bc / "tc_mayorista.csv")
        for k in (0, 1, 2, 3):
            pred[f"Dólar mayorista, {k} mes(es) antes" if k else "Dólar mayorista del mismo mes"] = \
                {m: tc[prev(m, k)] for m in ng if prev(m, k) in tc}
        for n, f in (("ICL", "icl.csv"), ("CER", "cer.csv")):
            s = mensual_bcra(bc / f)
            if s:
                pred[f"{n} del mismo mes"] = s
        res = {}
        for c, nombre in RUBROS.items():
            if c not in v:
                continue
            y = v[c]
            p = dict(pred)
            p["El mismo rubro, 1 mes antes"] = {m: y[prev(m)] for m in y if prev(m) in y}
            p["El mismo rubro, 12 meses antes (estacionalidad)"] = {m: y[prev(m, 12)] for m in y if prev(m, 12) in y}
            meses = sorted(y)
            filas = []
            for pn, x in p.items():
                com = [m for m in meses if m in x]
                reg = [m for m in com if REGIMEN[0] <= m <= REGIMEN[1]]
                r_all = corr([x[m] for m in com], [y[m] for m in com])
                r_reg = corr([x[m] for m in reg], [y[m] for m in reg])
                filas.append({"indicador": pn, "r_2017_2026": round(r_all, 3) if r_all is not None else None,
                              "r_regimen": round(r_reg, 3) if r_reg is not None else None,
                              "fuera_de_muestra": oos(x, y, meses)})
            filas.sort(key=lambda f: -(f["fuera_de_muestra"] or {}).get("mejora_pct", -999))
            res[c] = {"nombre": nombre, "meses": len(meses), "indicadores": filas}
        out[region] = res
    ruta = cfg.ruta("resultados") / "correlaciones_rubros.json"
    ruta.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for region, res in out.items():
        print("==", region)
        for c, r in res.items():
            print(f"{c:7s} {r['nombre'][:34]:34s}", " | ".join(
                f"{f['indicador'][:34]} r={f['r_regimen']} mejora={(f['fuera_de_muestra'] or {}).get('mejora_pct')}"
                for f in r["indicadores"][:3]))


if __name__ == "__main__":
    main()
