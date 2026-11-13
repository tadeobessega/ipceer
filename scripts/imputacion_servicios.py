"""¿Cómo completar lo que el IPCEER no mide? Prueba con el IPC del INDEC (GBA, aperturas). (D-47)

Simulación: se supone que lo que medimos (alimentos y bebidas, tabaco, limpieza del hogar, tarifas
de luz y gas, prepagas, combustibles, transporte público y cuidado personal) se mide perfecto, igual
que el INDEC, y se prueba solo la forma de completar lo que no medimos. Así el error que queda contra
el nivel general es el de la imputación y nada más.

Cuentas: con índices base diciembre de 2016 = 100 y los ponderadores de GBA, la incidencia de cada
apertura es w·(I_t − I_t−1)/NG_t−1 y suman exacto el nivel general. Las partes que el INDEC no publica
(ej. expensas = 04.1 menos 04.1.1) salen por diferencia de incidencias. Para lo no medido, cada
variante pone una variación "imputada" con lo que se sabía al cerrar el mes (el dato del INDEC del
mes anterior ya está publicado; los bienes del mismo mes los medimos nosotros).

Variantes:
  V0  hoy: el peso de lo no medido se reparte entre lo medido (todo a la inflación de lo medido).
  V1  bienes no medidos con los bienes medidos del mismo mes; servicios con el nivel general t−1.
      (la propuesta de D-46)
  V1s igual que V1 pero servicios con la inflación de servicios del INDEC t−1.
  V1p igual que V1 pero cada servicio con su propio dato t−1.
  Y para el alquiler (04.1.1), sobre V1: A1 su propio dato t−1, A2 promedio de sus últimos 3 meses,
  A3 de 6 meses, A4 ICL del mismo mes, A5 ICL t−1, A6 nivel general promedio de 6 meses,
  A7 regresión móvil (24 meses) contra su propio rezago, A8 promedio de su propio dato t−1 y t−2.

Uso: python scripts/imputacion_servicios.py   Salida: resultados/imputacion_servicios.json
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
from correlaciones_rubros import mensual_bcra, prev  # noqa: E402
from faltantes_vs_nivel_general import diebold_mariano, rmse  # noqa: E402
from ipceer.config import cargar_config  # noqa: E402
from ipceer.indec import directorio  # noqa: E402
from ipceer.validacion import leer_indec  # noqa: E402

# hojas: (código de la hoja, índice que se usa o None si sale por diferencia, padre, hijos a restar)
MEDIDO = ["01", "02", "04.5", "05.6", "06.4.1", "07.2.2", "07.3", "12.1"]
BIENES_NO = ["03", "05-r", "06.1", "07.1", "07.2-r", "09.1", "09.5"]
SERV_NO = ["04.1.1", "04.1-r", "04.3", "04-r", "06-r", "08", "09.4", "09-r", "10", "11", "12-r"]
RESTOS = {"05-r": ("05", ["05.6"]), "07.2-r": ("07.2", ["07.2.2"]), "04.1-r": ("04.1", ["04.1.1"]),
          "04-r": ("04", ["04.1", "04.3", "04.5"]), "06-r": ("06", ["06.1", "06.4.1"]),
          "09-r": ("09", ["09.1", "09.4", "09.5"]), "12-r": ("12", ["12.1"])}
PESO_DE = {"06.4.1": "06.4"}


def main() -> None:
    cfg = cargar_config(RAIZ)
    ind = leer_indec(directorio(cfg)).filter(pl.col("region") == "GBA")
    I = {c: dict(g.select("mes", "indice").iter_rows()) for (c,), g in ind.group_by(["codigo"])}
    pond = {r["codigo"]: float(r["GBA"]) for r in pl.read_csv(cfg.dir_config / "ponderadores_clase.csv", infer_schema_length=0).to_dicts() if r["GBA"]}
    base = "2016-12"
    ng = I["0"]
    meses = sorted(m for m in ng if m > "2017-01" and prev(m) in ng)
    total_w = sum(pond[c] for c in ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"])

    def w_ef(c, m):  # ponderador efectivo en m (participación en el gasto a precios de m)
        return pond[PESO_DE.get(c, c)] / total_w * (I[c][m] / I[c][base]) / (ng[m] / ng[base])

    def inc(c, m):  # incidencia en pp
        return w_ef(c, prev(m)) * (I[c][m] / I[c][prev(m)] - 1) * 100

    # incidencia, peso efectivo t-1 y variación de cada hoja
    H = {}
    for m in meses:
        h = {}
        for c in MEDIDO + BIENES_NO + SERV_NO:
            if c in RESTOS:
                p, hijos = RESTOS[c]
                i_ = inc(p, m) - sum(inc(x, m) for x in hijos)
                w_ = w_ef(p, prev(m)) - sum(w_ef(x, prev(m)) for x in hijos)
            else:
                i_, w_ = inc(c, m), w_ef(c, prev(m))
            h[c] = (i_, w_, i_ / w_ if w_ > 1e-9 else 0.0)
        H[m] = h
    vng = {m: (ng[m] / ng[prev(m)] - 1) * 100 for m in ng if prev(m) in ng}
    chk = max(abs(sum(x[0] for x in H[m].values()) - vng[m]) for m in meses)
    print(f"control: suma de incidencias contra el nivel general, desvío máximo {chk:.4f} pp; suma de pesos {sum(x[1] for x in H[meses[-1]].values()):.4f}")
    vS = {m: (I["S"][m] / I["S"][prev(m)] - 1) * 100 for m in I["S"] if prev(m) in I["S"]}
    alq = {m: H[m]["04.1.1"][2] for m in H}
    bc = cfg.ruta("panel_diario").parent.parent / "otras" / "bcra"
    icl = mensual_bcra(bc / "icl.csv")

    def prom(d, m, k):
        v = [d.get(prev(m, j)) for j in range(1, k + 1)]
        return sum(v) / k if all(x is not None for x in v) else None

    def ar_alq(m):
        hist = [k for k in meses if k < m and prev(k) in alq][-24:]
        if len(hist) < 24 or prev(m) not in alq:
            return None
        xs, ys = [alq[prev(k)] for k in hist], [alq[k] for k in hist]
        mx, my = sum(xs) / 24, sum(ys) / 24
        sxx = sum((a - mx) ** 2 for a in xs)
        b = sum((a - mx) * (c - my) for a, c in zip(xs, ys)) / sxx if sxx else 0
        return my + b * (alq[prev(m)] - mx)

    reglas_alq = {
        "A1 alquiler: su dato t−1": lambda m: alq.get(prev(m)),
        "A2 alquiler: promedio de sus 3 meses previos": lambda m: prom(alq, m, 3),
        "A3 alquiler: promedio de sus 6 meses previos": lambda m: prom(alq, m, 6),
        "A4 alquiler: ICL del mismo mes": lambda m: icl.get(m),
        "A5 alquiler: ICL t−1": lambda m: icl.get(prev(m)),
        "A6 alquiler: nivel general, promedio de 6 meses": lambda m: prom(vng, m, 6),
        "A7 alquiler: regresión contra su rezago (24 meses)": ar_alq,
        "A8 alquiler: promedio de su dato t−1 y t−2": lambda m: prom(alq, m, 2),
    }

    def estimar(m, serv, alq_regla=None):
        h = H[m]
        med_i = sum(h[c][0] for c in MEDIDO)
        med_w = sum(h[c][1] for c in MEDIDO)
        if serv is None:      # V0: todo lo no medido al ritmo de lo medido
            return med_i / med_w * 1.0 * sum(x[1] for x in h.values())
        # bienes medidos del mismo mes (sin tarifas ni transporte ni prepagas)
        bm = ["01", "02", "05.6", "07.2.2", "12.1"]
        vb = sum(h[c][0] for c in bm) / sum(h[c][1] for c in bm)
        est = med_i + sum(h[c][1] * vb for c in BIENES_NO)
        for c in SERV_NO:
            v = serv(m, c)
            if c == "04.1.1" and alq_regla is not None:
                v = alq_regla(m)
            if v is None:
                return None
            est += h[c][1] * v
        return est

    variantes = {
        "V0 hoy: lo no medido al ritmo de lo medido": lambda m: estimar(m, None),
        "V1 servicios con el nivel general t−1": lambda m: estimar(m, lambda mm, c: vng.get(prev(mm))),
        "V1s servicios con servicios del INDEC t−1": lambda m: estimar(m, lambda mm, c: vS.get(prev(mm))),
        "V1p cada servicio con su propio dato t−1": lambda m: estimar(m, lambda mm, c: H[prev(mm)][c][2] if prev(mm) in H else None),
    }
    for k, r in reglas_alq.items():
        variantes["V1 + " + k] = (lambda rr: (lambda m: estimar(m, lambda mm, c: vng.get(prev(mm)), rr)))(r)
        variantes["V1p + " + k] = (lambda rr: (lambda m: estimar(m, lambda mm, c: H[prev(mm)][c][2] if prev(mm) in H else None, rr)))(r)

    err = {}
    for k, f in variantes.items():
        e = {}
        for m in meses:
            v = f(m)
            if v is not None:
                e[m] = v - vng[m]
        err[k] = e
    comunes = sorted(set.intersection(*[set(e) for e in err.values()]))
    ult = comunes[-24:]

    def resumen(lista):
        out = {}
        for k, e in err.items():
            x = [e[m] for m in lista]
            out[k] = {"rmse": round(rmse(x), 3), "mae": round(sum(map(abs, x)) / len(x), 3), "sesgo": round(sum(x) / len(x), 3)}
        return out

    r_all, r_24 = resumen(comunes), resumen(ult)
    ref0, ref1 = "V0 hoy: lo no medido al ritmo de lo medido", "V1 servicios con el nivel general t−1"
    dm = {}
    for k in err:
        if k in (ref0,):
            continue
        a = [err[k][m] for m in comunes]
        d0 = diebold_mariano(a, [err[ref0][m] for m in comunes])
        d1 = diebold_mariano(a, [err[ref1][m] for m in comunes]) if k != ref1 else (float("nan"), float("nan"))
        dm[k] = {"vs_V0_p": round(d0[1], 4), "vs_V1_p": None if math.isnan(d1[1]) else round(d1[1], 4)}
    out = {"region": "GBA", "n": len(comunes), "desde": comunes[0], "hasta": comunes[-1],
           "ultimos_24": {"desde": ult[0], "hasta": ult[-1]}, "todo": r_all, "ult24": r_24, "diebold_mariano": dm,
           "peso_medido_pct": round(100 * sum(H[comunes[-1]][c][1] for c in MEDIDO) / sum(x[1] for x in H[comunes[-1]].values()), 1)}
    (cfg.ruta("resultados") / "imputacion_servicios.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"GBA {out['desde']} a {out['hasta']} ({out['n']} meses); peso medido {out['peso_medido_pct']}%")
    print(f"{'variante':62s} {'RMSE':>6s} {'MAE':>6s} {'sesgo':>6s} | ult24: {'RMSE':>6s} {'MAE':>6s} {'sesgo':>6s} | p vs V0  p vs V1")
    for k in sorted(err, key=lambda k: r_all[k]["rmse"]):
        a, b, d = r_all[k], r_24[k], dm.get(k, {})
        print(f"{k[:62]:62s} {a['rmse']:6.3f} {a['mae']:6.3f} {a['sesgo']:+6.3f} |       {b['rmse']:6.3f} {b['mae']:6.3f} {b['sesgo']:+6.3f} | {d.get('vs_V0_p','')!s:7s} {d.get('vs_V1_p','')!s}")


if __name__ == "__main__":
    main()
