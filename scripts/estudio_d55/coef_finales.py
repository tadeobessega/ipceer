import numpy as np, pandas as pd
from scipy.optimize import nnls
df = pd.read_csv("work/base_servicios.csv", index_col=0, parse_dates=True)
s = pd.read_excel("salarios/Indice_Salarios_Paritarias.xlsx", sheet_name="Salarios")
g = s[(s.Convenio == "Gastronómicos") & (s.Serie == "Representativa")].set_index("Fecha")["Salario conformado"].pct_change() * 100
ix = pd.read_excel("salarios/Indice_Salarios_Paritarias.xlsx", sheet_name="Indice", header=7).dropna(subset=["Mes"]).set_index("Mes")["Índice (base=100)"]
par = ix[ix > 0].pct_change() * 100
g.index = pd.to_datetime(g.index); par.index = pd.to_datetime(par.index)
FIN = "2026-08-01"
def conv(y, X):
    d = pd.concat([y, X], axis=1).loc["2017-02":FIN].dropna(); A = d.iloc[:, 1:].to_numpy()
    w, _ = nnls(np.vstack([A, 1e4 * np.ones(A.shape[1])]), np.concatenate([d.iloc[:, 0], [1e4]])); return dict(zip(X.columns, w.round(4))), len(d)
X = pd.DataFrame({"alim": df.alim, "ngex": df.ngex, "sal": g.rolling(6).mean().shift(2).reindex(df.index)})
print("rest", conv(df.rest, X)); print("rest sin salario", conv(df.rest, X[["alim", "ngex"]]))
m = df.index.month
E = pd.DataFrame({"ngex": df.ngex, "mar_ng12": (m == 3) * df.ng.shift(1).rolling(12).sum(), "ene": (m == 1) * 1.0, "dic": (m == 12) * 1.0,
                  "par1": par.shift(1).reindex(df.index)}, index=df.index)
def mco(cols):
    d = pd.concat([df.educ, E[cols]], axis=1).loc["2017-02":FIN].dropna(); Xc = np.column_stack([np.ones(len(d)), d[cols]])
    b, *_ = np.linalg.lstsq(Xc, d.educ, rcond=None); return dict(zip(["const"] + cols, b.round(4))), len(d)
print("educ", mco(["ngex", "mar_ng12", "ene", "dic", "par1"])); print("educ sin par", mco(["ngex", "mar_ng12", "ene", "dic"]))
t = pd.read_csv("work/resultados/marzos.csv", index_col=0)
Xm = np.column_stack([np.ones(len(t)), t[["ng12", "educ_abr_feb"]]]); b, *_ = np.linalg.lstsq(Xm, t.educ_mar, rcond=None); print("marzo", b.round(4), list(t.index))
