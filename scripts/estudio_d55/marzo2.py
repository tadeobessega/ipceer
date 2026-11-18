import itertools
import numpy as np, pandas as pd
t = pd.read_csv("work/resultados/marzos.csv", index_col=0)
y = t.educ_mar
specs = {"NG12 (origen)": (["ng12"], False), "a+NG12": (["ng12"], True), "a+NG12+educ abr-feb": (["ng12", "educ_abr_feb"], True),
         "a+brecha": (["gap"], True), "a+paritarias12": (["par12"], True), "a+NG ene-feb+NG12": (["ng_ene_feb", "ng12"], True),
         "a+NG ene-feb": (["ng_ene_feb"], True), "a+ISALpub12 t-2": (["pub12_t2"], True),
         "a+paritarias feb-abr*": (["par_feb_abr"], True), "a+NG12+paritarias feb-abr*": (["ng12", "par_feb_abr"], True)}
def fit(tr, cols, c):
    X = tr[cols].to_numpy(); X = np.column_stack([np.ones(len(X)), X]) if c else X
    b, *_ = np.linalg.lstsq(X, y[tr.index].to_numpy(), rcond=None); return b
def pred(b, row, cols, c):
    x = row[cols].to_numpy(float); return (b[0] + x @ b[1:]) if c else x @ b
pp = lambda v: 100 * np.expm1(v)
res = {}
print("pronóstico de marzo (%), ventana creciente; dato:", " ".join(f"{a}:{pp(y[a]):.1f}" for a in range(2020, 2027)))
for k, (cols, c) in specs.items():
    for excl21 in (False, True):
        out = {}; loo = {}
        for a in range(2020, 2027):
            tr = t.loc[: a - 1]
            if excl21: tr = tr.drop(2021, errors="ignore")
            if len(tr) < len(cols) + c + 1: continue
            out[a] = pp(pred(fit(tr, cols, c), t.loc[a], cols, c))
        for a in t.index:
            tr = t.drop(a); tr = tr.drop(2021, errors="ignore") if excl21 else tr
            loo[a] = pp(pred(fit(tr, cols, c), t.loc[a], cols, c))
        e = np.array([out[a] - pp(y[a]) for a in out]); el = np.array([loo[a] - pp(y[a]) for a in loo])
        e2 = np.array([out[a] - pp(y[a]) for a in out if a >= 2023])
        lab = k + (" [sin 2021]" if excl21 else "")
        print(f" {lab:42s}", " ".join(f"{out.get(a, np.nan):5.1f}" for a in range(2020, 2027)),
              f"| RMSE {np.sqrt((e**2).mean()):.2f}  2023-26 {np.sqrt((e2**2).mean()):.2f}  LOO {np.sqrt((el**2).mean()):.2f}")
