"""Montecarlo de ponderaciones fijas para restaurantes (11.1, GBA) desde jul-2024.
pron_t = wa*alim_{t-la} + ww*salario_gastro(prom k meses, rezago L) + (1-wa-ww)*NG sin rubros_t
Sin estimación: cada combinación se evalúa tal cual. Selección en jul-24..ago-25, prueba en sep-25..ago-26,
y prueba final en sep-oct 2026 (meses que no se usaron en ningún análisis anterior)."""
import numpy as np, pandas as pd
rng = np.random.default_rng(2026)
df = pd.read_csv("work/base_servicios.csv", index_col=0, parse_dates=True)
y = df.rest; g = df.par_gastro_r; a = df.alim; n = df.ngex
SEL, TEST, FUT = ("2024-07", "2025-08"), ("2025-09", "2026-08"), ("2026-09", "2026-10")
N = 20000
wa = rng.uniform(0, 1, N); ww = rng.uniform(0, 1, N)
ok = wa + ww <= 1; wa, ww = wa[ok], ww[ok]
k = rng.choice([1, 3, 6, 9, 12], len(wa)); L = rng.integers(0, 9, len(wa)); la = rng.choice([0, 1], len(wa))
gs = {(kk, LL): g.rolling(kk).mean().shift(LL) for kk in [1, 3, 6, 9, 12] for LL in range(9)}
al = {0: a, 1: a.shift(1)}
def met(p, per):
    e = (p - y).loc[per[0]:per[1]].dropna(); yy = y.reindex(e.index)
    return np.sqrt((e**2).mean()), 1 - (e**2).sum() / ((yy - yy.mean())**2).sum(), len(e)
rows = []
for i in range(len(wa)):
    p = wa[i] * al[la[i]] + ww[i] * gs[(k[i], L[i])] + (1 - wa[i] - ww[i]) * n
    r = {"w_alim": wa[i], "w_sal": ww[i], "w_ngex": 1 - wa[i] - ww[i], "k": k[i], "rezago": L[i], "rez_alim": la[i]}
    for nom, per in [("tot", (SEL[0], TEST[1])), ("sel", SEL), ("test", TEST), ("fut", FUT)]:
        r[f"rmse_{nom}"], r[f"r2_{nom}"], _ = met(p, per)
    rows.append(r)
S = pd.DataFrame(rows)
# modelo en uso (R*) y referencias
def evalp(p): return {f"{m}_{nom}": v for nom, per in [("tot", (SEL[0], TEST[1])), ("sel", SEL), ("test", TEST), ("fut", FUT)] for m, v in zip(("rmse", "r2"), met(p, per)[:2])}
ref = {"R* (0,22 alim / 0,22 salario 6m t-2 / 0,57 NGex)": 0.22 * a + 0.22 * gs[(6, 2)] + 0.57 * n,
       "NG t-1": df.ng.shift(1), "rubro t-1": y.shift(1), "NG sin rubros t": n}
R = pd.DataFrame({k_: evalp(v) for k_, v in ref.items()}).T
S.to_csv("work/resultados/montecarlo_rest.csv", index=False); R.to_csv("work/resultados/montecarlo_ref.csv")
print(len(S), "simulaciones"); print(R.round(3).to_string())
for col in ["rmse_tot", "rmse_sel"]:
    b = S.nsmallest(1, col).iloc[0]; print("\nmejor por", col); print(b.round(3).to_string())
top = S.nsmallest(200, "rmse_sel")
print("\ntop 200 por selección: medianas"); print(top[["w_alim", "w_sal", "k", "rezago", "rmse_test", "r2_test", "rmse_fut"]].median().round(3).to_string())
print("\npercentil de R* en rmse_tot:", round((S.rmse_tot < R.iloc[0].rmse_tot).mean() * 100, 1), "%")
print("\nmejor rmse_tot por rezago y k:"); print(S.pivot_table(index="rezago", columns="k", values="rmse_tot", aggfunc="min").round(2).to_string())
print("\ncorrelación rmse sel vs test (rango):", round(S.rmse_sel.rank().corr(S.rmse_test.rank()), 3))
