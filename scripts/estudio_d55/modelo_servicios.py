"""Pronóstico fuera de muestra de restaurantes (11.1), educación (10) y servicios médicos (06.2) en GBA
con salarios (INDEC y paritarias), alimentos y nivel general.

Para cada mes t se estima con datos hasta t−1 y se pronostica t, usando solo lo que se conoce al cerrar
el mes t (variante "disponible"):
  - IPC del INDEC hasta t−1 (nivel general, el propio rubro);
  - lo que mide el IPCEER en t: alimentos y el nivel general sin los tres rubros (aproximados con el
    dato del INDEC, igual que la referencia "c");
  - paritarias firmadas: salario de convenio de t y de t+1 (se pactan por adelantado);
  - índice de salarios del INDEC hasta t−2 (se publica con dos meses de rezago).
La variante "sin restricción" deja usar el índice de salarios del INDEC de t y t−1 (diagnóstico).

Familia de modelos: combinaciones de regresores base × un indicador salarial (variación del mes,
promedio de 3 o de 6 meses, con rezago −1 a 3) × ventana (creciente o móvil de 36 meses) × MCO o
ponderación convexa (pesos ≥ 0 que suman 1, sin constante). La elección honesta se hace en cada mes con
el error fuera de muestra de los 24 meses anteriores (mejor modelo y promedio de los 10 mejores).
"""
from __future__ import annotations

import itertools
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import nnls

BASE = Path(sys.argv[1] if len(sys.argv) > 1 else "work/base_servicios.csv")
SALIDA = Path(sys.argv[2] if len(sys.argv) > 2 else "work/resultados")
SALIDA.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(BASE, index_col=0, parse_dates=True)
INI_EST, INI_OOS, INI_SEL, FIN = "2017-02-01", "2019-01-01", "2021-01-01", "2026-08-01"

SAL = {
    "rest": ["par_gastro_r", "par_past_r", "par_alim_r", "par_com_r", "par_indice", "isal_priv"],
    "educ": ["isal_pub", "isal_priv", "isal_reg", "par_indice", "par_maes_r"],
    "med": ["par_sanc_r", "par_sans_r", "par_farm_r", "par_indice", "isal_priv"],
}


def feat(df: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=df.index)
    for c in ["rest", "educ", "med", "ng", "prep"]:
        f[f"{c}_1"] = df[c].shift(1)
    f["ngex_0"] = df.ngex
    f["alim_0"] = df.alim
    f["alim_1"] = df.alim.shift(1)
    f["bienes_0"] = df.bienes
    m = df.index.month
    f["mar"] = (m == 3).astype(float)
    f["dic"] = (m == 12).astype(float)
    f["ene"] = (m == 1).astype(float)
    f["feb"] = (m == 2).astype(float)
    # marzo × inflación acumulada de los 12 meses previos (ajuste anual de cuotas)
    f["mar_ng12"] = f.mar * df.ng.shift(1).rolling(12).sum()
    f["mar_isal12"] = f.mar * df.isal_pub.shift(2).rolling(12).sum()
    f["educ_12"] = df.educ.shift(12)
    for w in sorted({x for v in SAL.values() for x in v}):
        for L in range(-1, 4):
            f[f"{w}|m1|{L}"] = df[w].shift(L)
            f[f"{w}|m3|{L}"] = df[w].rolling(3).mean().shift(L)
            f[f"{w}|m6|{L}"] = df[w].rolling(6).mean().shift(L)
    return f


F = feat(df)


def disponible(col: str) -> bool:
    if "|" not in col:
        return True
    w, _, L = col.split("|")
    L = int(L)
    if w.startswith("isal"):
        return L >= 2
    return L >= -1  # paritarias: t+1 ya pactado


def ajustar(X: np.ndarray, y: np.ndarray, x0: np.ndarray, forma: str) -> float:
    if forma == "mco":
        Xc = np.column_stack([np.ones(len(X)), X])
        b, *_ = np.linalg.lstsq(Xc, y, rcond=None)
        return float(b[0] + x0 @ b[1:])
    # convexa: min ||y − Xw||, w ≥ 0, Σw = 1 (penalización fuerte de la suma)
    lam = 1e4
    A = np.vstack([X, lam * np.ones(X.shape[1])])
    bb = np.concatenate([y, [lam]])
    w, _ = nnls(A, bb)
    return float(x0 @ w)


def pronosticar(target: str, cols: list[str], ventana: str, forma: str) -> pd.Series:
    d = pd.concat([df[target].rename("y"), F[cols]], axis=1).loc[INI_EST:FIN]
    A = d.to_numpy(float)
    ok = ~np.isnan(A).any(axis=1)
    xok = ~np.isnan(A[:, 1:]).any(axis=1)
    idx = d.index
    i0 = idx.searchsorted(pd.Timestamp(INI_OOS))
    out = {}
    for i in range(i0, len(idx)):
        if not xok[i]:
            continue
        tr = A[:i][ok[:i]]
        if ventana == "m36":
            tr = tr[-36:]
        if len(tr) < max(18, 3 * (len(cols) + 1)):
            continue
        out[idx[i]] = ajustar(tr[:, 1:], tr[:, 0], A[i, 1:], forma)
    return pd.Series(out, dtype=float)


def familia(target: str, restringida: bool) -> list[tuple[str, tuple, str, str]]:
    own = f"{target}_1"
    if target == "rest":
        bases = [(), ("ngex_0",), ("alim_0",), ("alim_0", "ngex_0"), (own,), ("ng_1",), ("alim_0", own),
                 ("ngex_0", own), ("alim_0", "alim_1"), ("alim_0", "ngex_0", own)]
    elif target == "educ":
        e = ("mar_ng12", "ene", "dic")
        bases = [(), ("ngex_0",), ("ng_1",), (own,), ("mar_ng12",), e, ("ngex_0",) + e, ("ng_1",) + e,
                 ("ngex_0", "mar_ng12"), ("educ_12",), ("ngex_0", "educ_12"), ("ngex_0", "mar_isal12")]
    else:
        bases = [(), ("ngex_0",), ("ng_1",), (own,), ("prep_1",), ("ngex_0", own), ("ngex_0", "prep_1"),
                 ("ngex_0", "feb", "dic"), ("ng_1", "feb", "dic")]
    sal = [c for c in F.columns if "|" in c and c.split("|")[0] in SAL[target]]
    if restringida:
        sal = [c for c in sal if disponible(c)]
    specs = []
    for b in bases:
        for s in [None] + sal:
            cols = tuple(b) + ((s,) if s else ())
            if not cols:
                continue
            for v in ("exp", "m36"):
                specs.append((f"{'+'.join(cols)}|{v}|mco", cols, v, "mco"))
            estac = any(c in ("mar", "ene", "dic", "feb", "mar_ng12", "mar_isal12") for c in cols)
            if len(cols) >= 2 and not estac:
                specs.append((f"{'+'.join(cols)}|exp|conv", cols, "exp", "conv"))
    return specs


def rmse(e) -> float:
    e = np.asarray(e, float)
    return float(np.sqrt(np.mean(e * e)))


def dm(e1, e2) -> float:
    d = np.asarray(e1) ** 2 - np.asarray(e2) ** 2
    n = len(d)
    md = d.mean()
    g0 = ((d - md) ** 2).mean()
    g1 = ((d[1:] - md) * (d[:-1] - md)).sum() / n
    lrv = g0 + g1
    if lrv <= 0:
        return float("nan")
    s = md / math.sqrt(lrv / n) * math.sqrt((n - 1) / n)
    return math.erfc(abs(s) / math.sqrt(2))


def seleccion(preds: pd.DataFrame, y: pd.Series, k: int) -> pd.Series:
    err2 = preds.sub(y, axis=0) ** 2
    out = {}
    for t in preds.loc[INI_SEL:FIN].index:
        hist = err2.loc[t - pd.DateOffset(months=24): t - pd.offsets.MonthBegin(1)]
        ok = hist.notna().sum() >= 16
        sc = hist.mean()[ok]
        sc = sc[preds.loc[t, sc.index].notna()]
        if sc.empty:
            continue
        top = sc.nsmallest(k).index
        out[t] = preds.loc[t, top].mean()
    return pd.Series(out)


def correr(target: str, restringida: bool) -> dict:
    y = df[target]
    specs = familia(target, restringida)
    preds = {}
    for nom, cols, v, forma in specs:
        p = pronosticar(target, list(cols), v, forma)
        if len(p):
            preds[nom] = p
    P = pd.DataFrame(preds)
    ng_lab = {"rest": "ngex", "educ": "ngex", "med": "ngex"}
    ref = pd.DataFrame({
        "a_ng_t-1": df.ng.shift(1),
        "b_rubro_t-1": y.shift(1),
        "c_ngex_t": df[ng_lab[target]],
    })
    sel1 = seleccion(P, y, 1)
    sel10 = seleccion(P, y, 10)
    ventanas = {"2021-01 a 2026-08": ("2021-01-01", FIN), "2021-01 a 2023-12": ("2021-01-01", "2023-12-01"),
                "2024-07 a 2026-08": ("2024-07-01", FIN)}
    res = {"n_modelos": P.shape[1], "ventanas": {}}
    for nv, (a, b) in ventanas.items():
        idx = y.loc[a:b].dropna().index
        idx = idx[sel10.reindex(idx).notna() & sel1.reindex(idx).notna()]
        e = {k: (ref[k].reindex(idx) - y.reindex(idx)) for k in ref}
        e["sel_mejor"] = sel1.reindex(idx) - y.reindex(idx)
        e["sel_top10"] = sel10.reindex(idx) - y.reindex(idx)
        sub = P.reindex(idx).dropna(axis=1)
        ex = (sub.sub(y.reindex(idx), axis=0) ** 2).mean().pow(0.5).sort_values()
        mejor_ref = min(ref, key=lambda k: rmse(e[k]))
        fila = {k: {"rmse": rmse(v), "mae": float(np.mean(np.abs(v)))} for k, v in e.items()}
        for k in ("sel_mejor", "sel_top10"):
            fila[k]["p_dm_vs_" + mejor_ref] = dm(e[k], e[mejor_ref])
        fila["ex_post_top5"] = {n: float(r) for n, r in ex.head(5).items()}
        top = ex.index[0]
        fila["ex_post_mejor_p_dm"] = dm(sub[top] - y.reindex(idx), e[mejor_ref])
        res["ventanas"][nv] = {"n": len(idx), "mejor_referencia": mejor_ref, "modelos": fila}
    # qué modelos elige la selección honesta (frecuencia), últimos 24 meses
    err2 = P.sub(y, axis=0) ** 2
    elegidos = []
    for t in P.loc["2024-09-01":FIN].index:
        hist = err2.loc[t - pd.DateOffset(months=24): t - pd.offsets.MonthBegin(1)]
        sc = hist.mean()[hist.notna().sum() >= 16]
        if len(sc):
            elegidos.append(sc.idxmin())
    res["elegidos_recientes"] = pd.Series(elegidos).value_counts().head(8).to_dict()
    serie = pd.DataFrame({"y": y, **{k: ref[k] for k in ref}, "sel_top10": sel10, "sel_mejor": sel1}).loc[INI_SEL:FIN]
    serie.to_csv(SALIDA / f"pronosticos_{target}_{'disp' if restringida else 'libre'}.csv")
    P.to_pickle(SALIDA / f"P_{target}_{'disp' if restringida else 'libre'}.pkl")
    return res


if __name__ == "__main__":
    out = {}
    for tgt in ["rest", "educ", "med"]:
        for r in (True, False):
            k = f"{tgt}_{'disponible' if r else 'sin_restriccion'}"
            out[k] = correr(tgt, r)
            print(k, json.dumps(out[k]["ventanas"]["2021-01 a 2026-08"], ensure_ascii=False)[:1500], flush=True)
    (SALIDA / "resultados.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))


def holdout(target: str, tipo: str = "disp", corte: str = "2023-12-01", desde: str = "2024-01-01") -> dict:
    """Elige el modelo con el error 2019-01..corte y lo evalúa en desde..FIN (prueba limpia)."""
    P = pd.read_pickle(SALIDA / f"P_{target}_{tipo}.pkl")
    y = df[target]
    a = P.loc[INI_OOS:corte]
    a = a.loc[:, a.notna().sum() >= 0.9 * y.loc[INI_OOS:corte].notna().sum()]
    sc = (a.sub(y, axis=0) ** 2).mean().pow(0.5).sort_values()
    idx = y.loc[desde:FIN].dropna().index
    ref = {"a_ng_t-1": df.ng.shift(1), "b_rubro_t-1": y.shift(1), "c_ngex_t": df.ngex}
    o = {k: rmse(v.reindex(idx) - y.reindex(idx)) for k, v in ref.items()}
    for n in sc.index[:5]:
        e = P[n].reindex(idx) - y.reindex(idx)
        mr = min(ref, key=lambda k: o[k])
        o[n] = {"rmse_holdout": rmse(e.dropna()), "rmse_seleccion": float(sc[n]),
                "p_dm": dm(e.dropna(), (ref[mr].reindex(idx) - y.reindex(idx))[e.notna()])}
    return o
