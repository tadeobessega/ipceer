import sys, json
import numpy as np, pandas as pd
sys.argv = ["x"]
exec(open("work/modelo_servicios.py").read().replace('if __name__ == "__main__":', 'if False:'))
E = ("mar_ng12", "ene", "dic")
FIN_ = {"rest": ("alim_0","ngex_0","par_gastro_r|m6|2"), }
MOD = {
 "rest": {"R* alim t + NG sin rubros t + salario gastronómico (prom. 6m, t-2)": (("alim_0","ngex_0","par_gastro_r|m6|2"),"conv"),
          "R** alim t + NG t-1 + salario gastronómico (prom. 6m, t-2)": (("alim_0","ng_1","par_gastro_r|m6|2"),"conv"),
          "R0 alim t + NG sin rubros t (sin salario)": (("alim_0","ngex_0"),"conv")},
 "educ": {"E0 NG sin rubros t + marzo×NG 12m + ene + dic": (("ngex_0",)+E,"mco"),
          "E* E0 + índice de paritarias t-1": (("ngex_0",)+E+("par_indice|m1|1",),"mco"),
          "E** NG t-1 + marzo×NG 12m + ene + dic + paritarias t-1": (("ng_1",)+E+("par_indice|m1|1",),"mco")},
 "med": {"M0 NG sin rubros t + rubro t-1": (("ngex_0","med_1"),"conv"),
         "M* NG sin rubros t + ISAL privado (prom. 3m, t-2)": (("ngex_0","isal_priv|m3|2"),"conv"),
         "M** NG t-1 + ISAL privado (prom. 3m, t-2)": (("ng_1","isal_priv|m3|2"),"conv")},
}
VENT = {"2021-01 a 2026-08": ("2021-01-01", FIN), "2024-01 a 2026-08": ("2024-01-01", FIN),
        "2024-07 a 2026-08": ("2024-07-01", FIN), "2025-01 a 2026-08": ("2025-01-01", FIN)}
out = {}
for tgt, specs in MOD.items():
    y = df[tgt]
    ref = {"a NG t-1": df.ng.shift(1), "b rubro t-1": y.shift(1), "c NG sin rubros t": df.ngex}
    P = {k: pronosticar(tgt, list(c), "exp", f) for k, (c, f) in specs.items()}
    tab = {}
    for nv, (a, b) in VENT.items():
        idx = y.loc[a:b].dropna().index
        er = {k: (v.reindex(idx) - y.reindex(idx)) for k, v in ref.items()}
        mr = min(er, key=lambda k: rmse(er[k]))
        t = {k: {"rmse": round(rmse(e), 2), "mae": round(float(e.abs().mean()), 2)} for k, e in er.items()}
        for k, p in P.items():
            e = p.reindex(idx) - y.reindex(idx)
            t[k] = {"rmse": round(rmse(e), 2), "mae": round(float(e.abs().mean()), 2),
                    "mejora": round(100 * (1 - rmse(e) / rmse(er[mr]))), "p": round(dm(e, er[mr]), 3)}
        tab[nv] = {"n": len(idx), "ref": mr, "t": t}
    co = {}
    for k, (c, f) in specs.items():
        d = pd.concat([y.rename("y"), F[list(c)]], axis=1).loc[INI_EST:FIN].dropna(); X = d[list(c)].to_numpy()
        if f == "conv":
            w, _ = nnls(np.vstack([X, 1e4*np.ones(X.shape[1])]), np.concatenate([d.y, [1e4]])); co[k] = dict(zip(c, np.round(w, 3).tolist()))
        else:
            b_, *_ = np.linalg.lstsq(np.column_stack([np.ones(len(X)), X]), d.y.to_numpy(), rcond=None); co[k] = dict(zip(("const",)+tuple(c), np.round(b_, 3).tolist()))
    out[tgt] = {"tablas": tab, "coef": co}
    pd.DataFrame({"y": y, **ref, **P}).loc["2021-01-01":FIN].round(3).to_csv(SALIDA / f"final_{tgt}.csv")
    for nv, r in tab.items():
        print(tgt, nv, r["n"], r["ref"]); [print("   ", k, v) for k, v in r["t"].items()]
    print(co)
(SALIDA / "final.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
