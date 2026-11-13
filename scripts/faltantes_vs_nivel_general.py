"""Rubros que el IPCEER no mide: ¿algún modelo predice mejor que imputarles la inflación general? (D-46)

Cuando un rubro no tiene precios propios, el índice lo deja afuera y reparte su peso entre los
rubros medidos: en la práctica le imputa la inflación de lo que sí se mide (bienes). La alternativa
es imputarle el nivel general, o una predicción propia del rubro. Para cada rubro sin precios
propios, con datos del INDEC de 2017 a 2026, se comparan fuera de muestra (cada mes con lo que se
sabía hasta el mes anterior):

  A. Bienes del mismo mes, tal cual (lo que hace hoy el índice al repartir el peso).
  B. Nivel general del mismo mes, tal cual (imputar el nivel general).
  C. Para cada indicador de scripts/correlaciones_rubros.py, una regresión y = a + b x reestimada
     cada mes con los 24 meses anteriores; se informa la mejor.

Para la mejor regresión contra A y contra B se hace la prueba de Diebold y Mariano (pérdida
cuadrática, varianza de largo plazo con un rezago de Newey y West y la corrección de Harvey,
Leybourne y Newbold para muestras chicas). Advertencia: elegir "la mejor" entre unos 20
indicadores y después probarla infla la significancia; por eso se exige p < 0,01 para recomendar.

Uso: python scripts/faltantes_vs_nivel_general.py   Salida: resultados/faltantes_vs_nivel_general.json
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))
sys.path.insert(0, str(RAIZ / "scripts"))
import polars as pl  # noqa: E402
from correlaciones_rubros import RUBROS, mensual_bcra, prev, var  # noqa: E402
from ipceer.config import cargar_config  # noqa: E402
from ipceer.indec import directorio  # noqa: E402
from ipceer.validacion import leer_indec  # noqa: E402

VENTANA = 24


def errores_regresion(x: dict, y: dict, meses: list[str]) -> dict[str, float]:
    out = {}
    for i, m in enumerate(meses):
        hist = [k for k in meses[:i] if k in x and k in y][-VENTANA:]
        if len(hist) < VENTANA or m not in x or m not in y:
            continue
        xs, ys = [x[k] for k in hist], [y[k] for k in hist]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((a - mx) ** 2 for a in xs)
        b = sum((a - mx) * (c - my) for a, c in zip(xs, ys)) / sxx if sxx else 0.0
        out[m] = y[m] - (my + b * (x[m] - mx))
    return out


def rmse(e) -> float:
    return math.sqrt(sum(v * v for v in e) / len(e))


def diebold_mariano(e1: list[float], e2: list[float], h: int = 1) -> tuple[float, float]:
    """DM con pérdida cuadrática; estadístico corregido (HLN) y p-valor bilateral (normal)."""
    d = [a * a - b * b for a, b in zip(e1, e2)]
    n = len(d)
    md = sum(d) / n
    g = [sum((d[t] - md) * (d[t - k] - md) for t in range(k, n)) / n for k in range(h + 1)]
    lrv = g[0] + 2 * sum((1 - k / (h + 1)) * g[k] for k in range(1, h + 1))
    if lrv <= 0:
        return float("nan"), float("nan")
    dm = md / math.sqrt(lrv / n)
    dm *= math.sqrt((n + 1 - 2 * h + h * (h - 1) / n) / n)
    p = math.erfc(abs(dm) / math.sqrt(2))
    return dm, p


def main() -> None:
    cfg = cargar_config(RAIZ)
    ind = leer_indec(directorio(cfg))
    out = {}
    for region in ("GBA", "Nacional"):
        g = ind.filter(pl.col("region") == region)
        v = {c: var(dict(x.select("mes", "indice").iter_rows())) for (c,), x in g.group_by(["codigo"])}
        if "0" not in v or "B" not in v:
            continue
        ng, bienes = v["0"], v["B"]
        pred = {f"Nivel general, {k} mes(es) antes": {m: ng[prev(m, k)] for m in ng if prev(m, k) in ng} for k in (1, 2, 3)}
        pred["Bienes del mismo mes"] = bienes
        pred["Bienes, 1 mes antes"] = {m: bienes[prev(m)] for m in bienes if prev(m) in bienes}
        if "01" in v:
            pred["Alimentos del mismo mes"] = v["01"]
        bc = cfg.ruta("panel_diario").parent.parent / "otras" / "bcra"
        tc = mensual_bcra(bc / "tc_mayorista.csv")
        for k in (0, 1, 2):
            pred[f"Dólar mayorista, {k} mes(es) antes" if k else "Dólar mayorista del mismo mes"] = {m: tc[prev(m, k)] for m in ng if prev(m, k) in tc}
        for n, f in (("ICL", "icl.csv"), ("CER", "cer.csv")):
            s = mensual_bcra(bc / f)
            if s:
                pred[f"{n} del mismo mes"] = s
        res = {}
        for c, nombre in RUBROS.items():
            if c not in v or c == "S":
                continue
            y = v[c]
            meses = sorted(y)
            p = dict(pred)
            p["El mismo rubro, 1 mes antes"] = {m: y[prev(m)] for m in y if prev(m) in y}
            p["El mismo rubro, 12 meses antes"] = {m: y[prev(m, 12)] for m in y if prev(m, 12) in y}
            regs = {k: errores_regresion(x, y, meses) for k, x in p.items()}
            regs = {k: e for k, e in regs.items() if len(e) >= 24}
            if not regs:
                continue
            mejor = min(regs, key=lambda k: rmse(list(regs[k].values())))
            em = regs[mejor]
            # mismos meses para todas las comparaciones
            com = [m for m in em if m in bienes and m in ng]
            eA = [y[m] - bienes[m] for m in com]
            eB = [y[m] - ng[m] for m in com]
            eC = [em[m] for m in com]
            dmA, pA = diebold_mariano(eC, eA)
            dmB, pB = diebold_mariano(eC, eB)
            dmAB, pAB = diebold_mariano(eB, eA)
            rec = ("modelo propio" if (pA < 0.01 and pB < 0.01 and rmse(eC) < min(rmse(eA), rmse(eB))) else
                   "nivel general" if (rmse(eB) < rmse(eA) and pAB < 0.05) else "lo que hace hoy (bienes)")
            res[c] = {"nombre": nombre, "n": len(com), "desde": com[0], "hasta": com[-1],
                      "rmse_bienes": round(rmse(eA), 3), "rmse_nivel_general": round(rmse(eB), 3),
                      "mejor_modelo": mejor, "rmse_mejor_modelo": round(rmse(eC), 3),
                      "dm_modelo_vs_bienes": {"estadistico": round(dmA, 2), "p": round(pA, 4)},
                      "dm_modelo_vs_nivel_general": {"estadistico": round(dmB, 2), "p": round(pB, 4)},
                      "dm_nivel_general_vs_bienes": {"estadistico": round(dmAB, 2), "p": round(pAB, 4)},
                      "recomendacion": rec}
        out[region] = res
    ruta = cfg.ruta("resultados") / "faltantes_vs_nivel_general.json"
    ruta.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for region, res in out.items():
        print("==", region)
        for c, r in res.items():
            print(f"{c:7s} {r['nombre'][:30]:30s} bienes {r['rmse_bienes']:.2f} | general {r['rmse_nivel_general']:.2f} | "
                  f"{r['mejor_modelo'][:30]:30s} {r['rmse_mejor_modelo']:.2f} p(vs bienes)={r['dm_modelo_vs_bienes']['p']} "
                  f"p(vs general)={r['dm_modelo_vs_nivel_general']['p']} -> {r['recomendacion']}")


if __name__ == "__main__":
    main()
