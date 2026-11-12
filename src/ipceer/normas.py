"""Normas de tarifas reguladas, detectadas en Infoleg (Ministerio de Justicia), y lectura automática
de los cuadros tarifarios de electricidad del AMBA (decisión D-34 de METODOLOGIA.md).

1. Detección. Infoleg numera cada norma que carga con un id correlativo
   (servicios.infoleg.gob.ar/infolegInternet/verNorma.do?id=N). En cada corrida se recorren los ids
   nuevos desde el último visto, de a uno y con pausa, hasta encontrar una racha de ids vacíos. Se
   guardan las normas de los organismos y temas de config/regulados/normas.yaml (tarifas de luz,
   gas, agua, transporte, prepagas, telecomunicaciones). Quedan en data/otras/normas/normas.csv con
   su enlace, para revisarlas y cargarlas.
2. Lectura de cuadros de electricidad. En las resoluciones del ente regulador sobre Edenor y Edesur,
   se bajan los anexos en PDF y se lee el cuadro "Tarifa 1 - R ... sin subsidio" (cargo fijo y
   variable de R1 y R2). Los valores quedan en data/otras/normas/tarifas_auto.csv con el mismo
   formato que config/regulados/tarifas.csv, la norma y el enlace al anexo. Entran al índice de
   regulados solo si pasan el control de plausibilidad (D-34): la variación contra el último valor
   del mismo concepto tiene que estar entre −30% y +60%. Los que no lo pasan quedan marcados.
   Gas, agua y transporte todavía no se leen solos: sus normas se detectan y se cargan a mano.
Infoleg no tiene robots.txt que lo impida; igual se pide de a uno, con pausa e identificándose.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from datetime import date
from pathlib import Path

import requests
import yaml

from .config import Config

log = logging.getLogger(__name__)
BASE = "https://servicios.infoleg.gob.ar/infolegInternet"
UA = {"User-Agent": "IPCEER/1.0 (indice de precios del CEER; +https://github.com/tadeobessega/ipceer)"}
MESES = {m: i for i, m in enumerate(["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
                                      "septiembre", "octubre", "noviembre", "diciembre"], 1)}
COLS_TARIFAS = ["rubro", "codigo", "region", "concepto", "valor", "unidad", "vigente_desde", "vigente_hasta",
                "norma", "fuente_url"]


def _dir(cfg: Config) -> Path:
    from .otras_fuentes import _dir as dir_otras
    return dir_otras(cfg, "normas")


def _conf(cfg: Config) -> dict:
    ruta = cfg.dir_config / "regulados" / "normas.yaml"
    return yaml.safe_load(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}


def _texto(html: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    t = re.sub(r"(?s)<[^>]+>", "\n", t)
    t = t.replace("&nbsp;", " ").replace("&aacute;", "á").replace("&eacute;", "é").replace("&iacute;", "í") \
        .replace("&oacute;", "ó").replace("&uacute;", "ú").replace("&ntilde;", "ñ").replace("&Ntilde;", "Ñ")
    return re.sub(r"\n\s*\n+", "\n", t)


def _limpio(x: str) -> str:
    return re.sub(r"\s+", " ", _texto(x)).strip()


def leer_norma(html: str, nid: int) -> dict | None:
    """Datos de la ficha de una norma en Infoleg (verNorma.do). None si el id no tiene norma."""
    if 'id="Textos_Completos"' not in html:
        return None
    cab = re.search(r'<div id="Textos_Completos">\s*<p>\s*<strong>(.*?)</strong>', html, flags=re.S)
    if not cab:
        return None
    partes = [p for p in re.split(r"&nbsp;|<br>", cab.group(1)) if _limpio(p)]
    tipo_num = _limpio(" ".join(partes[:2])) if partes else ""
    m = re.match(r"(.+?)\s+([\d/\-]+(?:/\d{4})?)$", tipo_num)
    org = re.search(r'<span class="destacado">(.*?)</span>', html, flags=re.S)
    tit = re.search(r"<h1>(.*?)</h1>", html, flags=re.S)
    pub = re.search(r"Publicada en el Bolet[ií]n Oficial del\s*<a[^>]*>([^<]+)</a>", html)
    res = re.search(r"Resumen:</strong><br>(.*?)</p>", html, flags=re.S)
    fecha = re.search(r'<span class="vr_azul11">(.*?)</span>', html, flags=re.S)
    return {"id": nid, "tipo": m.group(1) if m else tipo_num, "numero": m.group(2) if m else "",
            "organismo": _limpio(org.group(1)) if org else _limpio(" ".join(partes[2:])),
            "fecha": _limpio(fecha.group(1)) if fecha else "", "publicada": _limpio(pub.group(1)) if pub else "",
            "titulo": _limpio(tit.group(1)) if tit else "", "resumen": _limpio(res.group(1))[:600] if res else "",
            "url": f"{BASE}/verNorma.do?id={nid}"}


def interesa(n: dict, conf: dict) -> str | None:
    """Rubro de la norma según config/regulados/normas.yaml, o None si no interesa."""
    texto = f"{n['organismo']} {n['titulo']} {n['resumen']}".upper()
    for r in conf.get("rubros", []):
        if re.search(r["organismo"], n["organismo"].upper()) and re.search(r["tema"], texto):
            return r["id"]
    return None


def anexos(nid: int, html_norma: str) -> list[str]:
    links = re.findall(r'href="([^"]+\.pdf)"', html_norma, flags=re.I)
    out = []
    for l in links:
        if l.startswith("http"):
            out.append(l)
        else:
            out.append(f"{BASE}/anexos/{(nid // 5000) * 5000}-{(nid // 5000) * 5000 + 4999}/{nid}/{l.lstrip('./')}")
    return out


def _num(s: str) -> float:
    s = s.strip()
    if re.fullmatch(r"[\d.]+,\d+", s):        # 1.234,56
        return float(s.replace(".", "").replace(",", "."))
    return float(s.replace(",", ""))          # 1,234.567 (formato de los anexos del ente)


def leer_cuadro_electricidad(texto: str) -> list[dict]:
    """Cargos de R1 y R2 'sin subsidio' de un cuadro tarifario de Edenor o Edesur."""
    t = re.sub(r"[ \t]+", " ", texto)
    m = re.search(r"vigencia a partir del (\d{1,2})°? de (\w+) de (\d{4})", t, flags=re.I)
    emp = re.search(r"\b(EDENOR|EDESUR)\b", t)
    if not m or not emp:
        return []
    mes = MESES.get(m.group(2).lower())
    if not mes:
        return []
    desde = date(int(m.group(3)), mes, int(m.group(1)))
    out = []
    # bloque "Tarifa 1 - R sin subsidio" hasta el próximo "Tarifa"
    b = re.search(r"Tarifa 1\s*-\s*R\s+sin subsidio(.+?)(?:\nTarifa |\Z)", t, flags=re.I | re.S)
    if not b:
        return []
    blo = b.group(1)
    for cat, rango in (("R1", r"0\s*-\s*150"), ("R2", r"151\s*-\s*400")):
        f = re.search(rf"{cat}\s+Cargo Fijo\s+{rango}\s+\$/mes\s+([\d.,]+)", blo)
        v = re.search(rf"Cargo Variable\s+{rango}\s+\$/kWh\s+([\d.,]+)", blo)
        nombre = "0 a 150 kWh" if cat == "R1" else "151 a 400 kWh"
        if f:
            out.append({"concepto": f"{emp.group(1).title()} T1-{cat} ({nombre}) sin subsidio: cargo fijo",
                        "valor": _num(f.group(1)), "unidad": "$/mes", "vigente_desde": desde.isoformat()})
        if v:
            out.append({"concepto": f"{emp.group(1).title()} T1-{cat} ({nombre}) sin subsidio: cargo variable",
                        "valor": _num(v.group(1)), "unidad": "$/kWh", "vigente_desde": desde.isoformat()})
    return out


def _fin_de_mes(d: str) -> str:
    a, m, _ = map(int, d.split("-"))
    sig = date(a + (m == 12), 1 if m == 12 else m + 1, 1)
    return date.fromordinal(sig.toordinal() - 1).isoformat()


def _pdf_texto(datos: bytes) -> str:
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(datos)).pages)


def plausible(filas: list[dict], previas: dict[str, float]) -> list[dict]:
    """Marca cada valor nuevo: ok si cambia entre −30% y +60% contra el último del mismo concepto."""
    for f in filas:
        prev = previas.get(f["concepto"])
        f["control"] = "ok" if prev is None or 0.7 <= f["valor"] / prev <= 1.6 else f"revisar ({f['valor'] / prev - 1:+.0%})"
    return filas


def actualizar(cfg: Config, max_ids: int = 600, racha_vacia: int = 60, pausa: float = 1.0) -> dict:
    conf = _conf(cfg)
    if not conf:
        return {"error": "sin config/regulados/normas.yaml"}
    d = _dir(cfg)
    est_p = d / "estado.json"
    est = json.loads(est_p.read_text(encoding="utf-8")) if est_p.exists() else {"ultimo_id": int(conf["desde_id"])}
    ruta_n, ruta_t = d / "normas.csv", d / "tarifas_auto.csv"
    normas = list(csv.DictReader(open(ruta_n, encoding="utf-8"))) if ruta_n.exists() else []
    tarifas = list(csv.DictReader(open(ruta_t, encoding="utf-8"))) if ruta_t.exists() else []
    vistos = {int(n["id"]) for n in normas}
    nid, vacios, ultimo_con_norma, revisados = est["ultimo_id"], 0, est["ultimo_id"], 0
    s = requests.Session()
    s.headers.update(UA)
    while revisados < max_ids and vacios < racha_vacia:
        nid += 1
        revisados += 1
        try:
            r = s.get(f"{BASE}/verNorma.do", params={"id": nid}, timeout=40)
            n = leer_norma(r.text, nid) if r.status_code == 200 else None
        except requests.RequestException as e:
            log.warning("Infoleg %s: %s", nid, e)
            n = None
        time.sleep(pausa)
        if n is None:
            vacios += 1
            continue
        vacios, ultimo_con_norma = 0, nid
        rubro = interesa(n, conf)
        if not rubro or nid in vistos:
            continue
        n["rubro"] = rubro
        n["detectada"] = date.today().isoformat()
        n["leida"] = ""
        if rubro == "electricidad":
            try:
                h = s.get(f"{BASE}/anexos/{(nid // 5000) * 5000}-{(nid // 5000) * 5000 + 4999}/{nid}/norma.htm", timeout=40)
                previas = {}
                for t in tarifas:
                    if t.get("control") == "ok":
                        previas[t["concepto"]] = float(t["valor"])
                nuevas = []
                for url in anexos(nid, h.text):
                    time.sleep(pausa)
                    pdf = s.get(url, timeout=60)
                    if pdf.status_code != 200 or not pdf.content.startswith(b"%PDF"):
                        continue
                    for f in leer_cuadro_electricidad(_pdf_texto(pdf.content)):
                        f.update({"rubro": "electricidad", "codigo": "04.5.1", "region": "GBA",
                                  "vigente_hasta": _fin_de_mes(f["vigente_desde"]),
                                  "norma": f"{n['tipo']} {n['numero']} ({n['organismo']}), leída automáticamente",
                                  "fuente_url": url, "infoleg_id": nid})
                        nuevas.append(f)
                nuevas = plausible(nuevas, previas)
                tarifas += nuevas
                n["leida"] = f"{len(nuevas)} valores" if nuevas else "sin cuadro residencial"
            except Exception as e:
                n["leida"] = f"error: {e}"[:200]
        normas.append(n)
        vistos.add(nid)
        log.info("Infoleg %s: %s %s %s (%s)", nid, n["organismo"], n["numero"], n["titulo"][:60], rubro)
    est["ultimo_id"] = ultimo_con_norma
    est["revisado"] = date.today().isoformat()
    est_p.write_text(json.dumps(est), encoding="utf-8")
    if normas:
        campos = ["id", "detectada", "rubro", "tipo", "numero", "organismo", "fecha", "publicada", "titulo", "resumen", "leida", "url"]
        with open(ruta_n, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
            w.writeheader()
            w.writerows(normas)
    if tarifas:
        with open(ruta_t, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS_TARIFAS + ["control", "infoleg_id"], extrasaction="ignore")
            w.writeheader()
            w.writerows(tarifas)
    return {"ultimo_id": ultimo_con_norma, "revisados": revisados,
            "normas": normas[-40:], "tarifas_leidas": len(tarifas)}


def tarifas_automaticas(cfg: Config) -> list[dict]:
    """Valores leídos solos que pasaron el control, para sumarlos a config/regulados/tarifas.csv."""
    ruta = _dir(cfg) / "tarifas_auto.csv"
    if not ruta.exists():
        return []
    return [{k: r[k] for k in COLS_TARIFAS} for r in csv.DictReader(open(ruta, encoding="utf-8")) if r.get("control") == "ok"]
