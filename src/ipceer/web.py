"""Precios minoristas de tiendas en línea (lo que SEPA no cubre: frescos por kilo, medicamentos,
ropa, calzado, electrodomésticos, muebles, materiales, juguetes, libros).

Qué baja: el catálogo público de cada tienda de config/web/tiendas.yaml, el mismo que consulta su
propia página (plataformas VTEX y Shopify). Un pedido por vez por tienda, con pausa, identificándose
como robot del IPCEER. Si una tienda limita o rechaza los pedidos se anota y se sigue de largo.

Qué guarda:
  data/web/AAAA-MM-DD.parquet       precios del día tal como salen (una fila por artículo y tienda)
  data/web/gh_AAAA-MM-DD.parquet    lo mismo, bajado por GitHub Actions (copia de respaldo)
  data/panel/web_mensual|web_semanal/<período>.parquet
                                    el mismo formato del panel de SEPA: precio del período =
                                    media geométrica de los días, con su clase del IPC

Los precios web son un método aparte: entran al índice solo en la variante "con precios web",
que se calcula siempre al lado del índice de SEPA para ir comparando las dos contra el INDEC.
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import polars as pl
import requests
import yaml

from .config import REGIONES, Config
from .lectura import CLAVE

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; ipceer-bot/1.0; +https://github.com/tadeobessega/ipceer)"
H = {"User-Agent": UA, "Accept": "application/json"}
PAUSA = 0.5            # segundos entre pedidos a una misma tienda
MAX_PEDIDOS = 900      # tope de pedidos por tienda y por corrida
MAX_SEGUNDOS = 1500    # tope de tiempo por tienda
VTEX_PASO, VTEX_TOPE = 50, 2500   # el buscador de VTEX no pagina más allá de 2.500 por categoría

ESQUEMA = {"fecha": pl.Utf8, "tienda": pl.Utf8, "sku": pl.Utf8, "producto": pl.Utf8, "nombre": pl.Utf8,
           "marca": pl.Utf8, "categoria": pl.Utf8, "ean": pl.Utf8, "unidad": pl.Utf8, "multiplicador": pl.Float64,
           "precio": pl.Float64, "precio_lista": pl.Float64, "disponible": pl.Boolean}


def _norm(t: str | None) -> str:
    t = unicodedata.normalize("NFKD", (t or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9/]+", " ", t).strip()


def dir_web(cfg: Config) -> Path:
    d = cfg.ruta("panel_diario").parent.parent / "web"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tiendas(cfg: Config) -> list[dict]:
    with open(cfg.dir_config / "web" / "tiendas.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["tiendas"]


# ---------------------------------------------------------------- descarga

class _Tienda:
    def __init__(self, t: dict):
        self.t, self.host = t, t["host"]
        self.s = requests.Session()
        self.s.headers.update(H)
        self.pedidos, self.t0, self.estado = 0, time.time(), "ok"

    def agotada(self) -> bool:
        return self.pedidos >= MAX_PEDIDOS or time.time() - self.t0 > MAX_SEGUNDOS

    def get(self, ruta: str, **params):
        """Un pedido con pausa. Ante 429 o 503 espera lo que pide el servidor (hasta 60 s) y reintenta
        una vez; si sigue, la tienda se deja para la próxima corrida."""
        for intento in (1, 2):
            time.sleep(PAUSA)
            self.pedidos += 1
            r = self.s.get(f"https://{self.host}{ruta}", params=params, timeout=40)
            if r.status_code in (429, 503) and intento == 1:
                try:
                    espera = min(60.0, float(r.headers.get("Retry-After", 20)))
                except ValueError:
                    espera = 20.0
                time.sleep(espera)
                continue
            return r
        return r


def _hojas(arbol: list[dict], patron: re.Pattern | None) -> list[tuple[str, str]]:
    """Categorías a recorrer: (ruta de ids, ruta de nombres) de cada hoja del árbol (3 niveles)."""
    out = []

    def bajar(n: dict, ids: list[str], nombres: list[str]):
        ids, nombres = [*ids, str(n["id"])], [*nombres, n.get("name") or ""]
        hijos = n.get("children") or []
        if hijos:
            for h in hijos:
                bajar(h, ids, nombres)
        else:
            out.append(("/".join(ids), " / ".join(nombres)))

    for n in arbol:
        if patron is None or patron.search(_norm(n.get("name"))):
            bajar(n, [], [])
    return out


def _filas_vtex(prods: list[dict], tienda: str, hoy: str, cat_defecto: str) -> list[dict]:
    filas = []
    for p in prods:
        cats = p.get("categories") or []
        cat = max(cats, key=len).strip("/").replace("/", " / ") if cats else cat_defecto
        for it in p.get("items") or []:
            of = None
            for s in it.get("sellers") or []:
                o = s.get("commertialOffer") or {}
                if (o.get("Price") or 0) > 0 and (of is None or s.get("sellerDefault")):
                    of = o
            if of is None:
                continue
            nombre = p.get("productName") or ""
            if it.get("name") and it["name"] != nombre and it["name"] not in nombre:
                nombre = f"{nombre} {it['name']}"
            filas.append({
                "fecha": hoy, "tienda": tienda, "sku": str(it.get("itemId")), "producto": str(p.get("productId")),
                "nombre": nombre, "marca": p.get("brand"), "categoria": cat, "ean": str(it.get("ean") or "") or None,
                "unidad": it.get("measurementUnit"), "multiplicador": float(it.get("unitMultiplier") or 1),
                "precio": float(of["Price"]),
                "precio_lista": float(of.get("PriceWithoutDiscount") or of.get("ListPrice") or of["Price"]),
                "disponible": bool(of.get("IsAvailable", (of.get("AvailableQuantity") or 0) > 0)),
            })
    return filas


def bajar_vtex(t: dict, hoy: str) -> tuple[list[dict], dict]:
    T = _Tienda(t)
    r = T.get("/api/catalog_system/pub/category/tree/3")
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        return [], {"estado": f"no responde el catálogo (HTTP {r.status_code})", "pedidos": T.pedidos}
    patron = re.compile(t["deptos"]) if t.get("deptos") else None
    hojas = _hojas(r.json(), patron)
    filas, vistos, cortadas = [], set(), 0
    for ids, nombres in hojas:
        desde = 0
        while desde < VTEX_TOPE and not T.agotada():
            r = T.get("/api/catalog_system/pub/products/search", fq=f"C:/{ids}/", _from=desde, _to=desde + VTEX_PASO - 1)
            if r.status_code not in (200, 206):
                if r.status_code in (429, 503):
                    T.estado = f"limitó los pedidos (HTTP {r.status_code}): quedó incompleta"
                break
            try:
                prods = r.json()
            except ValueError:
                break
            for f in _filas_vtex(prods, t["id"], hoy, nombres):
                if f["sku"] not in vistos:
                    vistos.add(f["sku"])
                    filas.append(f)
            m = re.search(r"/(\d+)$", r.headers.get("resources", ""))
            total = int(m.group(1)) if m else 0
            desde += VTEX_PASO
            if len(prods) < VTEX_PASO or desde >= total:
                if total > VTEX_TOPE:
                    cortadas += 1
                break
        if T.estado != "ok" or T.agotada():
            break
    if T.agotada() and T.estado == "ok":
        T.estado = "se llegó al tope de pedidos o de tiempo: quedó incompleta"
    return filas, {"estado": T.estado, "pedidos": T.pedidos, "categorias": len(hojas), "categorias_cortadas": cortadas,
                   "segundos": round(time.time() - T.t0)}


def bajar_shopify(t: dict, hoy: str) -> tuple[list[dict], dict]:
    T = _Tienda(t)
    filas, pagina = [], 1
    while not T.agotada() and pagina <= 80:
        r = T.get("/products.json", limit=250, page=pagina)
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            if pagina == 1:
                return [], {"estado": f"no responde el catálogo (HTTP {r.status_code})", "pedidos": T.pedidos}
            T.estado = f"se cortó en la página {pagina} (HTTP {r.status_code})"
            break
        prods = r.json().get("products") or []
        if not prods:
            break
        for p in prods:
            cat = " / ".join(x for x in [p.get("product_type") or "", " ".join((p.get("tags") or [])[:12])] if x)
            for v in p.get("variants") or []:
                try:
                    precio = float(v.get("price") or 0)
                    lista = float(v.get("compare_at_price") or 0) or precio
                except (TypeError, ValueError):
                    continue
                if precio <= 0:
                    continue
                nombre = p.get("title") or ""
                if v.get("title") and v["title"] != "Default Title":
                    nombre = f"{nombre} {v['title']}"
                filas.append({"fecha": hoy, "tienda": t["id"], "sku": str(v.get("id")), "producto": str(p.get("id")),
                              "nombre": nombre, "marca": p.get("vendor"), "categoria": cat, "ean": None, "unidad": "un",
                              "multiplicador": 1.0, "precio": precio, "precio_lista": max(lista, precio),
                              "disponible": bool(v.get("available", True))})
        pagina += 1
    return filas, {"estado": T.estado, "pedidos": T.pedidos, "segundos": round(time.time() - T.t0)}


def _una(t: dict, hoy: str) -> tuple[str, list[dict], dict]:
    try:
        filas, info = (bajar_shopify if t.get("tipo") == "shopify" else bajar_vtex)(t, hoy)
    except Exception as e:
        filas, info = [], {"estado": f"error: {str(e)[:120]}"}
    info["articulos"] = len(filas)
    log.info("Web %s: %d artículos, %s", t["id"], len(filas), info["estado"])
    return t["id"], filas, info


def bajar(cfg: Config, prefijo: str = "", solo: list[str] | None = None, hilos: int = 12) -> dict:
    """Baja todas las tiendas (en paralelo entre tiendas, de a un pedido por tienda) y guarda el día."""
    hoy = date.today().isoformat()
    lista = [t for t in tiendas(cfg) if not solo or t["id"] in solo]
    with ThreadPoolExecutor(hilos) as ex:
        res = list(ex.map(lambda t: _una(t, hoy), lista))
    filas = [f for _, fs, _ in res for f in fs]
    estado = {tid: info for tid, _, info in res}
    d = dir_web(cfg)
    ruta = d / f"{prefijo}{hoy}.parquet"
    if filas:
        nuevo = pl.DataFrame(filas, schema=ESQUEMA)
        if ruta.exists():  # segunda corrida del día: se queda con lo último de cada artículo
            nuevo = pl.concat([pl.read_parquet(ruta), nuevo], how="diagonal_relaxed").unique(["tienda", "sku"], keep="last")
        nuevo.write_parquet(ruta, compression="zstd")
    resumen = {"fecha": hoy, "generado": datetime.now().isoformat(timespec="seconds"), "articulos": len(filas),
               "tiendas_con_datos": sum(1 for i in estado.values() if i["articulos"]), "tiendas": estado}
    (d / f"{prefijo}estado.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=1), encoding="utf-8")
    return resumen


def bajar_respaldo(cfg: Config) -> list[str]:
    """Trae del release de GitHub los días que bajó GitHub Actions (archivos web_AAAA-MM-DD.parquet)."""
    rg = (cfg["descarga"].get("respaldo_github") or {})
    if not rg.get("repo"):
        return []
    d = dir_web(cfg)
    try:
        r = requests.get(f"https://api.github.com/repos/{rg['repo']}/releases/tags/{rg['tag']}", timeout=40,
                         headers={"Accept": "application/vnd.github+json", "User-Agent": UA})
        r.raise_for_status()
        assets = r.json().get("assets", [])
    except Exception as e:
        log.warning("Respaldo web en GitHub: %s", e)
        return []
    nuevos = []
    for a in assets:
        if a["name"] == "web_estado_github.json":
            try:
                b = requests.get(a["browser_download_url"], timeout=60, headers={"User-Agent": UA})
                b.raise_for_status()
                (d / "gh_estado.json").write_bytes(b.content)
            except Exception as e:
                log.warning("Respaldo web (estado): %s", e)
            continue
        m = re.fullmatch(r"web_(\d{4}-\d\d-\d\d)\.parquet", a["name"])
        if not m:
            continue
        destino = d / f"gh_{m.group(1)}.parquet"
        if destino.exists() and destino.stat().st_size == a.get("size"):
            continue
        try:
            b = requests.get(a["browser_download_url"], timeout=180, headers={"User-Agent": UA})
            b.raise_for_status()
            destino.write_bytes(b.content)
            nuevos.append(m.group(1))
        except Exception as e:
            log.warning("Respaldo web %s: %s", a["name"], e)
    return nuevos


# ---------------------------------------------------------------- clasificación

class ClasesWeb:
    def __init__(self, ruta: Path):
        with open(ruta, encoding="utf-8") as f:
            datos = yaml.safe_load(f)
        self.global_exc = [re.compile(p) for p in datos.get("excluir_global", [])]
        self.descartar = [re.compile(p) for p in datos.get("descartar_por_nombre", [])]
        self.reglas = [(str(r["clase"]), [re.compile(p) for p in r.get("incluir", [])],
                        [re.compile(p) for p in r.get("excluir", [])], bool(r.get("solo_categoria")))
                       for r in datos["reglas"]]
        self.nombres = {str(r["clase"]): r.get("nombre", "") for r in datos["reglas"]}

    def _buscar(self, texto: str, contexto: str, es_nombre: bool) -> str | None:
        for clase, inc, exc, solo_cat in self.reglas:
            if es_nombre and solo_cat:
                continue
            if any(p.search(texto) for p in inc) and not any(p.search(contexto) for p in exc):
                return clase
        return None

    def clase(self, categoria: str | None, nombre: str | None) -> str | None:
        """Se prueba primero con el nombre del producto (lo más específico), después con el último
        nivel de la categoría y por último con la categoría completa. Las exclusiones miran siempre
        la categoría completa y el nombre."""
        c, n = f" {_norm(categoria)} ", f" {_norm(nombre)} "
        if any(p.search(c) for p in self.global_exc) or any(p.search(n) for p in self.descartar):
            return None
        hoja = f" {c.split('/')[-1].strip()} "
        contexto = c + "|" + n
        for texto, es_nombre in ((n, True), (hoja, False), (c, False)):
            clase = self._buscar(texto, contexto, es_nombre)
            if clase:
                return clase
        return None


def clasificar(df: pl.DataFrame, cl: ClasesWeb) -> pl.DataFrame:
    pares = df.select("categoria", "nombre").unique()
    mapa = {(c, n): cl.clase(c, n) for c, n in pares.iter_rows()}
    return df.with_columns(pl.Series("clase", [mapa[(c, n)] for c, n in df.select("categoria", "nombre").iter_rows()],
                                     dtype=pl.Utf8))


# ---------------------------------------------------------------- panel

def dias_web(cfg: Config) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {}
    for p in sorted(dir_web(cfg).glob("*.parquet")):
        m = re.search(r"(\d{4}-\d\d-\d\d)$", p.stem)
        if m:
            out.setdefault(m.group(1), []).append(p)
    return out


def leer_dia(rutas: list[Path]) -> pl.DataFrame:
    """Un día: lo bajado en la PC y lo de GitHub, sin repetir artículos (gana lo de la PC)."""
    partes = [pl.read_parquet(p).with_columns(pl.lit(0 if not p.name.startswith("gh_") else 1).alias("_o")) for p in rutas]
    df = pl.concat(partes, how="diagonal_relaxed").sort("_o").unique(["tienda", "sku"], keep="first").drop("_o")
    return df.filter(pl.col("disponible"), pl.col("precio") > 0, pl.col("precio_lista") > 0)


def construir_paneles(cfg: Config) -> dict:
    """Consolida los días en meses y semanas con el formato del panel de SEPA. Se rehace entero en
    cada corrida (son archivos chicos), así un cambio en las reglas de clases se aplica hacia atrás."""
    from .panel import id_semana
    dias = dias_web(cfg)
    if not dias:
        return {"dias": 0}
    cl = ClasesWeb(cfg.dir_config / "web" / "clases.yaml")
    regs = {t["id"]: (REGIONES if t.get("regiones") in (None, "todas") else list(t["regiones"])) for t in tiendas(cfg)}
    partes = []
    for f, rutas in dias.items():
        try:
            partes.append(leer_dia(rutas).with_columns(pl.lit(f).alias("fecha")))
        except Exception as e:
            log.warning("Web %s: %s", f, e)
    if not partes:
        return {"dias": 0}
    todo = clasificar(pl.concat(partes, how="diagonal_relaxed"), cl)
    resumen_cl = (todo.filter(pl.col("fecha") == todo["fecha"].max()).group_by("clase")
                  .agg(pl.len().alias("articulos"), pl.col("tienda").n_unique().alias("tiendas")).sort("clase"))
    todo = todo.filter(pl.col("clase").is_not_null())
    reg_df = pl.DataFrame([{"tienda": t, "region": r} for t, rs in regs.items() for r in rs])
    todo = todo.join(reg_df, on="tienda", how="inner").with_columns(
        (pl.lit("web:") + pl.col("tienda")).alias("id_comercio"), pl.lit("1").alias("id_bandera"),
        pl.col("sku").alias("id_producto"), pl.col("multiplicador").cast(pl.Utf8).alias("cantidad"),
        pl.col("unidad").fill_null("un"),
        pl.col("fecha").str.slice(0, 7).alias("_mes"),
        pl.col("fecha").map_elements(lambda s: id_semana(date.fromisoformat(s)), return_dtype=pl.Utf8).alias("_sem"))
    hechos = {}
    for frecuencia, col in (("mensual", "_mes"), ("semanal", "_sem")):
        dest = cfg.ruta("panel_diario").parent / f"web_{frecuencia}"
        dest.mkdir(parents=True, exist_ok=True)
        for viejo in dest.glob("*.parquet"):
            viejo.unlink()
        for (per,), g in todo.group_by([col]):
            n_dias = g["fecha"].n_unique()
            panel = g.group_by(CLAVE).agg(
                pl.col("precio_lista").log().mean().exp(), pl.col("precio").log().mean().exp().alias("precio_efectivo"),
                pl.col("fecha").n_unique().alias("n_dias"), pl.lit(1.0).alias("n_sucursales"),
                pl.col("nombre").last().alias("descripcion"), pl.col("marca").last(), pl.lit("0").alias("es_ean"),
                pl.col("clase").last(),
            ).with_columns(pl.lit(per).alias("periodo"), pl.lit(n_dias).alias("dias_con_datos"))
            panel.write_parquet(dest / f"{per}.parquet", compression="zstd")
            hechos.setdefault(frecuencia, {})[per] = panel.height
    return {"dias": len(dias), "desde": min(dias), "hasta": max(dias), "paneles": hechos,
            "clases": [{"clase": c, "nombre": cl.nombres.get(c, "sin clase") if c else "sin clase (no se usa)",
                        "articulos": a, "tiendas": t} for c, a, t in resumen_cl.iter_rows()]}


def actualizar(cfg: Config, bajar_ahora: bool = True, prefijo: str = "", solo: list[str] | None = None) -> dict:
    res = {}
    if bajar_ahora:
        res["descarga"] = bajar(cfg, prefijo=prefijo, solo=solo)
    if not prefijo:
        res["respaldo"] = bajar_respaldo(cfg)
        res["panel"] = construir_paneles(cfg)
    estado = dir_web(cfg) / "estado.json"
    gh = dir_web(cfg) / "gh_estado.json"
    salida = {"actualizado": datetime.now().isoformat(timespec="seconds"), **res}
    if not bajar_ahora and estado.exists():
        salida["descarga"] = json.loads(estado.read_text(encoding="utf-8"))
    if gh.exists():
        salida["descarga_github"] = json.loads(gh.read_text(encoding="utf-8"))
    (cfg.ruta("resultados") / "web.json").write_text(json.dumps(salida, ensure_ascii=False, indent=1), encoding="utf-8")
    return salida
