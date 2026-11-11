"""Registros cargados a mano con fuente: precios regulados (por norma) y mediciones de terceros
(consultoras, universidades). Nada se completa: cada valor rige solo en las fechas que dice su
fila y lo que no está cargado queda sin dato."""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta

import polars as pl

from .config import REGIONES, Config, ponderadores_clase, ponderadores_region
from .indice import NACIONAL

log = logging.getLogger(__name__)


def tarifas(cfg: Config) -> pl.DataFrame:
    ruta = cfg.dir_config / "regulados" / "tarifas.csv"
    if not ruta.exists():
        return pl.DataFrame()
    return pl.read_csv(ruta, schema_overrides={"codigo": pl.Utf8, "valor": pl.Float64})


def _meses(desde: date, hasta: date) -> list[str]:
    out, d = [], date(desde.year, desde.month, 1)
    while d <= hasta:
        out.append(d.strftime("%Y-%m"))
        d = date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1)
    return out


def precio_mensual(t: pl.DataFrame) -> pl.DataFrame:
    """Precio promedio de cada rubro, región y concepto en cada mes, ponderado por los días en que
    rige cada norma. Un mes entra solo si TODOS sus días tienen una tarifa vigente cargada."""
    filas = []
    for (rubro, codigo, region, concepto), g in t.group_by(["rubro", "codigo", "region", "concepto"]):
        vig = [(date.fromisoformat(a), date.fromisoformat(b), v) for a, b, v in
               g.select("vigente_desde", "vigente_hasta", "valor").iter_rows()]
        if not vig:
            continue
        for mes in _meses(min(v[0] for v in vig), max(v[1] for v in vig)):
            a, m = map(int, mes.split("-"))
            d, suma, n_dias, total = date(a, m, 1), 0.0, 0, 0
            while d.month == m:
                total += 1
                v = [x for x in vig if x[0] <= d <= x[1]]
                if v:
                    suma += v[-1][2]
                    n_dias += 1
                d += timedelta(days=1)
            if n_dias == total:
                filas.append({"rubro": rubro, "codigo": codigo, "region": region, "concepto": concepto,
                              "mes": mes, "precio": suma / total})
    return pl.DataFrame(filas, infer_schema_length=None) if filas else pl.DataFrame()


def indice_regulados(cfg: Config) -> pl.DataFrame | None:
    """Índice mensual por clase y región (formato de indices_mensual.csv). La variación de un mes
    existe solo si el mes y el anterior tienen precio; si no, ese mes queda sin dato."""
    t = tarifas(cfg)
    if t.is_empty():
        return None
    pm = precio_mensual(t)
    if pm.is_empty():
        return None
    # "Todas" = la misma tarifa en las seis regiones del IPC (norma de alcance nacional)
    pm = pl.concat([pm.filter(pl.col("region") != "Todas"),
                    *[pm.filter(pl.col("region") == "Todas").with_columns(pl.lit(r).alias("region")) for r in REGIONES]])
    rel = (pm.sort("codigo", "region", "concepto", "mes")
           .with_columns(pl.col("mes").shift(1).over("codigo", "region", "concepto").alias("mes0"),
                         pl.col("precio").shift(1).over("codigo", "region", "concepto").alias("p0")))
    consecutivo = pl.Series([m0 is not None and _meses(date.fromisoformat(m0 + "-01"), date.fromisoformat(m + "-01"))[1:] == [m]
                             for m0, m in rel.select("mes0", "mes").iter_rows()])
    rel = rel.with_columns(pl.when(consecutivo).then((pl.col("precio") / pl.col("p0")).log()).otherwise(None).alias("lr"))
    g = rel.group_by("codigo", "region", "mes").agg(pl.col("lr").mean().alias("lr"), pl.len().alias("n"))
    filas = []
    for (codigo, region), s in g.sort("mes").group_by(["codigo", "region"], maintain_order=True):
        nivel, tramo = 100.0, -1
        for mes, lr, n in s.select("mes", "lr", "n").iter_rows():
            if lr is None:  # sin el mes anterior no hay variación: empieza un tramo nuevo
                nivel, tramo, var = 100.0, tramo + 1, None
            else:
                nivel *= math.exp(lr)
                var = (math.exp(lr) - 1) * 100
            filas.append({"periodo": mes, "region": region, "codigo": codigo, "indice": nivel, "n_productos": n,
                          "tramo": tramo, "var_pct": var})
    if not filas:
        return None
    df = pl.DataFrame(filas, infer_schema_length=None)
    w_reg = ponderadores_region(cfg)
    pond = ponderadores_clase(cfg)
    w_cl = {(c, r): w for c, r, w in pond.select("codigo", "region", "ponderacion").iter_rows()}
    nac = []
    for (codigo, periodo), s in df.group_by(["codigo", "periodo"]):
        if s.height == len(REGIONES) and s["tramo"].n_unique() == 1:
            w = [w_reg[r] * w_cl.get((codigo, r), 0) for r in s["region"]]
            if sum(w):
                nac.append({"periodo": periodo, "region": NACIONAL, "codigo": codigo,
                            "indice": sum(i * x for i, x in zip(s["indice"], w)) / sum(w), "n_productos": int(s["n_productos"].sum()),
                            "tramo": int(s["tramo"][0]),
                            "var_pct": None if s["var_pct"].null_count() else (sum((1 + v / 100) * x for v, x in zip(s["var_pct"], w)) / sum(w) - 1) * 100})
    df = pl.concat([df, pl.DataFrame(nac, infer_schema_length=None)]) if nac else df
    nombres = dict(pond.select("codigo", "nombre").unique("codigo").iter_rows())
    return df.sort("codigo", "region", "periodo").with_columns(
        pl.lit("clase").alias("nivel"), pl.col("codigo").replace_strict(nombres, default="").alias("nombre"),
        pl.lit(None, dtype=pl.Float64).alias("var_interanual_pct"), pl.lit(False).alias("parcial"),
        pl.lit(False).alias("salto"), pl.col("periodo").shift(1).over("codigo", "region").alias("periodo_anterior"),
        pl.lit(None, dtype=pl.Float64).alias("var_salto_pct"), pl.col("tramo").cast(pl.Int32))


def datos_regulados(cfg: Config) -> dict:
    t = tarifas(cfg)
    if t.is_empty():
        return {}
    idx = indice_regulados(cfg)
    return {"tarifas": t.to_dicts(), "indice": [] if idx is None else idx.to_dicts()}


def externos(cfg: Config) -> list[dict]:
    ruta = cfg.dir_config / "externos.csv"
    if not ruta.exists():
        return []
    return pl.read_csv(ruta, schema_overrides={"valor": pl.Float64, "periodo": pl.Utf8, "region": pl.Utf8}).to_dicts()


def canasta_servicios(cfg: Config) -> dict:
    """Costo mensual de la canasta de servicios (config/regulados/servicios.yaml). Solo usa
    componentes con cantidad y tarifa con fuente; un mes tiene total solo si están todos."""
    import yaml
    ruta = cfg.dir_config / "regulados" / "servicios.yaml"
    if not ruta.exists():
        return {}
    conf = yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}
    comps = conf.get("componentes", [])
    t = tarifas(cfg)
    pm = precio_mensual(t) if not t.is_empty() else pl.DataFrame()
    precios = {}
    if not pm.is_empty():
        for concepto, mes, precio in pm.select("concepto", "mes", "precio").iter_rows():
            precios.setdefault(concepto, {})[mes] = precio
    # Un componente es una tarifa por una cantidad, o varias `partes` que se suman (una factura:
    # cargo fijo x 1 + cargo variable x consumo). Sin todas sus partes con tarifa ese mes, no hay valor.
    def partes(c):
        if c.get("partes"):
            return [(x.get("concepto"), x.get("cantidad")) for x in c["partes"]]
        return [(c.get("concepto"), c.get("cantidad"))]

    def completo(c):
        return all(con and cant for con, cant in partes(c))

    con_tarifa = [c for c in comps if any(con for con, _ in partes(c))]
    activos = [c for c in comps if not c.get("pendiente") and completo(c)]
    meses = sorted({m for c in con_tarifa for con, _ in partes(c) if con for m in precios.get(con, {})})
    filas = []
    for mes in meses:
        costo, valor = {}, {}
        for c in con_tarifa:
            ps = [(precios.get(con, {}).get(mes) if con else None, cant) for con, cant in partes(c)]
            if c.get("partes"):   # factura: se muestra el importe del componente
                costo[c["id"]] = sum(p * q for p, q in ps) if completo(c) and all(p is not None for p, _ in ps) else None
                valor[c["id"]] = costo[c["id"]] if c in activos else None
            else:
                costo[c["id"]] = ps[0][0]
                valor[c["id"]] = ps[0][0] * ps[0][1] if (c in activos and ps[0][0] is not None) else None
        total = (sum(valor[c["id"]] for c in activos)
                 if activos and len(activos) == len(comps) and all(valor.get(c["id"]) is not None for c in activos) else None)
        filas.append({"mes": mes, "precios": costo, "valores": valor, "total": total})
    for a, b in zip(filas, filas[1:]):
        if a["total"] and b["total"]:
            b["var"] = (b["total"] / a["total"] - 1) * 100
    return {"referencia": conf.get("referencia"), "componentes": comps, "meses": filas,
            "completa": bool(activos) and len(activos) == len(comps)}
