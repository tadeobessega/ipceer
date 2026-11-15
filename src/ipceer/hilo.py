"""Hilo de X (Twitter) con el dato de cada semana.

Arma los textos a partir de los resultados del índice y de los indicadores complementarios.
Las imágenes se dibujan en el tablero (pestaña "Hilo") con los mismos datos, para poder
bajarlas como PNG. El texto también se guarda en resultados/hilos/<semana>.txt.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import polars as pl

from .config import Config
from .indice import NACIONAL
from .panel import mes_de_semana, rango_semana
from .resumen import proyeccion_mes

log = logging.getLogger(__name__)

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre"]
ORDINALES = ["primera", "segunda", "tercera", "cuarta", "quinta"]
LIMITE = 280


def num(x: float | None, d: int = 1, signo: bool = False) -> str:
    """Formato argentino: 1.234,5 ; signo menos tipográfico."""
    if x is None:
        return "s/d"
    t = f"{abs(x):,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if x < 0 and t.strip("0,") != "":
        return "−" + t
    return ("+" + t) if signo and x > 0 else t


def pct(x: float | None, d: int = 1, signo: bool = False) -> str:
    return num(x, d, signo) + "%"


def pesos(x: float | None) -> str:
    return "s/d" if x is None else "$" + num(x, 0)


def ordinal_semana(semana: str) -> tuple[str, str]:
    """('cuarta', 'octubre') para la semana ISO `semana` (asignada al mes de su jueves)."""
    mes = mes_de_semana(semana)
    a, m = map(int, mes.split("-"))
    jueves = []
    d = date(a, m, 1)
    while d.month == m:
        if d.isoweekday() == 4:
            jueves.append(d)
        d += timedelta(days=1)
    lunes, _ = rango_semana(semana)
    j = lunes + timedelta(days=3)
    i = jueves.index(j) if j in jueves else 0
    return ORDINALES[min(i, 4)], MESES[m - 1]


def rango_texto(semana: str) -> str:
    l, d = rango_semana(semana)
    if l.month == d.month:
        return f"del {l.day} al {d.day} de {MESES[d.month - 1]}"
    return f"del {l.day} de {MESES[l.month - 1]} al {d.day} de {MESES[d.month - 1]}"


def _semana_objetivo(sem: list[dict]) -> int | None:
    completas = [i for i, s in enumerate(sem) if not s.get("parcial") and s.get("var") is not None]
    if completas:
        return completas[-1]
    con_var = [i for i, s in enumerate(sem) if s.get("var") is not None]
    return con_var[-1] if con_var else None


INDICES = {"alimentos": ("GBA", "01", "alimentos y bebidas en GBA", "Alimentos y bebidas, GBA"),
           "general": (NACIONAL, "TOTAL", "bienes", "Nivel general de bienes")}


def armar_todos(cfg: Config, datos: dict, semanal: pl.DataFrame | None, ind: dict) -> dict:
    """Un hilo por cada índice que se difunde (D-51): alimentos y bebidas de GBA (el que se publica
    primero) y el nivel general de bienes. Las placas y los textos de cada uno van por separado."""
    hs = {k: armar(cfg, datos, semanal, ind, clave=k) for k in INDICES}
    hs = {k: v for k, v in hs.items() if v}
    if not hs:
        return {}
    defecto = "alimentos" if "alimentos" in hs else next(iter(hs))
    return {**hs[defecto], "indices": hs, "defecto": defecto}


def armar(cfg: Config, datos: dict, semanal: pl.DataFrame | None, ind: dict, clave: str = "general") -> dict:
    reg_i, cod_i, nombre_i, titulo_i = INDICES[clave]
    serie = (datos.get("series", {}).get(reg_i, {}).get(cod_i) or {}).get("semanal") or []
    i = _semana_objetivo(serie)
    if i is None:
        return {}
    s = serie[i]
    semana, mes = s["semana"], s["mes"]
    ordn, mes_nombre = ordinal_semana(semana)
    proy = proyeccion_mes(serie[: i + 1], mes)
    proy_prev = proyeccion_mes(serie[:i], mes) if i > 0 and serie[i - 1]["mes"] == mes else None
    p1 = proy["proyeccion"] if proy else None
    p0 = proy_prev["proyeccion"] if proy_prev else None
    hc = cfg.datos.get("hilo") or {}
    tuits: list[dict] = []
    # 1. el dato
    if p1 is not None and p0 is not None and abs(round(p1, 1) - round(p0, 1)) >= 0.1:
        verbo = "elevando" if p1 > p0 else "reduciendo"
        cola = f", {verbo} la proyección mensual de {pct(p0)} a {pct(p1)}."
    elif p1 is not None and p0 is not None:
        cola = f". La proyección mensual se mantiene en {pct(p1)}."
    elif p1 is not None:
        cola = f". La proyección para {mes_nombre} es de {pct(p1)}."
    else:
        cola = "."
    t1 = (f"El ipceer midió una inflación semanal de {nombre_i} del {pct(s['var'], 2)} en la {ordn} semana de "
          f"{mes_nombre} ({rango_texto(semana)}){cola}")
    tuits.append({"texto": t1, "imagen": "portada"})

    # 2. datos clave
    claves = []
    if i > 0 and serie[i - 1].get("var") is not None:
        v0 = serie[i - 1]["var"]
        comp = "menor" if s["var"] < v0 else "mayor" if s["var"] > v0 else "igual"
        claves.append(f"Fue {comp} que el {pct(v0, 2)} de la semana previa.")
    if s.get("prom4") is not None:
        p4 = s["prom4"]
        p4_0 = serie[i - 1].get("prom4") if i > 0 else None
        if p4_0 is not None:
            mov = "subió" if p4 > p4_0 + 0.005 else "bajó" if p4 < p4_0 - 0.005 else "se mantuvo"
            claves.append(f"La media de 4 semanas {mov} de {pct(p4_0, 2)} a {pct(p4, 2)} "
                          f"({pct(s['prom4_mensualizada'])} mensualizada).")
        else:
            claves.append(f"La media de 4 semanas es {pct(p4, 2)} ({pct(s['prom4_mensualizada'])} mensualizada).")
    if semanal is not None:
        cl = semanal.filter(pl.col("region") == reg_i, pl.col("nivel") == "clase", pl.col("periodo") == semana,
                            pl.col("var_pct").is_not_null(), pl.col("n_productos") >= 20,
                            pl.col("codigo").str.starts_with(cod_i) if cod_i != "TOTAL" else pl.lit(True)).sort("var_pct")
        if cl.height >= 2:
            sube, baja = cl.row(-1, named=True), cl.row(0, named=True)
            claves.append(f"Lo que más subió: {sube['nombre'].lower()} ({pct(sube['var_pct'], 1, True)}).")
            claves.append(f"Lo que {'más bajó' if baja['var_pct'] <= -0.05 else 'menos subió'}"
                          f": {baja['nombre'].lower()} ({pct(baja['var_pct'], 1, True)}).")
    if claves:
        tuits.append({"texto": "Datos clave:\n\n" + "\n".join("• " + c for c in claves), "imagen": "semanal"})

    # 3. rotación de precios
    rot = [r for r in ind.get("rotacion", []) if r["semana"] == semana and r["region"] == reg_i
           and r["codigo"] == cod_i]
    if rot:
        r = rot[0]
        prev = [x for x in ind["rotacion"] if x["semana"] == r["semana_previa"] and x["region"] == reg_i
                and x["codigo"] == cod_i]
        comp = ""
        if prev:
            dif = r["cambia"] - prev[0]["cambia"]
            comp = (f" ({'más' if dif > 0 else 'menos'} que el {pct(prev[0]['cambia'])} de la semana previa)"
                    if abs(dif) >= 0.5 else " (similar a la semana previa)")
        t = (f"Rotación de precios: en la semana cambió el {pct(r['cambia'])} de los precios{comp}. "
             f"Subió el {pct(r['sube'])} y bajó el {pct(r['baja'])}.")
        if r.get("tam_sube") is not None and r.get("tam_baja") is not None:
            t += f" Las subas promediaron {pct(r['tam_sube'], 1, True)} y las bajas {pct(r['tam_baja'])}."
        if r.get("difusion") is not None:
            t += f" Subieron rubros que pesan el {pct(r['difusion'], 0)} de la canasta."
        tuits.append({"texto": t, "imagen": "rotacion"})

    # 5. regiones: la suba semanal del mismo rubro en cada región
    infl = []
    for reg, cods in (datos.get("series") or {}).items():
        if reg == NACIONAL:
            continue
        ss = (cods.get(cod_i) or {}).get("semanal") or []
        v = next((x["var"] for x in ss if x["semana"] == semana), None)
        if v is not None:
            infl.append((v, reg))
    if len(infl) >= 2:
        infl.sort()
        que = "de alimentos y bebidas" if cod_i == "01" else "de bienes"
        t = (f"Por región, la suba semanal {que} más alta fue en {infl[-1][1]} ({pct(infl[-1][0], 2)}) y la más "
             f"baja en {infl[0][1]} ({pct(infl[0][0], 2)}).")
        if cod_i == "01" and reg_i != NACIONAL:
            v = next((a for a, b in infl if b == reg_i), None)
            if v is not None:
                pos = sorted(infl, reverse=True).index((v, reg_i)) + 1
                t += f" {reg_i} quedó {pos}° de {len(infl)}."
        tuits.append({"texto": t, "imagen": "regiones"})

    # 6. cierre
    url = hc.get("url", "")
    tuits.append({"texto": "Series, rubros, regiones, cadenas y metodología completa en el tablero del ipceer:"
                           + (f"\n\n{url}" if url else ""), "imagen": None})

    for k, t in enumerate(tuits, start=1):
        t["n"] = k
        t["largo"] = len(t["texto"]) if "http" not in t["texto"] else len(t["texto"].split("http")[0]) + 23
        t["excede"] = t["largo"] > LIMITE
    return {
        "semana": semana, "mes": mes, "rango": rango_texto(semana), "ordinal": ordn, "mes_nombre": mes_nombre,
        "var": s["var"], "var_mensualizada": s["var_mensualizada"], "prom4": s.get("prom4"),
        "proyeccion": p1, "proyeccion_previa": p0, "relevado": proy["relevado"] if proy else None,
        "clave": clave, "region": reg_i, "codigo": cod_i, "nombre": nombre_i, "titulo": titulo_i, "tuits": tuits,
    }


def guardar(hilo: dict, cfg: Config) -> None:
    if not hilo:
        return
    d = cfg.ruta("resultados") / "hilos"
    d.mkdir(parents=True, exist_ok=True)
    texto = "\n\n---\n\n".join(f"{t['n']}/{len(hilo['tuits'])}\n{t['texto']}" for t in hilo["tuits"])
    (d / f"{hilo['semana']}.txt").write_text(texto + "\n", encoding="utf-8")
    log.info("Hilo %s: %d tuits", hilo["semana"], len(hilo["tuits"]))
