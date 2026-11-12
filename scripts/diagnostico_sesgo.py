"""Diagnóstico del sesgo a la baja contra el INDEC (prueba 2017-01 a 2018-03).

Calcula, para cada clase de alimentos y bebidas de una región, el índice elemental de varias formas
y las compara con el índice del INDEC de la misma clase. Cada forma prueba una hipótesis sobre la
causa del sesgo:

  base           el método publicado: Jevons de todos los artículos-cadena apareados (precio de lista)
  estrato_igual  Jevons dentro de cada variedad (config/variedades.yaml) y promedio simple entre
                 variedades: prueba si el problema es que las variedades con muchos artículos pesan de más
  estrato_pond   igual, con los pesos aproximados de cada variedad
  solo_variedad  como estrato_pond pero sin la variedad "otros" (prueba errores de clasificación)
  popular        solo códigos de barras que venden al menos 3 cadenas de la región (los más vendidos)
  sin_estancados sin los artículos-cadena cuyo precio no cambió en los 3 meses anteriores
                 (prueba precios desactualizados en SEPA)
  efectivo       precio con promociones
  directo        Jevons de base fija (diciembre de 2016) en lugar de encadenado (prueba deriva)
  valor_unitario media geométrica del precio por kilo o litro de TODOS los artículos de la variedad,
                 apareados o no (prueba sesgo de supervivencia: entradas y salidas de productos)

Agregación de las clases: ponderadores del INDEC de la región llevados con los índices del INDEC del
mes anterior (igual que la réplica). Así la diferencia con el INDEC es solo de método.

Uso (en la raíz del repo):  python scripts/diagnostico_sesgo.py [--region GBA] [--desde 2016-12 --hasta 2018-03]
Salida: resultados/diagnostico_sesgo.json y resultados/diagnostico_sesgo_clases.csv
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from pathlib import Path

import polars as pl
import yaml

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from ipceer.config import cargar_config, ponderadores_clase  # noqa: E402

CLAVE = ["id_comercio", "id_bandera", "id_producto", "region"]
CLASES = ["01.1.1", "01.1.2", "01.1.4", "01.1.5", "01.1.6", "01.1.7", "01.1.8", "01.2.1", "01.2.2"]
VARIANTES = ["base", "estrato_igual", "estrato_pond", "solo_variedad", "popular", "sin_estancados",
             "efectivo", "directo", "valor_unitario"]
A_GRAMOS = {"gr": 1.0, "g": 1.0, "kg": 1000.0, "ml": 1.0, "cc": 1.0, "cm3": 1.0, "lt": 1000.0, "l": 1000.0}


def normalizar(t: str | None) -> str:
    if not t:
        return " "
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " " + re.sub(r"[^a-z0-9]+", " ", t).strip() + " "


def cargar_variedades() -> dict[str, list[dict]]:
    with open(RAIZ / "config" / "variedades.yaml", encoding="utf-8") as f:
        v = yaml.safe_load(f)
    for clase, lista in v.items():
        for x in lista:
            x["_inc"] = re.compile(x["incluir"])
            x["_exc"] = re.compile(x["excluir"]) if x.get("excluir") else None
    return v


def asignar(desc: str, clase: str, var: dict) -> str:
    t = normalizar(desc)
    for x in var.get(clase, []):
        if x["_inc"].search(t) and not (x["_exc"] and x["_exc"].search(t)):
            return x["id"]
    return "otros"


def leer_mes(ruta: Path, region: str) -> pl.DataFrame:
    cols = [*CLAVE, "clase", "precio_lista", "precio_efectivo", "n_dias", "dias_con_datos", "descripcion",
            "cantidad", "unidad", "es_ean"]
    df = pl.read_parquet(ruta, columns=cols).filter(pl.col("region") == region, pl.col("clase").is_in(CLASES))
    dias = int(df["dias_con_datos"][0]) if df.height else 0
    minimo = max(1, min(5, math.ceil(dias / 2)))
    return df.filter(pl.col("n_dias") >= minimo).drop("dias_con_datos")


def indec_clases(cfg, region: str) -> dict[tuple[str, str], float]:
    from ipceer.indec import directorio
    from ipceer.validacion import leer_indec
    ind = leer_indec(directorio(cfg)).filter(pl.col("region") == region)
    return {(c, m): i for c, m, i in ind.select("codigo", "mes", "indice").iter_rows()}


def prev(m: str) -> str:
    a, mm = map(int, m.split("-"))
    return f"{a - (mm == 1)}-{12 if mm == 1 else mm - 1:02d}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="GBA")
    ap.add_argument("--desde", default="2016-12")
    ap.add_argument("--hasta", default="2018-03")
    a = ap.parse_args()
    cfg = cargar_config(RAIZ)
    var = cargar_variedades()
    peso_var = {(c, x["id"]): float(x["peso"]) for c, lista in var.items() for x in lista}
    rutas = [r for r in sorted(cfg.ruta("panel_mensual").glob("*.parquet")) if a.desde <= r.stem <= a.hasta]
    meses = [r.stem for r in rutas]
    print("meses:", meses[0], "a", meses[-1], len(meses), flush=True)

    # 1. paneles con variedad, precio por unidad y cantidad de cadenas por código de barras
    cache_var: dict[tuple[str, str], str] = {}
    paneles = {}
    for r in rutas:
        df = leer_mes(r, a.region)
        pares = df.select("descripcion", "clase").unique()
        nuevos = [(d, c) for d, c in pares.iter_rows() if (d, c) not in cache_var]
        for d, c in nuevos:
            cache_var[(d, c)] = asignar(d, c, var)
        mapa = pl.DataFrame({"descripcion": [k[0] for k in cache_var], "clase": [k[1] for k in cache_var],
                             "variedad": list(cache_var.values())})
        df = df.join(mapa, on=["descripcion", "clase"], how="left")
        df = df.with_columns(
            (pl.col("cantidad").cast(pl.Float64, strict=False)
             * pl.col("unidad").str.to_lowercase().replace_strict(A_GRAMOS, default=None, return_dtype=pl.Float64))
            .alias("gramos"),
            pl.col("id_comercio").n_unique().over("id_producto").alias("n_cadenas"))
        paneles[r.stem] = df
        print(r.stem, df.height, flush=True)

    # 2. estancados: artículo-cadena sin cambio de precio de lista en los 3 meses anteriores
    sin_cambio: dict[str, pl.DataFrame] = {}
    for i, m in enumerate(meses):
        if i < 3:
            sin_cambio[m] = pl.DataFrame(schema={k: pl.Utf8 for k in CLAVE})
            continue
        q = paneles[meses[i - 3]].select(*CLAVE, pl.col("precio_lista").alias("p3"))
        for j in (2, 1):
            q = q.join(paneles[meses[i - j]].select(*CLAVE, pl.col("precio_lista").alias(f"p{j}")), on=CLAVE)
        sin_cambio[m] = q.filter((pl.col("p3") == pl.col("p2")) & (pl.col("p2") == pl.col("p1"))).select(CLAVE)

    def jevons(p0: pl.DataFrame, p1: pl.DataFrame, col: str = "precio_lista") -> pl.DataFrame:
        d = (p0.select(*CLAVE, "clase", "variedad", "n_cadenas", pl.col(col).alias("a"))
             .join(p1.select(*CLAVE, pl.col(col).alias("b")), on=CLAVE)
             .with_columns((pl.col("b") / pl.col("a")).alias("rel"))
             .filter(pl.col("rel").is_between(0.33, 3.0)).with_columns(pl.col("rel").log().alias("lr")))
        return d

    def por_clase(d: pl.DataFrame) -> dict[str, float]:
        return dict(d.group_by("clase").agg(pl.col("lr").mean()).iter_rows())

    def estratos(d: pl.DataFrame, ponderar: bool, sin_otros: bool) -> dict[str, float]:
        if sin_otros:
            d = d.filter(pl.col("variedad") != "otros")
        v = d.group_by("clase", "variedad").agg(pl.col("lr").mean(), pl.len().alias("n")).filter(pl.col("n") >= 3)
        out = {}
        for c in v["clase"].unique().to_list():
            s = v.filter(pl.col("clase") == c)
            num = den = 0.0
            for _c, vv, lr, _n in s.iter_rows():
                w = (peso_var.get((c, vv), 5.0) if vv != "otros" else 5.0) if ponderar else 1.0
                num += w * lr
                den += w
            if den:
                out[c] = num / den
        return out

    def valor_unitario(p0: pl.DataFrame, p1: pl.DataFrame) -> dict[str, float]:
        def gm(p):
            return (p.filter(pl.col("gramos") > 0, pl.col("variedad") != "otros", pl.col("precio_lista") > 0)
                    .with_columns((pl.col("precio_lista") / pl.col("gramos")).log().alias("lpu"))
                    .filter(pl.col("lpu").is_finite())
                    .group_by("clase", "variedad").agg(pl.col("lpu").mean(), pl.len().alias("n")))
        j = gm(p0).join(gm(p1), on=["clase", "variedad"], suffix="_1").filter(pl.col("n") >= 5, pl.col("n_1") >= 5)
        j = j.with_columns((pl.col("lpu_1") - pl.col("lpu")).alias("lr"))
        out = {}
        for c in j["clase"].unique().to_list():
            s = j.filter(pl.col("clase") == c)
            num = sum(peso_var.get((c, v), 5.0) * lr for _c, v, lr in s.select("clase", "variedad", "lr").iter_rows())
            den = sum(peso_var.get((c, v), 5.0) for v in s["variedad"].to_list())
            out[c] = num / den if den else None
        return out

    # cadenas "quietas" en cada par de meses: parte de sus artículos con precio idéntico al mes anterior
    def quietas(d: pl.DataFrame, umbral: float = 0.85) -> list[str]:
        q = d.group_by("id_comercio").agg((pl.col("lr").abs() < 1e-9).mean().alias("p"), pl.len().alias("n"))
        return q.filter(pl.col("p") > umbral, pl.col("n") >= 30)["id_comercio"].to_list()

    def aplicar(d: pl.DataFrame, filtros: set[str], m: str) -> pl.DataFrame:
        if "popular" in filtros:
            d = d.filter(pl.col("n_cadenas") >= 3)
        if "sin_estancados" in filtros:
            d = d.join(sin_cambio[m], on=CLAVE, how="anti")
        if "sin_quietas" in filtros:
            d = d.filter(~pl.col("id_comercio").is_in(quietas(d)))
        return d

    def medir(d: pl.DataFrame, metodo: str) -> dict[str, float]:
        if metodo == "simple":
            return por_clase(d)
        return estratos(d, metodo == "estrato_pond", metodo == "solo_variedad")

    # (encadenado o directo, método dentro de la clase, filtros, columna de precio)
    DEF = {
        "base": ("cadena", "simple", set(), "precio_lista"),
        "estrato_igual": ("cadena", "estrato_igual", set(), "precio_lista"),
        "estrato_pond": ("cadena", "estrato_pond", set(), "precio_lista"),
        "solo_variedad": ("cadena", "solo_variedad", set(), "precio_lista"),
        "popular": ("cadena", "simple", {"popular"}, "precio_lista"),
        "sin_estancados": ("cadena", "simple", {"sin_estancados"}, "precio_lista"),
        "sin_quietas": ("cadena", "simple", {"sin_quietas"}, "precio_lista"),
        "efectivo": ("cadena", "simple", set(), "precio_efectivo"),
        "directo": ("directo", "simple", set(), "precio_lista"),
        "c_estrato_quietas": ("cadena", "estrato_pond", {"sin_quietas"}, "precio_lista"),
        "c_directo_estrato": ("directo", "estrato_pond", set(), "precio_lista"),
        "c_directo_estrato_quietas": ("directo", "estrato_pond", {"sin_quietas"}, "precio_lista"),
        "c_todo": ("directo", "estrato_pond", {"sin_quietas", "popular"}, "precio_lista"),
    }
    VARIANTES[:] = list(DEF) + ["valor_unitario"]
    res = {v: {} for v in VARIANTES}   # variante -> mes -> clase -> logrel
    base0 = meses[0]
    for m0, m1 in zip(meses[:-1], meses[1:]):
        p0, p1 = paneles[m0], paneles[m1]
        for v, (forma, metodo, filtros, col) in DEF.items():
            if forma == "cadena":
                res[v][m1] = medir(aplicar(jevons(p0, p1, col), filtros, m1), metodo)
            else:   # base fija en el primer mes; variación mensual = cociente de índices directos
                d1 = medir(aplicar(jevons(paneles[base0], p1, col), filtros, m1), metodo)
                d0 = (medir(aplicar(jevons(paneles[base0], p0, col), filtros, m0), metodo)
                      if m0 != base0 else {c: 0.0 for c in CLASES})
                res[v][m1] = {c: d1[c] - d0[c] for c in d1 if c in d0}
        res["valor_unitario"][m1] = valor_unitario(p0, p1)
        print("listo", m1, flush=True)

    # 3. comparación con el INDEC: por clase y agregado con los ponderadores del INDEC
    ind = indec_clases(cfg, a.region)
    pond = ponderadores_clase(cfg).filter(pl.col("region") == a.region)
    w = dict(pond.select("codigo", "ponderacion").iter_rows())
    filas, agregado = [], {}
    for v in VARIANTES:
        agregado[v] = {}
        for m in meses[1:]:
            num = den = num_i = 0.0
            for c in CLASES:
                lr = res[v].get(m, {}).get(c)
                ii, ip = ind.get((c, m)), ind.get((c, prev(m)))
                if lr is None or ii is None or ip is None or not w.get(c):
                    continue
                vi = (ii / ip - 1) * 100
                vp = (math.exp(lr) - 1) * 100
                filas.append({"variante": v, "mes": m, "clase": c, "propio": round(vp, 3), "indec": round(vi, 3)})
                ww = w[c] * ip / 100
                num += ww * vp
                num_i += ww * vi
                den += ww
            if den:
                agregado[v][m] = {"propio": num / den, "indec": num_i / den}
    cl = pl.DataFrame(filas)
    resumen = {}
    for v in VARIANTES:
        ms = sorted(agregado[v])
        d = [agregado[v][m]["propio"] - agregado[v][m]["indec"] for m in ms]
        n = len(d)
        if not n:
            continue
        acp = (math.prod(1 + agregado[v][m]["propio"] / 100 for m in ms) - 1) * 100
        aci = (math.prod(1 + agregado[v][m]["indec"] / 100 for m in ms) - 1) * 100
        xs = [agregado[v][m]["propio"] for m in ms]
        ys = [agregado[v][m]["indec"] for m in ms]
        mx, my = sum(xs) / n, sum(ys) / n
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        resumen[v] = {"n": n, "sesgo": round(sum(d) / n, 3), "rmse": round(math.sqrt(sum(x * x for x in d) / n), 3),
                      "mae": round(sum(abs(x) for x in d) / n, 3), "acum_propio": round(acp, 2), "acum_indec": round(aci, 2),
                      "correlacion": round(sxy / math.sqrt(sxx * syy), 3) if sxx and syy else None,
                      "por_clase": {c: {"sesgo": round(float(s), 3), "rmse": round(float(r), 3)} for c, s, r in
                                    cl.filter(pl.col("variante") == v).group_by("clase").agg(
                                        (pl.col("propio") - pl.col("indec")).mean().alias("s"),
                                        ((pl.col("propio") - pl.col("indec")) ** 2).mean().sqrt().alias("r")).sort("clase").iter_rows()}}
    # 4. composición: artículos por variedad y estancamiento por cadena
    ult = paneles[meses[-1]]
    comp = (ult.group_by("clase", "variedad").agg(pl.len().alias("n")).sort("clase", "n", descending=[False, True]))
    estanc = []
    for m in meses[3:]:
        tot = paneles[m].group_by("id_comercio").agg(pl.len().alias("n"))
        sc = sin_cambio[m].group_by("id_comercio").agg(pl.len().alias("quietos"))
        estanc += tot.join(sc, on="id_comercio", how="left").with_columns(pl.lit(m).alias("mes")).to_dicts()
    est = (pl.DataFrame(estanc).group_by("id_comercio").agg(pl.col("quietos").fill_null(0).sum() / pl.col("n").sum())
           .rename({"quietos": "parte_sin_cambio_3m"}).sort("parte_sin_cambio_3m", descending=True))
    salida = {"region": a.region, "desde": meses[1], "hasta": meses[-1], "resumen": resumen,
              "agregado": {v: {m: {k: round(x, 3) for k, x in agregado[v][m].items()} for m in agregado[v]} for v in agregado},
              "composicion_variedades": comp.to_dicts(), "estancados_por_comercio": est.to_dicts()}
    out = cfg.ruta("resultados")
    (out / f"diagnostico_sesgo_{a.region}.json").write_text(json.dumps(salida, ensure_ascii=False, indent=1), encoding="utf-8")
    cl.write_csv(out / f"diagnostico_sesgo_clases_{a.region}.csv")
    for v, r in resumen.items():
        print(f"{v:15s} sesgo {r['sesgo']:+.3f} rmse {r['rmse']:.3f} corr {r['correlacion']} acum {r['acum_propio']} vs {r['acum_indec']}")


if __name__ == "__main__":
    main()
