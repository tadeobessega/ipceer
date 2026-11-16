"""Arma la base mensual (GBA) para modelar restaurantes, educación y servicios médicos con salarios.

Fuentes:
  - IPC INDEC, aperturas (serie_ipc_aperturas.csv) y divisiones (serie_ipc_divisiones.csv), GBA.
  - Ponderadores GBA dic-2016 (config/ponderadores_clase.csv del repo).
  - Índice de salarios INDEC (indice_salarios.csv): privado registrado, público, total registrado.
  - Boletín STEySS de salarios de convenio (Excel Indice_Salarios_Paritarias.xlsx, hoja Salarios).

Todo en variaciones mensuales en puntos porcentuales.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(sys.argv[1] if len(sys.argv) > 1 else "data")
REPO = Path(sys.argv[2] if len(sys.argv) > 2 else "tadeobessega/ipceer")


def per(x) -> pd.Timestamp:
    x = str(int(x))
    return pd.Timestamp(int(x[:4]), int(x[4:]), 1)


def ipc_gba() -> pd.DataFrame:
    a = pd.read_csv(DATA / "serie_ipc_aperturas.csv", sep=";", encoding="latin1", decimal=",")
    a = a[a.Region == "GBA"].rename(columns={"Descripcion_aperturas": "Descripcion"})
    d = pd.read_csv(DATA / "serie_ipc_divisiones.csv", sep=";", encoding="latin1", decimal=",")
    d = d[d.Region == "GBA"]
    x = pd.concat([a, d])
    x["mes"] = x.Periodo.map(per)
    x["Codigo"] = x.Codigo.astype(str)
    lv = x.pivot_table(index="mes", columns="Codigo", values="Indice_IPC", aggfunc="first")
    return lv.sort_index()


def pesos() -> dict[str, float]:
    p = pd.read_csv(REPO / "config/ponderadores_clase.csv", dtype={"codigo": str})
    w = dict(zip(p.codigo, p.GBA / 100))
    w["06.4.1"] = w["06.4"]
    return w


def salarios() -> pd.DataFrame:
    s = pd.read_csv(DATA / "indice_salarios.csv", sep=";", decimal=",", na_values="NA")
    s["mes"] = pd.to_datetime(s.periodo, dayfirst=True)
    s = s.set_index("mes")[["IS_sector_privado_registrado", "IS_sector_publico", "IS_total_registrado"]]
    s.columns = ["isal_priv", "isal_pub", "isal_reg"]
    b = pd.read_excel(DATA / "par.xlsx", sheet_name="Salarios")
    b = b[b.Fecha >= "2012-01-01"]
    out = {}
    for conv, nom in [("Gastronómicos", "gastro"), ("Pasteleros", "past"), ("Alimentación", "alim"),
                      ("Comercio", "com"), ("Sanidad con internación", "sanc"),
                      ("Sanidad sin internación", "sans"), ("Farmacia", "farm"),
                      ("Maestranza", "maes"), ("Encargados de Edificio", "encar")]:
        for serie, suf in [("Representativa", "r"), ("Promedio", "p")]:
            z = b[(b.Convenio == conv) & (b.Serie == serie)].set_index("Fecha")["Salario conformado"]
            if len(z) > 100:
                out[f"par_{nom}_{suf}"] = z
    oficial = b[b.Convenio == "Promedio ponderado (oficial)"].set_index("Fecha")["Salario conformado"]
    out["par_oficial"] = oficial
    # índice ponderado propio (hoja Indice, columna "Índice (base=100)")
    ix = pd.read_excel(DATA / "par.xlsx", sheet_name="Indice", header=7)
    ix = ix.dropna(subset=["Mes"]).set_index("Mes")["Índice (base=100)"]
    out["par_indice"] = ix[ix > 0]
    p = pd.DataFrame(out)
    p.index = pd.to_datetime(p.index)
    return s.join(p, how="outer").sort_index()


def construir() -> pd.DataFrame:
    lv = ipc_gba()
    w = pesos()
    # servicios médicos y odontológicos (06.2) = Salud − medicamentos − prepagas (Laspeyres base fija)
    w062 = w["06"] - w["06.1"] - w["06.4"]
    lv["06.2r"] = (w["06"] * lv["06"] - w["06.1"] * lv["06.1"] - w["06.4"] * lv["06.4.1"]) / w062
    # nivel general sin los tres rubros
    wx = w["11.1"] + w["10"] + w062
    lv["NGex"] = (lv["0"] - w["11.1"] * lv["11.1"] - w["10"] * lv["10"] - w062 * lv["06.2r"]) / (1 - wx)
    # nivel general sin cada rubro por separado
    lv["NGex_11.1"] = (lv["0"] - w["11.1"] * lv["11.1"]) / (1 - w["11.1"])
    lv["NGex_10"] = (lv["0"] - w["10"] * lv["10"]) / (1 - w["10"])
    lv["NGex_06.2r"] = (lv["0"] - w062 * lv["06.2r"]) / (1 - w062)
    v = lv.pct_change(fill_method=None) * 100
    keep = {"0": "ng", "01": "alim_div", "01.1": "alim", "11.1": "rest", "10": "educ", "06.2r": "med",
            "06.4.1": "prep", "06.1": "medic", "B": "bienes", "S": "servicios", "Núcleo": "nucleo",
            "NGex": "ngex", "NGex_11.1": "ngex_rest", "NGex_10": "ngex_educ", "NGex_06.2r": "ngex_med"}
    v = v[[k for k in keep if k in v]].rename(columns=keep)
    sal = salarios()
    vs = sal.pct_change(fill_method=None) * 100
    df = v.join(vs, how="outer")
    df = df[df.index >= "2013-01-01"]
    return df, {"w_rest": w["11.1"], "w_educ": w["10"], "w_med": w062, "lv_check": float(lv["06.2r"].iloc[0])}


if __name__ == "__main__":
    df, meta = construir()
    print(meta)
    out = Path("work/base_servicios.csv")
    df.to_csv(out)
    print(df.loc["2017-01":].describe().T.round(2).to_string())
    print(df.tail(5).round(2).T.to_string())
