"""Modelos finales: educación (marzo con modelo propio) y restaurantes (combinación)."""
import sys
import numpy as np, pandas as pd
sys.argv = ["x"]
exec(open("work/modelo_servicios.py").read().replace('if __name__ == "__main__":', 'if False:'))
y = df.educ
# meses que no son marzo: E* (marzo se pisa abajo)
pE = pronosticar("educ", ["ngex_0", "mar_ng12", "ene", "dic", "par_indice|m1|1"], "exp", "mco")
t = pd.read_csv(SALIDA / "marzos.csv", index_col=0)
pm = {}
for a in range(2021, 2027):
    tr = t.loc[: a - 1]; X = np.column_stack([np.ones(len(tr)), tr[["ng12", "educ_abr_feb"]]])
    b, *_ = np.linalg.lstsq(X, tr.educ_mar, rcond=None)
    pm[pd.Timestamp(f"{a}-03-01")] = 100 * np.expm1(b[0] + t.loc[a, ["ng12", "educ_abr_feb"]].to_numpy(float) @ b[1:])
pEM = pE.copy(); [pEM.__setitem__(k, v) for k, v in pm.items()]
ref = {"a NG t-1": df.ng.shift(1), "b rubro t-1": y.shift(1), "c NG sin rubros t": df.ngex}
def tabla(nom, y, P):
    print("==", nom)
    for a, b in [("2021-01", "2026-08"), ("2024-01", "2026-08"), ("2024-07", "2026-08"), ("2025-01", "2026-08")]:
        idx = y.loc[a:b].dropna().index
        er = {k: (v.reindex(idx) - y.reindex(idx)) for k, v in ref.items()}; mr = min(er, key=lambda k: rmse(er[k]))
        s = f" {a}..{b} mejor ref {mr} {rmse(er[mr]):.2f} R2={1-(er[mr]**2).sum()/((y[idx]-y[idx].mean())**2).sum():.2f} |"
        for k, p in P.items():
            e = p.reindex(idx) - y.reindex(idx)
            s += f" {k}: {rmse(e):.2f} R2={1-(e**2).sum()/((y[idx]-y[idx].mean())**2).sum():.2f} ({100*(1-rmse(e)/rmse(er[mr])):+.0f}%, p={dm(e, er[mr]):.3f}) |"
        print(s)
tabla("educ", y, {"E* anterior": pE, "E* + marzo nuevo": pEM})
mi = list(pm)
print(" marzos: dato", [round(y[k], 1) for k in mi], "nuevo", [round(pm[k], 1) for k in mi], "anterior", [round(pE[k], 1) for k in mi])
yr = df.rest
C3 = pd.read_pickle(SALIDA / "rest3.pkl")
comb = (C3["R* (actual)"] + C3["relativo: R*+rubro t-1, MCO"]) / 2
ref = {"a NG t-1": df.ng.shift(1), "b rubro t-1": yr.shift(1), "c NG sin rubros t": df.ngex}
tabla("rest", yr, {"R*": C3["R* (actual)"], "R2 combinado": comb})
pd.DataFrame({"educ": y, "educ_pron": pEM, "rest": yr, "rest_pron_R*": C3["R* (actual)"], "rest_pron_combinado": comb}).loc["2021-01":FIN].round(2).to_csv(SALIDA / "pronosticos_finales.csv")
# coeficientes relativo con toda la muestra
R1 = ["alim_0", "par_gastro_r|m6|2", "rest_1"]
d = pd.concat([yr - df.ngex, F["alim_0"] - df.ngex, F["par_gastro_r|m6|2"] - df.ngex, F["rest_1"] - df.ngex.shift(1)], axis=1).loc[INI_EST:FIN].dropna()
b, *_ = np.linalg.lstsq(np.column_stack([np.ones(len(d)), d.iloc[:, 1:]]), d.iloc[:, 0], rcond=None)
print("relativo coef const, alim-ngex, gastro-ngex, (rest-ngex)_t-1:", np.round(b, 3))
