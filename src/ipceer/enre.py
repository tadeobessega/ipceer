"""Cuadros tarifarios mensuales de Edenor y Edesur publicados por el ENRE (D-48).

Fuente: el archivo público de cuadros del ENRE (https://www.enre.gov.ar/web/tarifasd.nsf/todoscuadros),
un documento por cuadro con las tarifas de cada categoría para las dos distribuidoras. robots.txt
del sitio lo permite; se baja un documento por vez con pausa, identificándose como robot del IPCEER,
y cada documento se guarda una sola vez (los cuadros publicados no cambian).

Qué guarda:
  data/otras/enre/<id>.html                 el documento tal como se publica
  data/otras/enre/indice.csv                id, rótulo y período de cada cuadro
  resultados/enre_cuadros.csv               una fila por cargo: período, tabla, categoría, cargo,
                                            rango, unidad, Edenor y Edesur

Lectura: cada tabla residencial ("Tarifa 1 - R ...", "Usuarios Residenciales") tiene categorías
(R1, R2, ...) y cargos (fijo en $/mes y variable en $/kWh, con su rango de consumo). Nada se
completa: un cargo que no se lee queda afuera y la factura de ese mes no se calcula.
"""
from __future__ import annotations

import csv
import html
import logging
import re
import time
from pathlib import Path

import shutil
import subprocess

import requests

from .config import Config

log = logging.getLogger(__name__)

BASE = "https://www.enre.gov.ar/web/tarifasd.nsf/todoscuadros"
UA = "Mozilla/5.0 (compatible; ipceer-bot/1.0; +https://github.com/tadeobessega/ipceer)"
PAUSA = 1.5


def _dir(cfg: Config) -> Path:
    d = cfg.ruta("panel_diario").parent.parent / "otras" / "enre"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _texto(s: str) -> str:
    t = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", "", s)
    t = re.sub(r"(?i)</(tr|p|h\d|div)>|<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</t[dh]>", " | ", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)


def _get(url: str) -> bytes:
    """El servidor del ENRE solo acepta cifrados viejos que la librería requests rechaza: se usa curl
    (que sí negocia) y requests queda de respaldo."""
    if shutil.which("curl"):
        r = subprocess.run(["curl", "-sS", "-f", "-L", "-m", "60", "-A", UA, url], capture_output=True)
        if r.returncode == 0:
            return r.stdout
        raise requests.RequestException(r.stderr.decode(errors="replace")[:200])
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    return r.content


def listar(cfg: Config, sesion=None) -> list[dict]:
    txt = _get(BASE + "?OpenView&Count=2000").decode("latin-1")
    out, vistos = [], set()
    for m in re.finditer(r'todoscuadros/([0-9A-F]{32})\?opendocument[^>]*>(.*?)</a>', txt, re.I | re.S):
        i, rot = m.group(1), _texto(m.group(2)).strip()
        if i in vistos:
            continue
        vistos.add(i)
        p = re.search(r"(\d{2})-(\d{4})", rot)
        out.append({"id": i, "rotulo": rot, "periodo": f"{p.group(2)}-{p.group(1)}" if p else ""})
    with open(_dir(cfg) / "indice.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "rotulo", "periodo"])
        w.writeheader()
        w.writerows(out)
    return out


def bajar(cfg: Config, max_docs: int = 400) -> int:
    lista = listar(cfg)
    d, n = _dir(cfg), 0
    for e in lista:
        ruta = d / f"{e['id']}.html"
        if ruta.exists() or n >= max_docs:
            continue
        try:
            ruta.write_bytes(_get(f"{BASE}/{e['id']}?opendocument"))
            n += 1
        except requests.RequestException as ex:
            log.warning("ENRE %s: %s", e["id"], ex)
        time.sleep(PAUSA)
    log.info("ENRE: %d cuadros nuevos (%d en el índice)", n, len(lista))
    return n


_NUM = re.compile(r"^-?[\d.,]+$")


def _num(x: str) -> float | None:
    x = x.strip()
    if not _NUM.match(x):
        return None
    # el ENRE usa coma de miles y punto decimal (1,870.51)
    return float(x.replace(",", ""))


def leer(cfg: Config) -> list[dict]:
    """Todas las filas residenciales (tarifa 1-R) de todos los cuadros bajados."""
    d = _dir(cfg)
    idx = {}
    if (d / "indice.csv").exists():
        idx = {r["id"]: r for r in csv.DictReader(open(d / "indice.csv", encoding="utf-8"))}
    filas = []
    for ruta in sorted(d.glob("*.html")):
        t = _texto(ruta.read_bytes().decode("latin-1"))
        mp = re.search(r"Per[ií]odo\s*(\d{1,2})/(\d{2,4})", t)
        if mp:
            a = mp.group(2)
            per = f"{('20' + a) if len(a) == 2 else a}-{int(mp.group(1)):02d}"
        else:
            per = idx.get(ruta.stem, {}).get("periodo", "")
        tabla, cat = "", ""
        for linea in t.split("\n"):
            l = linea.strip()
            if not l:
                continue
            celdas = [c.strip() for c in l.split("|")]
            celdas = [c for c in celdas if c != ""]
            # títulos de tabla: "Tarifa 1 - R sin subsidio", "Tarifa 1 - R Nivel 2 ...", "Usuarios Residenciales"
            if len(celdas) == 1 and re.match(r"(Tarifa\s*1\s*-\s*R\b|Usuarios Residenciales|Tarifa 1 -\s*G|Tarifa\s*[23]|TARIFAS SERVICIO)", celdas[0], re.I):
                tabla, cat = celdas[0], ""
                continue
            if len(celdas) >= 3 and re.match(r"^(Tarifa\s*1\s*-\s*)?R\s*\d", celdas[0], re.I) and "EDENOR" in l.upper():
                cat = re.sub(r"^Tarifa\s*1\s*-\s*", "", celdas[0], flags=re.I).replace(" ", "")
                continue
            if not cat or not re.search(r"\bR\b|Residencial", tabla, re.I):
                continue
            if len(celdas) >= 4 and re.match(r"Cargo (Fijo|Variable)", celdas[0], re.I):
                v1, v2 = _num(celdas[-2]), _num(celdas[-1])
                if v1 is None or v2 is None:
                    continue
                cargo = "fijo" if re.match(r"Cargo Fijo", celdas[0], re.I) else "variable"
                rango = re.sub(r"^Cargo (Fijo|Variable)\s*", "", celdas[0], flags=re.I).strip()
                filas.append({"periodo": per, "id": ruta.stem, "tabla": tabla, "categoria": cat, "cargo": cargo,
                              "rango": rango, "unidad": celdas[1] if len(celdas) >= 4 else "",
                              "edenor": v1, "edesur": v2})
    return filas


def guardar(cfg: Config) -> Path:
    filas = leer(cfg)
    ruta = cfg.ruta("resultados") / "enre_cuadros.csv"
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["periodo", "id", "tabla", "categoria", "cargo", "rango", "unidad", "edenor", "edesur"])
        w.writeheader()
        w.writerows(filas)
    log.info("ENRE: %d cargos residenciales en %s", len(filas), ruta)
    return ruta
