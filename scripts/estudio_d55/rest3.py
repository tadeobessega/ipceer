import sys
import numpy as np, pandas as pd
exec(open("work/rest2.py").read().split("C = {")[0])
def pron_w(cols, forma, hl=None, ventana=None, rel=False):
    d = pd.concat([y.rename("y"), F[cols]], axis=1).loc[INI_EST:FIN]
    if rel:  # en términos relativos al NG sin rubros
        d = d.copy(); base = d["ngex_0"].copy()
        d["y"] = d.y - base
        for c in cols:
            if c != "ngex_0": d[c] = d[c] - (base if not c.endswith("_1") else df.ngex.shift(1).reindex(d.index))
        d = d.drop(columns="ngex_0"); cc = [c for c in cols if c != "ngex_0"]
    else:
        base = None; cc = cols
    A = d[["y"] + cc].to_numpy(float); ok = ~np.isnan(A).any(1); idx = d.index
    out = {}
    for i in range(idx.searchsorted(pd.Timestamp(INI_OOS)), len(idx)):
        if np.isnan(A[i, 1:]).any(): continue
        tr = A[:i][ok[:i]]
        if ventana: tr = tr[-ventana:]
        n = len(tr); wt = np.ones(n) if hl is None else 0.5 ** (np.arange(n)[::-1] / hl)
        sw = np.sqrt(wt)[:, None]
        X, yy = tr[:, 1:], tr[:, 0]
        if forma == "mco":
            Xc = np.column_stack([np.ones(n), X]); b, *_ = np.linalg.lstsq(Xc * sw, yy * sw[:, 0], rcond=None)
            p = b[0] + A[i, 1:] @ b[1:]
        else:
            M = np.vstack([X * sw, 1e4 * np.ones(X.shape[1])]); w, _ = nnls(M, np.concatenate([yy * sw[:, 0], [1e4]])); p = A[i, 1:] @ w
        out[idx[i]] = p + (d.index.get_loc(idx[i]) * 0 + (df.ngex[idx[i]] if rel else 0))
    return pd.Series(out)
R1 = R + ["rest_1"]
V = {"21-26": ("2021-01-01", FIN), "24-26": ("2024-01-01", FIN), "24/07-26": ("2024-07-01", FIN), "25-26": ("2025-01-01", FIN)}
C = {"R* (actual)": pron_w(R, "conv")}
for hl in (12, 24, 36):
    C[f"R* conv, semivida {hl}m"] = pron_w(R, "conv", hl)
    C[f"R* MCO, semivida {hl}m"] = pron_w(R, "mco", hl)
    C[f"R*+rubro t-1 conv, semivida {hl}m"] = pron_w(R1, "conv", hl)
    C[f"R*+rubro t-1 MCO, semivida {hl}m"] = pron_w(R1, "mco", hl)
for v in (24, 36, 48):
    C[f"R*+rubro t-1 MCO ventana {v}"] = pron_w(R1, "mco", None, v)
C["relativo: R*+rubro t-1, MCO"] = pron_w(R1, "mco", rel=True)
C["relativo: R*+rubro t-1, MCO, semivida 24"] = pron_w(R1, "mco", 24, rel=True)
C["relativo: R*, MCO, semivida 24"] = pron_w(R, "mco", 24, rel=True)
print(f"{'':42s}" + "".join(f"{k:>18s}" for k in V))
for k, p in C.items():
    s = ""
    for a, b in V.values():
        idx = y.loc[a:b].dropna().index; e = (p.reindex(idx) - y.reindex(idx)).dropna()
        yy = y.reindex(e.index); s += f"   {rmse(e):5.2f} R2={1 - (e**2).sum() / ((yy - yy.mean())**2).sum():5.2f}"
    print(f"{k:42s}{s}")
pd.DataFrame(C).to_pickle("work/resultados/rest3.pkl")
