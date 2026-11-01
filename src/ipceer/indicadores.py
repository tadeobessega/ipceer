"""Indicadores complementarios del IPCEER.

1. Rotación de precios: qué parte de los precios cambió entre dos semanas, cuántos
   subieron y cuántos bajaron, de cuánto fueron los cambios y cuántos tienen promoción.
2. Niveles de precios por cadena: cuánto más cara o más barata es cada cadena que el
   promedio, comparando los mismos productos (mismo código de barras) en la misma región.
3. Niveles de precios por región: cuánto más caro o más barato es cada región que el
   promedio, comparando el mismo producto de la misma cadena en distintas regiones.
4. Costo de la canasta básica alimentaria (CBA) del INDEC en cada cadena.

Los niveles (2 y 3) salen de un modelo de efectos fijos de dos vías (el "country product
dummy" que usan las comparaciones internacionales de precios):

    log p(producto, cadena) = a(producto) + b(cadena)

ponderado con los ponderadores del IPC de cada clase. b(cadena) es el nivel de la cadena
con el producto fijo: no depende de qué productos venda cada una.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from datetime import date

import polars as pl
import yaml

from .clasificador import SIN_CLASIFICAR
from .config import REGIONES, Config, ponderadores_clase, ponderadores_region
from .indice import NACIONAL, resolver_codigo
from .lectura import CLAVE
from .panel import id_semana, rango_semana

log = logging.getLogger(__name__)

CADENA = ["id_comercio", "id_bandera"]
DIVISIONES = ["01", "02", "05", "09", "12"]


# ---------------------------------------------------------------- utilidades

def _params(cfg: Config) -> dict:
    base = {"semanas": 13, "umbral_cambio_pct": 0.5, "min_cadenas_producto": 3,
            "min_productos_cadena": 30, "cobertura_min_cba": 0.6}
    return base | (cfg.datos.get("indicadores") or {})


def _pesos(cfg: Config) -> tuple[pl.DataFrame, set[str]]:
    pond = ponderadores_clase(cfg).filter(pl.col("ponderacion") > 0)
    return pond.select("codigo", "region", "ponderacion"), set(pond["codigo"].unique().to_list())


def _con_codigo(df: pl.DataFrame, codigos: set[str]) -> pl.DataFrame:
    clases = [c for c in df["clase"].unique().to_list() if c and c != SIN_CLASIFICAR]
    mapa = {c: resolver_codigo(c, codigos) for c in clases}
    return (df.filter(pl.col("clase").is_in(list(mapa)))
            .with_columns(pl.col("clase").replace_strict(mapa, default=None, return_dtype=pl.Utf8).alias("codigo"))
            .filter(pl.col("codigo").is_not_null()))


def _cadena_id(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(pl.concat_str(["id_comercio", "id_bandera"], separator="-").alias("cadena_id"))


def nombres_cadenas(cfg: Config) -> dict[str, str]:
    ruta = cfg.ruta("panel_diario").parent / "cadenas.csv"
    if not ruta.exists():
        return {}
    df = pl.read_csv(ruta, schema_overrides={"id_comercio": pl.Utf8, "id_bandera": pl.Utf8, "cadena": pl.Utf8})
    return {f"{a}-{b}": n.strip().title() if n.isupper() else n.strip() for a, b, n in df.iter_rows()}


def efectos_fijos(df: pl.DataFrame, a: list[str], b: list[str], y: str = "y", w: str = "w",
                  iteraciones: int = 40) -> pl.DataFrame:
    """Resuelve y = alfa(a) + beta(b) por mínimos cuadrados ponderados (proyecciones alternadas).

    Devuelve una fila por grupo `b` con su efecto centrado en el promedio simple de los grupos
    (0 = promedio), la suma de pesos y la cantidad de observaciones.
    """
    d = df.select(*a, *b, y, w).with_columns(pl.lit(0.0).alias("_b"))
    for _ in range(iteraciones):
        d = d.with_columns(
            (((pl.col(y) - pl.col("_b")) * pl.col(w)).sum().over(a) / pl.col(w).sum().over(a)).alias("_a"))
        nuevo = (((pl.col(y) - pl.col("_a")) * pl.col(w)).sum().over(b) / pl.col(w).sum().over(b))
        d = d.with_columns(nuevo.alias("_b"))
    ef = d.group_by(b).agg(pl.col("_b").first().alias("efecto"), pl.col(w).sum().alias("peso"),
                           pl.len().alias("n"))
    return ef.with_columns(pl.col("efecto") - pl.col("efecto").mean())


def _semanas_consecutivas(semanas: list[str]) -> list[tuple[str, str]]:
    pares = []
    for s0, s1 in zip(semanas, semanas[1:]):
        l0, _ = rango_semana(s0)
        l1, _ = rango_semana(s1)
        if (l1 - l0).days == 7:
            pares.append((s0, s1))
    return pares


def _semanas_recientes(cfg: Config, n: int) -> list[str]:
    rutas = sorted(cfg.ruta("panel_semanal").glob("*.parquet"))
    semanas = [r.stem for r in rutas]
    # solo el último tramo de semanas consecutivas (sin saltos de más de 4 semanas)
    out = []
    for s in reversed(semanas):
        if out:
            l1, _ = rango_semana(out[-1])
            l0, _ = rango_semana(s)
            if (l1 - l0).days > 28:
                break
        out.append(s)
    return list(reversed(out))[-n:]


# ---------------------------------------------------------------- 1. rotación

def _ultimo_dia(cfg: Config, semana: str) -> date | None:
    lunes, domingo = rango_semana(semana)
    dias = sorted(date.fromisoformat(p.stem) for p in cfg.ruta("panel_diario").glob("*.parquet")
                  if lunes <= date.fromisoformat(p.stem) <= domingo)
    return dias[-1] if dias else None


def rotacion(cfg: Config, semanas: list[str]) -> list[dict]:
    """Rotación de precios entre el último día con datos de cada semana y el de la semana previa.

    Un precio "cambió" si su variación supera ±umbral_cambio_pct (por defecto 0,5%): los
    precios de cada cadena y región son promedios entre sucursales, y por debajo de ese
    umbral el movimiento suele ser ruido de una sucursal.
    """
    p = _params(cfg)
    lim = cfg["limpieza"]
    u = math.log(1 + p["umbral_cambio_pct"] / 100)
    pond, codigos = _pesos(cfg)
    w_reg = ponderadores_region(cfg)
    dir_d = cfg.ruta("panel_diario")
    filas = []
    for s0, s1 in _semanas_consecutivas(semanas):
        d0, d1 = _ultimo_dia(cfg, s0), _ultimo_dia(cfg, s1)
        if d0 is None or d1 is None:
            continue
        a = pl.read_parquet(dir_d / f"{d0.isoformat()}.parquet", columns=[*CLAVE, "precio_lista"])
        b = pl.read_parquet(dir_d / f"{d1.isoformat()}.parquet",
                            columns=[*CLAVE, "precio_lista", "precio_efectivo", "descripcion"])
        cl = pl.read_parquet(cfg.ruta("panel_semanal") / f"{s1}.parquet", columns=[*CLAVE, "clase"])
        par = (a.rename({"precio_lista": "p0"}).join(b.rename({"precio_lista": "p1"}), on=CLAVE)
               .join(cl, on=CLAVE, how="inner")
               .with_columns((pl.col("p1") / pl.col("p0")).alias("rel"))
               .filter(pl.col("rel").is_between(lim["relativo_min"], lim["relativo_max"])))
        par = _con_codigo(par, codigos).with_columns(
            pl.col("rel").log().alias("lr"),
            (pl.col("precio_efectivo") < pl.col("p1") * 0.995).alias("promo"),
            (1 - pl.col("precio_efectivo") / pl.col("p1")).alias("desc"),
        ).with_columns(
            (pl.col("lr") > u).alias("sube"), (pl.col("lr") < -u).alias("baja"))
        celdas = par.group_by("region", "codigo").agg(
            pl.len().alias("n"),
            pl.col("sube").mean().alias("f_s"), pl.col("baja").mean().alias("f_b"),
            pl.col("lr").filter(pl.col("sube")).mean().alias("m_s"),
            pl.col("lr").filter(pl.col("baja")).mean().alias("m_b"),
            pl.col("lr").mean().alias("m"),
            pl.col("promo").mean().alias("f_p"),
            pl.col("desc").filter(pl.col("promo")).mean().alias("m_p"),
        ).join(pond, on=["codigo", "region"], how="inner").with_columns(
            pl.col("region").replace_strict(w_reg, default=0.0, return_dtype=pl.Float64).alias("wr"))

        def resumir(df: pl.DataFrame, w: pl.Expr) -> dict | None:
            df = df.with_columns(w.alias("W"))
            if df.height == 0 or df["W"].sum() <= 0:
                return None
            W = df["W"].sum()
            fs = (df["W"] * df["f_s"]).sum() / W
            fb = (df["W"] * df["f_b"]).sum() / W
            fp = (df["W"] * df["f_p"]).sum() / W

            def media_cond(col: str, f: str) -> float | None:
                x = df.filter(pl.col(col).is_not_null())
                den = (x["W"] * x[f]).sum()
                return (x["W"] * x[f] * x[col]).sum() / den if den > 0 else None
            ms, mb, mp = media_cond("m_s", "f_s"), media_cond("m_b", "f_b"), media_cond("m_p", "f_p")
            return {
                "n": int(df["n"].sum()), "cambia": 100 * (fs + fb), "sube": 100 * fs, "baja": 100 * fb,
                "igual": 100 * (1 - fs - fb),
                "tam_sube": None if ms is None else (math.exp(ms) - 1) * 100,
                "tam_baja": None if mb is None else (math.exp(mb) - 1) * 100,
                "var": (math.exp((df["W"] * df["m"]).sum() / W) - 1) * 100,
                "promo": 100 * fp, "desc_promo": None if mp is None else 100 * mp,
            }

        base = {"semana": s1, "semana_previa": s0, "desde": d0.isoformat(), "hasta": d1.isoformat()}
        for reg in [NACIONAL, *REGIONES]:
            df = celdas if reg == NACIONAL else celdas.filter(pl.col("region") == reg)
            w = pl.col("ponderacion") * (pl.col("wr") if reg == NACIONAL else 1)
            r = resumir(df, w)
            if r:
                filas.append(base | {"region": reg, "codigo": "TOTAL"} | r)
            if reg == NACIONAL:
                for div in DIVISIONES:
                    r = resumir(df.filter(pl.col("codigo").str.starts_with(div)), w)
                    if r:
                        filas.append(base | {"region": reg, "codigo": div} | r)
        # difusión: parte de la canasta (por ponderación) cuyas clases subieron en la semana
        nac = celdas.with_columns((pl.col("ponderacion") * pl.col("wr")).alias("W")).group_by("codigo").agg(
            ((pl.col("W") * pl.col("m")).sum() / pl.col("W").sum()).alias("m"), pl.col("W").sum())
        if nac.height:
            dif = 100 * nac.filter(pl.col("m") > 0)["W"].sum() / nac["W"].sum()
            for f in filas:
                if f["semana"] == s1 and f["region"] == NACIONAL and f["codigo"] == "TOTAL":
                    f["difusion"] = dif
        log.info("Rotación %s: %d precios apareados", s1, par.height)
    return filas


# ---------------------------------------------------------------- 2 y 3. niveles

def _panel_semana(cfg: Config, semana: str, codigos: set[str]) -> pl.DataFrame:
    df = pl.read_parquet(cfg.ruta("panel_semanal") / f"{semana}.parquet")
    df = _con_codigo(df, codigos)
    col = "precio_lista"
    return _cadena_id(df).filter(pl.col(col) > 0).with_columns(pl.col(col).log().alias("y"))


def niveles_cadenas(cfg: Config, semanas: list[str]) -> list[dict]:
    """Nivel de precios de cada cadena (100 = promedio de las cadenas) por región y semana,
    con los mismos productos (código de barras) en la misma región."""
    p = _params(cfg)
    pond, codigos = _pesos(cfg)
    w_reg = ponderadores_region(cfg)
    nombres = nombres_cadenas(cfg)
    filas = []
    for s in semanas:
        df = _panel_semana(cfg, s, codigos)
        df = df.filter(pl.col("es_ean").cast(pl.Utf8).is_in(["1", "true", "True", "S", "SI"]))
        df = df.with_columns(pl.n_unique("cadena_id").over("id_producto", "cantidad", "unidad", "region").alias("nc"))
        df = df.filter(pl.col("nc") >= p["min_cadenas_producto"])
        if df.height == 0:
            continue
        df = df.join(pond, on=["codigo", "region"], how="inner").with_columns(
            (pl.col("ponderacion") / pl.len().over("region", "codigo")).alias("w"))
        prod = ["id_producto", "cantidad", "unidad"]
        for reg in [NACIONAL, *REGIONES]:
            if reg == NACIONAL:
                d = df.with_columns(
                    pl.col("w") * pl.col("region").replace_strict(w_reg, default=0.0, return_dtype=pl.Float64))
                ef = efectos_fijos(d, [*prod, "region"], ["cadena_id"])
            else:
                d = df.filter(pl.col("region") == reg)
                if d["cadena_id"].n_unique() < 2:
                    continue
                ef = efectos_fijos(d, prod, ["cadena_id"])
            ef = ef.filter(pl.col("n") >= p["min_productos_cadena"])
            if ef.height < 2:
                continue
            ef = ef.with_columns(pl.col("efecto") - pl.col("efecto").mean())
            for r in ef.sort("efecto").iter_rows(named=True):
                filas.append({"semana": s, "region": reg, "cadena_id": r["cadena_id"],
                              "cadena": nombres.get(r["cadena_id"], r["cadena_id"]),
                              "nivel": math.exp(r["efecto"]) * 100, "n": r["n"]})
    _rankear(filas, "nivel")
    return filas


def _rankear(filas: list[dict], campo: str) -> None:
    """Puesto (1 = más barata) y cambio de puesto respecto de la semana previa, por región."""
    grupos: dict[tuple, list[dict]] = {}
    for f in filas:
        grupos.setdefault((f["region"], f["semana"]), []).append(f)
    previo: dict[tuple, int] = {}
    for (reg, sem) in sorted(grupos, key=lambda k: (k[0], k[1])):
        g = sorted([f for f in grupos[(reg, sem)] if f.get(campo) is not None], key=lambda f: f[campo])
        for i, f in enumerate(g, start=1):
            f["puesto"] = i
            ant = previo.get((reg, f["cadena_id"]))
            f["cambio_puesto"] = None if ant is None else ant - i
        for f in g:
            previo[(reg, f["cadena_id"])] = f["puesto"]


def niveles_regiones(cfg: Config, semanas: list[str]) -> list[dict]:
    """Nivel de precios de cada región (100 = promedio ponderado del país) por semana, con
    el mismo producto de la misma cadena en distintas regiones."""
    pond, codigos = _pesos(cfg)
    w_reg = ponderadores_region(cfg)
    pond_nac = (pond.with_columns(pl.col("region").replace_strict(w_reg, default=0.0, return_dtype=pl.Float64).alias("wr"))
                .group_by("codigo").agg(((pl.col("ponderacion") * pl.col("wr")).sum() / pl.col("wr").sum()).alias("pn")))
    filas = []
    for s in semanas:
        df = _panel_semana(cfg, s, codigos)
        clave = ["cadena_id", "id_producto", "cantidad", "unidad"]
        df = df.with_columns(pl.n_unique("region").over(clave).alias("nr")).filter(pl.col("nr") >= 2)
        if df.height == 0 or df["region"].n_unique() < 2:
            continue
        df = df.join(pond_nac, on="codigo").with_columns((pl.col("pn") / pl.len().over("codigo")).alias("w"))
        ef = efectos_fijos(df, clave, ["region"])
        ef = ef.with_columns(pl.col("region").replace_strict(w_reg, default=0.0, return_dtype=pl.Float64).alias("wr"))
        centro = (ef["efecto"] * ef["wr"]).sum() / ef["wr"].sum()
        for r in ef.iter_rows(named=True):
            filas.append({"semana": s, "region": r["region"], "nivel": math.exp(r["efecto"] - centro) * 100,
                          "n": r["n"]})
    return filas


def inflacion_cadenas(cfg: Config, semanas: list[str]) -> list[dict]:
    """Variación semanal de precios de cada cadena (Jevons por clase, ponderada como el IPCEER)."""
    pond, codigos = _pesos(cfg)
    w_reg = ponderadores_region(cfg)
    lim = cfg["limpieza"]
    nombres = nombres_cadenas(cfg)
    filas = []
    for s0, s1 in _semanas_consecutivas(semanas):
        a = _panel_semana(cfg, s0, codigos).select(*CLAVE, "cadena_id", pl.col("y").alias("y0"))
        b = _panel_semana(cfg, s1, codigos).select(*CLAVE, "codigo", pl.col("y").alias("y1"))
        par = a.join(b, on=CLAVE).with_columns((pl.col("y1") - pl.col("y0")).alias("lr")).filter(
            pl.col("lr").is_between(math.log(lim["relativo_min"]), math.log(lim["relativo_max"])))
        cel = par.group_by("cadena_id", "region", "codigo").agg(pl.col("lr").mean(), pl.len().alias("n")).join(
            pond, on=["codigo", "region"]).with_columns(
            (pl.col("ponderacion") * pl.col("region").replace_strict(w_reg, default=0.0, return_dtype=pl.Float64)).alias("W"))
        for (cid,), g in cel.group_by(["cadena_id"]):
            if g["n"].sum() < 30:
                continue
            filas.append({"semana": s1, "cadena_id": cid, "cadena": nombres.get(cid, cid),
                          "var": (math.exp((g["W"] * g["lr"]).sum() / g["W"].sum()) - 1) * 100,
                          "n": int(g["n"].sum())})
    return filas


# ---------------------------------------------------------------- 4. CBA por cadena

def _normalizar(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().upper()
    return re.sub(r"\s+", " ", s).strip()


UNIDADES = {"g": ("kg", 0.001), "gr": ("kg", 0.001), "grs": ("kg", 0.001), "grm": ("kg", 0.001),
            "gramos": ("kg", 0.001), "kg": ("kg", 1.0), "kgs": ("kg", 1.0), "kilo": ("kg", 1.0),
            "ml": ("l", 0.001), "cc": ("l", 0.001), "cm3": ("l", 0.001), "l": ("l", 1.0),
            "lt": ("l", 1.0), "lts": ("l", 1.0), "litro": ("l", 1.0), "un": ("un", 1.0),
            "u": ("un", 1.0), "uni": ("un", 1.0), "unid": ("un", 1.0), "unidad": ("un", 1.0)}


def cargar_cba(cfg: Config) -> dict:
    with open(cfg.dir_config / "cba.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _cantidad_base(cantidad: str, unidad: str, base: str, gpu: float | None) -> float | None:
    try:
        q = float(cantidad)
    except (TypeError, ValueError):
        return None
    tipo = UNIDADES.get((unidad or "").strip().lower().rstrip("."))
    if q <= 0 or tipo is None:
        return None
    u, f = tipo
    if u == "un":
        return q * gpu / 1000 if gpu else None
    if u == "kg" and base == "l" or u == "l" and base == "kg":
        return q * f  # gramos y cm3 se toman equivalentes (leche, aceite, vinagre)
    return q * f


def precios_cba(cfg: Config, panel: pl.DataFrame) -> pl.DataFrame:
    """Precio por kilo o litro de cada producto que entra en un componente de la CBA."""
    cba = cargar_cba(cfg)
    df = panel.select(*CLAVE, "clase", "descripcion", "precio_lista").with_columns(
        pl.col("descripcion").map_elements(_normalizar, return_dtype=pl.Utf8).alias("desc"))
    partes = []
    for c in cba["componentes"]:
        m = df.filter(pl.col("desc").str.contains(c["incluir"]))
        if c.get("excluir"):
            m = m.filter(~pl.col("desc").str.contains(c["excluir"]))
        if c.get("clases"):
            m = m.filter(pl.any_horizontal([pl.col("clase").str.starts_with(x) for x in c["clases"]]))
        if m.height == 0:
            continue
        q = [
            _cantidad_base(a, b, c["base"], c.get("gramos_por_unidad"))
            for a, b in zip(m["cantidad"].to_list(), m["unidad"].to_list())
        ]
        m = m.with_columns(pl.Series("q", q, dtype=pl.Float64)).filter(pl.col("q").is_not_null() & (pl.col("q") > 0))
        m = m.with_columns((pl.col("precio_lista") / pl.col("q")).alias("pu"), pl.lit(c["id"]).alias("componente"))
        # descarta precios por kilo absurdos (presentaciones mal cargadas)
        m = m.with_columns(pl.col("pu").median().over("componente", "region").alias("_med")).filter(
            pl.col("pu").is_between(pl.col("_med") / 5, pl.col("_med") * 5)).drop("_med")
        partes.append(m.select(*CLAVE, "componente", "pu"))
    return _cadena_id(pl.concat(partes)) if partes else pl.DataFrame()


def costo_cba(cfg: Config, semanas: list[str], niveles: list[dict]) -> dict:
    """Costo mensual de la CBA de un adulto equivalente en cada cadena, por región y semana.

    Cada componente se valoriza con la mediana del precio por kilo de la cadena en la región.
    Si la cadena no tiene un componente, se imputa el precio mediano de la región ajustado por
    el nivel de precios general de la cadena (de `niveles_cadenas`).
    """
    p = _params(cfg)
    cba = cargar_cba(cfg)
    comp = {c["id"]: c for c in cba["componentes"]}
    q = {k: c["cantidad"] / 1000 for k, c in comp.items()}  # kg o litros por mes
    nombres = nombres_cadenas(cfg)
    nivel = {(n["semana"], n["region"], n["cadena_id"]): n["nivel"] / 100 for n in niveles}
    filas, detalle = [], []
    for s in semanas:
        panel = _cadena_id(pl.read_parquet(cfg.ruta("panel_semanal") / f"{s}.parquet"))
        pu = precios_cba(cfg, panel)
        if pu.is_empty():
            continue
        for reg in [NACIONAL, *REGIONES]:
            d = pu if reg == NACIONAL else pu.filter(pl.col("region") == reg)
            if d.is_empty():
                continue
            por_cadena = d.group_by("cadena_id", "componente").agg(pl.col("pu").median(), pl.len().alias("n"))
            ref = por_cadena.group_by("componente").agg(pl.col("pu").median().alias("ref"))
            ref_d = dict(ref.iter_rows())
            detalle += [{"semana": s, "region": reg, "componente": k, "nombre": comp[k]["nombre"],
                             "cantidad": comp[k]["cantidad"], "base": comp[k]["base"], "precio": v,
                         "costo": v * q[k]} for k, v in ref_d.items()]
            mediana = sum(ref_d.get(k, 0) * q[k] for k in q)
            completa = all(k in ref_d for k in q)
            for (cid,), g in por_cadena.group_by(["cadena_id"]):
                obs = dict(zip(g["componente"], g["pu"]))
                lv = nivel.get((s, reg, cid), 1.0)
                costo = cubierto = 0.0
                for k in q:
                    if k in obs:
                        costo += obs[k] * q[k]
                        cubierto += obs[k] * q[k]
                    elif k in ref_d:
                        costo += ref_d[k] * lv * q[k]
                cob = cubierto / costo if costo else 0
                filas.append({"semana": s, "region": reg, "cadena_id": cid, "cadena": nombres.get(cid, cid),
                              "costo": costo, "cobertura": cob, "componentes": len(obs),
                              "en_ranking": cob >= p["cobertura_min_cba"]})
            filas.append({"semana": s, "region": reg, "cadena_id": "MEDIANA", "cadena": "Mediana de las cadenas",
                          "costo": mediana, "cobertura": 1.0 if completa else None, "componentes": len(ref_d),
                          "en_ranking": False})
    rank = [f for f in filas if f["en_ranking"]]
    _rankear(rank, "costo")
    # variación semanal del costo de cada cadena
    prev = {}
    for f in sorted(filas, key=lambda f: f["semana"]):
        k = (f["region"], f["cadena_id"])
        f["var"] = (f["costo"] / prev[k] - 1) * 100 if k in prev and prev[k] else None
        prev[k] = f["costo"]
    return {"cadenas": filas, "componentes": detalle, "fuente": cba.get("fuente", "")}


def cba_mensual(cfg: Config, regiones: tuple[str, ...] = ("GBA", NACIONAL)) -> pl.DataFrame:
    """Costo mensual de la CBA (mediana entre cadenas de cada componente), para comparar con la
    CBA oficial del INDEC. Si un mes no tiene precio para un componente, se arrastra el del
    mes anterior (solo dentro de un tramo continuo de meses)."""
    cba = cargar_cba(cfg)
    q = {c["id"]: c["cantidad"] / 1000 for c in cba["componentes"]}
    filas, ultimo, mes_ant = [], {}, None
    for r in sorted(cfg.ruta("panel_mensual").glob("*.parquet")):
        a, m = map(int, r.stem.split("-"))
        if mes_ant and (a * 12 + m) - mes_ant > 2:
            ultimo = {}
        mes_ant = a * 12 + m
        panel = _cadena_id(pl.read_parquet(r))
        dias = int(panel["dias_con_datos"][0]) if "dias_con_datos" in panel.columns else 0
        pu = precios_cba(cfg, panel)
        if pu.is_empty():
            continue
        for reg in regiones:
            d = pu if reg == NACIONAL else pu.filter(pl.col("region") == reg)
            if d.is_empty():
                continue
            ref = (d.group_by("cadena_id", "componente").agg(pl.col("pu").median())
                   .group_by("componente").agg(pl.col("pu").median()))
            precios = dict(ref.iter_rows())
            observados = len(precios)
            for k in q:
                if k not in precios and (reg, k) in ultimo:
                    precios[k] = ultimo[(reg, k)]
            ultimo |= {(reg, k): v for k, v in precios.items()}
            filas.append({"mes": r.stem, "region": reg, "costo": sum(precios[k] * q[k] for k in q if k in precios),
                          "componentes": observados, "componentes_con_precio": sum(1 for k in q if k in precios),
                          "total_componentes": len(q), "dias": dias})
    return pl.DataFrame(filas) if filas else pl.DataFrame()


# ---------------------------------------------------------------- todo junto

def calcular(cfg: Config) -> dict:
    p = _params(cfg)
    semanas = _semanas_recientes(cfg, p["semanas"])
    out = {"semanas": semanas, "parametros": p}
    if not semanas:
        return out
    pasos = [("rotacion", lambda: rotacion(cfg, semanas)),
             ("cadenas", lambda: niveles_cadenas(cfg, semanas)),
             ("regiones", lambda: niveles_regiones(cfg, semanas)),
             ("inflacion_cadenas", lambda: inflacion_cadenas(cfg, semanas))]
    for clave, f in pasos:
        try:
            out[clave] = f()
        except Exception as e:  # un indicador que falla no debe frenar el resto
            log.warning("Indicador %s: %s", clave, e)
            out[clave] = []
    try:
        out["cba"] = costo_cba(cfg, semanas, out.get("cadenas", []))
    except Exception as e:
        log.warning("Indicador cba: %s", e)
        out["cba"] = {}
    # dispersión entre cadenas: desvío estándar de los niveles (en %) por región y semana
    disp = {}
    for f in out.get("cadenas", []):
        disp.setdefault((f["region"], f["semana"]), []).append(math.log(f["nivel"] / 100))
    out["dispersion"] = [
        {"region": r, "semana": s, "desvio": 100 * (sum(x * x for x in v) / len(v) - (sum(v) / len(v)) ** 2) ** 0.5,
         "brecha": 100 * (math.exp(max(v) - min(v)) - 1), "n": len(v)}
        for (r, s), v in sorted(disp.items()) if len(v) >= 2]
    return out


def guardar(ind: dict, cfg: Config) -> None:
    dir_res = cfg.ruta("resultados")
    for clave in ("rotacion", "cadenas", "regiones", "inflacion_cadenas", "dispersion"):
        if ind.get(clave):
            pl.DataFrame(ind[clave]).write_csv(dir_res / f"{clave}_semanal.csv")
    if ind.get("cba", {}).get("cadenas"):
        pl.DataFrame(ind["cba"]["cadenas"]).write_csv(dir_res / "cba_cadenas_semanal.csv")
        pl.DataFrame(ind["cba"]["componentes"]).write_csv(dir_res / "cba_componentes_semanal.csv")


__all__ = ["calcular", "guardar", "efectos_fijos", "id_semana"]
