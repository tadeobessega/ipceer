"""Precios mayoristas de alimentos frescos, que SEPA casi no trae por kilo.

- Hacienda en pie: Mercado Agroganadero de Cañuelas (MAG), consulta pública "Precios por
  categoría" entre dos fechas. Se pide un mes por vez y se guarda el precio promedio ponderado
  ($ por kilo vivo = importe / kilos) de novillos, novillitos, vaquillonas, vacas, toros y MEJ.
- Frutas y hortalizas: Mercado Central de Buenos Aires (MCBA), archivos mensuales de precios
  mayoristas diarios (un .zip por mes para frutas y otro para hortalizas). Se archivan todos y se
  calcula el precio modal por kilo promedio del mes por especie.

Son precios MAYORISTAS: sirven para ver hacia dónde van la carne y la verdura antes de que llegue
el dato minorista, no reemplazan ningún precio de góndola ni entran al índice. Lo que no se pudo
bajar o leer queda anotado como error: no se completa con el mes anterior.
"""
from __future__ import annotations

import calendar
import io
import json
import logging
import re
import time
import zipfile
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import requests

from .config import Config

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36"}

MAG_URL = "https://www.mercadoagroganadero.com.ar/dll/hacienda1.dll/haciinfo000002"
MCBA_PAGINA = "https://mercadocentral.gob.ar/informaci%C3%B3n/precios-mayoristas"
MCBA_BASE = "https://mercadocentral.gob.ar"
GRUPOS_MAG = ("NOVILLITOS", "NOVILLOS", "VAQUILLONAS", "VACAS", "TOROS", "MEJ")
MESES_ES = {"ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4, "MAYO": 5, "JUNIO": 6, "JULIO": 7, "AGOSTO": 8,
            "SEPTIEMBRE": 9, "SEPTIENBRE": 9, "SETIEMBRE": 9, "OCTUBRE": 10, "NOVIEMBRE": 11, "DICIEMBRE": 12}


def _dir(cfg: Config, fuente: str) -> Path:
    d = cfg.ruta("panel_diario").parent.parent / "otras" / fuente
    d.mkdir(parents=True, exist_ok=True)
    return d


def _num(s: str) -> float | None:
    """'4.392,734' / '$420.604.300,00' / '95.750' (formato argentino) a número."""
    s = re.sub(r"[^\d,.\-]", "", s or "")
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


class _Filas(HTMLParser):
    """Devuelve las filas (listas de celdas de texto) de todas las tablas de una página."""

    def __init__(self):
        super().__init__()
        self.filas, self._fila, self._celda = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._fila = []
        elif tag in ("td", "th") and self._fila is not None:
            self._celda = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._celda is not None and self._fila is not None:
            self._fila.append(re.sub(r"\s+", " ", "".join(self._celda)).strip())
            self._celda = None
        elif tag == "tr" and self._fila is not None:
            if self._fila:
                self.filas.append(self._fila)
            self._fila = None

    def handle_data(self, data):
        if self._celda is not None:
            self._celda.append(data)


def leer_mag(html: str) -> dict:
    """Agrega la tabla 'Precios por categoría' del MAG por grupo: cabezas, importe, kilos y precio
    promedio ponderado ($ por kilo vivo). Solo usa las filas de detalle (las que traen categoría)."""
    p = _Filas()
    p.feed(html)
    grupos: dict[str, dict] = {}
    for f in p.filas:
        if len(f) < 9:
            continue
        nombre = f[0].upper()
        g = next((x for x in GRUPOS_MAG if nombre.startswith(x)), None)
        if not g:
            continue
        cab, imp, kg = _num(f[5]), _num(f[6]), _num(f[7])
        if not cab or not imp or not kg:
            continue
        a = grupos.setdefault(g, {"cabezas": 0.0, "importe": 0.0, "kilos": 0.0})
        a["cabezas"] += cab
        a["importe"] += imp
        a["kilos"] += kg
    if not grupos:
        return {}
    tot = {k: sum(g[k] for g in grupos.values()) for k in ("cabezas", "importe", "kilos")}
    grupos["TOTAL"] = tot
    return {g: {"precio": round(v["importe"] / v["kilos"], 3), "cabezas": int(v["cabezas"]), "kilos": int(v["kilos"])}
            for g, v in grupos.items()}


def canuelas(cfg: Config, meses: int = 30) -> dict:
    """Serie mensual de precios de la hacienda en Cañuelas. Los meses cerrados se guardan y no se
    vuelven a pedir; el mes en curso se pide siempre y se marca como parcial."""
    d = _dir(cfg, "canuelas")
    hoy = date.today()
    a, m, lista = hoy.year, hoy.month, []
    for _ in range(meses):
        lista.append((a, m))
        m -= 1
        if m == 0:
            a, m = a - 1, 12
    serie, errores = [], []
    for a, m in sorted(lista):
        clave = f"{a}-{m:02d}"
        ruta = d / f"{clave}.json"
        en_curso = (a, m) == (hoy.year, hoy.month)
        if ruta.exists() and not en_curso:
            serie.append(json.loads(ruta.read_text(encoding="utf-8")))
            continue
        fin = hoy.day if en_curso else calendar.monthrange(a, m)[1]
        try:
            r = requests.get(MAG_URL, headers=UA, timeout=60, params={
                "txtFechaIni": f"01/{m:02d}/{a}", "txtFechaFin": f"{fin:02d}/{m:02d}/{a}", "CP": "0", "LISTADO": "SI"})
            r.raise_for_status()
            r.encoding = r.encoding or "latin-1"
            g = leer_mag(r.text)
        except Exception as e:
            errores.append(f"{clave}: {e}")
            continue
        if not g:
            errores.append(f"{clave}: la consulta no trajo filas")
            continue
        fila = {"mes": clave, "hasta": f"{a}-{m:02d}-{fin:02d}", "parcial": en_curso, "grupos": g}
        if not en_curso:
            ruta.write_text(json.dumps(fila, ensure_ascii=False), encoding="utf-8")
        serie.append(fila)
        time.sleep(1)
    serie.sort(key=lambda x: x["mes"])
    for ant, act in zip(serie, serie[1:]):
        if _consecutivos(ant["mes"], act["mes"]):
            act["var"] = {g: round((v["precio"] / ant["grupos"][g]["precio"] - 1) * 100, 2)
                          for g, v in act["grupos"].items() if g in ant["grupos"]}
    return {"fuente": "Mercado Agroganadero de Cañuelas, precios por categoría", "url": MAG_URL,
            "unidad": "$ por kilo vivo, promedio ponderado del mes (importe / kilos)",
            "meses": serie, "errores": errores}


def _consecutivos(a: str, b: str) -> bool:
    ya, ma = map(int, a.split("-"))
    yb, mb = map(int, b.split("-"))
    return (yb * 12 + mb) - (ya * 12 + ma) == 1


def _mes_de_nombre(nombre: str) -> str | None:
    n = nombre.upper()
    mes = next((v for k, v in MESES_ES.items() if k in n), None)
    anio = re.search(r"(20\d\d)|[-_ ](\d\d)(?:[_.]|$)", n)
    if not mes or not anio:
        return None
    a = int(anio.group(1) or "20" + anio.group(2))
    return f"{a}-{mes:02d}"


def _leer_biff2(crudo: bytes) -> list[dict]:
    """Los archivos diarios del MCBA son planillas de Excel 2.0 (BIFF2), que las librerías actuales
    no abren. El formato es una lista de registros (tipo, largo, datos); alcanza con leer las
    celdas de texto (0x04), número (0x03) y entero (0x02). La primera fila son los encabezados."""
    import struct
    celdas: dict[int, dict[int, object]] = {}
    i, n = 0, len(crudo)
    while i + 4 <= n:
        tipo, largo = struct.unpack_from("<HH", crudo, i)
        d = crudo[i + 4:i + 4 + largo]
        i += 4 + largo
        if tipo in (2, 3, 4) and len(d) >= 7:
            fila, col = struct.unpack_from("<HH", d, 0)
            if tipo == 4:
                v = d[8:8 + d[7]].decode("latin-1").strip() if len(d) >= 8 else ""
            elif tipo == 3 and len(d) >= 15:
                v = struct.unpack_from("<d", d, 7)[0]
            elif tipo == 2 and len(d) >= 9:
                v = float(struct.unpack_from("<H", d, 7)[0])
            else:
                continue
            celdas.setdefault(fila, {})[col] = v
    if 0 not in celdas:
        return []
    enc = {c: str(v).strip().upper() for c, v in celdas[0].items()}
    return [{enc[c]: v for c, v in f.items() if c in enc} for k, f in sorted(celdas.items()) if k > 0]


def _leer_tabla(nombre: str, crudo: bytes) -> list[dict] | None:
    """Lee un archivo diario del MCBA como lista de filas (dict con encabezados en mayúscula)."""
    ext = nombre.lower().rsplit(".", 1)[-1]
    if ext not in ("xls", "xlsx", "csv", "txt"):
        return None
    if crudo[:2] == b"\t\x00":   # BOF de BIFF2
        return _leer_biff2(crudo)
    import polars as pl
    if ext in ("xls", "xlsx"):
        df = pl.read_excel(io.BytesIO(crudo), engine="calamine")
    else:
        txt = crudo.decode("latin-1")
        sep = ";" if txt.count(";") > txt.count(",") else ","
        df = pl.read_csv(io.StringIO(txt), separator=sep, infer_schema_length=0, truncate_ragged_lines=True)
    return df.rename({c: str(c).strip().upper() for c in df.columns}).to_dicts()


def _fecha_de_archivo(nombre: str, mes: str) -> str | None:
    """Los archivos diarios traen la fecha en el nombre (ddmmaa o ddmmaaaa)."""
    dig = re.findall(r"\d+", Path(nombre).stem)
    for d in dig:
        if len(d) in (6, 8):
            dd, mm = d[:2], d[2:4]
            if mm == mes[5:7] and 1 <= int(dd) <= 31:
                return f"{mes}-{dd}"
    return None


def _resumen_zip(ruta: Path, mes: str) -> dict:
    """Precio modal por kilo (columna MOPK) promedio por especie y día, y de ahí el promedio del mes.
    Dentro de cada día se promedian todas las variedades, procedencias, envases y calidades."""
    por_dia: dict[str, dict[str, float]] = {}
    miembros, problemas = [], []
    with zipfile.ZipFile(ruta) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            miembros.append(info.filename)
            dia = _fecha_de_archivo(info.filename, mes)
            try:
                filas = _leer_tabla(info.filename, z.read(info))
            except Exception as e:
                problemas.append(f"{info.filename}: {str(e)[:120]}")
                continue
            if filas is None:
                continue
            if not dia:
                problemas.append(f"{info.filename}: el nombre no trae una fecha de {mes}")
                continue
            if not filas or "ESP" not in filas[0] or "MOPK" not in filas[0]:
                problemas.append(f"{info.filename}: sin columnas ESP y MOPK")
                continue
            suma: dict[str, list[float]] = {}
            for f in filas:
                esp, p = str(f.get("ESP") or "").strip().upper(), f.get("MOPK")
                try:
                    p = float(str(p).replace(",", ".")) if p is not None else None
                except ValueError:
                    p = None
                if esp and p and p > 0:
                    suma.setdefault(esp, []).append(p)
            for esp, v in suma.items():
                por_dia.setdefault(esp, {})[dia] = sum(v) / len(v)
    especies = {e: {"precio": round(sum(v.values()) / len(v), 2), "dias": len(v)} for e, v in por_dia.items()}
    return {"miembros": len(miembros), "ejemplo": miembros[:3], "especies": especies, "problemas": problemas[:5]}


def mcba(cfg: Config) -> dict:
    """Archiva los .zip mensuales del Mercado Central y arma el precio mensual por especie."""
    d = _dir(cfg, "mcba")
    r = requests.get(MCBA_PAGINA, headers=UA, timeout=60)
    r.raise_for_status()
    enlaces = sorted(set(re.findall(r'href="([^"]*precios_mayoristas/[^"]+\.zip)"', r.text, re.I)))
    hoy = date.today()
    recientes = {f"{hoy.year}-{hoy.month:02d}"}
    archivos, errores = [], []
    for href in enlaces:
        url = href if href.startswith("http") else MCBA_BASE + href
        nombre = requests.utils.unquote(url.rsplit("/", 1)[-1]).strip()
        mes = _mes_de_nombre(nombre)
        tipo = "frutas" if nombre.upper().startswith("FRU") else "hortalizas" if nombre.upper().startswith("HORT") else "otros"
        ruta = d / re.sub(r"\s+", "_", nombre)
        if not ruta.exists() or mes in recientes:   # el mes en curso se va completando día a día
            try:
                z = requests.get(url, headers=UA, timeout=180)
                z.raise_for_status()
                ruta.write_bytes(z.content)
                time.sleep(1)
            except Exception as e:
                errores.append(f"{nombre}: {e}")
                if not ruta.exists():
                    continue
        fila = {"archivo": ruta.name, "url": url, "mes": mes, "tipo": tipo, "kb": ruta.stat().st_size // 1024}
        if mes:
            try:
                fila.update(_resumen_zip(ruta, mes))
            except Exception as e:
                fila["error"] = str(e)
        archivos.append(fila)
    # serie mensual por especie (si hay dos archivos del mismo mes y tipo, gana el más grande)
    mejor: dict[tuple, dict] = {}
    for f in archivos:
        if f.get("especies") and (k := (f["tipo"], f["mes"])) and f["kb"] >= mejor.get(k, {"kb": -1})["kb"]:
            mejor[k] = f
    serie = [{"mes": mes, "tipo": tipo, "especie": e, **v, "parcial": mes in recientes}
             for (tipo, mes), f in sorted(mejor.items(), key=lambda x: x[0][1]) for e, v in f["especies"].items()]
    previo = {}
    for s in serie:
        k = (s["tipo"], s["especie"])
        if k in previo and _consecutivos(previo[k]["mes"], s["mes"]):
            s["var"] = round((s["precio"] / previo[k]["precio"] - 1) * 100, 2)
        previo[k] = s
    for f in archivos:
        f.pop("especies", None)
    return {"fuente": "Mercado Central de Buenos Aires, precios mayoristas", "url": MCBA_PAGINA,
            "unidad": "$ por kilo, precio modal promedio del mes", "archivos": archivos, "serie": serie,
            "errores": errores}
