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


def armar(cfg: Config, datos: dict, semanal: pl.DataFrame | None, ind: dict) -> dict:
    serie = (datos.get("series", {}).get(NACIONAL, {}).get("TOTAL") or {}).get("semanal") or []
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
    t1 = (f"El IPCEER midió una inflación semanal de bienes del {pct(s['var'], 2)} en la {ordn} semana de "
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
        cl = semanal.filter(pl.col("region") == NACIONAL, pl.col("nivel") == "clase", pl.col("periodo") == semana,
                            pl.col("var_pct").is_not_null(), pl.col("n_productos") >= 20).sort("var_pct")
        if cl.height >= 2:
            sube, baja = cl.row(-1, named=True), cl.row(0, named=True)
            claves.append(f"Lo que más subió: {sube['nombre'].lower()} ({pct(sube['var_pct'], 1, True)}).")
            claves.append(f"Lo que {'más bajó' if baja['var_pct'] <= -0.05 else 'menos subió'}"
                          f": {baja['nombre'].lower()} ({pct(baja['var_pct'], 1, True)}).")
    if claves:
        tuits.append({"texto": "Datos clave:\n\n" + "\n".join("• " + c for c in claves), "imagen": "semanal"})

    # 3. rotación de precios
    rot = [r for r in ind.get("rotacion", []) if r["semana"] == semana and r["region"] == NACIONAL
           and r["codigo"] == "TOTAL"]
    if rot:
        r = rot[0]
        prev = [x for x in ind["rotacion"] if x["semana"] == r["semana_previa"] and x["region"] == NACIONAL
                and x["codigo"] == "TOTAL"]
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

    # 4. cadenas y CBA
    reg_c = hc.get("region_cadenas", "GBA")
    cba = [c for c in (ind.get("cba") or {}).get("cadenas", []) if c["semana"] == semana and c["region"] == reg_c
           and c.get("en_ranking")]
    if len(cba) >= 2:
        cba.sort(key=lambda c: c["costo"])
        bar, car = cba[0], cba[-1]
        brecha = (car["costo"] / bar["costo"] - 1) * 100
        t = (f"Cadenas ({reg_c}): la canasta básica alimentaria del INDEC para un adulto cuesta "
             f"{pesos(bar['costo'])} en {bar['cadena']} y {pesos(car['costo'])} en {car['cadena']}: "
             f"una diferencia de {pct(brecha, 0)}.")
        prev = [c for c in ind["cba"]["cadenas"] if c["region"] == reg_c and c.get("en_ranking")
                and c["semana"] < semana]
        if prev:
            sp = max(c["semana"] for c in prev)
            pv = sorted([c for c in prev if c["semana"] == sp], key=lambda c: c["costo"])
            if len(pv) >= 2:
                b0 = (pv[-1]["costo"] / pv[0]["costo"] - 1) * 100
                if abs(brecha - b0) >= 0.5:
                    t += f" La brecha se {'amplió' if brecha > b0 else 'achicó'} desde {pct(b0, 0)}."
        mov = [c for c in cba if c.get("cambio_puesto")]
        if mov:
            m = max(mov, key=lambda c: abs(c["cambio_puesto"]))
            t += (f" {m['cadena']} {'mejoró' if m['cambio_puesto'] > 0 else 'empeoró'} "
                  f"{abs(m['cambio_puesto'])} {'puesto' if abs(m['cambio_puesto']) == 1 else 'puestos'} "
                  f"(ahora {m['puesto']}° de {len(cba)}).")
        tuits.append({"texto": t, "imagen": "cadenas"})
    else:
        niv = [c for c in ind.get("cadenas", []) if c["semana"] == semana and c["region"] == reg_c]
        if len(niv) >= 2:
            niv.sort(key=lambda c: c["nivel"])
            t = (f"Cadenas ({reg_c}): con los mismos productos, la más barata fue {niv[0]['cadena']} "
                 f"({pct(niv[0]['nivel'] - 100, 1, True)} contra el promedio) y la más cara "
                 f"{niv[-1]['cadena']} ({pct(niv[-1]['nivel'] - 100, 1, True)}).")
            tuits.append({"texto": t, "imagen": "cadenas"})

    # 5. regiones
    regs = [r for r in ind.get("regiones", []) if r["semana"] == semana]
    if len(regs) >= 2:
        regs.sort(key=lambda r: r["nivel"])
        t = (f"Por región, con el mismo producto de la misma cadena, la más cara es {regs[-1]['region']} "
             f"({pct(regs[-1]['nivel'] - 100, 1, True)} sobre el promedio del país) y la más barata "
             f"{regs[0]['region']} ({pct(regs[0]['nivel'] - 100, 1, True)}).")
        series = datos.get("series", {})
        infl = []
        for reg, cods in series.items():
            if reg == NACIONAL:
                continue
            ss = (cods.get("TOTAL") or {}).get("semanal") or []
            v = next((x["var"] for x in ss if x["semana"] == semana), None)
            if v is not None:
                infl.append((v, reg))
        if len(infl) >= 2:
            infl.sort()
            t += (f" La suba semanal más alta fue en {infl[-1][1]} ({pct(infl[-1][0], 2)}) y la más baja en "
                  f"{infl[0][1]} ({pct(infl[0][0], 2)}).")
        tuits.append({"texto": t, "imagen": "regiones"})

    # 6. cierre
    url = hc.get("url", "")
    tuits.append({"texto": "Series, rubros, regiones, cadenas y metodología completa en el tablero del IPCEER:"
                           + (f"\n\n{url}" if url else ""), "imagen": None})

    for k, t in enumerate(tuits, start=1):
        t["n"] = k
        t["largo"] = len(t["texto"]) if "http" not in t["texto"] else len(t["texto"].split("http")[0]) + 23
        t["excede"] = t["largo"] > LIMITE
    return {
        "semana": semana, "mes": mes, "rango": rango_texto(semana), "ordinal": ordn, "mes_nombre": mes_nombre,
        "var": s["var"], "var_mensualizada": s["var_mensualizada"], "prom4": s.get("prom4"),
        "proyeccion": p1, "proyeccion_previa": p0, "relevado": proy["relevado"] if proy else None,
        "region_cadenas": reg_c, "tuits": tuits,
    }


def guardar(hilo: dict, cfg: Config) -> None:
    if not hilo:
        return
    d = cfg.ruta("resultados") / "hilos"
    d.mkdir(parents=True, exist_ok=True)
    texto = "\n\n---\n\n".join(f"{t['n']}/{len(hilo['tuits'])}\n{t['texto']}" for t in hilo["tuits"])
    (d / f"{hilo['semana']}.txt").write_text(texto + "\n", encoding="utf-8")
    log.info("Hilo %s: %d tuits", hilo["semana"], len(hilo["tuits"]))
