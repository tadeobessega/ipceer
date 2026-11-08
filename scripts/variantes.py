"""Compara variantes de método del IPCEER contra el INDEC en el tramo 2016-2018.

Variantes (todas con los mismos datos de SEPA y los mismos ponderadores):
  base        el método actual: Jevons de los mismos productos entre un mes y el anterior
              (precio mensual = promedio de los días del mes), encadenado.
  A           base sin carnes, pescados, frutas ni verduras (lo que SEPA no mide bien: el INDEC
              los releva frescos, por kilo, en carnicerías y verdulerías).
  B1          base fija: cada mes contra diciembre de 2016, sin encadenar.
  B2          base + reemplazos comparables y ajuste por cantidad, como hace el INDEC: cuando un
              producto de una cadena deja de aparecer y aparece otro de la misma marca y clase,
              se compara el precio por unidad (kg, litro, unidad) del que sale con el que entra.
  C           base sin las cadenas con precios "quietos" (proporción de productos que no cambian
              de precio en 3 meses seguidos por encima de un umbral).
  A+B2+C      las tres correcciones juntas.

Contra qué: la réplica del INDEC con las mismas clases y ponderadores (índices de las aperturas
publicadas). Medidas: acumulado, error medio, RMSE, correlación en el mismo mes y con un mes de
rezago (IPCEER del mes t contra INDEC del mes t+1).

Uso: python scripts/variantes.py [--umbral-quietos 0.6]  -> resultados/variantes.json
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import polars as pl

from ipceer.config import REGIONES, cargar_config, ponderadores_clase, ponderadores_region
from ipceer.indicadores import _con_codigo
from ipceer.validacion import leer_indec, replica_indec

CLAVE = ["id_comercio", "id_bandera", "id_producto", "cantidad", "unidad", "region"]
FRESCOS = ("01.1.2", "01.1.3", "01.1.6", "01.1.7")
BASE_FIJA = "2016-12"
DESDE, HASTA = "2016-06", "2018-03"
UNI = {"gr": ("kg", 0.001), "kg": ("kg", 1.0), "ml": ("lt", 0.001), "cc": ("lt", 0.001), "lt": ("lt", 1.0),
       "un": ("un", 1.0), "mt": ("mt", 1.0)}


def cargar(ruta: Path, codigos: set[str], min_dias: int) -> pl.DataFrame:
    df = pl.read_parquet(ruta, columns=[*CLAVE, "precio_lista", "n_dias", "marca", "clase"])
    df = df.filter(pl.col("precio_lista").is_not_nan(), pl.col("precio_lista") > 0, pl.col("n_dias") >= min_dias)
    df = _con_codigo(df, codigos)
    u = df["unidad"].to_list()
    fam = [UNI.get((x or "").lower(), (None, None)) for x in u]
    return df.with_columns(
        pl.Series("u_fam", [f[0] for f in fam], dtype=pl.Utf8),
        (pl.col("cantidad").cast(pl.Float64, strict=False) * pl.Series([f[1] for f in fam], dtype=pl.Float64)).alias("u_cant"),
    ).with_columns((pl.col("precio_lista") / pl.col("u_cant")).alias("p_unit"))


def rel_matched(p0: pl.DataFrame, p1: pl.DataFrame, lim) -> pl.DataFrame:
    return (p0.select(*CLAVE, pl.col("precio_lista").alias("a"))
            .join(p1.select(*CLAVE, "codigo", pl.col("precio_lista").alias("b")), on=CLAVE)
            .with_columns((pl.col("b") / pl.col("a")).log().alias("lr"))
            .filter(pl.col("lr").is_between(math.log(lim[0]), math.log(lim[1])))
            .select("codigo", "region", "id_comercio", "id_bandera", "lr"))


def rel_reemplazos(p0: pl.DataFrame, p1: pl.DataFrame, lim) -> pl.DataFrame:
    """Productos que salen y entran en la misma cadena, región, clase y marca: precio por unidad."""
    k = ["id_comercio", "id_bandera", "region", "codigo", "marca", "u_fam"]
    sale = p0.join(p1.select(CLAVE), on=CLAVE, how="anti").filter(pl.col("marca").is_not_null(), pl.col("p_unit") > 0)
    entra = p1.join(p0.select(CLAVE), on=CLAVE, how="anti").filter(pl.col("marca").is_not_null(), pl.col("p_unit") > 0)
    a = sale.group_by(k).agg(pl.col("p_unit").log().mean().alias("la"), pl.len().alias("na"))
    b = entra.group_by(k).agg(pl.col("p_unit").log().mean().alias("lb"), pl.len().alias("nb"))
    j = a.join(b, on=k).with_columns((pl.col("lb") - pl.col("la")).alias("lr"),
                                     pl.min_horizontal("na", "nb").alias("n"))
    j = j.filter(pl.col("lr").is_between(math.log(lim[0]), math.log(lim[1])))
    # cada reemplazo cuenta tantas veces como pares sale/entra
    return j.select("codigo", "region", "id_comercio", "id_bandera", "lr", "n").with_columns(
        pl.int_ranges(0, pl.col("n")).alias("_r")).explode("_r").drop("_r", "n")


def agregar(rel: pl.DataFrame, pond: pl.DataFrame, wr: dict, minp: int) -> dict:
    """logrel de clase por región (mín. productos), Laspeyres de un mes solo con clases con datos,
    nacional ponderando regiones. Devuelve {'Nacional': lr, region: lr}."""
    c = rel.group_by("codigo", "region").agg(pl.col("lr").mean(), pl.len().alias("n")).filter(pl.col("n") >= minp)
    c = c.join(pond, on=["codigo", "region"])
    if c.is_empty():
        return {}
    r = c.group_by("region").agg(((pl.col("lr") * pl.col("ponderacion")).sum() / pl.col("ponderacion").sum()).alias("lr"))
    out = dict(r.iter_rows())
    if all(x in out for x in REGIONES):
        out["Nacional"] = sum(out[x] * wr[x] for x in REGIONES) / sum(wr[x] for x in REGIONES)
    return out


def quietos(meses: list[str], datos: dict) -> pl.DataFrame:
    """Por cadena, en una ventana de 4 meses: proporción de productos con el mismo precio en los 4."""
    ps = [datos[m].select(*CLAVE, pl.col("precio_lista").round(2).alias(f"p{j}")) for j, m in enumerate(meses)]
    j = ps[0]
    for p in ps[1:]:
        j = j.join(p, on=CLAVE)
    j = j.with_columns(((pl.col("p0") == pl.col("p1")) & (pl.col("p1") == pl.col("p2")) & (pl.col("p2") == pl.col("p3"))).alias("q"))
    return j.group_by("id_comercio", "id_bandera").agg(pl.col("q").mean().alias("quietos"), pl.len().alias("n"))


def medir(propio: dict[str, float], oficial: dict[str, float]) -> dict:
    ms = sorted(m for m in propio if m in oficial)
    a = [propio[m] for m in ms]
    b = [oficial[m] for m in ms]
    if len(ms) < 3:
        return {"n": len(ms)}

    def corr(x, y):
        mx, my = sum(x) / len(x), sum(y) / len(y)
        sx = math.sqrt(sum((v - mx) ** 2 for v in x)); sy = math.sqrt(sum((v - my) ** 2 for v in y))
        return sum((u - mx) * (v - my) for u, v in zip(x, y)) / (sx * sy) if sx and sy else None

    sig = [(propio[m], oficial[n]) for m, n in zip(ms, ms[1:])]
    acum = lambda v: (math.prod(1 + x / 100 for x in v) - 1) * 100
    d = [x - y for x, y in zip(a, b)]
    return {"n": len(ms), "desde": ms[0], "hasta": ms[-1], "acum_propio": round(acum(a), 2), "acum_indec": round(acum(b), 2),
            "sesgo": round(sum(d) / len(d), 3), "rmse": round(math.sqrt(sum(x * x for x in d) / len(d)), 3),
            "r_mismo_mes": round(corr(a, b), 3), "r_un_mes_antes": round(corr([s[0] for s in sig], [s[1] for s in sig]), 3),
            "meses": [{"mes": m, "propio": round(x, 3), "indec": round(y, 3)} for m, x, y in zip(ms, a, b)]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--umbral-quietos", type=float, default=0.6)
    args, _ = ap.parse_known_args()
    cfg = cargar_config()
    ic, lim = cfg["indice"], (cfg["limpieza"]["relativo_min"], cfg["limpieza"]["relativo_max"])
    pond_all = ponderadores_clase(cfg).filter(pl.col("ponderacion") > 0)
    codigos = set(pond_all["codigo"].unique().to_list())
    wr = ponderadores_region(cfg)
    rutas = sorted(p for p in cfg.ruta("panel_mensual").glob("*.parquet") if DESDE <= p.stem <= HASTA)
    minp = ic["min_productos_clase"]
    pond = pond_all.select("codigo", "region", "ponderacion")
    sin_frescos_f = lambda cods: [c for c in cods if not c.startswith(FRESCOS)]
    # primera pasada: precios quietos por cadena (ventana de 4 meses, poca memoria)
    ventana, filas_q = [], []
    for r in rutas:
        ventana.append((r.stem, cargar(r, codigos, ic["min_dias_mes"]).select(*CLAVE, "precio_lista")))
        ventana = ventana[-4:]
        if len(ventana) == 4:
            filas_q.append(quietos([m for m, _ in ventana], dict(ventana)))
    q = (pl.concat(filas_q).group_by("id_comercio", "id_bandera")
         .agg((pl.col("quietos") * pl.col("n")).sum() / pl.col("n").sum(), pl.col("n").sum()).sort("quietos", descending=True))
    malas = set(map(tuple, q.filter(pl.col("quietos") > args.umbral_quietos, pl.col("n") >= 500)
                    .select("id_comercio", "id_bandera").iter_rows()))

    def filtrar_c(r):
        return r.filter(~pl.concat_str(["id_comercio", "id_bandera"], separator="|").is_in([f"{a}|{b}" for a, b in malas]))

    var = {k: {} for k in ("base", "A", "B1", "B2", "C", "A+B2+C")}
    usados: set[str] = set()
    base_fija = None
    nivel = {}
    prev = None
    for r in rutas:
        act = cargar(r, codigos, ic["min_dias_mes"])
        usados |= set(act["codigo"].unique().to_list())
        m1 = r.stem
        if m1 == BASE_FIJA:
            base_fija = act
        elif base_fija is not None:
            out = agregar(rel_matched(base_fija, act, (0.2, 5.0)), pond, wr, minp)
            if "Nacional" in out:
                nivel[m1] = math.exp(out["Nacional"])
        if prev is not None:
            rm = rel_matched(prev, act, lim)
            rb2 = pl.concat([rm, rel_reemplazos(prev, act, lim)])
            pond_a = pond.filter(~pl.col("codigo").str.starts_with(FRESCOS[0]) & ~pl.col("codigo").str.starts_with(FRESCOS[1])
                                 & ~pl.col("codigo").str.starts_with(FRESCOS[2]) & ~pl.col("codigo").str.starts_with(FRESCOS[3]))
            for k, rel, pnd in (("base", rm, pond), ("A", rm, pond_a), ("B2", rb2, pond), ("C", filtrar_c(rm), pond),
                                ("A+B2+C", filtrar_c(rb2), pond_a)):
                out = agregar(rel, pnd, wr, minp)
                if "Nacional" in out:
                    var[k][m1] = (math.exp(out["Nacional"]) - 1) * 100
        prev = act
    anterior = 1.0
    for m in sorted(nivel):
        var["B1"][m] = (nivel[m] / anterior - 1) * 100
        anterior = nivel[m]
    usados = sorted(usados)
    sin_frescos = sin_frescos_f(usados)

    indec = leer_indec(Path("data/indec"))
    def oficial(cods):
        r = replica_indec(indec, cfg, cods).filter(pl.col("region") == "Nacional").sort("mes")
        idx = dict(r.select("mes", "indice").iter_rows())
        ms = sorted(idx)
        return {b: (idx[b] / idx[a] - 1) * 100 for a, b in zip(ms, ms[1:])}
    of_todo, of_a = oficial(usados), oficial(sin_frescos)
    res = {}
    for k, v in var.items():
        res[k] = medir(v, of_a if k in ("A", "A+B2+C") else of_todo)
    salida = {"variantes": res, "umbral_quietos": args.umbral_quietos,
              "cadenas_quietas": [{"cadena": f"{a}-{b}", "quietos": round(x, 3), "n": n}
                                  for a, b, x, n in q.iter_rows()],
              "excluidas_C": sorted(f"{a}-{b}" for a, b in malas), "frescos_excluidos_A": list(FRESCOS)}
    Path("resultados/variantes.json").write_text(json.dumps(salida, ensure_ascii=False, indent=1), encoding="utf-8")
    for k, v in res.items():
        print(f"{k:8s} n={v.get('n')} acum {v.get('acum_propio')} vs {v.get('acum_indec')}  sesgo {v.get('sesgo')}  "
              f"rmse {v.get('rmse')}  r {v.get('r_mismo_mes')}  r(t,t+1) {v.get('r_un_mes_antes')}")
    print(q.head(15))


if __name__ == "__main__":
    main()
