"""Carrera de métodos: el mismo índice calculado de varias formas, siempre, para ver cuál se
acerca más al INDEC y decidir con evidencia.

Reglas:
  - La variante "base" es la publicada. Ninguna otra la reemplaza por estar acá.
  - Todas las variantes se calculan en cada corrida, también las descartadas.
  - Se comparan contra dos referencias del INDEC: el nivel general (lo que se quiere anticipar) y
    la "réplica" (los índices del INDEC de las mismas clases que mide la variante, con los mismos
    ponderadores: compara método contra método, sin el efecto de lo que no medimos).
  - Lo que cada variante estimó para un mes queda anotado en resultados/pronosticos.csv con la
    fecha del cálculo. Cuando el INDEC publica ese mes se mide el error: ese registro no se reescribe.

Qué produce: resultados/carrera.json (tablero) y resultados/pronosticos.csv (historial).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import date
from pathlib import Path

import polars as pl

from . import indice
from .config import REGIONES, Config, ponderadores_clase, ponderadores_region

log = logging.getLogger(__name__)

VERSION = "2"   # cambia si cambia la forma de calcular alguna variante: invalida la prueba histórica
FRESCOS = ("01.1.2", "01.1.3", "01.1.6", "01.1.7")

# id, nombre, opciones de indice.calcular, ¿solo en el tramo actual? (necesita datos que antes no había)
VARIANTES: list[tuple[str, str, dict, bool]] = [
    ("base", "Publicado (método vigente en config.yaml)", {}, False),
    ("anterior", "Método hasta noviembre de 2026: Jevons encadenado de todos los artículos, sin estratos",
     {"estratificar": False, "populares": 0, "directo": False}, False),
    ("estratificado", "Jevons por variedad y promedio ponderado de variedades (D-12)",
     {"estratificar": True, "populares": 0, "directo": False}, False),
    ("populares", "Solo códigos que venden 3 comercios o más en la región (D-13)",
     {"estratificar": False, "populares": 3, "directo": False}, False),
    ("directo", "Comparación directa contra diciembre del año anterior, sin encadenar (D-14)",
     {"estratificar": False, "populares": 0, "directo": True}, False),
    ("mejorado", "Directo + estratificado + populares (D-12 a D-14 juntas)",
     {"estratificar": True, "populares": 3, "directo": True}, False),
    ("pond_propia", "Ponderadores actualizados con el nivel propio (método anterior)", {"ponderacion": "propia"}, False),
    ("efectivo", "Precio con promociones en lugar de precio de lista", {"precio": "efectivo"}, False),
    ("recortada", "Media recortada: sin el 5% de variaciones más bajas y más altas de cada clase", {"agregador": "recortada"}, False),
    ("mediana", "Mediana de las variaciones de cada clase", {"agregador": "mediana"}, False),
    ("sucursales", "Cada producto pesa por la cantidad de sucursales que lo informan", {"peso": "sucursales"}, False),
    ("sin_fyv", "Sin frutas ni verduras", {"excluir": ("01.1.6", "01.1.7")}, False),
    ("sin_frescos", "Sin carnes, pescados, frutas ni verduras", {"excluir": FRESCOS}, False),
    ("web", "Con precios de tiendas en línea (ropa, calzado, medicamentos, frescos por kilo)", {"web": True}, True),
    ("completo", "Con tiendas en línea, tarifas, combustibles, alquileres y prepagas", {"web": True, "extra": True}, True),
]
NOMBRES = {v[0]: v[1] for v in VARIANTES} | {
    "base_mas_sesgo": "Publicado más el sesgo medio de 2017-2018 (experimental)",
    "calibrado": "Publicado calibrado por regresión con 2017-2018 (experimental)"}


_CLAVES_METODO = ("precio", "ponderacion", "estratificar", "populares", "directo", "min_dias_mes", "min_productos_clase")


def _firma(opciones: dict, cfg: Config | None = None) -> str:
    """Identifica la definición de una variante: sus opciones y, para lo que no fija, el método de
    config.yaml (así la variante publicada se vuelve a probar si cambia el método vigente)."""
    met = {k: cfg["indice"].get(k) for k in _CLAVES_METODO} if cfg is not None else {}
    try:
        var = (cfg.dir_config / "variedades.yaml").read_text(encoding="utf-8") if cfg is not None else ""
    except OSError:
        var = ""
    return hashlib.sha1((VERSION + json.dumps(opciones, sort_keys=True, default=str) + json.dumps(met, sort_keys=True)
                         + hashlib.sha1(var.encode()).hexdigest()).encode()).hexdigest()[:12]


def _prev(mes: str) -> str:
    a, m = map(int, mes.split("-"))
    return f"{a - (m == 1)}-{12 if m == 1 else m - 1:02d}"


def relativos_externos(cfg: Config) -> pl.DataFrame:
    """Variaciones mensuales de clases que no salen de precios relevados: tarifas por norma
    (config/regulados/tarifas.csv y las leídas de Infoleg), combustibles (Secretaría de Energía),
    alquileres publicados (informe de Zonaprop, solo GBA) y cuotas de prepagas (Superintendencia de
    Servicios de Salud). Columnas: periodo, codigo, region, logrel, fuente."""
    esquema = {"periodo": pl.Utf8, "codigo": pl.Utf8, "region": pl.Utf8, "logrel": pl.Float64, "fuente": pl.Utf8}
    partes = []
    try:
        from . import externos
        reg = externos.indice_regulados(cfg)
        if reg is not None:
            partes.append(reg.filter(pl.col("region").is_in(REGIONES), pl.col("var_pct").is_not_null()).select(
                "periodo", "codigo", "region", (1 + pl.col("var_pct") / 100).log().alias("logrel"),
                pl.lit("tarifas").alias("fuente")))
    except Exception as e:
        log.warning("Carrera, tarifas: %s", e)
    try:
        ruta = cfg.ruta("resultados") / "combustibles_mensual.csv"
        if ruta.exists():
            c = pl.read_csv(ruta, schema_overrides={"codigo": pl.Utf8}, infer_schema_length=None)
            partes.append(c.filter(pl.col("region").is_in(REGIONES), pl.col("var_pct").is_not_null(),
                                   pl.col("nivel") == "clase").select(
                "periodo", "codigo", "region", (1 + pl.col("var_pct") / 100).log().alias("logrel"),
                pl.lit("combustibles").alias("fuente")))
    except Exception as e:
        log.warning("Carrera, combustibles: %s", e)
    try:
        ruta = cfg.ruta("resultados") / "otras_fuentes.json"
        if ruta.exists():
            alq = json.loads(ruta.read_text(encoding="utf-8")).get("alquileres_zonaprop") or []
            filas = [{"periodo": a["mes"], "codigo": "04.1.1", "region": "GBA",
                      "logrel": math.log(1 + a["var_alquiler"] / 100), "fuente": "alquileres"}
                     for a in alq if isinstance(a, dict) and a.get("var_alquiler") is not None]
            if filas:
                partes.append(pl.DataFrame(filas, schema=esquema))
    except Exception as e:
        log.warning("Carrera, alquileres: %s", e)
    try:
        from . import prepagas
        pp = prepagas.relativos_externos(cfg)
        if not pp.is_empty():
            partes.append(pp)
    except Exception as e:
        log.warning("Carrera, prepagas: %s", e)
    if not partes:
        return pl.DataFrame(schema=esquema)
    return pl.concat([p.select(list(esquema)).cast(esquema) for p in partes])


def correr(cfg: Config, opciones: dict, tramo: int | None) -> dict:
    """Una variante sobre un tramo (None = el último). Devuelve la variación mensual nacional."""
    op = dict(opciones)
    if op.pop("extra", False):
        op["extra"] = relativos_externos(cfg)
    if "excluir" in op:
        op["excluir"] = tuple(op["excluir"])
    res = indice.calcular(cfg, "mensual", solo_tramo=tramo, solo_tramo_actual=tramo is None, **op)
    ind = res["indices"]
    tot = ind.filter((pl.col("nivel") == "total") & (pl.col("region") == indice.NACIONAL)).sort("periodo")
    meses = [{"mes": p, "var": v if (v is not None and math.isfinite(v)) else None, "parcial": bool(par), "n": int(n or 0)}
             for p, v, par, n in tot.select("periodo", "var_pct", "parcial", "n_productos").iter_rows()]
    medido = pl.col("var_pct").is_not_null() & pl.col("var_pct").is_finite()   # sin datos puede venir como NaN
    codigos = sorted(set(ind.filter(pl.col("nivel") == "clase", medido)["codigo"].to_list()))
    # canasta medida en el último mes: peso de las clases que tienen variación propia ese mes
    # (una clase con precios pero sin mes anterior para comparar todavía no mide nada)
    ult = meses[-1]["mes"] if meses else None
    con = ind.filter(pl.col("nivel") == "clase", pl.col("periodo") == ult, medido,
                     pl.col("region").is_in(REGIONES)).select("codigo", "region")
    w = ponderadores_clase(cfg).select("codigo", "region", "ponderacion")
    wr = ponderadores_region(cfg)
    por_reg = dict(con.join(w, on=["codigo", "region"]).group_by("region").agg(pl.col("ponderacion").sum()).iter_rows())
    cobertura = sum(wr[r] * por_reg.get(r, 0.0) for r in REGIONES) / sum(wr.values())
    # subíndices de alimentos y bebidas no alcohólicas (división 01): GBA y nacional (decisión D-16)
    alim = {}
    for reg in ("GBA", indice.NACIONAL):
        a = ind.filter(pl.col("nivel") == "division", pl.col("codigo") == "01", pl.col("region") == reg).sort("periodo")
        alim[reg] = {p: v for p, v in a.select("periodo", "var_pct").iter_rows() if v is not None and math.isfinite(v)}
    return {"meses": meses, "codigos": codigos, "cobertura": round(cobertura, 2),
            "codigos_ultimo": sorted(set(con["codigo"].to_list())), "alimentos": alim}


def referencias_alimentos(cfg: Config) -> dict[str, dict[str, float]]:
    """Variación mensual del INDEC en Alimentos y bebidas no alcohólicas (división 01), GBA y nacional."""
    from .indec import directorio
    from .validacion import leer_indec
    ind = leer_indec(directorio(cfg))
    out = {}
    for reg in ("GBA", indice.NACIONAL):
        g = dict(ind.filter((pl.col("codigo") == "01") & (pl.col("region") == reg)).select("mes", "indice").iter_rows())
        out[reg] = {m: (g[m] / g[_prev(m)] - 1) * 100 for m in g if _prev(m) in g}
    return out


def referencias(cfg: Config, codigos: list[str]) -> tuple[dict, dict]:
    """Variación mensual nacional del INDEC: nivel general y réplica con las clases dadas (ponderadores
    de la canasta actualizados con los índices del INDEC del mes anterior)."""
    from .indec import directorio
    from .validacion import leer_indec
    ind = leer_indec(directorio(cfg))
    if ind.is_empty():
        return {}, {}
    g = dict(ind.filter((pl.col("codigo") == "0") & (pl.col("region") == "Nacional")).select("mes", "indice").iter_rows())
    general = {m: (g[m] / g[_prev(m)] - 1) * 100 for m in g if _prev(m) in g}
    niv, meses = indice.niveles_indec(cfg, codigos)
    if niv.is_empty():
        return general, {}
    w = ponderadores_clase(cfg).select("codigo", "region", "ponderacion")
    wr = ponderadores_region(cfg)
    n = {(c, r, m): i for c, r, m, i in niv.select("codigo", "region", "mes", "nivel_indec").iter_rows()}
    wd = {(c, r): x for c, r, x in w.iter_rows()}
    replica = {}
    for m in meses:
        p = _prev(m)
        num = den = 0.0
        for r in REGIONES:
            a = b = 0.0
            for c in codigos:
                if (c, r, m) in n and (c, r, p) in n and wd.get((c, r)):
                    ww = wd[(c, r)] * n[(c, r, p)] / 100
                    a += ww * (n[(c, r, m)] / n[(c, r, p)] - 1) * 100
                    b += ww
            if b:
                num += wr[r] * a
                den += wr[r] * b
        if den:
            replica[m] = num / den
    return general, replica


def puntuar(propio: dict, ref: dict) -> dict | None:
    ms = sorted(m for m in propio if propio[m] is not None and m in ref)
    if not ms:
        return None
    d = [propio[m] - ref[m] for m in ms]
    n = len(d)
    return {"n": n, "desde": ms[0], "hasta": ms[-1], "sesgo": round(sum(d) / n, 3),
            "rmse": round(math.sqrt(sum(x * x for x in d) / n), 3), "mae": round(sum(abs(x) for x in d) / n, 3),
            "acum_propio": round((math.prod(1 + propio[m] / 100 for m in ms) - 1) * 100, 2),
            "acum_ref": round((math.prod(1 + ref[m] / 100 for m in ms) - 1) * 100, 2)}


def _ruta_hist(cfg: Config) -> Path:
    return cfg.ruta("resultados") / "carrera_historico.json"


def historico(cfg: Config, solo: list[str] | None = None, forzar: bool = False) -> dict:
    """Prueba de cada variante en el tramo 2016-2018 contra el INDEC. Tarda (recorre todos los meses
    por variante): el resultado se guarda y solo se recalcula la variante cuya definición cambió."""
    ruta = _ruta_hist(cfg)
    guardado = json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}
    for vid, _nombre, opciones, solo_actual in VARIANTES:
        if solo_actual or (solo and vid not in solo):
            continue
        firma = _firma(opciones, cfg)
        if not forzar and guardado.get(vid, {}).get("firma") == firma:
            continue
        try:
            r = correr(cfg, opciones, 0)
        except Exception as e:
            log.warning("Carrera histórica %s: %s", vid, e)
            continue
        propio = {x["mes"]: x["var"] for x in r["meses"] if x["var"] is not None and "2017-01" <= x["mes"] <= "2018-12"}
        general, replica = referencias(cfg, r["codigos"])
        ra = referencias_alimentos(cfg)
        alim = {reg: {"vs_indec": puntuar({m: v for m, v in r["alimentos"][reg].items() if "2017-01" <= m <= "2018-12"},
                                          ra.get(reg, {}))} for reg in r["alimentos"]}
        guardado[vid] = {"firma": firma, "calculado": date.today().isoformat(), "codigos": r["codigos"], "alimentos": alim,
                         "meses": [{"mes": m, "propio": round(propio[m], 3),
                                    "general": round(general[m], 3) if m in general else None,
                                    "replica": round(replica[m], 3) if m in replica else None} for m in sorted(propio)],
                         "vs_general": puntuar(propio, general), "vs_replica": puntuar(propio, replica)}
        ruta.write_text(json.dumps(guardado, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("Carrera histórica %s: %s", vid, guardado[vid]["vs_replica"])
    return guardado


def _calibraciones(hist: dict) -> dict:
    """Dos ajustes experimentales de la variante publicada, estimados con 2017-2018 contra el nivel
    general del INDEC, y su error dejando afuera de a un mes (para no medirlos con los mismos datos
    con los que se estiman)."""
    base = (hist.get("base") or {}).get("meses") or []
    pares = [(x["propio"], x["general"]) for x in base if x.get("general") is not None]
    if len(pares) < 8:
        return {}

    def ols(ps):
        n = len(ps)
        mx, my = sum(p[0] for p in ps) / n, sum(p[1] for p in ps) / n
        sxx = sum((p[0] - mx) ** 2 for p in ps)
        b = sum((p[0] - mx) * (p[1] - my) for p in ps) / sxx if sxx else 0.0
        return my - b * mx, b

    def loo(f):
        e = []
        for i, (x, y) in enumerate(pares):
            resto = pares[:i] + pares[i + 1:]
            e.append(f(resto)(x) - y)
        n = len(e)
        return {"n": n, "sesgo": round(sum(e) / n, 3), "rmse": round(math.sqrt(sum(v * v for v in e) / n), 3),
                "mae": round(sum(abs(v) for v in e) / n, 3)}

    def con_sesgo(ps):
        s = sum(y - x for x, y in ps) / len(ps)
        return lambda x: x + s

    def con_ols(ps):
        a, b = ols(ps)
        return lambda x: a + b * x

    a, b = ols(pares)
    sesgo = sum(y - x for x, y in pares) / len(pares)
    return {"base_mas_sesgo": {"parametros": {"sesgo": round(sesgo, 3)}, "vs_general": loo(con_sesgo), "f": con_sesgo(pares)},
            "calibrado": {"parametros": {"a": round(a, 3), "b": round(b, 3)}, "vs_general": loo(con_ols), "f": con_ols(pares)}}


def _consultoras(cfg: Config, meses: list[str]) -> list[dict]:
    ruta = cfg.dir_config / "externos.csv"
    if not ruta.exists():
        return []
    e = pl.read_csv(ruta, infer_schema_length=None, schema_overrides={"valor": pl.Float64})
    e = e.filter(pl.col("periodo").is_in(meses), pl.col("rubro") == "total")
    return e.select("fuente", "periodo", "medida", "valor", "publicado", "url").sort("periodo", "publicado").to_dicts()


def _anotar(cfg: Config, filas: list[dict]) -> None:
    """Historial de estimaciones: una fila por día de cálculo, mes y variante."""
    ruta = cfg.ruta("resultados") / "pronosticos.csv"
    esquema = {"calculado": pl.Utf8, "mes": pl.Utf8, "variante": pl.Utf8, "var_pct": pl.Float64,
               "parcial": pl.Boolean, "cobertura_pct": pl.Float64}
    nuevo = pl.DataFrame(filas, schema=esquema)
    if ruta.exists():
        viejo = pl.read_csv(ruta, schema_overrides=esquema)
        nuevo = pl.concat([viejo, nuevo]).unique(["calculado", "mes", "variante"], keep="last")
    nuevo.sort("mes", "variante", "calculado").write_csv(ruta)


def actual(cfg: Config) -> dict:
    """Todas las variantes en el tramo actual, comparadas con el INDEC (meses ya publicados), con las
    consultoras y con su propia prueba histórica."""
    hist = historico(cfg) if _ruta_hist(cfg).exists() else {}
    hoy = date.today().isoformat()
    variantes, anotar, todos_meses = [], [], set()
    general = {}
    for vid, nombre, opciones, _solo in VARIANTES:
        try:
            r = correr(cfg, opciones, None)
        except Exception as e:
            log.warning("Carrera %s: %s", vid, e)
            variantes.append({"id": vid, "nombre": nombre, "error": str(e)[:200]})
            continue
        propio = {x["mes"]: x["var"] for x in r["meses"] if x["var"] is not None}
        parcial = {x["mes"]: x["parcial"] for x in r["meses"]}
        general, replica = referencias(cfg, r["codigos"])
        ra = referencias_alimentos(cfg)
        cerrados = {m: v for m, v in propio.items() if not parcial[m]}
        todos_meses |= set(propio)
        h = hist.get(vid) or {}
        variantes.append({
            "id": vid, "nombre": nombre, "cobertura_pct": r["cobertura"], "clases": len(r["codigos_ultimo"]),
            "codigos": r["codigos_ultimo"],
            "meses": [{"mes": m, "var": round(v, 3), "parcial": parcial[m],
                       "general": round(general[m], 3) if m in general else None,
                       "replica": round(replica[m], 3) if m in replica else None} for m, v in sorted(propio.items())],
            "actual_vs_general": puntuar(cerrados, general), "actual_vs_replica": puntuar(cerrados, replica),
            "historico_vs_general": h.get("vs_general"), "historico_vs_replica": h.get("vs_replica"),
            "alimentos": {reg: {"meses": [{"mes": m, "var": round(v, 3), "parcial": parcial.get(m, False),
                                           "indec": round(ra[reg][m], 3) if m in ra.get(reg, {}) else None}
                                          for m, v in sorted(r["alimentos"][reg].items())],
                                "actual_vs_indec": puntuar({m: v for m, v in r["alimentos"][reg].items() if not parcial.get(m)},
                                                           ra.get(reg, {})),
                                "historico_vs_indec": ((h.get("alimentos") or {}).get(reg) or {}).get("vs_indec")}
                          for reg in r["alimentos"]},
            "historico_al_dia": bool(h) and h.get("firma") == _firma(opciones, cfg)})
        anotar += [{"calculado": hoy, "mes": m, "variante": f"{vid}_alim_{'gba' if reg == 'GBA' else 'nac'}",
                    "var_pct": round(v, 4), "parcial": parcial.get(m, False), "cobertura_pct": None}
                   for reg in r["alimentos"] for m, v in r["alimentos"][reg].items()
                   if m not in ra.get(reg, {}) and vid in ("base", "mejorado")]
        anotar += [{"calculado": hoy, "mes": m, "variante": vid, "var_pct": round(v, 4), "parcial": parcial[m],
                    "cobertura_pct": r["cobertura"]} for m, v in propio.items() if m not in general]
    base = next((v for v in variantes if v["id"] == "base" and v.get("meses")), None)
    for vid, c in _calibraciones(hist).items():
        if not base:
            break
        ms = [{**x, "var": round(c["f"](x["var"]), 3)} for x in base["meses"]]
        variantes.append({"id": vid, "nombre": NOMBRES[vid], "cobertura_pct": base["cobertura_pct"], "experimental": True,
                          "parametros": c["parametros"], "meses": ms, "historico_vs_general": c["vs_general"],
                          "actual_vs_general": puntuar({x["mes"]: x["var"] for x in ms if not x["parcial"]}, general),
                          "historico_al_dia": True})
        anotar += [{"calculado": hoy, "mes": x["mes"], "variante": vid, "var_pct": x["var"], "parcial": x["parcial"],
                    "cobertura_pct": base["cobertura_pct"]} for x in ms if x["mes"] not in general]
    if anotar:
        _anotar(cfg, anotar)
    meses = sorted(todos_meses)
    salida = {"generado": hoy, "meses": meses, "ultimo_indec": max(general) if general else None,
              "indec_general": {m: round(general[m], 3) for m in meses if m in general},
              "variantes": variantes, "consultoras": _consultoras(cfg, meses),
              "historico_meses": (hist.get("base") or {}).get("meses") or []}
    (cfg.ruta("resultados") / "carrera.json").write_text(json.dumps(salida, ensure_ascii=False, indent=1), encoding="utf-8")
    return salida
