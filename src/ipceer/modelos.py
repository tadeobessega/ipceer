"""Rubros sin precios propios que entran al índice con un modelo (D-55): restaurantes y comidas
fuera del hogar (11.1) y educación (10.1, 10.2, 10.3 y 10.5).

Los coeficientes están congelados en config.yaml (`indice.modelos`); se estimaron con el IPC del
INDEC de GBA (2017-02 a 2026-08) y se probaron fuera de muestra (METODOLOGIA.md, D-55). Se aplican
igual en las seis regiones con los datos propios de cada región.

  Restaurantes (mes t, en %):
    w_alim × alimentos(t) + w_ngex × nivel general sin estos rubros(t) + w_sal × S(t)
    S(t) = promedio del aumento mensual del salario de convenio de gastronómicos (CCT 389/04, serie
    Representativa del boletín STEySS) en los meses t−7 a t−2. Sin salario disponible, la variante
    sin salario (w_alim y w_ngex propios).
  Educación (mes t, en %):
    marzo:   ln(1+v) = a + b × ln(1 + NG acumulado marzo a febrero) + c × ln(1 + educación acumulada
             abril a febrero)
    resto:   const + b_ngex × NG sin rubros(t) + b_ene × [enero] + b_dic × [diciembre]
             + b_par × índice de paritarias(t−1). Sin paritarias, la variante sin ese término.

"Alimentos" y "nivel general sin estos rubros" son los del propio IPCEER del período (grupo 01.1 y
total de un primer cálculo sin los modelos). Lo acumulado de marzo usa el IPC del INDEC de la región
en los meses publicados y, si falta alguno, el nivel general propio y la educación del modelo.

Salarios: repositorio público github.com/marcos-sacca/salarios (Excel del índice de paritarias,
acuerdos relevados de los meses que el boletín todavía no publicó y nowcast del índice). Se guarda
una copia en data/otras/salarios; si no se puede bajar, se usa la copia.

Semanal: restaurantes con la misma fórmula sobre las variaciones semanales (el término salarial
semanalizado: (1+S)^(7/30,44) − 1); educación por devengado de la variación del mes (cuotas que
rigen desde el día 1, como las tarifas, D-53).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from .config import REGIONES, Config

log = logging.getLogger(__name__)

ESQUEMA = {"periodo": pl.Utf8, "codigo": pl.Utf8, "region": pl.Utf8, "logrel": pl.Float64, "fuente": pl.Utf8}
CLASES_EDUC = ("10.1", "10.2", "10.3", "10.5")
MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "oct": 10,
         "nov": 11, "dic": 12}
REPO = "marcos-sacca/salarios"
RAW = f"https://raw.githubusercontent.com/{REPO}/main/"
API = f"https://api.github.com/repos/{REPO}/contents/"


# ---------------------------------------------------------------- utilidades de meses
def _mes(a: int, m: int) -> str:
    a, m = a + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{a}-{m:02d}"


def _mas(mes: str, k: int) -> str:
    a, m = map(int, mes.split("-"))
    return _mes(a, m + k)


def _mes_de_semana(sem: str) -> str:
    a, w = sem.split("-W")
    jueves = date.fromisocalendar(int(a), int(w), 4)
    return f"{jueves.year}-{jueves.month:02d}"


# ---------------------------------------------------------------- salarios
def _dir(cfg: Config) -> Path:
    d = cfg.ruta("panel_diario").parent.parent / "otras" / "salarios"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get(url: str) -> bytes:
    import os
    import requests
    h = {"User-Agent": "ipceer (CEER)"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok and url.startswith("https://api.github.com"):
        h["Authorization"] = f"Bearer {tok}"
    r = requests.get(url, timeout=60, headers=h)
    r.raise_for_status()
    return r.content


def bajar_salarios(cfg: Config, max_horas: float = 12) -> Path:
    """Copia local del Excel, los acuerdos (nowcast/acuerdos_*.csv) y el último nowcast del índice
    (estudio/nowcast_v*.csv). Si GitHub no responde, queda la copia anterior."""
    d = _dir(cfg)
    marca = d / "bajado.json"
    if marca.exists() and time.time() - marca.stat().st_mtime < max_horas * 3600:
        return d
    nombres, ok = ["Indice_Salarios_Paritarias.xlsx"], True
    for carpeta, patron in (("nowcast", r"acuerdos_.*\.csv$"), ("estudio", r"nowcast_v\d+\.csv$")):
        try:
            lista = json.loads(_get(API + carpeta))
            nombres += [f"{carpeta}/{x['name']}" for x in lista if re.match(patron, x["name"])]
        except Exception as e:
            ok = False
            log.warning("Salarios: no se pudo listar %s/%s (%s); quedan los archivos ya bajados", REPO, carpeta, e)
    for n in nombres:
        try:
            (d / n.replace("/", "__")).write_bytes(_get(RAW + n))
        except Exception as e:
            ok = False
            log.warning("Salarios: no se pudo bajar %s (%s); se usa la copia local", n, e)
    if ok:
        marca.write_text(json.dumps({"bajado": time.strftime("%Y-%m-%d %H:%M"), "archivos": nombres}), encoding="utf-8")
        log.info("Salarios: %d archivos de %s", len(nombres), REPO)
    return d


def _variaciones(niveles: dict[str, float]) -> dict[str, float]:
    ms = sorted(niveles)
    return {m: (niveles[m] / niveles[p] - 1) * 100 for p, m in zip(ms, ms[1:])
            if niveles[p] and _mas(p, 1) == m}


def _leer_excel(d: Path):
    ruta = d / "Indice_Salarios_Paritarias.xlsx"
    if not ruta.exists():
        return {}, {}
    s = pl.read_excel(ruta, sheet_name="Salarios")
    s = s.with_columns(pl.col("Fecha").cast(pl.Date).dt.strftime("%Y-%m").alias("mes"))
    g = s.filter((pl.col("Convenio") == "Gastronómicos") & (pl.col("Serie") == "Representativa"))
    gastro = _variaciones(dict(g.select("mes", "Salario conformado").iter_rows()))
    ix = pl.read_excel(ruta, sheet_name="Indice", read_options={"header_row": 7})
    col = next((c for c in ix.columns if c.startswith("Índice (base")), None)
    par = {}
    if col:
        ix = ix.filter(pl.col("Mes").is_not_null(), pl.col(col).cast(pl.Float64, strict=False) > 0)
        par = _variaciones({m.strftime("%Y-%m") if hasattr(m, "strftime") else str(m)[:7]: float(v)
                            for m, v in ix.select("Mes", col).iter_rows()})
    return gastro, par


def _acuerdos(d: Path, ultimo: str | None) -> dict[str, float]:
    """Gastronómicos en los meses que el boletín todavía no publicó (relevados de prensa)."""
    out = {}
    for ruta in sorted(d.glob("nowcast__acuerdos_*.csv")):
        anio = int(re.search(r"(\d{4})", ruta.name).group(1))
        filas = list(csv.DictReader(io.StringIO(ruta.read_text(encoding="utf-8-sig"))))
        cols = [c for c in (filas[0].keys() if filas else []) if c.lower().startswith("var %")]
        meses = [MESES.get(c.split()[-1].lower()[:3]) for c in cols]
        # el año del archivo es el del último mes; si los meses cruzan diciembre, los anteriores son del año previo
        anios, a = [], anio
        for i in range(len(meses) - 1, -1, -1):
            if i < len(meses) - 1 and meses[i] > meses[i + 1]:
                a -= 1
            anios.insert(0, a)
        for f in filas:
            if f.get("Convenio", "").strip() != "Gastronómicos":
                continue
            for c, m, a in zip(cols, meses, anios):
                if m and f.get(c) not in (None, ""):
                    out[f"{a}-{m:02d}"] = float(f[c])
    return {m: v for m, v in out.items() if ultimo is None or m > ultimo}


def _nowcast_indice(d: Path, ultimo: str | None) -> dict[str, float]:
    """Índice de paritarias estimado en los meses posteriores al último del Excel (último nowcast)."""
    rutas = sorted(d.glob("estudio__nowcast_v*.csv"), key=lambda p: int(re.search(r"v(\d+)", p.name).group(1)))
    if not rutas or ultimo is None:
        return {}
    out = {}
    for f in csv.DictReader(io.StringIO(rutas[-1].read_text(encoding="utf-8-sig"))):
        m = re.match(r"Indice m/m (\w+)", f.get("Concepto", ""))
        if m and MESES.get(m.group(1)[:3].lower()):
            k = MESES[m.group(1)[:3].lower()]
            mes = next(_mas(ultimo, i) for i in range(1, 13) if int(_mas(ultimo, i)[5:]) == k)
            out[mes] = float(str(f["Valor"]).replace(",", "."))
    return out


def salarios(cfg: Config) -> dict[str, dict[str, float]]:
    """Variación mensual (%) del salario de gastronómicos y del índice de paritarias, con los meses
    relevados o estimados del repositorio después del último boletín. Columna 'origen' aparte."""
    d = bajar_salarios(cfg, (cfg["indice"].get("modelos") or {}).get("salarios_max_horas", 12))
    try:
        gastro, par = _leer_excel(d)
    except Exception as e:
        log.warning("Salarios: no se pudo leer el Excel (%s)", e)
        gastro, par = {}, {}
    gastro.update(_acuerdos(d, max(gastro) if gastro else None))
    par.update(_nowcast_indice(d, max(par) if par else None))
    return {"gastro": gastro, "paritarias": par}


# ---------------------------------------------------------------- insumos del IPCEER y del INDEC
def _var(res: pl.DataFrame | None, nivel: str, codigo: str) -> dict[tuple[str, str], float]:
    if res is None or res.is_empty():
        return {}
    x = res.filter((pl.col("nivel") == nivel) & (pl.col("codigo") == codigo) & pl.col("region").is_in(REGIONES)
                   & pl.col("var_pct").is_not_null())
    return {(p, r): v for p, r, v in x.select("periodo", "region", "var_pct").iter_rows()}


def _indec(cfg: Config) -> dict[tuple[str, str, str], float]:
    """Variación mensual del INDEC por (codigo, region, mes) para el nivel general (0) y educación (10)."""
    from .indec import directorio
    from .validacion import leer_indec
    ind = leer_indec(directorio(cfg)).filter(pl.col("codigo").is_in(["0", "10"]), pl.col("region").is_in(REGIONES))
    out = {}
    for (c, r), g in ind.group_by("codigo", "region"):
        out.update({(c, r, m): v for m, v in _variaciones(dict(g.select("mes", "indice").iter_rows())).items()})
    return out


# ---------------------------------------------------------------- modelos
def _restaurantes(p: dict, alim: float, ngex: float, s: float | None) -> tuple[float, str]:
    if s is not None:
        w = p["con_salario"]
        return w["alim"] * alim + w["ngex"] * ngex + w["salario"] * s, "modelo_restaurantes"
    w = p["sin_salario"]
    return w["alim"] * alim + w["ngex"] * ngex, "modelo_restaurantes_sin_salario"


def _termino_salarial(gastro: dict[str, float], mes: str) -> float | None:
    meses = [_mas(mes, -k) for k in range(2, 8)]  # t−2 a t−7
    v = [gastro.get(m) for m in meses]
    return None if any(x is None for x in v) else sum(v) / 6


def _educ_mes(p: dict, mes: str, ngex: float, par: dict[str, float], ng_acum: float | None,
              ed_acum: float | None) -> tuple[float | None, str]:
    m = int(mes[5:])
    if m == 3:
        if ng_acum is None or ed_acum is None:
            return None, ""
        c = p["marzo"]
        return (math.exp(c["const"] + c["ng12"] * ng_acum + c["educ_abr_feb"] * ed_acum) - 1) * 100, "modelo_educacion_marzo"
    pp = par.get(_mas(mes, -1))
    c, fu = (p["con_paritarias"], "modelo_educacion") if pp is not None else (p["sin_paritarias"], "modelo_educacion_sin_paritarias")
    v = c["const"] + c["ngex"] * ngex + c["ene"] * (m == 1) + c["dic"] * (m == 12)
    if pp is not None:
        v += c["paritarias_t1"] * pp
    return v, fu


def mensual(cfg: Config, base: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """Variaciones modeladas por mes y región desde `indice.modelos.desde_mes`. `base` son los
    índices mensuales de un primer cálculo sin los modelos (de ahí salen alimentos y el nivel
    general sin estos rubros). Devuelve también el detalle de cada estimación (para el tablero)."""
    pm = cfg["indice"]["modelos"]
    desde = pm["desde_mes"]
    sal = salarios(cfg)
    alim, ngex = _var(base, "grupo", "01.1"), _var(base, "total", "TOTAL")
    ind = _indec(cfg)
    periodos = sorted({p for p, _ in ngex if p >= desde})
    filas, detalle, educ_prop = [], [], {}
    for p in periodos:
        for r in REGIONES:
            ng, al = ngex.get((p, r)), alim.get((p, r))
            if ng is None:
                continue
            fila_det = {"periodo": p, "region": r, "alimentos": al, "ngex": ng}
            if al is not None:
                s = _termino_salarial(sal["gastro"], p)
                v, fu = _restaurantes(pm["restaurantes"], al, ng, s)
                filas.append({"periodo": p, "codigo": "11.1", "region": r, "logrel": math.log1p(v / 100), "fuente": fu})
                fila_det.update(restaurantes=round(v, 4), salario_gastro_6m=s, fuente_restaurantes=fu)
            # educación: acumulados de marzo con el INDEC y, donde falta, lo propio
            ng_ac = ed_ac = None
            if p[5:] == "03":
                a = int(p[:4])
                meses_ng = [_mes(a - 1, 3 + k) for k in range(12)]       # marzo a febrero
                meses_ed = [_mes(a - 1, 4 + k) for k in range(11)]       # abril a febrero
                vng = [ind.get(("0", r, m), ngex.get((m, r))) for m in meses_ng]
                ved = [ind.get(("10", r, m), educ_prop.get((m, r))) for m in meses_ed]
                if all(x is not None for x in vng + ved):
                    ng_ac = sum(math.log1p(x / 100) for x in vng)
                    ed_ac = sum(math.log1p(x / 100) for x in ved)
            v, fu = _educ_mes(pm["educacion"], p, ng, sal["paritarias"], ng_ac, ed_ac)
            if v is not None:
                educ_prop[(p, r)] = v
                filas += [{"periodo": p, "codigo": c, "region": r, "logrel": math.log1p(v / 100), "fuente": fu}
                          for c in CLASES_EDUC]
                fila_det.update(educacion=round(v, 4), paritarias_t1=sal["paritarias"].get(_mas(p, -1)), fuente_educacion=fu)
            detalle.append(fila_det)
    df = pl.DataFrame(filas, schema=ESQUEMA) if filas else pl.DataFrame(schema=ESQUEMA)
    info = {"desde_mes": desde, "detalle": detalle,
            "salarios": {"gastro_ultimos": dict(sorted(sal["gastro"].items())[-8:]),
                         "paritarias_ultimos": dict(sorted(sal["paritarias"].items())[-4:]), "fuente": REPO}}
    return df, info


def semanal(cfg: Config, base: pl.DataFrame, mens: pl.DataFrame) -> pl.DataFrame:
    """Restaurantes con las variaciones semanales propias; educación por devengado del mes."""
    pm = cfg["indice"]["modelos"]
    desde = pm["desde_mes"]
    sal = salarios(cfg)
    alim, ngex = _var(base, "grupo", "01.1"), _var(base, "total", "TOTAL")
    filas = []
    for (sem, r), ng in sorted(ngex.items()):
        mes = _mes_de_semana(sem)
        al = alim.get((sem, r))
        if mes < desde or al is None:
            continue
        s = _termino_salarial(sal["gastro"], mes)
        s_sem = None if s is None else ((1 + s / 100) ** (7 / 30.44) - 1) * 100
        v, fu = _restaurantes(pm["restaurantes"], al, ng, s_sem)
        filas.append({"periodo": sem, "codigo": "11.1", "region": r, "logrel": math.log1p(v / 100), "fuente": fu})
    # educación: el nivel diario sube el día 1 de cada mes; la semana es el promedio de sus 7 días
    ed = mens.filter(pl.col("codigo") == CLASES_EDUC[0])
    for (r,), g in ed.group_by("region"):
        rel = dict(g.select("periodo", "logrel").iter_rows())
        fu = dict(g.select("periodo", "fuente").iter_rows())
        if not rel:
            continue
        ini = date.fromisoformat(min(rel) + "-01")
        ini -= timedelta(days=ini.weekday() + 7)  # una semana antes: la que contiene el día 1 tiene contra qué compararse
        ua, um = map(int, max(rel).split("-"))
        fin = date(ua + (um == 12), 1 if um == 12 else um + 1, 1)
        fin += timedelta(days=(7 - fin.weekday()) % 7 + 7)
        sem, meses_sem, acum, d = {}, {}, 0.0, ini
        while d < fin:
            if d.day == 1:
                acum += rel.get(d.strftime("%Y-%m"), 0.0)
            y, w, _ = d.isocalendar()
            k = f"{y}-W{w:02d}"
            sem.setdefault(k, []).append(math.exp(acum))
            meses_sem.setdefault(k, set()).add(d.strftime("%Y-%m"))
            d += timedelta(days=1)
        ks = [k for k in sorted(sem) if len(sem[k]) == 7]
        for k0, k1 in zip(ks, ks[1:]):
            mes = _mes_de_semana(k1)
            ms = meses_sem[k1]
            # entra si tiene algún día de un mes modelado y ninguno de un mes sin modelo (no se arrastra el nivel);
            # la semana que contiene el día 1 del primer mes modelado entra aunque su jueves sea del mes anterior
            if all(m < desde for m in ms) or any(m >= desde and m not in rel for m in ms):
                continue
            lr = math.log((sum(sem[k1]) / 7) / (sum(sem[k0]) / 7))
            f = fu.get(mes) or fu.get(_mas(mes, 1)) or "modelo_educacion"
            filas += [{"periodo": k1, "codigo": c, "region": r, "logrel": lr, "fuente": f} for c in CLASES_EDUC]
    return pl.DataFrame(filas, schema=ESQUEMA) if filas else pl.DataFrame(schema=ESQUEMA)
