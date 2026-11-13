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


def tarifas(cfg: Config, solo_conceptos_manuales: bool = False) -> pl.DataFrame:
    """Tarifas cargadas a mano (config/regulados/tarifas.csv) más las leídas automáticamente de los
    anexos de Infoleg y de los boletines de la Ciudad y la Provincia que pasaron el control
    (normas.tarifas_automaticas, D-34; boletines.tarifas_automaticas, D-40). Si un mismo concepto
    y fecha de inicio está en las dos, manda la carga manual."""
    ruta = cfg.dir_config / "regulados" / "tarifas.csv"
    sch = {"codigo": pl.Utf8, "valor": pl.Float64}
    manual = pl.read_csv(ruta, schema_overrides=sch) if ruta.exists() else pl.DataFrame()
    try:
        from . import boletines, normas
        auto = normas.tarifas_automaticas(cfg) + boletines.tarifas_automaticas(cfg)
    except Exception as e:
        log.warning("Tarifas automáticas: %s", e)
        auto = []
    if not auto:
        return manual
    a = pl.DataFrame(auto).with_columns(pl.col("valor").cast(pl.Float64), pl.col("codigo").cast(pl.Utf8))
    if manual.is_empty():
        return a
    a = a.join(manual.select("concepto", "vigente_desde"), on=["concepto", "vigente_desde"], how="anti")
    if solo_conceptos_manuales:
        # el índice de regulados usa solo los conceptos que ya estaban en la carga manual: lo leído
        # automáticamente le agrega meses, no conceptos nuevos (eso cambiaría el método, D-38)
        a = a.filter(pl.col("concepto").is_in(manual["concepto"].unique().to_list()))
    return pl.concat([manual, a.select(manual.columns)], how="vertical_relaxed")


COLS_BASE = ["jurisdiccion", "servicio", "empresa", "categoria", "subzona", "componente", "concepto", "valor", "unidad",
             "vigente_desde", "vigente_hasta", "rubro", "codigo", "region", "norma", "fuente_url", "origen", "control"]


def base_regulados(cfg: Config) -> pl.DataFrame:
    """Base única de tarifas reguladas para análisis posteriores (D-38): carga manual más todo lo
    leído automáticamente de Infoleg y de los boletines de la Ciudad y la Provincia, incluidos los
    valores que no pasaron el control (columna `control`). No se calcula nada: es el insumo para
    medir, por ejemplo, la variación real de las tarifas por jurisdicción o la canasta completa."""
    import csv as _csv
    from . import boletines, normas
    filas = []
    ruta = cfg.dir_config / "regulados" / "tarifas.csv"
    if ruta.exists():
        for r in _csv.DictReader(open(ruta, encoding="utf-8")):
            jur = "Ciudad" if "CABA" in r.get("norma", "") or r["concepto"].startswith(("Colectivo líneas de la Ciudad", "Subte")) \
                else "Provincia" if "PBA" in r.get("norma", "") else "Nación"
            filas.append({**r, "jurisdiccion": jur, "servicio": r["rubro"], "origen": "manual", "control": "ok"})
    for p in (normas._dir(cfg) / "tarifas_auto.csv", boletines._dir(cfg) / "tarifas_caba.csv",
              boletines._dir(cfg) / "tarifas_pba.csv"):
        if p.exists():
            filas += [{**r, "origen": r.get("origen") or "automatico", "jurisdiccion": r.get("jurisdiccion") or "Nación"}
                      for r in _csv.DictReader(open(p, encoding="utf-8"))]
    if not filas:
        return pl.DataFrame()
    df = pl.DataFrame([{c: (f.get(c) or None) for c in COLS_BASE} for f in filas], infer_schema_length=None)
    df = df.with_columns(pl.col("valor").cast(pl.Float64, strict=False)).sort("jurisdiccion", "servicio", "concepto", "vigente_desde")
    out = cfg.ruta("resultados") / "regulados_base.csv"
    df.write_csv(out)
    return df


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


def indice_regulados(cfg: Config, todos_los_conceptos: bool = False) -> pl.DataFrame | None:
    """Índice mensual por clase y región (formato de indices_mensual.csv). La variación de un mes
    existe solo si el mes y el anterior tienen precio; si no, ese mes queda sin dato.
    `todos_los_conceptos`: también los conceptos que solo se leyeron automáticamente (variante
    "todo" de la carrera, D-45); por defecto, solo los que ya estaban en la carga manual (D-38)."""
    t = tarifas(cfg, solo_conceptos_manuales=not todos_los_conceptos)
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


def errores_consultoras(cfg: Config) -> list[dict]:
    """Error de cada estimación mensual publicada (config/externos.csv) contra el dato del INDEC del
    mismo mes, rubro y región (D-29). Usa la variación mensual que publica el INDEC (v_m_IPC). Total
    se compara con el nivel general y alimentos con "Alimentos y bebidas no alcohólicas"; sin región
    se toma Nacional. Las regiones que el INDEC no publica (CABA, Bahía Blanca) quedan sin error."""
    import io
    from . import indec
    ext = externos(cfg)
    ruta = indec.directorio(cfg) / "serie_ipc_divisiones.csv"
    if not ext or not ruta.exists():
        return []
    crudo = ruta.read_bytes()
    try:
        texto = crudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = crudo.decode("latin-1")
    df = pl.read_csv(io.StringIO(texto), separator=";", infer_schema=False)
    of = {}
    for cod, reg, per, v in df.select("Codigo", "Region", "Periodo", "v_m_IPC").iter_rows():
        try:
            of[(cod.strip(), reg.strip(), f"{per[:4]}-{per[4:6]}")] = float(v.replace(",", "."))
        except (AttributeError, ValueError):
            continue
    cod = {"total": "0", "alimentos": "01"}
    out, vistos = [], set()
    for x in ext:
        if x.get("medida") not in ("mensual", "proyeccion_mensual") or x.get("valor") is None:
            continue
        c = cod.get((x.get("rubro") or "").lower())
        reg = x.get("region") or "Nacional"
        clave = (x["fuente"], x["periodo"], x["medida"], x["rubro"], reg, x["valor"])
        if not c or clave in vistos:
            continue
        vistos.add(clave)
        o = of.get((c, reg, x["periodo"]))
        out.append({"fuente": x["fuente"], "periodo": x["periodo"], "medida": x["medida"], "rubro": x["rubro"],
                    "region": reg, "valor": x["valor"], "indec": o,
                    "error": round(x["valor"] - o, 2) if o is not None else None, "url": x.get("url")})
    return sorted(out, key=lambda r: (r["periodo"], r["rubro"], r["region"], r["fuente"]), reverse=False)


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


def tarifas_reales(cfg: Config) -> dict:
    """Tarifas de la canasta de servicios en términos reales (D-43). Para cada componente
    (config/regulados/servicios.yaml): el precio unitario (boletos) o la factura del hogar tipo (luz
    y gas), mes a mes, deflactado por el IPC del INDEC de GBA (nivel general). Solo meses con tarifa
    vigente todo el mes; después del último dato del INDEC queda solo la serie nominal. Por
    componente: la serie (precio, índice nominal y real con base 100 en su primer mes) y las
    variaciones del último mes, del año, interanual y desde el primer dato. Nada se completa: un mes
    sin tarifa corta la serie."""
    from .indec import directorio
    from .validacion import leer_indec
    can = canasta_servicios(cfg)
    if not can.get("meses"):
        return {}
    ind = leer_indec(directorio(cfg)).filter((pl.col("codigo") == "0") & (pl.col("region") == "GBA"))
    ipc = dict(ind.select("mes", "indice").iter_rows())

    def mes_ant(m, n=1):
        a, x = int(m[:4]), int(m[5:]) - n
        while x < 1:
            a, x = a - 1, x + 12
        return f"{a}-{x:02d}"

    def var(a, b, k):
        return round((b[k] / a[k] - 1) * 100, 2) if a and b and a.get(k) and b.get(k) else None

    out = []
    for c in can["componentes"]:
        serie = [(f["mes"], f["precios"].get(c["id"])) for f in can["meses"] if f["precios"].get(c["id"]) is not None]
        base = {"id": c["id"], "nombre": c.get("nombre"),
                "unidad": "factura mensual del hogar tipo" if c.get("partes") else "precio por viaje"}
        if len(serie) < 2:
            out.append({**base, "serie": [], "sin_datos": True})
            continue
        m0, p0 = serie[0]
        filas = []
        for m, p in serie:
            real = (p / ipc[m]) / (p0 / ipc[m0]) * 100 if m in ipc and m0 in ipc else None
            filas.append({"mes": m, "precio": round(p, 2), "nominal": round(p / p0 * 100, 2),
                          "real": round(real, 2) if real is not None else None})
        por_mes = {f["mes"]: f for f in filas}
        ult = filas[-1]
        con_real = [f for f in filas if f["real"] is not None]
        ur = con_real[-1] if con_real else None
        dic = lambda m: f"{int(m[:4]) - 1}-12"
        out.append({**base, "serie": filas, "desde": m0, "ultimo": ult["mes"], "ultimo_real": ur["mes"] if ur else None,
                    "var_mes_nominal": var(por_mes.get(mes_ant(ult["mes"])), ult, "nominal"),
                    "var_mes_real": var(por_mes.get(mes_ant(ur["mes"])), ur, "real") if ur else None,
                    "var_anio_nominal": var(por_mes.get(dic(ult["mes"])), ult, "nominal"),
                    "var_anio_real": var(por_mes.get(dic(ur["mes"])), ur, "real") if ur else None,
                    "var_ia_nominal": var(por_mes.get(mes_ant(ult["mes"], 12)), ult, "nominal"),
                    "var_ia_real": var(por_mes.get(mes_ant(ur["mes"], 12)), ur, "real") if ur else None,
                    "var_total_nominal": round(ult["nominal"] - 100, 2),
                    "var_total_real": round(ur["real"] - 100, 2) if ur else None})
    return {"componentes": out, "deflactor": "IPC del INDEC, nivel general de GBA", "ipc_ultimo": max(ipc) if ipc else None}
