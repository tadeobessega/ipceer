"""Otras fuentes de precios que no están en SEPA, bajadas en cada corrida de la tarea programada.

Solo fuentes públicas y que se pueden bajar sin saltear protecciones ni condiciones de uso:

- Alquileres: informe mensual "Index Zonaprop" de la Ciudad de Buenos Aires (PDF público). Se
  archiva cada informe y se extrae la variación mensual del precio de alquiler publicado.
  (Los portales de avisos no permiten bajar sus listados con robots; por eso se usa el informe.)
- Medicamentos: listado de precios de medicamentos para afiliados de PAMI (datos abiertos, CKAN).
  Se archiva cada versión; con dos versiones se puede medir la variación de los mismos productos.

Todo queda en data/otras/<fuente>/ y un resumen en resultados/otras_fuentes.json. Si una fuente no
responde, se anota el error y se sigue: nunca se completa un dato que no se bajó.
"""
from __future__ import annotations

import io
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

import requests

from .config import Config

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36"}


def _dir(cfg: Config, fuente: str) -> Path:
    d = cfg.ruta("panel_diario").parent.parent / "otras" / fuente
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meses_atras(n: int) -> list[tuple[int, int]]:
    hoy = date.today()
    out, a, m = [], hoy.year, hoy.month
    for _ in range(n):
        m -= 1
        if m == 0:
            a, m = a - 1, 12
        out.append((a, m))
    return out


def zonaprop(cfg: Config, meses: int = 6) -> list[dict]:
    """Baja los informes Index Zonaprop CABA de los últimos meses y extrae la variación mensual del
    alquiler. El informe del mes M se sube en la carpeta del mes M+1."""
    d = _dir(cfg, "zonaprop")
    filas = []
    for a, m in _meses_atras(meses):
        ruta = d / f"INDEX_CABA_REPORTE_{a}-{m:02d}.pdf"
        if not ruta.exists():
            sa, sm = (a + (m == 12), 1 if m == 12 else m + 1)
            url = (f"https://www.zonaprop.com.ar/blog/wp-content/uploads/{sa}/{sm:02d}/"
                   f"INDEX_CABA_REPORTE_{a}-{m:02d}.pdf")
            try:
                r = requests.get(url, headers=UA, timeout=60)
                if r.status_code != 200 or not r.content.startswith(b"%PDF"):
                    continue
                ruta.write_bytes(r.content)
                log.info("Zonaprop: %s", ruta.name)
            except requests.RequestException as e:
                log.warning("Zonaprop %s-%02d: %s", a, m, e)
                continue
        filas.append({"mes": f"{a}-{m:02d}", "archivo": ruta.name, **_zonaprop_alquiler(ruta)})
    return sorted(filas, key=lambda x: x["mes"])


def _n(s: str) -> float:
    """'909.058' -> 909058.0 ; '1.2' o '1,2' -> 1.2 (los informes usan punto decimal en los porcentajes)."""
    s = s.strip()
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        return float(s.replace(".", ""))
    return float(s.replace(",", "."))


def leer_zonaprop(texto: str) -> dict:
    """Saca del informe el alquiler medio publicado de un 2 ambientes (pesos por mes) y su variación
    mensual. Busca la frase que habla del ALQUILER medio: el informe también trae la de venta
    ("El precio medio sube...") y esa no se usa."""
    t = re.sub(r"\s+", " ", texto)
    m = re.search(r"alquiler medio de un departamento de (\d) ambientes[^$]{0,80}\$\s*([\d.]+)\s*por mes"
                  r"[^%]{0,40}?(sube|aumenta|crece|baja|cae|desciende|se mantiene)[^%\d]{0,15}(-?\d+[.,]?\d*)?\s*%?", t, re.I)
    if not m:
        return {"var_alquiler": None, "alquiler_2amb": None}
    out = {"ambientes": int(m.group(1)), "alquiler_2amb": _n(m.group(2)), "texto": m.group(0)[:220]}
    verbo = m.group(3).lower()
    if m.group(4):
        v = abs(_n(m.group(4)))
        out["var_alquiler"] = -v if verbo in ("baja", "cae", "desciende") else v
    else:
        out["var_alquiler"] = 0.0 if verbo == "se mantiene" else None
    return out


def _zonaprop_alquiler(ruta: Path) -> dict:
    try:
        import pdfplumber
    except ImportError:
        return {"error": "falta pdfplumber"}
    try:
        with pdfplumber.open(ruta) as pdf:
            txt = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:
        return {"error": str(e)}
    return leer_zonaprop(txt)


def pami(cfg: Config) -> dict:
    """Archiva la última versión del listado de precios de medicamentos de PAMI (CKAN)."""
    d = _dir(cfg, "pami")
    api = "https://datos.pami.org.ar/api/3/action/package_show"
    try:
        r = requests.get(api, params={"id": "medicamentos-para-afiliados"}, headers=UA, timeout=60)
        r.raise_for_status()
        res = r.json()["result"]["resources"]
    except Exception as e:
        log.warning("PAMI: %s", e)
        return {"error": str(e)}
    out = {"versiones": []}
    for rec in res:
        url = rec.get("url") or ""
        if not url.lower().endswith((".csv", ".xlsx", ".xls")):
            continue
        mod = (rec.get("last_modified") or rec.get("created") or "")[:10]
        nombre = f"pami_{mod or date.today().isoformat()}_{Path(url).name}"
        destino = d / nombre
        if not destino.exists():
            try:
                rr = requests.get(url, headers=UA, timeout=180)
                rr.raise_for_status()
                destino.write_bytes(rr.content)
                log.info("PAMI: %s", nombre)
            except requests.RequestException as e:
                log.warning("PAMI %s: %s", url, e)
                continue
        out["versiones"].append({"archivo": nombre, "modificado": mod})
    return out


BCRA = "https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias"
# series del BCRA que se buscan por su descripción (no por número, que puede cambiar)
BCRA_SERIES = {"tc_mayorista": ("tipo de cambio mayorista",), "icl": ("contratos de locaci",),
               "cer": ("coeficiente de estabilizaci",), "uva": ("unidad de valor adquisitivo",)}


def bcra(cfg: Config, desde: str = "2016-01-01") -> dict:
    """Series diarias del BCRA (API pública): tipo de cambio mayorista (Com. A 3500), ICL (índice
    para contratos de locación, que ajusta alquileres), CER y UVA. Quedan en data/otras/bcra/*.csv."""
    d = _dir(cfg, "bcra")
    out = {}
    try:
        variables, offset = [], 0
        while True:   # el catálogo de la v4.0 viene paginado (más de 1.000 series)
            r = requests.get(BCRA, params={"limit": 1000, "offset": offset}, headers=UA, timeout=60)
            r.raise_for_status()
            lote = r.json().get("results", [])
            variables += lote
            offset += 1000
            if len(lote) < 1000 or offset > 5000:
                break
    except Exception as e:
        log.warning("BCRA: %s", e)
        return {"error": str(e)}
    for clave, palabras in BCRA_SERIES.items():
        v = next((x for x in variables if all(w in (x.get("descripcion") or "").lower() for w in palabras)), None)
        if v is None:
            parecidas = [x.get("descripcion") for x in variables if "cambio" in (x.get("descripcion") or "").lower()]
            out[clave] = {"error": "no está en la lista de variables", "variables": len(variables),
                          "parecidas": parecidas[:15] if clave == "tc_mayorista" else []}
            continue
        filas, hasta = [], date.today()
        try:
            # la API devuelve hasta 1 año por pedido: se pide por tramos
            a = date.fromisoformat(desde)
            while a <= hasta:
                b = min(date(a.year + 1, a.month, a.day) if not (a.month == 2 and a.day == 29) else date(a.year + 1, 2, 28), hasta)
                rr = requests.get(f"{BCRA}/{v['idVariable']}", params={"desde": a.isoformat(), "hasta": b.isoformat(), "limit": 3000},
                                  headers=UA, timeout=60)
                rr.raise_for_status()
                res = rr.json().get("results", [])
                if res and isinstance(res[0], dict) and "detalle" in res[0]:
                    res = res[0]["detalle"]
                filas += [(x["fecha"], x["valor"]) for x in res]
                a = date.fromordinal(b.toordinal() + 1)
        except Exception as e:
            out[clave] = {"error": str(e), "id": v.get("idVariable")}
            continue
        filas = sorted(set(filas))
        (d / f"{clave}.csv").write_text("fecha,valor\n" + "\n".join(f"{f},{x}" for f, x in filas), encoding="utf-8")
        out[clave] = {"id": v.get("idVariable"), "descripcion": v.get("descripcion"), "n": len(filas),
                      "desde": filas[0][0] if filas else None, "hasta": filas[-1][0] if filas else None}
        log.info("BCRA %s: %d datos hasta %s", clave, len(filas), out[clave]["hasta"])
    return out


def afispop(cfg: Config) -> list[dict]:
    """Canasta de servicios públicos del AMBA del IIEP (UBA-CONICET): valores publicados, cargados
    a mano con su fuente en config/regulados/afispop.csv."""
    import csv
    ruta = cfg.dir_config / "regulados" / "afispop.csv"
    if not ruta.exists():
        return []
    with open(ruta, encoding="utf-8") as f:
        return [dict(r, valor=float(r["valor"])) for r in csv.DictReader(f)]


def actualizar(cfg: Config) -> dict:
    res = {"actualizado": datetime.now().isoformat(timespec="seconds")}
    from . import frescos
    for nombre, f in (("alquileres_zonaprop", zonaprop), ("medicamentos_pami", pami), ("bcra", bcra),
                      ("hacienda_canuelas", frescos.canuelas), ("frutas_hortalizas_mcba", frescos.mcba)):
        try:
            res[nombre] = f(cfg)
        except Exception as e:  # una fuente que falla no frena las demás
            log.warning("%s: %s", nombre, e)
            res[nombre] = {"error": str(e)}
    (cfg.ruta("resultados") / "otras_fuentes.json").write_text(json.dumps(res, ensure_ascii=False, indent=1),
                                                               encoding="utf-8")
    return res
