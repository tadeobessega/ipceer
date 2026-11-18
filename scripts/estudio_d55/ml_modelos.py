"""Comparación de familias de modelos (pronóstico mes a mes, ventana creciente desde 2017-02).
Evaluación jul-24 a ago-26 (26 meses) y sep-oct 2026 (no usados antes)."""
import sys, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
from sklearn.linear_model import LinearRegression, Ridge, Lasso, ElasticNet, RidgeCV, LassoCV
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import TimeSeriesSplit
sys.argv = ["x"]
exec(open("work/grafico_educ.py").read().split("d = pd.DataFrame")[0].replace('matplotlib.use("Agg"); import matplotlib.pyplot as plt', ''))
y_r, y_e = df.rest, df.educ
X = pd.DataFrame(index=df.index)
for c in ["alim", "ngex", "bienes", "nucleo"]: X[f"{c}_0"] = df[c]
for c in ["alim", "ng", "rest", "educ", "servicios"]:
    X[f"{c}_1"] = df[c].shift(1)
X["rest_2"] = df.rest.shift(2); X["educ_12"] = df.educ.shift(12)
for w in ["par_gastro_r", "par_indice"]:
    for k in (1, 3, 6, 12):
        for L in (2, 3, 4): X[f"{w}_m{k}_L{L}"] = df[w].rolling(k).mean().shift(L)
for w in ["isal_priv", "isal_pub"]:
    for k in (1, 3, 6): X[f"{w}_m{k}_L2"] = df[w].rolling(k).mean().shift(2)
m = df.index.month
for k in range(1, 13): X[f"mes{k}"] = (m == k).astype(float)
mar = (m == 3).astype(float)
X["mar_ng12"] = mar * df.ng.shift(1).rolling(12).sum()
X["mar_educ11"] = mar * df.educ.shift(1).rolling(11).sum()
X["mar_par12"] = mar * df.par_indice.shift(2).rolling(12).sum()
FR = [c for c in X if not c.startswith(("educ", "mar_", "isal_pub", "mes"))]
FE = [c for c in X if not c.startswith(("rest", "par_gastro"))]
tscv = TimeSeriesSplit(5)
MOD = {
 "MCO (todas las variables)": lambda: make_pipeline(StandardScaler(), LinearRegression()),
 **{f"Ridge α={a}": (lambda a=a: make_pipeline(StandardScaler(), Ridge(alpha=a))) for a in (0.1, 1, 10, 100)},
 "Ridge α por validación": lambda: make_pipeline(StandardScaler(), RidgeCV(alphas=np.logspace(-2, 3, 30), cv=tscv)),
 **{f"Lasso α={a}": (lambda a=a: make_pipeline(StandardScaler(), Lasso(alpha=a, max_iter=50000))) for a in (0.01, 0.05, 0.1, 0.3)},
 "Lasso α por validación": lambda: make_pipeline(StandardScaler(), LassoCV(cv=tscv, max_iter=50000, alphas=40)),
 "Elastic net α=0,1": lambda: make_pipeline(StandardScaler(), ElasticNet(alpha=0.1, l1_ratio=0.5, max_iter=50000)),
 "Random forest": lambda: RandomForestRegressor(n_estimators=400, min_samples_leaf=3, max_features=0.33, random_state=0, n_jobs=2),
 "Gradient boosting": lambda: GradientBoostingRegressor(n_estimators=300, max_depth=2, learning_rate=0.05, subsample=0.8, random_state=0),
}
def oos(y, feats, mk, ini="2024-07-01", fin="2026-10-01"):
    d = pd.concat([y.rename("y"), X[feats]], axis=1).loc["2017-02-01":fin]
    out = {}
    for t in d.loc[ini:fin].index:
        x0 = d.loc[[t], feats]
        tr = d.loc[: t - pd.offsets.MonthBegin(1)].dropna()
        cols = [c for c in feats if not np.isnan(x0[c].iloc[0])]
        mdl = mk(); mdl.fit(tr[cols], tr.y); out[t] = float(mdl.predict(x0[cols])[0])
    return pd.Series(out)
def metr(p, y, a, b, solo=None):
    s = pd.DataFrame({"p": p, "y": y}).loc[a:b].dropna()
    if solo: s = s[s.index.month.isin(solo)]
    e = s.p - s.y
    return {"RMSE": np.sqrt((e**2).mean()), "MAE": e.abs().mean(), "MAPE": (e.abs() / s.y.abs()).mean() * 100,
            "WAPE": e.abs().sum() / s.y.abs().sum() * 100, "R2": 1 - (e**2).sum() / ((s.y - s.y.mean())**2).sum() if len(s) > 2 else np.nan}
res = {}
for nom, y, feats, mio in [("rest", y_r, FR, 0.22 * df.alim + 0.22 * df.par_gastro_r.rolling(6).mean().shift(2) + 0.57 * df.ngex),
                           ("educ", y_e, FE, pE)]:
    P = {"Modelo en uso": mio, "Ref: NG t-1": df.ng.shift(1), "Ref: rubro t-1": y.shift(1), "Ref: NG sin rubros t": df.ngex}
    for k, mk in MOD.items():
        P[k] = oos(y, feats, mk); print(nom, k, flush=True)
    rows = {}
    for k, p in P.items():
        r = {f"{a} {b}": v for b, v in metr(p, y, "2024-07", "2026-08").items() for a in ["jul24-ago26"]}
        r.update({f"sep-oct26 {b}": v for b, v in metr(p, y, "2026-09", "2026-10").items() if b in ("MAE", "WAPE")})
        if nom == "educ":
            r.update({f"marzos {b}": v for b, v in metr(p, y, "2025-03", "2026-03", solo=[3]).items() if b in ("MAE", "WAPE")})
            r.update({f"sin marzo {b}": v for b, v in metr(p, y, "2024-07", "2026-08", solo=[1,2,4,5,6,7,8,9,10,11,12]).items() if b in ("RMSE", "WAPE")})
        rows[k] = r
    T = pd.DataFrame(rows).T
    res[nom] = T; T.to_csv(f"work/resultados/ml_{nom}.csv"); pd.DataFrame(P).loc["2024-07":].to_csv(f"work/resultados/ml_pred_{nom}.csv")
for nom, T in res.items():
    print("\n=====", nom); print(T.sort_values("jul24-ago26 RMSE").round(2).to_string())
