"""Comparación del IPCEER con el IPC oficial del INDEC.

Fuente oficial: series del IPC base diciembre 2016 = 100, por región y apertura
(data/indec/serie_ipc_divisiones.csv y serie_ipc_aperturas.csv, que baja `ipceer indec`).

Qué se compara (mismo código COICOP, misma región, mismo mes):
  - clases y grupos de alimentos y bebidas que el INDEC publica (01.1.1 ... 01.2.2, 02.1, 02.2)
  - divisiones 01 y 02
  - el total relevado contra (a) los "Bienes" del INDEC y (b) una réplica del INDEC con la
    misma canasta que cubre el IPCEER: las aperturas publicadas del INDEC ponderadas con los
    mismos ponderadores. (b) es la comparación justa: aísla la diferencia de fuente de precios
    de la diferencia de cobertura.

Estadísticos por par (variaciones mensuales en %):
  sesgo medio (propio − INDEC) con prueba t de Student pareada, error absoluto medio, raíz del
  error cuadrático medio, correlación de Pearson y de Spearman, regresión INDEC = a + b·propio
  (pendiente y R²), U de Theil (contra pronosticar "igual que el mes anterior"), acierto en la
  dirección del cambio (acelera/desacelera) y desvío acumulado del nivel.

Además: nowcast (qué tan bien anticipa el IPCEER la variación del INDEC con 1, 2, 3 o 4 semanas
del mes) y, para tramos con huecos (ej. abril 2025 → octubre 2026), la variación acumulada
contra la del INDEC en el mismo lapso.
"""

from __future__ import annotations

import io
import math
from pathlib import Path

import polars as pl

from .config import REGIONES, Config, ponderadores_clase
from .indice import tramos, NACIONAL

# códigos propios -> código INDEC (cuando no coinciden)
EQUIVALENCIAS = {"02.1.1": "02.1", "02.1.2": "02.1", "02.1.3": "02.1"}
NOMBRES_INDEC = {"B": "Bienes (INDEC)"}


# ---------------------------------------------------------------- lectura INDEC

FUERA_DEL_TOTAL = {"07.2.2"}


def leer_indec(dir_indec: Path) -> pl.DataFrame:
    """Une divisiones y aperturas en formato largo: codigo, region, mes (AAAA-MM), indice."""
    partes = []
    for nombre in ("serie_ipc_divisiones.csv", "serie_ipc_aperturas.csv"):
        ruta = dir_indec / nombre
        if not ruta.exists():
            continue
        crudo = ruta.read_bytes()
        try:
            texto = crudo.decode("utf-8-sig")
        except UnicodeDecodeError:
            texto = crudo.decode("latin-1")
        df = pl.read_csv(io.StringIO(texto), separator=";", infer_schema=False)
        desc = "Descripcion" if "Descripcion" in df.columns else "Descripcion_aperturas"
        partes.append(df.select(
            pl.col("Codigo").str.strip_chars().alias("codigo"),
            pl.col(desc).str.strip_chars().alias("nombre_indec"),
            pl.col("Region").str.strip_chars().alias("region"),
            (pl.col("Periodo").str.slice(0, 4) + "-" + pl.col("Periodo").str.slice(4, 2)).alias("mes"),
            pl.col("Indice_IPC").str.replace(",", ".").cast(pl.Float64, strict=False).alias("indice"),
        ))
    if not partes:
        return pl.DataFrame(schema={"codigo": pl.Utf8, "nombre_indec": pl.Utf8, "region": pl.Utf8,
                                    "mes": pl.Utf8, "indice": pl.Float64})
    return (pl.concat(partes).filter(pl.col("indice").is_not_null())
            .unique(["codigo", "region", "mes"], keep="first").sort("codigo", "region", "mes"))


def replica_indec(indec: pl.DataFrame, cfg: Config, codigos: list[str]) -> pl.DataFrame:
    """Índice del INDEC con la misma canasta del IPCEER: aperturas publicadas ponderadas con los
    mismos ponderadores (Laspeyres sobre los índices del INDEC, base dic-2016)."""
    pond = ponderadores_clase(cfg).filter(pl.col("codigo").is_in(codigos))
    reg = (
        indec.join(pond.select("codigo", "region", "ponderacion"), on=["codigo", "region"], how="inner")
        .group_by("region", "mes")
        .agg(((pl.col("indice") * pl.col("ponderacion")).sum() / pl.col("ponderacion").sum()).alias("indice"),
             pl.col("ponderacion").sum().alias("w"), pl.len().alias("n"))
    )
    n_max = reg["n"].max() if reg.height else 0
    reg = reg.filter(pl.col("n") == n_max)  # solo meses con todas las aperturas
    from .config import ponderadores_region
    wr = ponderadores_region(cfg)
    nac = (
        reg.with_columns((pl.col("region").replace_strict(wr, default=0.0, return_dtype=pl.Float64) * pl.col("w")).alias("ww"))
        .group_by("mes").agg(((pl.col("indice") * pl.col("ww")).sum() / pl.col("ww").sum()).alias("indice"))
        .with_columns(pl.lit(NACIONAL).alias("region"))
    )
    return pl.concat([reg.select("region", "mes", "indice"), nac.select("region", "mes", "indice")]).with_columns(
        pl.lit("REPLICA").alias("codigo"))


# ---------------------------------------------------------------- estadística sin dependencias

def _media(x):
    return sum(x) / len(x)


def _desvio(x):
    m = _media(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1)) if len(x) > 1 else float("nan")


def _pearson(x, y):
    if len(x) < 3:
        return float("nan")
    mx, my = _media(x), _media(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    return sxy / (sx * sy) if sx and sy else float("nan")


def _rangos(x):
    orden = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    i = 0
    while i < len(orden):
        j = i
        while j + 1 < len(orden) and x[orden[j + 1]] == x[orden[i]]:
            j += 1
        for k in range(i, j + 1):
            r[orden[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def _betacf(a, b, x):
    m_max, eps = 200, 3e-14
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, m_max + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1 + aa / c if abs(c) > 1e-300 else 1e300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1 + aa / c if abs(c) > 1e-300 else 1e300
        delta = d * c
        h *= delta
        if abs(delta - 1) < eps:
            break
    return h


def _beta_inc(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1 - bt * _betacf(b, a, 1 - x) / b


def p_valor_t(t, gl):
    """p-valor bilateral de la t de Student."""
    if gl <= 0 or math.isnan(t):
        return float("nan")
    return _beta_inc(gl / 2, 0.5, gl / (gl + t * t))


def estadisticos(propio: list[float], oficial: list[float]) -> dict:
    n = len(propio)
    if n < 3:
        return {"n": n}
    dif = [a - b for a, b in zip(propio, oficial)]
    sesgo, sd = _media(dif), _desvio(dif)
    t = sesgo / (sd / math.sqrt(n)) if sd else float("nan")
    mx, my = _media(propio), _media(oficial)
    sxx = sum((a - mx) ** 2 for a in propio)
    b = sum((a - mx) * (c - my) for a, c in zip(propio, oficial)) / sxx if sxx else float("nan")
    a0 = my - b * mx
    r = _pearson(propio, oficial)
    # U de Theil: error del IPCEER contra el error de repetir la variación del mes anterior
    num = sum((propio[i] - oficial[i]) ** 2 for i in range(1, n))
    den = sum((oficial[i - 1] - oficial[i]) ** 2 for i in range(1, n))
    # dirección: ¿acelera o desacelera en el mismo sentido que el INDEC?
    dirs = [(propio[i] - propio[i - 1]) * (oficial[i] - oficial[i - 1]) for i in range(1, n)]
    acum_p = math.prod(1 + v / 100 for v in propio) - 1
    acum_o = math.prod(1 + v / 100 for v in oficial) - 1
    return {
        "n": n, "sesgo": sesgo, "sesgo_t": t, "sesgo_p": p_valor_t(t, n - 1),
        "mae": _media([abs(d) for d in dif]), "rmse": math.sqrt(_media([d * d for d in dif])),
        "desvio_dif": sd, "pearson": r, "spearman": _pearson(_rangos(propio), _rangos(oficial)),
        "r2": r * r if not math.isnan(r) else float("nan"), "pendiente": b, "ordenada": a0,
        "theil_u": math.sqrt(num / den) if den else float("nan"),
        "direccion": sum(1 for d in dirs if d > 0) / len([d for d in dirs if d != 0]) if any(dirs) else float("nan"),
        "acum_propio": acum_p * 100, "acum_indec": acum_o * 100, "acum_dif": (acum_p - acum_o) * 100,
        "media_propio": mx, "media_indec": my,
    }


# ---------------------------------------------------------------- armado

def _limpio(x):
    if isinstance(x, float):
        return None if math.isnan(x) or math.isinf(x) else round(x, 4)
    if isinstance(x, dict):
        return {k: _limpio(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_limpio(v) for v in x]
    return x


def construir(cfg: Config, mensual: pl.DataFrame, semanal: pl.DataFrame | None, dir_indec: Path,
              cba_propia: pl.DataFrame | None = None) -> dict:
    indec = leer_indec(dir_indec)
    if indec.is_empty() or mensual is None or mensual.is_empty():
        return {}
    meses_indec = set(indec["mes"].unique().to_list())

    propios = mensual.filter(~pl.col("parcial"))
    codigos_propios = propios.filter(pl.col("nivel") == "clase")["codigo"].unique().to_list()
    # la réplica es la canasta del total de bienes de góndola: sin combustibles, que van aparte
    en_total = [c for c in codigos_propios if c not in FUERA_DEL_TOTAL]
    replica = replica_indec(indec, cfg, [EQUIVALENCIAS.get(c, c) for c in en_total] + en_total)

    # pares: (codigo propio, nivel, codigo INDEC)
    pares = [("TOTAL", "total", "REPLICA"), ("TOTAL", "total", "B")]
    for c in ("01", "02"):
        pares.append((c, "division", c))
    for c in ("01.1", "01.2", "02.1"):
        pares.append((c, "grupo", c))
    for c in sorted(codigos_propios):
        if c in EQUIVALENCIAS:  # se comparan agregadas en su grupo (ej. 02.1)
            continue
        ci = c
        if ci in set(indec["codigo"].unique().to_list()):
            pares.append((c, "clase", ci))
    # grupo 02.1 propio = las tres clases de alcohol: viene agregado por código en el índice
    indec_todo = pl.concat([indec.select("codigo", "region", "mes", "indice"), replica.select("codigo", "region", "mes", "indice")])
    nombres = dict(mensual.select("codigo", "nombre").unique("codigo").iter_rows())
    nombres_indec = dict(indec.select("codigo", "nombre_indec").unique("codigo").iter_rows()) | NOMBRES_INDEC | {
        "REPLICA": "INDEC con la misma canasta"}

    resultados, filas_tabla = [], []
    for cod, nivel, ci in pares:
        for reg in [NACIONAL, *REGIONES]:
            p = propios.filter(pl.col("codigo") == cod, pl.col("nivel") == nivel, pl.col("region") == reg)
            if p.is_empty():
                continue
            o = indec_todo.filter(pl.col("codigo") == ci, pl.col("region") == reg)
            if o.is_empty():
                continue
            for tr in sorted(p["tramo"].unique().to_list()) if "tramo" in p.columns else [0]:
                pt = p.filter(pl.col("tramo") == tr) if "tramo" in p.columns else p
                pt = pt.filter(pl.col("periodo").is_in(meses_indec))
                if pt.height < 2:
                    continue
                j = (pt.select(pl.col("periodo").alias("mes"), pl.col("indice").alias("idx_propio"), "salto")
                     .join(o.select("mes", pl.col("indice").alias("idx_indec")), on="mes", how="inner").sort("mes"))
                if j.height < 2:
                    continue
                filas = j.to_dicts()
                serie = []
                base_p, base_o = filas[0]["idx_propio"], filas[0]["idx_indec"]
                for i, f in enumerate(filas):
                    reg_ = {"mes": f["mes"], "propio": f["idx_propio"] / base_p * 100,
                            "indec": f["idx_indec"] / base_o * 100, "var_propio": None, "var_indec": None}
                    if i:
                        ant = filas[i - 1]
                        consecutivo = not f["salto"]
                        if consecutivo:
                            reg_["var_propio"] = (f["idx_propio"] / ant["idx_propio"] - 1) * 100
                            reg_["var_indec"] = (f["idx_indec"] / ant["idx_indec"] - 1) * 100
                    serie.append(reg_)
                vp = [s["var_propio"] for s in serie if s["var_propio"] is not None]
                vo = [s["var_indec"] for s in serie if s["var_propio"] is not None]
                est = estadisticos(vp, vo)
                item = {"codigo": cod, "nivel": nivel, "codigo_indec": ci, "region": reg, "tramo": tr,
                        "nombre": nombres.get(cod, cod) if cod != "TOTAL" else "Bienes relevados (total)",
                        "nombre_indec": nombres_indec.get(ci, ci), "desde": serie[0]["mes"], "hasta": serie[-1]["mes"],
                        "serie": serie, "est": est}
                resultados.append(item)
                filas_tabla.append({k: item[k] for k in ("codigo", "codigo_indec", "region", "tramo", "nombre", "nombre_indec",
                                                         "desde", "hasta")} | est)

    # canasta básica alimentaria: costo propio (mediana de las cadenas, GBA) contra la CBA oficial
    from .indec import leer_cba
    cba_o = leer_cba(dir_indec)
    if cba_propia is not None and not cba_propia.is_empty() and not cba_o.is_empty():
        oficial = dict(cba_o.select("mes", "cba").iter_rows())
        cp = cba_propia.filter(pl.col("region") == "GBA", pl.col("dias") >= 10).sort("mes")
        meses = [m for m in cp["mes"].to_list() if m in oficial]
        tr = tramos(meses, "mensual", cfg["indice"]["max_meses_salto"])
        costo = dict(cp.select("mes", "costo").iter_rows())
        comp = dict(cp.select("mes", "componentes").iter_rows())
        var_c = dict(cp.select("mes", "var_comun").iter_rows()) if "var_comun" in cp.columns else {}
        for t in sorted(set(tr.values())):
            ms = [m for m in meses if tr[m] == t]
            if len(ms) < 2:
                continue
            serie = []
            for i, m in enumerate(ms):
                f = {"mes": m, "propio": costo[m], "indec": oficial[m], "var_propio": None, "var_indec": None,
                     "componentes": comp[m]}
                if i:
                    a0, m0 = map(int, ms[i - 1].split("-"))
                    a1, m1 = map(int, m.split("-"))
                    if (a1 * 12 + m1) - (a0 * 12 + m0) == 1:
                        # variación con los mismos componentes en los dos meses
                        f["var_propio"] = var_c.get(m, (costo[m] / costo[ms[i - 1]] - 1) * 100)
                        f["var_indec"] = (oficial[m] / oficial[ms[i - 1]] - 1) * 100
                serie.append(f)
            vp = [x["var_propio"] for x in serie if x["var_propio"] is not None]
            vo = [x["var_indec"] for x in serie if x["var_propio"] is not None]
            est = estadisticos(vp, vo) | {"nivel_dif_pct": 100 * (_media([x["propio"] / x["indec"] for x in serie]) - 1)}
            item = {"codigo": "CBA", "nivel": "cba", "codigo_indec": "CBA", "region": "GBA", "tramo": t,
                    "nombre": "Canasta básica alimentaria", "nombre_indec": "CBA del INDEC (adulto equivalente)",
                    "unidad": "pesos", "desde": serie[0]["mes"], "hasta": serie[-1]["mes"], "serie": serie, "est": est}
            resultados.append(item)
            filas_tabla.append({k: item[k] for k in ("codigo", "codigo_indec", "region", "tramo", "nombre",
                                                     "nombre_indec", "desde", "hasta")} | est)
    cba_oficial = cba_o.to_dicts() if not cba_o.is_empty() else []

    out = {"pares": resultados, "cba_indec": cba_oficial,
           "nowcast": nowcast(semanal, indec_todo) if semanal is not None else [],
           "saltos": saltos_vs_indec(mensual, semanal, indec_todo)}
    if filas_tabla:
        pl.from_dicts(_limpio(filas_tabla), infer_schema_length=None).write_csv(cfg.ruta("resultados") / "validacion_indec.csv")
    return _limpio(out)


def nowcast(semanal: pl.DataFrame, indec_todo: pl.DataFrame) -> list[dict]:
    """Error al anticipar la variación mensual del INDEC (réplica, nacional) con k semanas del mes.

    Para cada mes con el mes anterior completo: estimación = promedio de las primeras k semanas
    contra el promedio de las semanas del mes anterior (lo "relevado" a esa altura del mes)."""
    from .panel import mes_de_semana
    s = semanal.filter(pl.col("region") == NACIONAL, pl.col("codigo") == "TOTAL", ~pl.col("parcial")).sort("periodo")
    if s.is_empty():
        return []
    s = s.with_columns(pl.col("periodo").map_elements(mes_de_semana, return_dtype=pl.Utf8).alias("mes"))
    o = indec_todo.filter(pl.col("codigo") == "REPLICA", pl.col("region") == NACIONAL).sort("mes")
    filas_o = list(o.select("mes", "indice").iter_rows())
    vo = {m1: (i1 / i0 - 1) * 100 for (m0, i0), (m1, i1) in zip(filas_o, filas_o[1:])}
    por_mes = {}
    for r in s.iter_rows(named=True):
        por_mes.setdefault((r["tramo"], r["mes"]), []).append(r["indice"])
    errores = {k: [] for k in (1, 2, 3, 4)}
    for (tr, m), niveles in por_mes.items():
        a, mm = map(int, m.split("-"))
        prev = f"{a - 1}-12" if mm == 1 else f"{a}-{mm - 1:02d}"
        if (tr, prev) not in por_mes or m not in vo or len(niveles) < 4 or len(por_mes[(tr, prev)]) < 4:
            continue
        base = sum(por_mes[(tr, prev)]) / len(por_mes[(tr, prev)])
        for k in (1, 2, 3, 4):
            est = (sum(niveles[:k]) / k / base - 1) * 100
            errores[k].append(est - vo[m])
    return [{"semanas": k, "n": len(e), "rmse": math.sqrt(_media([x * x for x in e])) if e else None,
             "sesgo": _media(e) if e else None} for k, e in errores.items()]


def saltos_vs_indec(mensual: pl.DataFrame, semanal: pl.DataFrame | None, indec_todo: pl.DataFrame) -> list[dict]:
    """Para los tramos con hueco (ej. abril 2025 -> octubre 2026): variación acumulada propia contra la
    del INDEC entre los mismos meses (o el último publicado), ambas como promedio mensual."""
    out = []
    fuente = semanal if semanal is not None else mensual
    if fuente is None or "salto" not in fuente.columns:
        return out
    from .panel import mes_de_semana
    meses_ok = sorted(indec_todo["mes"].unique().to_list())
    for r in fuente.filter(pl.col("salto"), pl.col("codigo").is_in(["TOTAL", "01"]), pl.col("nivel") != "clase").iter_rows(named=True):
        m0 = mes_de_semana(r["periodo_anterior"]) if "-W" in r["periodo_anterior"] else r["periodo_anterior"]
        m1 = mes_de_semana(r["periodo"]) if "-W" in r["periodo"] else r["periodo"]
        m1_ind = m1 if m1 in meses_ok else max(m for m in meses_ok if m <= m1)
        ci = "REPLICA" if r["codigo"] == "TOTAL" else r["codigo"]
        o = indec_todo.filter(pl.col("codigo") == ci, pl.col("region") == r["region"])
        d = dict(o.select("mes", "indice").iter_rows())
        if m0 not in d or m1_ind not in d:
            continue
        meses = lambda a, b: (int(b[:4]) - int(a[:4])) * 12 + int(b[5:]) - int(a[5:])
        n_p, n_o = meses(m0, m1), meses(m0, m1_ind)
        acum_o = (d[m1_ind] / d[m0] - 1) * 100
        out.append({"region": r["region"], "codigo": r["codigo"], "desde": r["periodo_anterior"], "hasta": r["periodo"],
                    "mes_desde": m0, "mes_hasta": m1, "mes_hasta_indec": m1_ind,
                    "acum_propio": r["var_salto_pct"], "acum_indec": acum_o,
                    "mensual_propio": ((1 + r["var_salto_pct"] / 100) ** (1 / max(n_p, 1)) - 1) * 100,
                    "mensual_indec": ((1 + acum_o / 100) ** (1 / max(n_o, 1)) - 1) * 100})
    return out


# compatibilidad con `ipceer validar`
def comparar(indices: pl.DataFrame, ruta_indec: Path) -> pl.DataFrame:
    raise SystemExit("Usá `ipceer calcular`: la validación contra el INDEC se calcula sola y queda en "
                     "resultados/validacion_indec.csv")
