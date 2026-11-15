"""¿Cambia la inflación de servicios en las corridas cambiarias? Y un modelo para estimarla. (D-50)

Datos: IPC del INDEC, GBA, bienes (B) y servicios (S), dic-2016 = 100; tipo de cambio mayorista
del BCRA (Com. A 3500, promedio del mes); dólar blue (Bluelytics, promedio del mes, venta).

Corridas: meses en que el oficial sube más de 5 puntos por encima de la inflación del mes anterior,
o el blue más de 10 puntos por encima (se listan y se contrastan con los episodios que marcó Tadeo).

Parte 1, patrón: con todo el período, servicios contra su rezago y contra los bienes del mismo mes,
en meses normales y en meses de corrida (y el mes siguiente). Si en las corridas los servicios se
pegan a los bienes, el coeficiente de bienes sube y el del rezago baja.

Parte 2, modelos (fuera de muestra, ventana móvil de 36 meses, se estima con lo anterior a cada mes):
  S0  servicios = nivel general del mes anterior (la regla de D-47)
  S1  servicios = a + b · servicios t−1
  S2  S1 + c · bienes del mismo mes (los medimos nosotros)
  S3  S2 + tipo de cambio oficial (variación del mes y del mes anterior)
  S4  S3 + brecha (variación de la brecha en el mes)
  S5  S0 en meses normales; en corrida, mezcla de S0 y bienes del mismo mes con el peso estimado
Se mide la raíz del error cuadrático medio contra servicios del INDEC, en todos los meses y en los de
corrida, con la prueba de Diebold-Mariano contra S0.

Uso: python scripts/servicios_corridas.py   Salida: resultados/servicios_corridas.json
"""
from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))
sys.path.insert(0, str(RAIZ / "scripts"))
import polars as pl  # noqa: E402
from faltantes_vs_nivel_general import diebold_mariano  # noqa: E402
from ipceer.config import cargar_config  # noqa: E402
from ipceer.indec import directorio  # noqa: E402
from ipceer.validacion import leer_indec  # noqa: E402

EPISODIOS_TADEO = ["2017", "2018-04/05", "2018-08/09", "2019-04", "2019-08", "2020-10 (blue)", "2022-07 (blue)", "2023-08", "2023-12"]


def prev(m: str, k: int = 1) -> str:
    a, b = int(m[:4]), int(m[5:7])
    t = a * 12 + b - 1 - k
    return f"{t // 12}-{t % 12 + 1:02d}"


def promedio_mensual(pares) -> dict:
    s = defaultdict(list)
    for f, v in pares:
        s[f[:7]].append(v)
    return {m: sum(v) / len(v) for m, v in s.items()}


def ols(X, y):
    X, y = np.asarray(X, float), np.asarray(y, float)
    b, *_ = np.linalg.lstsq(X, y, rcond=None)
    return b


def main() -> None:
    cfg = cargar_config(RAIZ)
    ind = leer_indec(directorio(cfg)).filter(pl.col("region") == "GBA")
    I = {c: dict(g.select("mes", "indice").iter_rows()) for (c,), g in ind.group_by(["codigo"]) if c in ("0", "B", "S")}
    var = {c: {m: (d[m] / d[prev(m)] - 1) * 100 for m in d if prev(m) in d} for c, d in I.items()}
    bc = cfg.ruta("panel_diario").parent.parent / "otras"
    of = promedio_mensual((r["fecha"], float(r["valor"])) for r in csv.DictReader(open(bc / "bcra" / "tc_mayorista.csv")))
    blue_raw = json.load(open(bc / "blue" / "evolution.json"))
    bl = promedio_mensual((r["date"], float(r["value_sell"])) for r in blue_raw if r["source"] == "Blue")
    dof = {m: (of[m] / of[prev(m)] - 1) * 100 for m in of if prev(m) in of}
    dbl = {m: (bl[m] / bl[prev(m)] - 1) * 100 for m in bl if prev(m) in bl}
    brecha = {m: (bl[m] / of[m] - 1) * 100 for m in of if m in bl}
    dbr = {m: brecha[m] - brecha[prev(m)] for m in brecha if prev(m) in brecha}
    meses = sorted(m for m in var["S"] if prev(m) in var["S"] and prev(m) in var["0"] and m in var["B"]
                   and m in dof and prev(m) in dof and m in dbr)
    # corrida: el dólar sube bastante más que la inflación del mes anterior (así el ajuste diario del
    # oficial de 2022-2023 no cuenta como corrida)
    corrida = {m: (dof[m] - var["0"][prev(m)] > 5 or dbl.get(m, 0) - var["0"][prev(m)] > 10) for m in meses}
    post = {m: corrida.get(prev(m), False) and not corrida[m] for m in meses}
    lista = [(m, round(dof[m], 1), round(dbl.get(m, float("nan")), 1), round(brecha[m], 0)) for m in meses if corrida[m]]

    # Parte 1: patrón en todo el período
    def ajuste(sel):
        ms = [m for m in meses if sel(m)]
        X = [[1, var["S"][prev(m)], var["B"][m]] for m in ms]
        y = [var["S"][m] for m in ms]
        b = ols(X, y)
        brecha_sb = sum(var["B"][m] - var["S"][m] for m in ms) / len(ms)
        return {"n": len(ms), "constante": round(b[0], 2), "rezago_servicios": round(b[1], 2), "bienes_mismo_mes": round(b[2], 2),
                "bienes_menos_servicios_pp": round(brecha_sb, 2)}
    patron = {"normales": ajuste(lambda m: not corrida[m] and not post[m]),
              "corrida": ajuste(lambda m: corrida[m]),
              "mes_siguiente": ajuste(lambda m: post[m]) if sum(post.values()) >= 4 else None}

    # Parte 2: modelos fuera de muestra
    V = 36

    def feats(m, k):
        f = [1, var["S"][prev(m)]]
        if k >= 2:
            f.append(var["B"][m])
        if k >= 3:
            f += [dof[m], dof[prev(m)]]
        if k >= 4:
            f.append(dbr[m])
        return f

    pred = defaultdict(dict)
    for i, m in enumerate(meses):
        hist = [q for q in meses[:i]][-V:]
        pred["S0 nivel general t−1"][m] = var["0"][prev(m)]
        if len(hist) < V:
            continue
        for k, nom in [(1, "S1 rezago"), (2, "S2 rezago + bienes"), (3, "S3 + tipo de cambio oficial"), (4, "S4 + brecha")]:
            b = ols([feats(q, k) for q in hist], [var["S"][q] for q in hist])
            pred[nom][m] = float(np.dot(b, feats(m, k)))
        # S5: en corrida, mezcla de S0 y bienes con el peso que mejor ajustó en las corridas anteriores
        hc = [q for q in meses[:i] if corrida[q]]
        if hc:
            num = sum((var["S"][q] - var["0"][prev(q)]) * (var["B"][q] - var["0"][prev(q)]) for q in hc)
            den = sum((var["B"][q] - var["0"][prev(q)]) ** 2 for q in hc)
            lam = max(0.0, min(1.0, num / den)) if den else 0.0
        else:
            lam = 0.0
        pred["S5 regla con corridas"][m] = (lam * var["B"][m] + (1 - lam) * var["0"][prev(m)]) if corrida[m] else var["0"][prev(m)]
    comunes = sorted(set.intersection(*[set(p) for p in pred.values()]))
    cor = [m for m in comunes if corrida[m]]
    nor = [m for m in comunes if not corrida[m]]

    def rm(nom, ms):
        e = [pred[nom][m] - var["S"][m] for m in ms]
        return round(math.sqrt(sum(x * x for x in e) / len(e)), 3) if e else None

    res = {}
    for nom in pred:
        e = [pred[nom][m] - var["S"][m] for m in comunes]
        e0 = [pred["S0 nivel general t−1"][m] - var["S"][m] for m in comunes]
        dm = diebold_mariano(e, e0) if nom != "S0 nivel general t−1" else (float("nan"), float("nan"))
        res[nom] = {"rmse_todos": rm(nom, comunes), "rmse_corridas": rm(nom, cor), "rmse_normales": rm(nom, nor),
                    "sesgo": round(sum(e) / len(e), 3), "p_vs_S0": None if math.isnan(dm[1]) else round(dm[1], 4)}
    out = {"meses": [comunes[0], comunes[-1], len(comunes)], "meses_corrida_fuera_de_muestra": cor,
           "corridas_detectadas": lista, "episodios_tadeo": EPISODIOS_TADEO, "patron": patron, "modelos": res}
    (cfg.ruta("resultados") / "servicios_corridas.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Corridas detectadas (mes, oficial %, blue %, brecha %):")
    for x in lista:
        print("  ", x)
    print("Patrón (servicios = c + a·servicios t−1 + b·bienes t):", json.dumps(patron, ensure_ascii=False, indent=1))
    print(f"Fuera de muestra {comunes[0]} a {comunes[-1]} ({len(comunes)} meses, {len(cor)} de corrida):")
    for nom, r in sorted(res.items(), key=lambda kv: kv[1]["rmse_todos"]):
        print(f"  {nom:30s} RMSE {r['rmse_todos']}  corridas {r['rmse_corridas']}  normales {r['rmse_normales']}  sesgo {r['sesgo']:+}  p {r['p_vs_S0']}")


if __name__ == "__main__":
    main()
