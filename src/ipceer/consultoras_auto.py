"""Relevador automático de consultoras (D-74): cuentas de X por la API oficial y notas de prensa.

Dos lecturas, las dos con frases inequívocas (si una publicación no trae el dato con las palabras
esperadas, no se carga nada) y con el enlace a la publicación:

1. **Cuentas de las consultoras en X, por la API oficial** (no se lee x.com: su robots.txt lo
   prohíbe). Corre solo si hay un token en la variable X_BEARER_TOKEN (secret del repositorio). Lee
   los tuits nuevos de las cuentas de config/consultoras.yaml y los pasa por el lector de cada
   consultora. Resultado: data/otras/consultoras/x.csv.
2. **Notas de prensa**, por los RSS de los diarios de config/consultoras.yaml (respetando robots.txt
   y con pausa entre pedidos). Solo se toman frases que nombran a una única consultora, una sola cifra
   y el período ("LCG ... 0,8% en la segunda semana de noviembre"). Resultado:
   data/otras/consultoras/prensa.csv. Es la fuente de menor prioridad: cualquier dato cargado a mano,
   leído de la página de la consultora o de su cuenta de X lo reemplaza (externos.externos).

Las filas tienen las columnas de config/externos.csv.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import requests
import yaml

from .config import Config
from .consultoras import COLUMNAS, MESES, _MES, _dir, _num, _texto, semana_del_mes
from .telefonia import UA, _robots

log = logging.getLogger(__name__)

PAUSA = 3.0
ORDINAL = {"primera": 1, "primer": 1, "1ra": 1, "1era": 1, "segunda": 2, "2da": 2, "tercera": 3, "tercer": 3,
           "3ra": 3, "3era": 3, "cuarta": 4, "4ta": 4, "quinta": 5, "5ta": 5}
_ORD = r"(primera|primer|segunda|tercera|tercer|cuarta|quinta|última|ultima|1ra|1era|2da|3ra|3era|4ta|5ta)"
_NUM = r"([-−]?\d+(?:[.,]\d+)?)"


def _config(cfg: Config) -> dict:
    ruta = cfg.dir_config / "consultoras.yaml"
    return yaml.safe_load(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}


def _n(s: str) -> float:
    s = s.replace("−", "-")
    return _num(s) if "," in s else float(s)


def _mes(nombre: str) -> int:
    return MESES.index(nombre.lower()) + 1


def _anio(mes: int, publicado: date) -> int:
    """Año del período: el de la publicación, salvo un dato de diciembre publicado en enero."""
    return publicado.year - 1 if (mes == 12 and publicado.month == 1) else publicado.year


def semana(ordinal: str, mes: int, anio: int) -> str | None:
    """Semana ISO de "la n-ésima semana del mes" (mismo criterio que consultoras.semana_del_mes).
    "Última" es la última semana cuyo jueves cae en el mes."""
    o = ordinal.lower()
    if o in ("última", "ultima"):
        for n in (5, 4):
            s = semana_del_mes(anio, mes, n)
            if s:
                return s
        return None
    return semana_del_mes(anio, mes, ORDINAL[o])


def _fila(fuente, periodo, medida, rubro, region, valor, publicado, url, nota) -> dict:
    return {"fuente": fuente, "tipo": "consultora", "periodo": periodo, "medida": medida, "rubro": rubro,
            "region": region, "valor": valor, "publicado": publicado.isoformat(), "url": url, "nota": nota}


# ---------------------------------------------------------------------------------------------
# Lectores de tuits, uno por consultora (formatos vistos en sus cuentas, 2026)
# ---------------------------------------------------------------------------------------------

def tuit_ecogo(t: str, pub: date, url: str) -> list[dict]:
    """"Relevamiento de Precios Minoristas Eco Go | 2da semana de noviembre 2026 📌Var. semanal de
    alimentos: 0,26% 📌Proyección RPM Noviembre: 1,9%" o el cierre del mes ("| Octubre 2026 📌Var.
    promedio octubre de Alimentos y bebidas: 2,5% 📌Var. mensual RPM: 2,1%")."""
    filas = []
    ms = re.search(r"Eco ?Go \|\s*(\d)\s*\w{0,3}\.?\s+semana de " + _MES + r"\s+(?:de\s+)?(\d{4})", t, re.I)
    mm = re.search(r"Eco ?Go \|\s*" + _MES + r"\s+(?:de\s+)?(\d{4})", t, re.I)
    if ms:
        n, mes, anio = int(ms.group(1)), _mes(ms.group(2)), int(ms.group(3))
        per = semana_del_mes(anio, mes, n)
        nom = f"{n}.ª semana de {MESES[mes - 1]}"
        a = re.search(r"Var\. semanal de alimentos:\s*" + _NUM + r"\s?%", t, re.I)
        if per and a:
            filas.append(_fila("EcoGo", per, "semanal", "alimentos", None, _n(a.group(1)), pub, url,
                               f"RPM, {nom}: alimentos (cuenta de EcoGo en X; lectura automática)"))
        p = re.search(r"Proyección RPM " + _MES + r":\s*" + _NUM + r"\s?%", t, re.I)
        if p:
            mp = _mes(p.group(1))
            filas.append(_fila("EcoGo", f"{_anio(mp, pub)}-{mp:02d}", "proyeccion_mensual", "total", "Nacional",
                               _n(p.group(2)), pub, url, f"Proyección del RPM con datos a la {nom} (cuenta de EcoGo en X; lectura automática)"))
    elif mm:
        mes, anio = _mes(mm.group(1)), int(mm.group(2))
        a = re.search(r"Var\. promedio " + _MES + r" de Alimentos y bebidas:\s*" + _NUM + r"\s?%", t, re.I)
        g = re.search(r"Var\. mensual RPM:\s*" + _NUM + r"\s?%", t, re.I)
        if a:
            filas.append(_fila("EcoGo", f"{anio}-{mes:02d}", "mensual", "alimentos", "Nacional", _n(a.group(2)), pub, url,
                               f"RPM de {MESES[mes - 1]}: alimentos y bebidas, variación promedio (cuenta de EcoGo en X; lectura automática)"))
        if g:
            filas.append(_fila("EcoGo", f"{anio}-{mes:02d}", "mensual", "total", "Nacional", _n(g.group(1)), pub, url,
                               f"RPM de {MESES[mes - 1]} (cuenta de EcoGo en X; lectura automática)"))
    return filas


def tuit_econviews(t: str, pub: date, url: str) -> list[dict]:
    """"Nuestro relevamiento de precios arroja una suba de 0.4% en la primera semana de noviembre para
    una canasta de alimentos y bebidas en supermercados ... Las últimas 4 semanas acumulan 2%."
    También "caída del 0.1%" y "Octubre cerró con una suba promedio del 2.2%"."""
    m = re.search(r"relevamiento de precios arroja (?:una )?(suba|caída|variación|baja)\s+(?:de|del)\s+" + _NUM +
                  r"\s?%\s*(?:promedio\s*)?en la " + _ORD + r" semana de " + _MES, t, re.I)
    if not m or not re.search(r"alimentos y bebidas", t, re.I):
        return []
    signo = -1 if m.group(1).lower() in ("caída", "baja") else 1
    mes = _mes(m.group(4))
    anio = _anio(mes, pub)
    per = semana(m.group(3), mes, anio)
    if not per:
        return []
    nom = f"{m.group(3).lower()} semana de {MESES[mes - 1]}"
    v = abs(_n(m.group(2))) * signo
    filas = [_fila("Econviews", per, "semanal", "alimentos", "GBA", v, pub, url,
                   f"Canasta de alimentos y bebidas en supermercados, {nom} (cuenta de Econviews en X; lectura automática)")]
    c = re.search(r"últimas 4 semanas acumulan\s+" + _NUM + r"\s?%", t, re.I)
    if c:
        filas.append(_fila("Econviews", per, "promedio_4_semanas", "alimentos", "GBA", _n(c.group(1)), pub, url,
                           f"Acumulado de las últimas cuatro semanas a la {nom} (cuenta de Econviews en X; lectura automática)"))
    cm = re.search(_MES + r" cerró con una suba promedio del\s+" + _NUM + r"\s?%", t, re.I)
    if cm:
        mc = _mes(cm.group(1))
        filas.append(_fila("Econviews", f"{_anio(mc, pub)}-{mc:02d}", "mensual", "alimentos", "GBA", _n(cm.group(2)), pub, url,
                           f"Canasta de alimentos y bebidas, variación promedio de {MESES[mc - 1]} (cuenta de Econviews en X; lectura automática)"))
    return filas


def tuit_analytica(t: str, pub: date, url: str) -> list[dict]:
    """"Durante la tercera semana de octubre @AnalyticaARG registró una variación de 0,8% en los precios
    de alimentos y bebidas. De esta forma, el promedio de cuatro semanas es de 2,6%. Para el nivel
    general de precios proyectan una suba mensual del 2,0% durante octubre." """
    m = re.search(r"Durante la " + _ORD + r" semana de " + _MES + r".{0,40}?(?:registró|registramos|se registró)"
                  r" una variación (?:semanal )?de\s+" + _NUM + r"\s?% en los precios de alimentos y bebidas", t, re.I)
    if not m:
        return []
    mes = _mes(m.group(2))
    per = semana(m.group(1), mes, _anio(mes, pub))
    if not per:
        return []
    nom = f"{m.group(1).lower()} semana de {MESES[mes - 1]}"
    filas = [_fila("Analytica", per, "semanal", "alimentos", None, _n(m.group(3)), pub, url,
                   f"Alimentos y bebidas, {nom} (X; lectura automática)")]
    c = re.search(r"promedio de cuatro semanas es de\s+" + _NUM + r"\s?%", t, re.I)
    if c:
        filas.append(_fila("Analytica", per, "promedio_4_semanas", "alimentos", None, _n(c.group(1)), pub, url,
                           f"Promedio de las últimas cuatro semanas a la {nom} (X; lectura automática)"))
    p = re.search(r"proyecta(?:n|mos)? una suba mensual del\s+" + _NUM + r"\s?% durante " + _MES, t, re.I)
    if p:
        mp = _mes(p.group(2))
        filas.append(_fila("Analytica", f"{_anio(mp, pub)}-{mp:02d}", "proyeccion_mensual", "total", "Nacional",
                           _n(p.group(1)), pub, url, f"Proyección del nivel general con datos a la {nom} (X; lectura automática)"))
    return filas


LECTORES = {"ecogo": tuit_ecogo, "econviews": tuit_econviews, "analytica": tuit_analytica}


# ---------------------------------------------------------------------------------------------
# API oficial de X
# ---------------------------------------------------------------------------------------------

API = "https://api.x.com/2"


def x_api(cfg: Config, token: str | None = None) -> dict:
    """Tuits nuevos de las cuentas de config/consultoras.yaml (sección x), por la API oficial de X.
    Sin token no hace nada. Guarda el último tuit visto de cada cuenta para no pagar dos veces el mismo."""
    token = token or os.environ.get("X_BEARER_TOKEN", "")
    if not token:
        return {"omitido": "sin X_BEARER_TOKEN"}
    cuentas = _config(cfg).get("x", [])
    d = _dir(cfg)
    estado_ruta = d / "x_estado.json"
    estado = json.loads(estado_ruta.read_text(encoding="utf-8")) if estado_ruta.exists() else {}
    h = {"Authorization": f"Bearer {token}", "User-Agent": UA}
    filas, leidos, errores = [], 0, {}
    for c in cuentas:
        usuario, lector = c["usuario"], LECTORES.get(c.get("lector", ""))
        if not lector:
            continue
        e = estado.setdefault(usuario, {})
        try:
            if "id" not in e:
                r = requests.get(f"{API}/users/by/username/{usuario}", headers=h, timeout=30)
                r.raise_for_status()
                e["id"] = r.json()["data"]["id"]
            params = {"max_results": 20, "tweet.fields": "created_at", "exclude": "retweets,replies"}
            if e.get("ultimo"):
                params["since_id"] = e["ultimo"]
            else:  # primera vez: solo lo de las últimas 6 semanas
                params["start_time"] = (datetime.utcnow() - timedelta(weeks=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
            r = requests.get(f"{API}/users/{e['id']}/tweets", headers=h, params=params, timeout=30)
            r.raise_for_status()
            tuits = r.json().get("data", [])
        except Exception as ex:
            errores[usuario] = str(ex)[:200]
            continue
        leidos += len(tuits)
        for tw in tuits:
            pub = date.fromisoformat(tw["created_at"][:10])
            filas += lector(tw["text"], pub, f"https://x.com/{usuario}/status/{tw['id']}")
        if tuits:
            e["ultimo"] = max(tuits, key=lambda x: int(x["id"]))["id"]
        time.sleep(1)
    _guardar(d / "x.csv", filas)
    estado_ruta.write_text(json.dumps(estado, indent=1), encoding="utf-8")
    return {"tuits_leidos": leidos, "filas_nuevas": len(filas), "errores": errores}


# ---------------------------------------------------------------------------------------------
# Notas de prensa
# ---------------------------------------------------------------------------------------------

def frases_prensa(texto: str, nombres: dict, pub: date, url: str) -> list[dict]:
    """Datos inequívocos de una nota: una frase que nombra a una sola consultora, trae una sola cifra
    con % y dice el período. Semanal: "la N semana de <mes>" y "alimentos"; promedio de cuatro semanas:
    "últimas cuatro/4 semanas" (una cifra); proyección: "proyecta ... <mes>"."""
    filas = []
    texto = " ".join(texto.split())
    for frase in re.split(r"(?<=[.;])\s+(?=[A-ZÁÉÍÓÚ\"“])", texto):
        cuales = [k for k, pats in nombres.items() if any(re.search(p, frase) for p in pats)]
        if len(cuales) != 1:
            continue
        fuente = cuales[0]
        cifras = re.findall(_NUM + r"\s?%", frase)
        if len(cifras) != 1:
            continue
        v = _n(cifras[0])
        if re.search(r"\b(baj[óo]|cayó|caída|descenso)\b", frase, re.I) and v > 0 and \
                not re.search(r"suba|aument|increment|alza", frase, re.I):
            v = -v
        cita = f"lectura automática de prensa: «{frase[:180]}»"
        s = re.search(r"\b(?:la|en la) " + _ORD + r" semana de " + _MES, frase, re.I)
        if s and re.search(r"alimentos", frase, re.I) and not re.search(r"cuatro semanas|4 semanas|mensual|mes", frase, re.I):
            mes = _mes(s.group(2))
            per = semana(s.group(1), mes, _anio(mes, pub))
            if per:
                filas.append(_fila(fuente, per, "semanal", "alimentos", None, v, pub, url, cita))
            continue
        c = re.search(r"últimas (?:cuatro|4) semanas", frase, re.I)
        if c and s and re.search(r"alimentos", frase, re.I):
            mes = _mes(s.group(2))
            per = semana(s.group(1), mes, _anio(mes, pub))
            if per:
                filas.append(_fila(fuente, per, "promedio_4_semanas", "alimentos", None, v, pub, url, cita))
            continue
        p = re.search(r"proyect\w* (?:una )?(?:inflación|suba|variación)[^%]{0,60}?(?:para|de|en) " + _MES, frase, re.I)
        if p and not re.search(r"alimentos", frase, re.I):
            mes = _mes(p.group(1))
            filas.append(_fila(fuente, f"{_anio(mes, pub)}-{mes:02d}", "proyeccion_mensual", "total", "Nacional", v, pub, url, cita))
    return filas


def prensa(cfg: Config, max_notas: int = 30) -> dict:
    """Lee los RSS de config/consultoras.yaml (sección prensa) y las notas nuevas que nombran a una
    consultora y hablan de precios."""
    conf = _config(cfg)
    nombres = {k: v for k, v in (conf.get("nombres") or {}).items()}
    feeds = (conf.get("prensa") or {}).get("rss", [])
    d = _dir(cfg)
    vistos_ruta = d / "prensa_vistos.txt"
    vistos = set(vistos_ruta.read_text(encoding="utf-8").split()) if vistos_ruta.exists() else set()
    cache, filas, notas, errores = {}, [], 0, {}
    for feed in feeds:
        try:
            if not _robots(feed, cache):
                errores[feed] = "robots.txt no lo permite"
                continue
            r = requests.get(feed, headers={"User-Agent": UA}, timeout=40)
            r.raise_for_status()
            items = ET.fromstring(r.content).iter("item")
        except Exception as e:
            errores[feed] = str(e)[:200]
            continue
        for it in items:
            link = (it.findtext("link") or "").strip()
            resumen = (it.findtext("title") or "") + " " + (it.findtext("description") or "")
            if not link or link in vistos or notas >= max_notas:
                continue
            if not re.search(r"inflaci|precios|alimentos", resumen, re.I):
                continue
            if not _robots(link, cache):
                continue
            time.sleep(PAUSA)
            try:
                p = requests.get(link, headers={"User-Agent": UA, "Accept": "text/html"}, timeout=40)
                p.raise_for_status()
            except Exception as e:
                errores[link] = str(e)[:120]
                continue
            notas += 1
            vistos.add(link)
            fecha = it.findtext("pubDate") or ""
            try:
                pub = datetime.strptime(fecha[5:16], "%d %b %Y").date()
            except ValueError:
                pub = date.today()
            filas += frases_prensa(_texto(p.text), nombres, pub, link)
    _guardar(d / "prensa.csv", filas)
    vistos_ruta.write_text("\n".join(sorted(vistos)), encoding="utf-8")
    return {"notas_leidas": notas, "filas_nuevas": len(filas), "errores": errores}


def _guardar(ruta: Path, filas: list[dict]) -> None:
    viejo = (pl.read_csv(ruta, infer_schema_length=0) if ruta.exists()
             else pl.DataFrame(schema={c: pl.Utf8 for c in COLUMNAS}))
    if not filas:
        if not ruta.exists():
            viejo.write_csv(ruta)
        return
    nuevo = pl.DataFrame([{c: (None if f.get(c) is None else str(f.get(c))) for c in COLUMNAS} for f in filas])
    (pl.concat([viejo, nuevo], how="diagonal_relaxed")
     .unique(["fuente", "periodo", "medida", "rubro", "url"], keep="last")
     .sort("periodo", "fuente", "medida").select(COLUMNAS).write_csv(ruta))


def actualizar(cfg: Config) -> dict:
    return {"x": x_api(cfg), "prensa": prensa(cfg)}
