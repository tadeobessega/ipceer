"""Indicadores derivados para el tablero: semanal mensualizada, promedio de 4 semanas,
mes en curso y proyección mensual. Arma el JSON que consume el sitio."""

from __future__ import annotations

import math
from datetime import date, datetime

import polars as pl

from .config import REGIONES, Config, ponderadores_region
from .indice import NACIONAL
from .panel import mes_de_semana, rango_semana

DIAS_MES = 365.25 / 12  # 30,44


def mensualizar(var_pct: float | None, dias: float) -> float | None:
    """Lleva una variación de `dias` días a 30,44 días por interés compuesto."""
    if var_pct is None or (isinstance(var_pct, float) and math.isnan(var_pct)):
        return None
    return ((1 + var_pct / 100) ** (DIAS_MES / dias) - 1) * 100


def serie_semanal(sem: pl.DataFrame) -> list[dict]:
    """Por semana: variación, variación mensualizada y promedio móvil de 4 semanas."""
    sem = sem.sort("periodo")
    filas, rels = [], []
    for r in sem.iter_rows(named=True):
        v = r["var_pct"]
        rels.append(None if v is None else 1 + v / 100)
        ult4 = [x for x in rels[-4:] if x is not None]
        prom4 = (math.prod(ult4) ** (1 / len(ult4)) - 1) * 100 if len(ult4) == 4 else None
        ini, fin = rango_semana(r["periodo"])
        filas.append({
            "semana": r["periodo"], "desde": ini.isoformat(), "hasta": fin.isoformat(),
            "mes": mes_de_semana(r["periodo"]), "indice": r["indice"],
            "var": v, "var_mensualizada": mensualizar(v, 7),
            "prom4": prom4, "prom4_mensualizada": mensualizar(prom4, 7) if prom4 is not None else None,
            "parcial": r["parcial"], "n": r["n_productos"],
        })
    return filas


def saltos(df: pl.DataFrame, frecuencia: str) -> list[dict]:
    """Tramos entre períodos no consecutivos (ej. de la copia de la Wayback Machine a hoy):
    variación acumulada y su equivalente mensual."""
    out = []
    if "salto" not in df.columns:
        return out
    for r in df.filter(pl.col("salto")).sort("periodo").iter_rows(named=True):
        if r["var_salto_pct"] is None:
            continue
        if frecuencia == "semanal":
            d0, _ = rango_semana(r["periodo_anterior"])
            d1, _ = rango_semana(r["periodo"])
            meses = (d1 - d0).days / DIAS_MES
            desde, hasta = rango_semana(r["periodo_anterior"]), rango_semana(r["periodo"])
            desde, hasta = [desde[0].isoformat(), desde[1].isoformat()], [hasta[0].isoformat(), hasta[1].isoformat()]
        else:
            a0, m0 = map(int, r["periodo_anterior"].split("-"))
            a1, m1 = map(int, r["periodo"].split("-"))
            meses = (a1 - a0) * 12 + (m1 - m0)
            desde, hasta = r["periodo_anterior"], r["periodo"]
        out.append({"frecuencia": frecuencia, "desde": desde, "hasta": hasta, "var": r["var_salto_pct"],
                    "meses": meses, "var_mensual": ((1 + r["var_salto_pct"] / 100) ** (1 / meses) - 1) * 100})
    return out


def proyeccion_mes(semanal: list[dict], mes: str) -> dict | None:
    """Variación del mes `mes` con el índice semanal promedio del mes contra el del mes anterior.

    Las semanas que faltan del mes se completan de dos formas:
      - arrastre: el nivel de la última semana se mantiene (piso si los precios no bajan)
      - proyección: el nivel crece al ritmo del promedio de las últimas 4 semanas
    """
    por_mes: dict[str, list[dict]] = {}
    for s in semanal:
        por_mes.setdefault(s["mes"], []).append(s)
    anio, m = map(int, mes.split("-"))
    previo = f"{anio - 1}-12" if m == 1 else f"{anio}-{m - 1:02d}"
    if mes not in por_mes or previo not in por_mes:
        return None
    nivel_prev = sum(s["indice"] for s in por_mes[previo]) / len(por_mes[previo])
    obs = por_mes[mes]
    # semanas ISO cuyo jueves cae en el mes
    semanas_mes = 0
    d = date(anio, m, 1)
    while d.month == m:
        if d.isoweekday() == 4:
            semanas_mes += 1
        d = date.fromordinal(d.toordinal() + 1)
    faltan = max(0, semanas_mes - len(obs))
    ultimo = obs[-1]
    g = 1 + (ultimo["prom4"] or ultimo["var"] or 0) / 100
    niveles_obs = [s["indice"] for s in obs]
    arr = niveles_obs + [ultimo["indice"]] * faltan
    proy = niveles_obs + [ultimo["indice"] * g ** (k + 1) for k in range(faltan)]
    relevado = sum(niveles_obs) / len(niveles_obs) / nivel_prev * 100 - 100
    return {
        "mes": mes, "semanas_observadas": len(obs), "semanas_mes": semanas_mes,
        "relevado": relevado,
        "arrastre": sum(arr) / len(arr) / nivel_prev * 100 - 100,
        "proyeccion": sum(proy) / len(proy) / nivel_prev * 100 - 100,
        "ritmo_semanal": (g - 1) * 100,
    }


def _limpio(x):
    if isinstance(x, float):
        return None if math.isnan(x) or math.isinf(x) else round(x, 4)
    if isinstance(x, dict):
        return {k: _limpio(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_limpio(v) for v in x]
    return x


def armar_datos(cfg: Config, mensual: pl.DataFrame | None, semanal: pl.DataFrame | None,
                cobertura: pl.DataFrame | None, demo: bool = False, validacion: dict | None = None) -> dict:
    series = {}
    # El tablero principal muestra el tramo actual; los tramos anteriores (ej. 2016-2018) van a validación.
    if semanal is not None and "tramo" in semanal.columns:
        semanal = semanal.filter(pl.col("tramo") == semanal["tramo"].max())
    if mensual is not None and "tramo" in mensual.columns:
        mensual = mensual.filter(pl.col("tramo") == mensual["tramo"].max())
    regiones = [NACIONAL, *REGIONES]
    if semanal is not None:
        for reg in regiones:
            for niv, cod in [("total", "TOTAL"), *[("division", c) for c in ("01", "02", "05", "09", "12")]]:
                s = semanal.filter(pl.col("region") == reg, pl.col("nivel") == niv, pl.col("codigo") == cod)
                if s.height:
                    series.setdefault(reg, {})[cod] = {"semanal": serie_semanal(s), "saltos": saltos(s, "semanal")}
    if mensual is not None:
        for reg in regiones:
            for cod in list(series.get(reg, {})) or ["TOTAL"]:
                s = mensual.filter(pl.col("region") == reg, pl.col("codigo") == cod,
                                   pl.col("nivel") == ("total" if cod == "TOTAL" else "division")).sort("periodo")
                if s.height:
                    series.setdefault(reg, {}).setdefault(cod, {})["saltos_mensual"] = saltos(s, "mensual")
                    series.setdefault(reg, {}).setdefault(cod, {})["mensual"] = [
                        {"mes": r["periodo"], "indice": r["indice"], "var": r["var_pct"],
                         "var_ia": r["var_interanual_pct"], "parcial": r["parcial"]}
                        for r in s.iter_rows(named=True)]
    for reg, cods in series.items():
        for cod, d in cods.items():
            if d.get("semanal"):
                d["proyeccion"] = proyeccion_mes(d["semanal"], d["semanal"][-1]["mes"])

    rubros = []
    if semanal is not None:
        cl = semanal.filter(pl.col("nivel") == "clase")
        mes_cl = mensual.filter(pl.col("nivel") == "clase") if mensual is not None else None
        for (reg, cod), g in cl.group_by(["region", "codigo"], maintain_order=True):
            ss = serie_semanal(g)
            ult = ss[-1]
            fila = {"region": reg, "codigo": cod, "nombre": g["nombre"][0], "semana": ult["semana"],
                    "var": ult["var"], "prom4_mensualizada": ult["prom4_mensualizada"], "n": ult["n"]}
            if mes_cl is not None:
                gm = mes_cl.filter(pl.col("region") == reg, pl.col("codigo") == cod).sort("periodo")
                if gm.height:
                    fila |= {"mes": gm["periodo"][-1], "var_mes": gm["var_pct"][-1],
                             "mes_parcial": gm["parcial"][-1]}
            rubros.append(fila)
        rubros.sort(key=lambda r: (r["region"], r["codigo"]))
    nombres = {}
    for df in (mensual, semanal):
        if df is not None:
            nombres.update(dict(df.select("codigo", "nombre").unique("codigo").iter_rows()))

    pond_div = pl.read_csv(cfg.dir_config / "ponderadores_division.csv", schema_overrides={"codigo": pl.Utf8})
    ruta_cl = cfg.dir_config / "ponderadores_clase.csv"
    pond_cl = pl.read_csv(ruta_cl, schema_overrides={"codigo": pl.Utf8}).to_dicts() if ruta_cl.exists() else []
    return _limpio({
        "generado": datetime.now().isoformat(timespec="minutes"),
        "demo": demo,
        "regiones": regiones,
        "nombres": nombres,
        "series": series,
        "rubros": rubros,
        "cobertura": cobertura.to_dicts() if cobertura is not None else [],
        "ponderadores": {
            "region": [{"region": k, "ponderacion": v} for k, v in ponderadores_region(cfg).items()],
            "division": pond_div.to_dicts(),
            "clase": pond_cl,
        },
        "validacion": validacion or {},
        "parametros": {k: cfg["indice"][k] for k in cfg["indice"]} | {
            "factor_atipico_sucursal": cfg["limpieza"]["factor_atipico_sucursal"],
            "relativo_min": cfg["limpieza"]["relativo_min"], "relativo_max": cfg["limpieza"]["relativo_max"],
            "radio_gba_km": cfg["regiones"]["radio_gba_km"]},
    })
