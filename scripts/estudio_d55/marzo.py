import numpy as np, pandas as pd
df = pd.read_csv("work/base_servicios.csv", index_col=0, parse_dates=True)
L = lambda s, a, b: np.log1p(s.loc[a:b] / 100).sum()
rows = []
for yr in range(2018, 2027):
    m = pd.Timestamp(f"{yr}-03-01"); pm = pd.Timestamp(f"{yr-1}-03-01")
    r = {"año": yr, "educ_mar": np.log1p(df.educ[m] / 100)}
    r["ng12"] = L(df.ng, m - pd.DateOffset(months=12), m - pd.DateOffset(months=1))   # mar-prev..feb
    r["ng12_ex_mar"] = L(df.ng, pm, m - pd.DateOffset(months=1))
    r["educ_abr_feb"] = L(df.educ, pm + pd.DateOffset(months=1), m - pd.DateOffset(months=1))
    r["gap"] = r["ng12"] - r["educ_abr_feb"]
    r["par12"] = L(df.par_indice, m - pd.DateOffset(months=12), m - pd.DateOffset(months=1))
    r["par_feb_abr"] = L(df.par_indice, m - pd.DateOffset(months=1), m + pd.DateOffset(months=1))
    r["pub12_t2"] = L(df.isal_pub, m - pd.DateOffset(months=14), m - pd.DateOffset(months=3))
    r["ng_ene_feb"] = L(df.ng, m - pd.DateOffset(months=2), m - pd.DateOffset(months=1))
    rows.append(r)
t = pd.DataFrame(rows).set_index("año")
t["ratio_ng12"] = t.educ_mar / t.ng12; t["ratio_gap"] = t.educ_mar / t.gap; t["ratio_par12"] = t.educ_mar / t.par12
print(t.round(3).to_string())
t.to_csv("work/resultados/marzos.csv")
