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
# Columnas de la base de regulados (D-38): sirven para análisis posteriores (variación real por
# jurisdicción, servicio, empresa y categoría) sin volver a leer las normas.
COLS_EXTRA = ["jurisdiccion", "servicio", "empresa", "categoria", "subzona", "componente", "origen", "control", "infoleg_id"]


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


def _meses(a: str, b: str) -> int:
    return abs((int(a[:4]) - int(b[:4])) * 12 + int(a[5:7]) - int(b[5:7]))


def plausible(filas: list[dict], previas) -> list[dict]:
    """Marca cada valor nuevo: ok si, contra el valor del mismo concepto más cercano en el tiempo, el
    cambio del más viejo al más nuevo está entre −30% y +60% por cada mes de distancia (como mínimo un
    mes). Como la historia se lee también hacia atrás (D-38), el vecino puede ser posterior.
    `previas` es la lista de tarifas ya aceptadas (o, por compatibilidad, un dict concepto: valor)."""
    if isinstance(previas, dict):
        previas = [{"concepto": k, "valor": v, "vigente_desde": None} for k, v in previas.items()]
    for f in filas:
        cands = [p for p in previas if p["concepto"] == f["concepto"]]
        if not cands:
            f["control"] = "ok"
            continue
        if f.get("vigente_desde") and all(p.get("vigente_desde") for p in cands):
            vec = min(cands, key=lambda p: _meses(p["vigente_desde"], f["vigente_desde"]))
            g = max(1, _meses(vec["vigente_desde"], f["vigente_desde"]))
            despues = vec["vigente_desde"] > f["vigente_desde"]
        else:
            vec, g, despues = cands[-1], 1, False
        v, pv = float(f["valor"]), float(vec["valor"])
        r = pv / v if despues else v / pv
        f["control"] = "ok" if 0.7 ** g <= r <= 1.6 ** g else f"revisar ({v / pv - 1:+.0%} contra {vec.get('vigente_desde') or 'el anterior'})"
    return filas


SUBZONAS = {"CAPITAL FEDERAL": "Ciudad de Buenos Aires", "BUENOS AIRES": "conurbano", "BUENOS AIRES NORTE": "conurbano norte"}
CATS_FIJO = ["R1", "R2 1°", "R2 2°", "R2 3°", "R3 1°", "R3 2°", "R3 3°", "R3 4°", "R4"]
EMPRESAS_GAS = {"METROGAS": "Metrogas", "NATURGY BAN": "Naturgy BAN", "GAS NATURAL BAN": "Naturgy BAN"}


def vigencia_de_norma(texto: str) -> str | None:
    """Fecha desde la que rigen los cuadros, según el articulado de la norma."""
    t = re.sub(r"\s+", " ", texto)
    m = re.search(r"vigencia a partir del (?:d[ií]a )?(\d{1,2})°? de (\w+) de (\d{4})", t, flags=re.I)
    if m and MESES.get(m.group(2).lower()):
        return date(int(m.group(3)), MESES[m.group(2).lower()], int(m.group(1))).isoformat()
    return None


def leer_cuadro_gas(paginas: list[str], empresa: str, vigencia: str) -> list[dict]:
    """Cargos residenciales sin subsidio de un cuadro tarifario de gas (formato de los anexos del
    ente regulador: los valores salen antes que los rótulos al extraer el texto). En la página de
    residenciales sin subsidios, el bloque que sigue a "TIPO DE CARGO <subzonas>" trae primero los 9
    cargos fijos (R1, R2 1° a 3°, R3 1° a 4°, R4) y después los cargos por m3; de estos se toma solo el
    primero (R1 y R2 1°), porque cuántas líneas trae y a qué categorías agrupa cambia entre empresas.
    En los cuadros de septiembre de 2026 de Metrogas y Naturgy BAN ese valor es también el de R2 2° y
    R2 3°."""
    for pag in paginas:
        if "TIPO DE CARGO" not in pag or not re.search(r"RESIDENCIALES SIN SUBSIDIOS", pag):
            continue
        cab = re.search(r"TIPO DE CARGO ([A-ZÁÉÍÓÚ ]+)\n", pag)
        if not cab:
            continue
        nombres = cab.group(1).strip()
        resto = nombres
        subz = []
        if resto.startswith("CAPITAL FEDERAL"):
            subz.append("CAPITAL FEDERAL")
            resto = resto[len("CAPITAL FEDERAL"):].strip()
        if resto:
            subz.append(resto)
        filas = []
        for linea in pag[cab.end():].splitlines():
            nums = re.findall(r"-?\d{1,3}(?:\.\d{3})*,\d+", linea)
            if not nums or len(nums) != len(subz) or re.search(r"[A-Za-z]", linea):
                break
            filas.append([float(x.replace(".", "").replace(",", ".")) for x in nums])
        if len(filas) < 10:
            continue
        out = []
        for j, z in enumerate(subz):
            zona = SUBZONAS.get(z, z.title())
            for cat, fila in zip(CATS_FIJO, filas[:9]):   # todas las categorías, para la base de regulados
                out.append({"concepto": f"{empresa} {cat} sin subsidio ({zona}): cargo fijo", "valor": fila[j],
                            "unidad": "$/mes", "vigente_desde": vigencia, "subzona": zona, "categoria": cat,
                            "componente": "cargo fijo"})
            out.append({"concepto": f"{empresa} R1-R2 1° sin subsidio ({zona}): cargo por metro cúbico",
                        "valor": filas[9][j], "unidad": "$/m3", "vigente_desde": vigencia, "subzona": zona,
                        "categoria": "R1-R2 1°", "componente": "cargo por m3"})
        return out
    return []


def _guardar_texto(d: Path, nid: int, nombre: str, texto: str) -> None:
    """Archiva el texto de cada anexo leído, para poder volver a leerlo si cambia el método."""
    t = d / "textos"
    t.mkdir(exist_ok=True)
    (t / f"{nid}_{Path(nombre).stem}.txt").write_text(texto, encoding="utf-8")


def _procesar(s: requests.Session, d: Path, nid: int, conf: dict, tarifas: list[dict], pausa: float) -> dict | None:
    r = s.get(f"{BASE}/verNorma.do", params={"id": nid}, timeout=40)
    n = leer_norma(r.text, nid) if r.status_code == 200 else None
    time.sleep(pausa)
    if n is None:
        return None
    rubro = interesa(n, conf)
    if not rubro:
        return {"_vacia": False}
    n.update({"rubro": rubro, "detectada": date.today().isoformat(), "leida": ""})
    if rubro not in ("electricidad", "gas"):
        return n
    try:
        base = f"{BASE}/anexos/{(nid // 5000) * 5000}-{(nid // 5000) * 5000 + 4999}/{nid}"
        h = s.get(f"{base}/norma.htm", timeout=40)
        _guardar_texto(d, nid, "norma", _texto(h.text))
        vig = vigencia_de_norma(_texto(h.text))
        previas = [t for t in tarifas if t.get("control") == "ok"]
        nuevas = []
        empresa = next((v for k, v in EMPRESAS_GAS.items() if k in f"{n['titulo']} {n['resumen']}".upper()), None)
        for url in anexos(nid, h.text):
            time.sleep(pausa)
            pdf = s.get(url, timeout=60)
            if pdf.status_code != 200 or not pdf.content.startswith(b"%PDF"):
                continue
            from pypdf import PdfReader
            paginas = [(p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf.content)).pages]
            _guardar_texto(d, nid, url.rsplit("/", 1)[-1], "\n\f".join(paginas))
            if rubro == "electricidad":
                filas = leer_cuadro_electricidad("\n".join(paginas))
                extra = {"rubro": "electricidad", "codigo": "04.5.1", "servicio": "electricidad"}
            elif empresa and vig:
                filas = leer_cuadro_gas(paginas, empresa, vig)
                extra = {"rubro": "gas", "codigo": "04.5.2", "servicio": "gas natural", "empresa": empresa}
            else:
                filas = []
            for f in filas:
                f.update(extra)
                f.update({"region": "GBA", "jurisdiccion": "Nación",
                          "empresa": f.get("empresa") or f["concepto"].split()[0],
                          "vigente_hasta": _fin_de_mes(f["vigente_desde"]),
                          "norma": f"{n['tipo']} {n['numero']} ({n['organismo']}), leída automáticamente",
                          "fuente_url": url, "infoleg_id": nid, "origen": "automatico"})
            nuevas += filas
        nuevas = plausible(nuevas, previas)
        tarifas += nuevas
        n["leida"] = f"{len(nuevas)} valores" if nuevas else ("sin cuadro residencial" if empresa or rubro == "electricidad" else "empresa fuera del AMBA")
    except Exception as e:
        n["leida"] = f"error: {e}"[:200]
    return n


def actualizar(cfg: Config, max_ids: int = 600, racha_vacia: int = 60, pausa: float = 1.0,
               max_retro: int = 900) -> dict:
    """Revisa los ids nuevos de Infoleg (hacia adelante) y, además, `max_retro` ids hacia atrás en
    cada corrida hasta `historia_hasta_id` de config/regulados/normas.yaml, para reconstruir la
    historia de las tarifas (D-38)."""
    conf = _conf(cfg)
    if not conf:
        return {"error": "sin config/regulados/normas.yaml"}
    d = _dir(cfg)
    est_p = d / "estado.json"
    est = json.loads(est_p.read_text(encoding="utf-8")) if est_p.exists() else {"ultimo_id": int(conf["desde_id"])}
    est.setdefault("retro_id", int(conf["desde_id"]))
    ruta_n, ruta_t = d / "normas.csv", d / "tarifas_auto.csv"
    normas = list(csv.DictReader(open(ruta_n, encoding="utf-8"))) if ruta_n.exists() else []
    tarifas = list(csv.DictReader(open(ruta_t, encoding="utf-8"))) if ruta_t.exists() else []
    vistos = {int(n["id"]) for n in normas}
    s = requests.Session()
    s.headers.update(UA)

    def registrar(n):
        if n and "id" in n and int(n["id"]) not in vistos:
            normas.append(n)
            vistos.add(int(n["id"]))
            log.info("Infoleg %s: %s %s %s (%s)", n["id"], n["organismo"], n["numero"], n["titulo"][:60], n["rubro"])

    # normas de luz y gas ya registradas pero nunca leídas (por ejemplo, detectadas antes de que
    # existiera el lector de gas): se leen ahora, hasta 40 por corrida
    for n in [n for n in normas if n.get("rubro") in ("electricidad", "gas") and not n.get("leida")][:40]:
        try:
            nuevo = _procesar(s, d, int(n["id"]), conf, tarifas, pausa)
            if nuevo and nuevo.get("leida") is not None:
                n["leida"] = nuevo["leida"] or "sin lectura"
                log.info("Infoleg %s releída: %s", n["id"], n["leida"])
        except requests.RequestException as e:
            log.warning("Infoleg %s: %s", n["id"], e)
    nid, vacios, ultimo, revisados = est["ultimo_id"], 0, est["ultimo_id"], 0
    while revisados < max_ids and vacios < racha_vacia:
        nid += 1
        revisados += 1
        try:
            n = _procesar(s, d, nid, conf, tarifas, pausa)
        except requests.RequestException as e:
            log.warning("Infoleg %s: %s", nid, e)
            n = None
        if n is None:
            vacios += 1
            continue
        vacios, ultimo = 0, nid
        registrar(n)
    est["ultimo_id"] = ultimo
    hasta, retro = int(conf.get("historia_hasta_id", est["retro_id"])), 0
    while est["retro_id"] > hasta and retro < max_retro:
        est["retro_id"] -= 1
        retro += 1
        try:
            registrar(_procesar(s, d, est["retro_id"], conf, tarifas, pausa))
        except requests.RequestException as e:
            log.warning("Infoleg %s: %s", est["retro_id"], e)
    est["revisado"] = date.today().isoformat()
    est_p.write_text(json.dumps(est), encoding="utf-8")
    if normas:
        campos = ["id", "detectada", "rubro", "tipo", "numero", "organismo", "fecha", "publicada", "titulo", "resumen", "leida", "url"]
        normas.sort(key=lambda x: int(x["id"]))
        with open(ruta_n, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
            w.writeheader()
            w.writerows(normas)
    if tarifas:
        tarifas.sort(key=lambda x: (x.get("concepto", ""), x.get("vigente_desde", "")))
        with open(ruta_t, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS_TARIFAS + COLS_EXTRA, extrasaction="ignore")
            w.writeheader()
            w.writerows(tarifas)
    return {"ultimo_id": ultimo, "revisados": revisados, "retro_id": est["retro_id"], "retro_revisados": retro,
            "normas": normas[-40:], "tarifas_leidas": len(tarifas)}


def tarifas_automaticas(cfg: Config) -> list[dict]:
    """Valores leídos solos que pasaron el control, para sumarlos a config/regulados/tarifas.csv."""
    ruta = _dir(cfg) / "tarifas_auto.csv"
    if not ruta.exists():
        return []
    return [{k: r[k] for k in COLS_TARIFAS} for r in csv.DictReader(open(ruta, encoding="utf-8")) if r.get("control") == "ok"]
