"""Datos de las consultoras leídos de sus propias páginas (D-66).

EcoGo publica cada Relevamiento de Precios Minoristas (RPM) en su sitio, con el dato en el texto:
"El RPM subió 0,37% en la segunda semana", "Noviembre se encamina a 1,9% mensual", "el RPM registró
un incremento del 2,1% mensual en el nivel general". Se lee la lista de la categoría y cada informe
nuevo (respetando robots.txt, con pausa entre pedidos) y se guarda en
data/otras/consultoras/ecogo.csv con las mismas columnas que config/externos.csv. Solo se cargan
frases con un patrón inequívoco: si un informe no trae el dato con esas palabras, no se carga nada
(un supuesto como "con 0,5% semanal para alimentos en las semanas que faltan" no es un dato).

X (Twitter) y LinkedIn prohíben la lectura automática en su robots.txt: los datos que una
consultora publica solo ahí se cargan a mano en config/externos.csv.
"""
from __future__ import annotations

import html as _html
import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import requests

from .config import Config
from .telefonia import UA, _robots

log = logging.getLogger(__name__)

LISTA_ECOGO = "https://ecogo.com.ar/relevamiento-precios-minoristas/"
PAUSA = 3.0
COLUMNAS = ["fuente", "tipo", "periodo", "medida", "rubro", "region", "valor", "publicado", "url", "nota"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre"]
_MES = "(" + "|".join(MESES) + ")"


def _dir(cfg: Config) -> Path:
    d = cfg.ruta("panel_diario").parent.parent / "otras" / "consultoras"
    d.mkdir(parents=True, exist_ok=True)
    return d


def semana_del_mes(anio: int, mes: int, n: int) -> str | None:
    """Semana ISO que una consultora llama "n-ésima semana del mes": la n-ésima semana (de lunes a
    domingo) cuyo jueves cae en el mes, el mismo criterio de las semanas ISO y de las etiquetas S3-nov."""
    d = date(anio, mes, 1)
    jueves = d + timedelta(days=(3 - d.weekday()) % 7)
    j = jueves + timedelta(weeks=n - 1)
    if j.month != mes:
        return None
    a, w, _ = j.isocalendar()
    return f"{a}-W{w:02d}"


def _num(s: str) -> float:
    return float(s.replace(".", "").replace(",", ".")) if "," in s else float(s)


def _texto(h: str) -> str:
    h = re.sub(r"<script.*?</script>|<style.*?</style>", " ", h, flags=re.S | re.I)
    t = _html.unescape(re.sub(r"<[^>]+>", "\n", h)).replace("​", "")
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n", t))


def leer_rpm(h: str, url: str) -> list[dict]:
    """Filas de externos.csv que salen de un informe del RPM (vacío si no hay un dato inequívoco)."""
    t = _texto(h)
    fin = t.find("Accede a tu cuenta")
    t = t[:fin] if fin > 0 else t
    i = t.rfind("\nRPM")  # el título del informe (el primero es el <title> de la página)
    cuerpo = t[i:] if i >= 0 else t
    titulo = cuerpo.strip().splitlines()[0] if cuerpo.strip() else ""
    cuerpo = " ".join(cuerpo.split())  # las frases vienen cortadas en varias líneas
    m_pub = re.search(r"(\d{1,2}) " + _MES + r",? (\d{4})", cuerpo, re.I)
    publicado = (date(int(m_pub.group(3)), MESES.index(m_pub.group(2).lower()) + 1, int(m_pub.group(1))).isoformat()
                 if m_pub else "")
    base = {"fuente": "EcoGo", "tipo": "consultora", "region": "Nacional", "publicado": publicado, "url": url}
    filas = []
    m_sem = re.search(r"RPM\s*[–-]\s*(\d)\s*(?:ra|da|era|ta|ma|va|a)?\.?\s+semana\s+(?:de\s+)?" + _MES +
                      r"\s+(?:de\s+)?(\d{4})", titulo, re.I)
    m_mes = re.search(r"RPM\s*[–-]\s*" + _MES + r"\s+(?:de\s+)?(\d{4})\s*$", titulo, re.I)
    if m_sem:
        n, mes, anio = int(m_sem.group(1)), MESES.index(m_sem.group(2).lower()) + 1, int(m_sem.group(3))
        per = semana_del_mes(anio, mes, n)
        nom = titulo.split("–", 1)[-1].strip()
        v = re.search(r"El RPM (subió|aumentó|creció|bajó|cayó) (-?\d+(?:,\d+)?)\s?% en la \w+ semana", cuerpo)
        va = re.search(r"semana del mes los precios de los alimentos consumidos dentro del hogar registraron una "
                       r"(suba|baja|variación) de (-?\d+(?:,\d+)?)\s?%", cuerpo)
        if per and v:
            val = _num(v.group(2)) * (-1 if v.group(1) in ("bajó", "cayó") else 1)
            filas.append(base | {"periodo": per, "medida": "semanal", "rubro": "total", "valor": val,
                                 "nota": f"RPM, {nom} (página de EcoGo; lectura automática)"})
        if per and va:
            val = _num(va.group(2)) * (-1 if va.group(1) == "baja" else 1)
            filas.append(base | {"periodo": per, "medida": "semanal", "rubro": "alimentos", "region": None, "valor": val,
                                 "nota": f"RPM, {nom}: alimentos consumidos dentro del hogar (página de EcoGo; lectura automática)"})
        if per and not (v or va):
            log.warning("EcoGo: sin dato semanal reconocible en %s", url)
        # proyección del nivel general en el formato anterior: "la inflación general se proyecta en torno al 1,9% mensual"
        pg = re.search(r"inflación general se proyecta en torno al (\d+(?:,\d+)?)\s?% mensual", cuerpo)
        if per and pg:
            filas.append(base | {"periodo": f"{anio}-{mes:02d}", "medida": "proyeccion_mensual", "rubro": "total",
                                 "valor": _num(pg.group(1)), "nota": f"Proyección con datos a la {nom} (página de EcoGo; lectura automática)"})
    elif m_mes:
        mes, anio = MESES.index(m_mes.group(1).lower()) + 1, int(m_mes.group(2))
        v = re.search(r"RPM registró un (incremento|aumento|alza|baja) del (-?\d+(?:,\d+)?)\s?% mensual en el nivel general", cuerpo)
        if v:
            val = _num(v.group(2)) * (-1 if v.group(1) == "baja" else 1)
            filas.append(base | {"periodo": f"{anio}-{mes:02d}", "medida": "mensual", "rubro": "total", "valor": val,
                                 "nota": f"RPM de {MESES[mes - 1]} (página de EcoGo; lectura automática)"})
        else:
            log.warning("EcoGo: sin dato mensual reconocible en %s", url)
    # proyección del mes: "Noviembre se encamina a 1,9% mensual"
    for p in re.finditer(_MES + r" se encamina a (\d+(?:,\d+)?)\s?% mensual", cuerpo, re.I):
        mes = MESES.index(p.group(1).lower()) + 1
        anio = int(publicado[:4]) if publicado else date.today().year
        if publicado and mes == 12 and publicado[5:7] == "01":
            anio -= 1
        filas.append(base | {"periodo": f"{anio}-{mes:02d}", "medida": "proyeccion_mensual", "rubro": "total",
                             "valor": _num(p.group(2)), "nota": "Proyección del RPM (página de EcoGo; lectura automática)"})
        break
    return filas


def ecogo(cfg: Config, max_nuevos: int = 40) -> dict:
    """Lee los informes del RPM que todavía no están en data/otras/consultoras/ecogo.csv."""
    ruta = _dir(cfg) / "ecogo.csv"
    viejo = (pl.read_csv(ruta, infer_schema_length=0) if ruta.exists()
             else pl.DataFrame(schema={c: pl.Utf8 for c in COLUMNAS}))
    vistos_ruta = _dir(cfg) / "ecogo_vistos.txt"
    vistos = set(vistos_ruta.read_text(encoding="utf-8").split()) if vistos_ruta.exists() else set()
    cache: dict = {}
    if not _robots(LISTA_ECOGO, cache):
        return {"error": "robots.txt no permite la lista"}
    r = requests.get(LISTA_ECOGO, headers={"User-Agent": UA, "Accept": "text/html"}, timeout=40)
    r.raise_for_status()
    links = sorted(set(re.findall(r'href="(https://ecogo\.com\.ar/(?:[a-z]{2}/)?(?:relevamiento-precios-minoristas|informes)/rpm-[^"#?]+)"', r.text)))
    links = [u.rstrip("/") for u in links]
    nuevos, filas = 0, []
    for u in links:
        if u in vistos or nuevos >= max_nuevos:
            continue
        if not _robots(u, cache):
            log.info("EcoGo: robots.txt no permite %s", u)
            continue
        time.sleep(PAUSA)
        try:
            p = requests.get(u, headers={"User-Agent": UA, "Accept": "text/html"}, timeout=40)
            p.raise_for_status()
        except Exception as e:
            log.warning("EcoGo %s: %s", u, e)
            continue
        nuevos += 1
        vistos.add(u)
        filas += leer_rpm(p.text, u)
    if filas:
        nuevo = pl.DataFrame([{c: (None if f.get(c) is None else str(f.get(c))) for c in COLUMNAS} for f in filas])
        todo = pl.concat([viejo, nuevo], how="diagonal_relaxed").unique(
            ["periodo", "medida", "rubro", "url"], keep="last").sort("periodo", "medida")
        todo.select(COLUMNAS).write_csv(ruta)
    vistos_ruta.write_text("\n".join(sorted(vistos)), encoding="utf-8")
    return {"informes_nuevos": nuevos, "filas_nuevas": len(filas), "links": len(links)}


def leidos(cfg: Config) -> pl.DataFrame:
    """Lo que se leyó de las páginas de las consultoras, con las columnas de externos.csv."""
    partes = [pl.read_csv(f, infer_schema_length=0) for f in sorted(_dir(cfg).glob("*.csv"))]
    if not partes:
        return pl.DataFrame(schema={c: pl.Utf8 for c in COLUMNAS})
    return pl.concat(partes, how="diagonal_relaxed").select(COLUMNAS)
