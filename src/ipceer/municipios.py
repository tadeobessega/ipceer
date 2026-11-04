"""IPCEER por municipio: el mismo índice, con las sucursales de SEPA que caen dentro del
municipio (por coordenadas y los límites de Georef) y los ponderadores de su región del IPC.

Se configura en config.yaml (`municipios: [{id: "06568", nombre: "Morón", region: GBA}]`).
Cada municipio tiene su propio panel en data/municipios/<id>/ y sus resultados en
resultados/municipios/<id>/.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path

import polars as pl

from . import indice, panel
from .config import Config
from .descarga import fecha_de_zip
from .lectura import dir_geo, dir_municipio, procesar_zip_diario
from .resumen import proyeccion_mes, serie_semanal

log = logging.getLogger(__name__)


def config_municipio(cfg: Config, muni: dict) -> Config:
    d = dir_municipio(cfg, muni["id"])
    datos = copy.deepcopy(cfg.datos)
    datos["rutas"] |= {"panel_diario": str(d / "panel" / "diario"), "panel_semanal": str(d / "panel" / "semanal"),
                       "panel_mensual": str(d / "panel" / "mensual"),
                       "resultados": str(Path(cfg.ruta("resultados")) / "municipios" / muni["id"])}
    datos["municipios"] = []
    return Config(raiz=cfg.raiz, datos=datos)


def reprocesar(cfg: Config) -> int:
    """Arma los paneles diarios de los municipios con los zips archivados que todavía no los tienen."""
    munis = cfg.datos.get("municipios") or []
    geo = bool((cfg.datos.get("geo") or {}).get("activo", True))
    if not munis and not geo:
        return 0
    from . import geo as geo_mod
    hechas = geo_mod.semanas_hechas(cfg)
    n = 0
    for z in sorted(cfg.ruta("crudos").glob("sepa_*.zip")):
        f = fecha_de_zip(z)
        if f is None:
            continue
        falta_geo = geo and not (dir_geo(cfg) / "diario" / f"{f.isoformat()}.parquet").exists() \
            and geo_mod.semana_de(f) not in hechas
        if not falta_geo and all((dir_municipio(cfg, m["id"]) / "panel" / "diario" / f"{f.isoformat()}.parquet").exists() for m in munis):
            continue
        try:
            procesar_zip_diario(z, f, cfg, solo_municipios=True, geo=falta_geo)
            n += 1
        except Exception as e:
            log.warning("Municipios %s: %s", z.name, e)
    return n


def calcular(cfg: Config) -> dict:
    out = {}
    for m in cfg.datos.get("municipios") or []:
        cm = config_municipio(cfg, m)
        if not list(cm.ruta("panel_diario").glob("*.parquet")):
            continue
        reg = m.get("region", "GBA")
        res = {"nombre": m["nombre"], "region": reg}
        for f in ("semanal", "mensual"):
            panel.actualizar_paneles(cm, f)
            try:
                r = indice.calcular(cm, f)
            except (ValueError, FileNotFoundError) as e:
                log.info("Municipio %s (%s): %s", m["nombre"], f, e)
                continue
            indice.guardar(r, cm, f)
            ind = r["indices"].filter(pl.col("region") == reg)
            if "tramo" in ind.columns:
                ind = ind.filter(pl.col("tramo") == ind["tramo"].max())
            series = {}
            for cod in ("TOTAL", "01", "02", "05", "09", "12"):
                s = ind.filter(pl.col("codigo") == cod, pl.col("nivel") == ("total" if cod == "TOTAL" else "division"))
                if not s.height:
                    continue
                if f == "semanal":
                    sem = serie_semanal(s)
                    series[cod] = {"semanal": sem, "proyeccion": proyeccion_mes(sem, sem[-1]["mes"]) if sem else None}
                else:
                    series[cod] = {"mensual": [{"mes": x["periodo"], "indice": x["indice"], "var": x["var_pct"],
                                                "parcial": x["parcial"]} for x in s.sort("periodo").iter_rows(named=True)]}
            for cod, v in series.items():
                res.setdefault("series", {}).setdefault(cod, {}).update(v)
            res.setdefault("cobertura", {})[f] = r["cobertura"].filter(pl.col("region") == reg)[
                "ponderacion_cubierta_pct"].to_list()
        out[m["id"]] = res
    return out
