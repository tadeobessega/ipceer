"""Precios publicados en los sitios de las empresas (D-57, D-59): planes de telefonía e internet,
TV paga y streaming, y suscripciones a diarios.

Clases del IPC: 08.3.1 telefonía fija, 08.3.2 telefonía móvil, 08.3.3 internet. Se leen una vez por
día las páginas públicas de planes de Claro, Movistar y Personal (respetando robots.txt, con pausa
entre pedidos y sin entrar a nada que pida cuenta). De cada página se guarda el HTML comprimido (para
poder reprocesar) y una tabla con cada precio y su contexto: el texto de la tarjeta del plan (nombre,
GB, megas) que lo identifica de un día a otro.

Precio: el final con impuestos que paga un cliente que ya tiene el plan. Cuando la tarjeta muestra
un precio tachado ("antes") y uno de promoción, se usa el de lista; los precios "sin impuestos" se
descartan; si la tarjeta muestra dos precios sin tachar (lista y promoción), el mayor. El índice compara el mismo plan (misma empresa, página y contexto) entre períodos, Jevons
por empresa y clase, y promedia las empresas.
"""
from __future__ import annotations

import gzip
import hashlib
import logging
import math
import re
import time
import unicodedata
import urllib.robotparser
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import polars as pl
import requests

from .config import REGIONES, Config

log = logging.getLogger(__name__)

UA = "ipceer/1.0 (indice de precios; contacto en el repositorio tadeobessega/ipceer)"
PAUSA = 3.0
# (empresa, clase, url). Las páginas de hogar mezclan internet, TV y teléfono: la clase de cada
# precio se decide por su contexto (megas / fibra -> internet; línea fija -> fija).
PAGINAS = [
    ("claro", "08.3.2", "https://www.claro.com.ar/personas/planes-prepago-pospago"),
    ("claro", "hogar", "https://www.claro.com.ar/personas/internet-wifi-telefonia-tv"),
    ("movistar", "08.3.2", "https://www.movistar.com.ar/movil"),
    ("movistar", "08.3.2", "https://www.movistar.com.ar/prepago"),
    ("movistar", "08.3.2", "https://www.movistar.com.ar/productos-y-servicios/paquetes/pospago"),
    ("movistar", "hogar", "https://www.movistar.com.ar/productos-y-servicios/internet_b"),
    ("movistar", "hogar", "https://www.movistar.com.ar/productos-y-servicios/hogar"),
    ("personal", "08.3.2", "https://www.personal.com.ar/planes"),
    ("personal", "08.3.2", "https://www.personal.com.ar/prepago"),
    ("personal", "hogar", "https://www.personal.com.ar/internet"),
    # D-59: TV paga y streaming (servicios culturales, 09.4.2)
    # Netflix y Disney+ no permiten robots en sus páginas de planes (robots.txt)
    ("max", "09.4.2", "https://www.max.com/ar/es"),
    ("telecentro", "hogar", "https://www.telecentro.com.ar/"),
    # D-60: cine (entradas de Cinemark Hoyts por complejo y formato)
    ("cinemark", "09.4.2", "https://www.cinemark.com.ar/precios"),
    # D-60: paquetes turísticos (precios "desde" por destino; se prueba si la página los trae)
    ("despegar", "09.6", "https://www.despegar.com.ar/paquetes/"),
    # D-59: diarios (suscripciones digitales y en papel, 09.5.2)
    ("lanacion", "09.5.2", "https://www.lanacion.com.ar/suscripciones/"),
    ("clarin", "09.5.2", "https://www.clarin.com/suscripciones/landing.html"),
    ("cronista", "09.5.2", "https://www.cronista.com/suscripciones/"),
]
PRECIO = re.compile(r"\$\s?(\d{1,3}(?:\.\d{3})+|\d{3,6})(?:,(\d{2}))?")


def _dir(cfg: Config) -> Path:
    d = cfg.ruta("panel_diario").parent.parent / "otras" / "telefonia"
    d.mkdir(parents=True, exist_ok=True)
    return d


class _Texto(HTMLParser):
    """Texto visible por bloques (cada etiqueta de bloque corta), sin scripts ni estilos."""
    BLOQUE = {"div", "li", "p", "section", "article", "tr", "td", "h1", "h2", "h3", "h4", "h5", "br", "button", "a"}

    def __init__(self):
        super().__init__()
        self.partes, self._skip, self._tachado = [], 0, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        if tag in ("s", "del", "strike"):
            self._tachado += 1
            self.partes.append("\n[ANTES]")
        elif tag in ("h1", "h2", "h3", "h4"):
            self.partes.append("\n## ")  # título: identifica la sección (cine, plan) de los precios de abajo
        elif tag in self.BLOQUE:
            self.partes.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip = max(0, self._skip - 1)
        if tag in ("s", "del", "strike"):
            self._tachado = max(0, self._tachado - 1)
            self.partes.append("[/ANTES]\n")

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.partes.append(data.strip() + " ")


def texto(html: str) -> list[str]:
    p = _Texto()
    p.feed(html)
    lineas = [re.sub(r"\s+", " ", x).strip() for x in "".join(p.partes).split("\n")]
    return [x for x in lineas if x]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()


def _clase(pagina_clase: str, ctx: str) -> str | None:
    """En las páginas de telecomunicaciones, por el contexto de la tarjeta (las de móvil también
    publicitan fibra y TV): megas o fibra -> internet; TV sola -> TV paga (servicios culturales,
    09.4.2); línea fija sola -> fija; si no, la clase de la página. Las demás páginas (streaming,
    diarios) tienen clase fija."""
    if pagina_clase not in ("08.3.2", "hogar"):
        return pagina_clase
    c = _norm(ctx)
    if re.search(r"\b(\d+ ?mb|megas?|fibra)\b", c):
        return "08.3.3"
    if re.search(r"\b(tv|television|flow|disney|hbo|netflix|canales)\b", c):
        return "09.4.2"
    if re.search(r"\b(telefonia fija|linea fija|telefono fijo)\b", c):
        return "08.3.1"
    return None if pagina_clase == "hogar" else pagina_clase

# líneas que no son el precio de un plan: legales, beneficios, regalos, montos de recarga sueltos
RUIDO = re.compile(r"(legal|beneficio|regalo|gana|recomenda|credito|cashback|reintegro|si cargas|desafio|"
                   r"vigente (del|desde)|alcance nacional|oferta|promocion|sorteo|bases y condiciones|cuotas sin interes)")


def precios(lineas: list[str], ventana: int = 6) -> list[dict]:
    """Cada precio con su contexto: las líneas sin precios desde el precio anterior (nombre del plan,
    GB, megas; a lo sumo `ventana`). Marca si es tachado ("antes"), sin impuestos o de promoción, con
    el texto de la misma línea que está justo antes de la cifra."""
    out, ultimo, ctx, titulos, futuro = [], -1, "", [], False
    for i, l in enumerate(lineas):
        n_l = _norm(l)
        if l.startswith("## ") and not PRECIO.search(l):
            titulos = [l[3:].strip()]  # el título más cercano
        # precios anunciados que todavía no rigen ("a partir del día 19/11"): se descartan
        if re.search(r"a partir del?( dia)? \d", n_l):
            futuro = True
        elif re.search(r"(tarifas|precios) vigentes", n_l):
            futuro = False
        ms = list(PRECIO.finditer(l))
        if not ms:
            continue
        if len(l) > 140 or RUIDO.search(_norm(l)):
            ultimo = i
            continue
        previas = lineas[max(ultimo + 1, i - ventana):i]
        luego = bool(re.search(r"\bluego\b", _norm(l)))
        # "Durante 9 meses / Luego $22.400": el precio después de la promoción es de la misma tarjeta
        if previas and not (luego and ultimo >= 0 and i - ultimo <= 3):
            ctx = " | ".join(x[3:] if x.startswith("## ") else x for x in previas[-4:])
        for m in ms:
            v = float(m.group(1).replace(".", "") + ("." + m.group(2) if m.group(2) else ""))
            if v < 500:  # recargas mínimas y cifras sueltas
                continue
            antes_txt = _norm(l[max(0, m.start() - 40):m.start()])
            if futuro:
                continue
            tit = " > ".join(t for t in titulos if t not in ctx)
            out.append({"linea": l[:200], "contexto": ((tit + " | ") if tit else "") + ctx[:300], "precio": v,
                        "antes": "[antes]" in antes_txt[-12:] or bool(re.search(r"\b(antes|luego)\b\W*$", antes_txt)),
                        "sin_impuestos": bool(re.search(r"sin impuesto", antes_txt)),
                        "promo": bool(re.search(r"\b(off|descuento|promo|bonific|primer(os)? \d+ mes)", _norm(l)))})
        ultimo = i
    return out


def _robots(url: str, cache: dict) -> bool:
    base = re.match(r"https?://[^/]+", url).group(0)
    if base not in cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = requests.get(base + "/robots.txt", headers={"User-Agent": UA}, timeout=30)
            rp.parse(r.text.splitlines() if r.ok else [])
        except Exception:
            rp.parse([])
        cache[base] = rp
    return cache[base].can_fetch(UA, url)


def relevar(cfg: Config, hoy: date | None = None) -> pl.DataFrame:
    """Lee las páginas del día, guarda el HTML y devuelve la tabla de precios (y la guarda)."""
    hoy = hoy or date.today()
    d = _dir(cfg)
    dh = d / "html" / hoy.isoformat()
    dh.mkdir(parents=True, exist_ok=True)
    filas, robots = [], {}
    for empresa, clase, url in PAGINAS:
        if not _robots(url, robots):
            log.info("Telefonía: robots.txt no permite %s", url)
            continue
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "es-AR"}, timeout=60)
            time.sleep(PAUSA)
            if r.status_code != 200:
                log.warning("Telefonía: %s -> HTTP %s", url, r.status_code)
                continue
            html = r.text
        except Exception as e:
            log.warning("Telefonía: %s: %s", url, e)
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", url.split("//", 1)[1].lower()).strip("_")[:80]
        (dh / f"{slug}.html.gz").write_bytes(gzip.compress(html.encode("utf-8")))
        ps = precios(texto(html))
        for p in ps:
            c = _clase(clase, p["contexto"] + " " + p["linea"])
            if c is None:
                continue
            clave = hashlib.sha1(f"{empresa}|{url}|{_norm(re.sub(PRECIO, '', p['contexto']))}".encode()).hexdigest()[:16]
            filas.append({"fecha": hoy.isoformat(), "empresa": empresa, "clase": c, "url": url, "clave": clave, **p})
        log.info("Telefonía: %s %s -> %d precios", empresa, url, len(ps))
    df = pl.DataFrame(filas) if filas else pl.DataFrame()
    if not df.is_empty():
        df.write_parquet(d / f"{hoy.isoformat()}.parquet")
    return df


def _vigentes(cfg: Config) -> pl.DataFrame:
    """Un precio por plan y día: el de lista (tachado) si la tarjeta lo muestra, si no el único con
    impuestos; se descartan los sin impuestos y las tarjetas ambiguas (más de dos precios)."""
    ar = sorted(_dir(cfg).glob("20*.parquet"))
    if not ar:
        return pl.DataFrame()
    df = pl.concat([pl.read_parquet(a) for a in ar], how="diagonal_relaxed").filter(~pl.col("sin_impuestos"))
    g = df.group_by("fecha", "empresa", "clase", "clave").agg(
        pl.col("precio").filter(pl.col("antes")).max().alias("lista"),
        pl.col("precio").filter(~pl.col("antes")).alias("otros"))
    g = g.with_columns(pl.when(pl.col("lista").is_not_null()).then(pl.col("lista"))
                       .when(pl.col("otros").list.len() == 1).then(pl.col("otros").list.first())
                       # dos precios sin tachado: lista y promoción ("72% OFF por 6 meses"); el de lista es el mayor
                       .when(pl.col("otros").list.len() == 2).then(pl.col("otros").list.max())
                       .otherwise(None).alias("precio")).drop_nulls("precio")
    return g.select("fecha", "empresa", "clase", "clave", "precio").with_columns(pl.col("fecha").str.to_date())


def relativos(cfg: Config, frecuencia: str) -> pl.DataFrame:
    """Variación por clase (formato `extra` de indice.calcular, igual en todas las regiones: los
    planes son nacionales). Promedio del período por plan, Jevons por empresa, promedio de empresas."""
    v = _vigentes(cfg)
    esquema = {"periodo": pl.Utf8, "codigo": pl.Utf8, "region": pl.Utf8, "logrel": pl.Float64, "fuente": pl.Utf8}
    if v.is_empty():
        return pl.DataFrame(schema=esquema)
    if frecuencia == "semanal":
        v = v.with_columns(pl.col("fecha").map_elements(lambda f: "%d-W%02d" % f.isocalendar()[:2], return_dtype=pl.Utf8).alias("p"))
    else:
        v = v.with_columns(pl.col("fecha").dt.strftime("%Y-%m").alias("p"))
    m = v.group_by("p", "empresa", "clase", "clave").agg(pl.col("precio").log().mean().alias("lp")).sort("p")
    ps = sorted(m["p"].unique().to_list())
    filas = []
    for a, b in zip(ps, ps[1:]):
        x = m.filter(pl.col("p") == a).join(m.filter(pl.col("p") == b), on=["empresa", "clase", "clave"], suffix="_b")
        if x.is_empty():
            continue
        e = x.with_columns((pl.col("lp_b") - pl.col("lp")).alias("lr")).filter(pl.col("lr").abs() < math.log(2)) \
             .group_by("clase", "empresa").agg(pl.col("lr").mean(), pl.len().alias("n")) \
             .filter(pl.col("n") >= 2).group_by("clase").agg(pl.col("lr").mean())  # al menos 2 planes por empresa
        lrs = dict(e.iter_rows())
        # D-59: telefonía fija con el aumento de las mismas empresas en móvil e internet (suben todo
        # junto); ninguna publica la línea fija sola
        if "08.3.1" not in lrs and any(k in lrs for k in ("08.3.2", "08.3.3")):
            xs = [lrs[k] for k in ("08.3.2", "08.3.3") if k in lrs]
            lrs["08.3.1"] = sum(xs) / len(xs)
        for clase, lr in lrs.items():
            filas += [{"periodo": b, "codigo": clase, "region": r, "logrel": lr, "fuente": "telefonia"} for r in REGIONES]
    return pl.DataFrame(filas, schema=esquema) if filas else pl.DataFrame(schema=esquema)


def resumen(cfg: Config) -> dict:
    v = _vigentes(cfg)
    if v.is_empty():
        return {"dias": 0}
    u = v.filter(pl.col("fecha") == v["fecha"].max())
    return {"dias": v["fecha"].n_unique(), "desde": str(v["fecha"].min()), "hasta": str(v["fecha"].max()),
            "planes_ultimo_dia": u.group_by("empresa", "clase").len().sort("empresa", "clase").to_dicts()}
