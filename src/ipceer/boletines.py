"""Tarifas reguladas de la Ciudad y de la Provincia de Buenos Aires leídas de sus boletines (D-40).

Ciudad (Boletín Oficial porteño). La API pública del boletín (api-restboletinoficial.buenosaires.gob.ar,
robots.txt sin restricciones) devuelve cada edición entera en un JSON con todas sus normas, sumario y
anexos. Se recorre por número de edición: hacia adelante las nuevas y, en cada corrida, un tramo
hacia atrás hasta `historia_hasta_boletin` para reconstruir la historia. De cada edición se guardan
en normas_caba.csv las normas de tarifas (transporte, subte, taxis, estacionamiento, peajes, agua,
unidad tarifaria) y se leen:
  - colectivos de la Ciudad: resolución mensual de la Secretaría de Transporte que "aprueba los
    cuadros tarifarios"; el anexo es una imagen, así que se lee con OCR (tesseract). El mes de
    vigencia sale de la referencia del anexo ("Aprueba cuadros tarifarios - octubre 2026").
  - subte: resolución de directorio de SBASE "nuevos cuadros tarifarios"; el anexo trae texto. Rige
    desde el 5° día hábil posterior a la publicación; para la serie mensual se usa el mes en que
    cae ese día, contado sin feriados. Si la referencia del anexo nombra el mes ("CUADROS TARIFARIOS
    NOVIEMBRE 2026"), manda ese mes.

Provincia (normas.gba.gob.ar, robots.txt sin restricciones). Se busca "valores tarifarios" y
"cuadro tarifario" desde 2024; se guardan todas las normas encontradas en normas_pba.csv y se leen
los anexos de las del Ministerio de Transporte sobre autotransporte (tarifa urbana de GBA, sección
0-3 km). Las actualizaciones mensuales automáticas que la Provincia comunica sin una resolución
nueva no están en el buscador y siguen en la carga manual.

Todo lo leído pasa por normas.plausible y se guarda con las columnas de la base de regulados.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from .config import Config
from .normas import COLS_EXTRA, COLS_TARIFAS, MESES, _fin_de_mes, plausible

log = logging.getLogger(__name__)
UA = {"User-Agent": "IPCEER/1.0 (+https://github.com/tadeobessega/ipceer)"}
API_CABA = "https://api-restboletinoficial.buenosaires.gob.ar"
DOCS_CABA = "https://documentosboletinoficial.buenosaires.gob.ar/publico"
PBA = "https://normas.gba.gob.ar"
INTERES_CABA = (r"/SECT/|SBASE|SUBTE|cuadros? tarifari|tarifa|boleto|taxímetro|Estacionamiento Regulado|"
                r"peaje|AUSA|Unidad Tarifaria")
COLECTIVO_CABA = "Colectivo líneas de la Ciudad: boleto mínimo (0-3 km) con SUBE registrada"
SUBTE = "Subte: pasaje de 1 a 20 viajes mensuales con SUBE registrada"
COLECTIVO_PBA = "Colectivo líneas provinciales (200 en adelante): boleto mínimo (0-3 km) con SUBE registrada"
CAMPOS_NORMA = ["jurisdiccion", "id", "fecha", "nombre", "sumario", "rubro", "anexos", "leida", "url"]


def _dir(cfg: Config) -> Path:
    from .otras_fuentes import _dir as dir_otras
    return dir_otras(cfg, "boletines")


def _num(s: str) -> float:
    return float(s.replace(" ", "").replace(".", "").replace(",", "."))


def _normas_json(o):
    if isinstance(o, dict):
        if "id_norma" in o:
            yield o
        for v in o.values():
            yield from _normas_json(v)
    elif isinstance(o, list):
        for v in o:
            yield from _normas_json(v)


def rubro_caba(nombre: str, sumario: str) -> str | None:
    s = f"{nombre} {sumario}"
    if not re.search(INTERES_CABA, s, re.I):
        return None
    if re.search(r"SBASE|SUBTE", s) and re.search(r"cuadros? tarifari", s, re.I):
        return "subte"
    if "/SECT/" in nombre and re.search(r"cuadros? tarifari", s, re.I) and re.search(r"colectivo|automotor", s, re.I):
        return "colectivo"
    if re.search(r"taxímetro", s, re.I):
        return "taxi"
    if re.search(r"Estacionamiento", s, re.I):
        return "estacionamiento"
    if re.search(r"peaje|AUSA", s, re.I):
        return "peajes"
    if re.search(r"Unidad Tarifaria", s, re.I):
        return "unidad_tarifaria"
    return "otras" if re.search(r"tarif|boleto", s, re.I) else None


def leer_subte(texto: str) -> float | None:
    t = re.sub(r"\s+", " ", texto)
    m = re.search(r"Tarifa SUBTE TARIFA SIN NOMINALIZAR 1 al 20 viajes \$ ?([\d.]+,\d+)", t, re.I)
    return _num(m.group(1)) if m else None


def mes_de_referencia(texto: str) -> str | None:
    """Mes que nombra la referencia del anexo ("CUADROS TARIFARIOS NOVIEMBRE 2026")."""
    for m in re.finditer(r"CUADROS? TARIFARIOS?\s*-?\s*(\w+)\s+(\d{4})", re.sub(r"\s+", " ", texto), re.I):
        if MESES.get(m.group(1).lower()):
            return f"{m.group(2)}-{MESES[m.group(1).lower()]:02d}-01"
    return None


def leer_colectivo_caba(texto_ocr: str) -> dict:
    """Del OCR del cuadro I (servicios comunes, tarifa plena): 0-3, 3-6, 6-12 y 12-27 km, y el mes."""
    t = texto_ocr
    out = {}
    m = re.search(r"cuadros tarifarios\s*-\s*(\w+)\s+(\d{4})", t, re.I)
    if m and MESES.get(m.group(1).lower()):
        out["mes"] = f"{m.group(2)}-{MESES[m.group(1).lower()]:02d}-01"
    bloque = t.split("Cuadro II")[0]
    for sec in ("0-3", "3-6", "6-12", "12-27"):
        s = re.search(rf"{sec}\s*km\s*\$\s*([\d. ]+,\s*\d+)", bloque)
        if s:
            out[sec] = _num(s.group(1))
    return out


def leer_colectivo_pba(texto: str) -> dict:
    """Anexo de las resoluciones del Ministerio de Transporte bonaerense: tabla de Servicios Urbanos
    Provinciales Gran Buenos Aires, Tarifa Plena, fila 0-3 km: actual, nueva y SUBE sin nominar."""
    out = {}
    m = re.search(r"Tarifa\s+((?:[A-ZÁÉÍÓÚ]\s?){4,12})\s+(\d{4})", texto)
    if m:
        mes = MESES.get(m.group(1).replace(" ", "").lower())
        if mes:
            out["mes"] = f"{m.group(2)}-{mes:02d}-01"
    i = texto.find("Servicios Urbanos Provinciales Gran Buenos Aires")
    if i >= 0:
        s = re.search(r"0-3 km\s+([\d.]+,\d+)\s+([\d.]+,\d+)(?:\s+([\d.]+,\d+))?", texto[i:])
        if s:
            out["0-3"] = _num(s.group(2))
            if s.group(3):
                out["0-3 sin nominar"] = _num(s.group(3))
    return out


def _habiles(d: date, n: int) -> date:
    while n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _pdf_texto(c: bytes) -> str:
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(c)).pages)


def ocr_pdf(c: bytes) -> str | None:
    """Texto de un PDF escaneado con pdftoppm + tesseract, si están instalados."""
    if not (shutil.which("pdftoppm") and shutil.which("tesseract")):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "a.pdf"
        p.write_bytes(c)
        subprocess.run(["pdftoppm", "-r", "200", "-png", str(p), str(Path(tmp) / "pag")], check=True, timeout=120)
        textos = []
        for img in sorted(Path(tmp).glob("pag*.png")):
            r = subprocess.run(["tesseract", str(img), "-", "--psm", "6"], capture_output=True, text=True, timeout=120)
            textos.append(r.stdout)
    return "\n".join(textos)


def _fila(concepto, valor, desde, jur, servicio, empresa, categoria, componente, norma, url, codigo="07.3.2"):
    return {"rubro": "transporte", "codigo": codigo, "region": "GBA", "concepto": concepto, "valor": valor,
            "unidad": "$/viaje", "vigente_desde": desde, "vigente_hasta": _fin_de_mes(desde), "norma": norma,
            "fuente_url": url, "jurisdiccion": jur, "servicio": servicio, "empresa": empresa,
            "categoria": categoria, "subzona": "", "componente": componente, "origen": "automatico"}


def _leer_csv(ruta: Path) -> list[dict]:
    return list(csv.DictReader(open(ruta, encoding="utf-8"))) if ruta.exists() else []


def _escribir(ruta: Path, filas: list[dict], campos: list[str], orden) -> None:
    filas.sort(key=orden)
    with open(ruta, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)


def _procesar_norma_caba(s, d, x, fecha, rubro, tarifas) -> str:
    anexos = x.get("anexos") or []
    if rubro not in ("colectivo", "subte") or not anexos:
        return ""
    a = next((a for a in anexos if re.search(r"-ANX(-II)?\.pdf$", a["nombre_anexo"])), anexos[0])
    url = f"{DOCS_CABA}/{a['nombre_anexo']}"
    r = s.get(url, timeout=60)
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        r = s.get(a["filenet_firmado"], timeout=60)
    if not r.content.startswith(b"%PDF"):
        return "anexo no disponible"
    norma = f"{x['nombre']} (Boletín Oficial de la Ciudad, {fecha}), leída automáticamente"
    previas = [t for t in tarifas if t.get("control") == "ok"]
    nuevas = []
    if rubro == "subte":
        texto = _pdf_texto(r.content)
        (d / "textos").mkdir(exist_ok=True)
        (d / "textos" / f"caba_{x['id_norma']}.txt").write_text(texto, encoding="utf-8")
        v = leer_subte(texto)
        if v is None:
            return "sin tarifa de subte"
        mes = mes_de_referencia(texto)
        if mes:
            desde, nota = mes, "mes que nombra el anexo"
        else:
            pub = date(int(fecha[6:]), int(fecha[3:5]), int(fecha[:2]))
            desde, nota = _habiles(pub, 5).isoformat(), "rige desde el 5° día hábil de publicada, sin contar feriados"
        nuevas.append(_fila(SUBTE, v, desde, "Ciudad", "subte", "SBASE", "1 a 20 viajes", "pasaje",
                            f"{norma} ({nota})", url))
    else:
        texto = ocr_pdf(r.content)
        if texto is None:
            return "falta OCR (tesseract)"
        (d / "textos").mkdir(exist_ok=True)
        (d / "textos" / f"caba_{x['id_norma']}.txt").write_text(texto, encoding="utf-8")
        c = leer_colectivo_caba(texto)
        if "mes" not in c or "0-3" not in c:
            return "cuadro no leído"
        for sec in ("0-3", "3-6", "6-12", "12-27"):
            if sec in c:
                con = COLECTIVO_CABA if sec == "0-3" else f"Colectivo líneas de la Ciudad: sección {sec} km con SUBE registrada"
                nuevas.append(_fila(con, c[sec], c["mes"], "Ciudad", "colectivo", "", f"{sec} km",
                                    "boleto tarifa plena", norma + " (anexo leído con OCR)", url))
    nuevas = plausible(nuevas, previas)
    for f in nuevas:
        f["infoleg_id"] = f"caba:{x['id_norma']}"
    tarifas += nuevas
    return f"{len(nuevas)} valores"


def actualizar_caba(cfg: Config, conf: dict, pausa: float = 1.0, max_adelante: int = 40, max_retro: int = 150) -> dict:
    d = _dir(cfg)
    est_p = d / "estado_caba.json"
    est = json.loads(est_p.read_text(encoding="utf-8")) if est_p.exists() else {}
    inicio = int(conf.get("caba_desde_boletin", 7480))
    est.setdefault("ultimo", inicio - 1)
    est.setdefault("retro", inicio)
    hasta = int(conf.get("caba_historia_hasta_boletin", inicio))
    ruta_n, ruta_t = d / "normas_caba.csv", d / "tarifas_caba.csv"
    normas, tarifas = _leer_csv(ruta_n), _leer_csv(ruta_t)
    vistos = {n["id"] for n in normas}
    s = requests.Session()
    s.headers.update(UA)

    def edicion(num: int) -> bool | None:
        """True si había edición, False si la API devolvió otra (la última) o nada."""
        try:
            j = s.get(f"{API_CABA}/obtenerBoletin/{num}/true", timeout=90).json()
        except (requests.RequestException, ValueError) as e:
            log.warning("Boletín CABA %s: %s", num, e)
            return None
        b = j.get("boletin") or {}
        if num not in (b.get("numero"), b.get("numero2")):
            return False
        fecha = b.get("fecha_publicacion", "")
        for x in _normas_json(j.get("normas")):
            rub = rubro_caba(x.get("nombre", ""), x.get("sumario", ""))
            if not rub or str(x["id_norma"]) in vistos:
                continue
            try:
                leida = _procesar_norma_caba(s, d, x, fecha, rub, tarifas)
            except Exception as e:
                leida = f"error: {e}"[:200]
            normas.append({"jurisdiccion": "Ciudad", "id": str(x["id_norma"]), "fecha": fecha, "nombre": x["nombre"],
                           "sumario": x["sumario"], "rubro": rub, "anexos": " ".join(a["nombre_anexo"] for a in x.get("anexos") or []),
                           "leida": leida, "url": x.get("url_norma", "")})
            vistos.add(str(x["id_norma"]))
            log.info("Boletín CABA %s %s: %s (%s) %s", num, fecha, x["nombre"], rub, leida)
        return True

    adelante = 0
    while adelante < max_adelante:
        num = est["ultimo"] + 1
        ok = edicion(num)
        time.sleep(pausa)
        if not ok:
            if ok is False and edicion(num + 1):   # números salteados (ediciones dobles)
                est["ultimo"] = num + 1
                adelante += 2
                continue
            break
        est["ultimo"] = num
        adelante += 1
    retro = 0
    while est["retro"] > hasta and retro < max_retro:
        est["retro"] -= 1
        retro += 1
        edicion(est["retro"])
        time.sleep(pausa)
    est["revisado"] = date.today().isoformat()
    est_p.write_text(json.dumps(est), encoding="utf-8")
    if normas:
        _escribir(ruta_n, normas, CAMPOS_NORMA, lambda n: int(n["id"]))
    if tarifas:
        _escribir(ruta_t, tarifas, COLS_TARIFAS + COLS_EXTRA, lambda t: (t["concepto"], t["vigente_desde"]))
    return {"ultimo_boletin": est["ultimo"], "retro_boletin": est["retro"], "normas": len(normas),
            "tarifas": len(tarifas)}


def rubro_pba(titulo: str) -> str:
    t = titulo.upper()
    if "TRANSPORTE" in t and "AUTOTRANSPORTE" in t:
        return "colectivo"
    if "FLUVIAL" in t:
        return "fluvial"
    if "ENERG" in t and ("ELÉCTRICA" in t or "ELECTRICA" in t):
        return "electricidad"
    if "SANITARIO" in t or "AGUA" in t:
        return "agua"
    if "PEAJE" in t:
        return "peajes"
    return "otras"


def actualizar_pba(cfg: Config, conf: dict, pausa: float = 1.0) -> dict:
    d = _dir(cfg)
    ruta_n, ruta_t = d / "normas_pba.csv", d / "tarifas_pba.csv"
    normas, tarifas = _leer_csv(ruta_n), _leer_csv(ruta_t)
    vistos = {n["id"] for n in normas}
    s = requests.Session()
    s.headers.update(UA)
    desde = conf.get("pba_desde", "01/01/2024")
    for frase in conf.get("pba_frases", ["valores tarifarios", "cuadro tarifario"]):
        for pg in range(1, 30):
            u = (f"{PBA}/resultados?page={pg}&q%5Bterms%5D%5Braw_type%5D=Resolution&q%5Bphrase%5D="
                 + requests.utils.quote(frase) + "&q%5Bdate_ranges%5D%5Bpublication_date%5D%5Bgte%5D="
                 + requests.utils.quote(desde, safe=""))
            try:
                r = s.get(u, timeout=60)
            except requests.RequestException as e:
                log.warning("Normas PBA: %s", e)
                break
            time.sleep(pausa)
            res = list(re.finditer(r'href="(/ar-b/resolucion/(\d+)/(\d+)/(\d+))"[^>]*>(.*?)</a>(.{0,700})', r.text, re.S))
            if not res:
                break
            for m in res:
                nid = m.group(4)
                if nid in vistos:
                    continue
                txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(5) + m.group(6))).strip()
                titulo = txt.split(" Fecha de")[0][:400]
                rub = rubro_pba(titulo)
                fila = {"jurisdiccion": "Provincia", "id": nid, "fecha": "", "nombre": f"Resolución {m.group(3)}/{m.group(2)}",
                        "sumario": titulo, "rubro": rub, "anexos": "", "leida": "", "url": PBA + m.group(1)}
                f = re.search(r"Fecha de publicaci[oó]n:?\s*(\d{2}/\d{2}/\d{4})", txt)
                if f:
                    fila["fecha"] = f.group(1)
                if rub == "colectivo":
                    try:
                        fila.update(_procesar_pba(s, d, fila, tarifas, pausa))
                    except Exception as e:
                        fila["leida"] = f"error: {e}"[:200]
                normas.append(fila)
                vistos.add(nid)
                log.info("Normas PBA %s: %s (%s) %s", nid, titulo[:80], rub, fila["leida"])
    if normas:
        _escribir(ruta_n, normas, CAMPOS_NORMA, lambda n: int(n["id"]))
    if tarifas:
        _escribir(ruta_t, tarifas, COLS_TARIFAS + COLS_EXTRA, lambda t: (t["concepto"], t["vigente_desde"]))
    return {"normas": len(normas), "tarifas": len(tarifas)}


def _procesar_pba(s, d, fila, tarifas, pausa) -> dict:
    h = s.get(fila["url"], timeout=60)
    links = re.findall(r'href="(/anexos/descargar/[^"]+\.pdf)"', h.text)
    out = {"anexos": " ".join(links), "leida": "sin cuadro urbano GBA"}
    for l in links:
        time.sleep(pausa)
        c = s.get(PBA + l, timeout=60).content
        if not c.startswith(b"%PDF"):
            continue
        texto = _pdf_texto(c)
        (d / "textos").mkdir(exist_ok=True)
        (d / "textos" / f"pba_{fila['id']}_{Path(l).stem}.txt").write_text(texto, encoding="utf-8")
        v = leer_colectivo_pba(texto)
        if "0-3" in v and "mes" in v:
            norma = f"{fila['nombre']} del Ministerio de Transporte bonaerense, leída automáticamente"
            nuevas = [_fila(COLECTIVO_PBA, v["0-3"], v["mes"], "Provincia", "colectivo", "", "0-3 km",
                            "boleto tarifa plena", norma, PBA + l)]
            if "0-3 sin nominar" in v:
                nuevas.append(_fila("Colectivo líneas provinciales (200 en adelante): boleto mínimo (0-3 km) sin SUBE registrada",
                                    v["0-3 sin nominar"], v["mes"], "Provincia", "colectivo", "", "0-3 km",
                                    "boleto sin nominar", norma, PBA + l))
            nuevas = plausible(nuevas, [t for t in tarifas if t.get("control") == "ok"])
            for f in nuevas:
                f["infoleg_id"] = f"pba:{fila['id']}"
            tarifas += nuevas
            out["leida"] = f"{len(nuevas)} valores"
            break
    return out


def actualizar(cfg: Config) -> dict:
    import yaml
    ruta = cfg.dir_config / "regulados" / "boletines.yaml"
    conf = yaml.safe_load(open(ruta, encoding="utf-8")) if ruta.exists() else {}
    out = {}
    for nombre, f in (("caba", actualizar_caba), ("pba", actualizar_pba)):
        try:
            out[nombre] = f(cfg, conf)
        except Exception as e:
            log.warning("Boletines %s: %s", nombre, e)
            out[nombre] = {"error": str(e)[:300]}
    return out


def tarifas_automaticas(cfg: Config) -> list[dict]:
    d = _dir(cfg)
    out = []
    for n in ("tarifas_caba.csv", "tarifas_pba.csv"):
        out += [{k: r[k] for k in COLS_TARIFAS} for r in _leer_csv(d / n) if r.get("control") == "ok"]
    return out
