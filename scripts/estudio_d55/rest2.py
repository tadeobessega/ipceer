import sys
import numpy as np, pandas as pd
sys.argv = ["x"]
exec(open("work/modelo_servicios.py").read().replace('if __name__ == "__main__":', 'if False:'))
sys.argv = ["x", "data", "tadeobessega/ipceer"]
exec(open("work/datos_servicios.py").read().split('if __name__')[0])
LV = ipc_gba(); SL = salarios(); W = pesos()
w062 = W["06"] - W["06.1"] - W["06.4"]
lv06 = (W["06"] * LV["06"] - W["06.1"] * LV["06.1"] - W["06.4"] * LV["06.4.1"]) / w062
ngexL = (LV["0"] - W["11.1"] * LV["11.1"] - W["10"] * LV["10"] - w062 * lv06) / (1 - W["11.1"] - W["10"] - w062)
lr = np.log(LV["11.1"]); la = np.log(LV["01.1"]); ln = np.log(ngexL)
lw = np.log(SL.par_gastro_r).shift(2).reindex(LV.index)
y = df.rest
m = df.index.month
for k in range(1, 13): F[f"m{k}"] = (m == k).astype(float)
F["serv_1"] = df.servicios.shift(1); F["nuc_1"] = df.nucleo.shift(1)
F["gastro_lead"] = df.par_gastro_r.shift(-1)
F["gastro_m12_2"] = df.par_gastro_r.rolling(12).mean().shift(2)
F["gastro_m9_2"] = df.par_gastro_r.rolling(9).mean().shift(2)
F["isal_m6_3"] = F["isal_priv|m6|3"]
WG = "par_gastro_r|m6|2"
R = ["alim_0", "ngex_0", WG]

def ecm_pron(cols, ventana="exp", lw_=lw):
    lev = pd.concat([lr, la, ln, lw_], axis=1, keys=["r", "a", "n", "w"]).reindex(df.index)
    d = pd.concat([y.rename("y"), F[cols]], axis=1)
    out = {}
    for t in d.loc[INI_OOS:FIN].index:
        x0 = d.loc[t, cols]
        if x0.isna().any(): continue
        tl = lev.loc[INI_EST: t - pd.offsets.MonthBegin(1)].dropna()
        Xl = np.column_stack([np.ones(len(tl)), tl[["a", "n", "w"]]]); b, *_ = np.linalg.lstsq(Xl, tl.r, rcond=None)
        z = (lev.r - np.column_stack([np.ones(len(lev)), lev[["a", "n", "w"]]]) @ b).shift(1)
        tr = pd.concat([d, z.rename("z")], axis=1).loc[INI_EST: t - pd.offsets.MonthBegin(1)].dropna()
        if ventana == "m48": tr = tr.iloc[-48:]
        Xc = np.column_stack([np.ones(len(tr)), tr[cols + ["z"]]]); bb, *_ = np.linalg.lstsq(Xc, tr.y, rcond=None)
        out[t] = float(bb[0] + np.r_[x0.to_numpy(float), z[t]] @ bb[1:])
    return pd.Series(out)

C = {
 "R* (actual)": pronosticar("rest", R, "exp", "conv"),
 "R* MCO": pronosticar("rest", R, "exp", "mco"),
 "R* ventana 48m": None,
 "+ rubro t-1": pronosticar("rest", R + ["rest_1"], "exp", "conv"),
 "+ alimentos t-1": pronosticar("rest", R + ["alim_1"], "exp", "conv"),
 "+ servicios t-1": pronosticar("rest", R + ["serv_1"], "exp", "conv"),
 "+ núcleo t-1": pronosticar("rest", R + ["nuc_1"], "exp", "conv"),
 "+ paritaria gastro t+1": pronosticar("rest", R + ["gastro_lead"], "exp", "conv"),
 "+ ISAL priv m6 t-3": pronosticar("rest", R + ["isal_m6_3"], "exp", "conv"),
 "gastro prom 9m": pronosticar("rest", ["alim_0", "ngex_0", "gastro_m9_2"], "exp", "conv"),
 "gastro prom 12m": pronosticar("rest", ["alim_0", "ngex_0", "gastro_m12_2"], "exp", "conv"),
 "bienes en vez de alimentos": pronosticar("rest", ["bienes_0", "ngex_0", WG], "exp", "conv"),
 "+ estacionalidad (MCO)": pronosticar("rest", R + [f"m{k}" for k in (5, 7, 9, 11)], "exp", "mco"),
 "+ corrección de error (MCO)": ecm_pron(R),
 "+ corrección de error + rubro t-1": ecm_pron(R + ["rest_1"]),
}
C["R* ventana 48m"] = None
del C["R* ventana 48m"]
C["promedio R* y R*+ISAL"] = (C["R* (actual)"] + C["+ ISAL priv m6 t-3"]) / 2
C["promedio R*, ECM y rubro t-1"] = (C["R* (actual)"] + C["+ corrección de error (MCO)"] + C["+ rubro t-1"]) / 3
ref = {"a NG t-1": df.ng.shift(1), "b rubro t-1": y.shift(1), "c NG sin rubros t": df.ngex}
V = {"21-26": ("2021-01-01", FIN), "24-26": ("2024-01-01", FIN), "24/07-26": ("2024-07-01", FIN), "25-26": ("2025-01-01", FIN)}
print(f"{'':36s}" + "".join(f"{k:>18s}" for k in V))
for k, p in {**ref, **C}.items():
    s = ""
    for a, b in V.values():
        idx = y.loc[a:b].dropna().index; e = (p.reindex(idx) - y.reindex(idx)).dropna()
        yy = y.reindex(e.index); r2 = 1 - (e**2).sum() / ((yy - yy.mean())**2).sum()
        s += f"   {rmse(e):5.2f} R2={r2:5.2f}"
    print(f"{k:36s}{s}")
pd.DataFrame(C).to_pickle("work/resultados/rest2.pkl")
