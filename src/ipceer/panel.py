"""Consolidación del panel diario en períodos (meses y semanas).

Cada archivo consolidado tiene una fila por producto-cadena-región con:
  precio_lista, precio_efectivo: media geométrica de los precios diarios del período
                                 (cada precio diario ya es la media geométrica entre sucursales)
  n_dias:          días del período en que se observó el producto
  dias_con_datos:  días del período con archivo SEPA descargado
  dias_periodo:    días calendario del período (7 en semanas, 28-31 en meses)
  clase:           clase COICOP asignada por el clasificador
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from .clasificador import Clasificador
from .config import Config
from .lectura import CLAVE

log = logging.getLogger(__name__)


def dias_disponibles(cfg: Config) -> list[date]:
    return sorted(date.fromisoformat(p.stem) for p in cfg.ruta("panel_diario").glob("*.parquet"))


def id_semana(d: date) -> str:
    a, s, _ = d.isocalendar()
    return f"{a}-W{s:02d}"


def rango_semana(sid: str) -> tuple[date, date]:
    a, s = sid.split("-W")
    lunes = date.fromisocalendar(int(a), int(s), 1)
    return lunes, lunes + timedelta(days=6)


def mes_de_semana(sid: str) -> str:
    """Mes al que se asigna una semana: el de su jueves (convención ISO)."""
    lunes, _ = rango_semana(sid)
    return (lunes + timedelta(days=3)).strftime("%Y-%m")


def periodos_disponibles(cfg: Config, frecuencia: str) -> list[str]:
    dias = dias_disponibles(cfg)
    if frecuencia == "mensual":
        return sorted({d.strftime("%Y-%m") for d in dias})
    return sorted({id_semana(d) for d in dias})


def _dias_del_periodo(periodo: str, frecuencia: str) -> list[date]:
    if frecuencia == "mensual":
        a, m = map(int, periodo.split("-"))
        return [date(a, m, d) for d in range(1, calendar.monthrange(a, m)[1] + 1)]
    lunes, _ = rango_semana(periodo)
    return [lunes + timedelta(days=i) for i in range(7)]


def consolidar(periodo: str, frecuencia: str, cfg: Config, clasificador: Clasificador | None = None) -> Path:
    dias = _dias_del_periodo(periodo, frecuencia)
    dir_diario = cfg.ruta("panel_diario")
    archivos = [dir_diario / f"{d.isoformat()}.parquet" for d in dias]
    archivos = [a for a in archivos if a.exists()]
    if not archivos:
        raise FileNotFoundError(f"No hay datos diarios para {periodo}")
    diario = pl.concat([pl.read_parquet(a) for a in archivos], how="vertical_relaxed")
    panel = diario.group_by(CLAVE).agg(
        pl.col("precio_lista").log().mean().exp(),
        pl.col("precio_efectivo").log().mean().exp(),
        pl.col("fecha").n_unique().alias("n_dias"),
        pl.col("n_sucursales").mean().alias("n_sucursales"),
        pl.col("descripcion").drop_nulls().last(),
        pl.col("marca").drop_nulls().last(),
        pl.col("es_ean").drop_nulls().last(),
    )
    clasificador = clasificador or Clasificador.desde_config(cfg)
    panel = clasificador.clasificar(panel).with_columns(
        pl.lit(periodo).alias("periodo"),
        pl.lit(len(archivos)).alias("dias_con_datos"),
        pl.lit(len(dias)).alias("dias_periodo"),
    )
    destino = cfg.ruta(f"panel_{frecuencia}") / f"{periodo}.parquet"
    panel.write_parquet(destino, compression="zstd")
    log.info("%s %s: %d productos-cadena-región, %d días con datos, %.1f%% clasificados", frecuencia,
             periodo, panel.height, len(archivos), 100 * (panel["clase"] != "sin_clasificar").mean())
    return destino


def actualizar_paneles(cfg: Config, frecuencia: str, todos: bool = False) -> list[Path]:
    """Consolida los períodos que no estén consolidados o que puedan haber cambiado.

    Un período se rehace si falta su archivo o si tiene menos días con datos de los que
    hay hoy en el panel diario (por ejemplo, la semana o el mes en curso).
    """
    clasificador = Clasificador.desde_config(cfg)
    dias = set(dias_disponibles(cfg))
    hechos = []
    for p in periodos_disponibles(cfg, frecuencia):
        destino = cfg.ruta(f"panel_{frecuencia}") / f"{p}.parquet"
        con_datos = sum(1 for d in _dias_del_periodo(p, frecuencia) if d in dias)
        if not todos and destino.exists():
            previo = pl.read_parquet(destino, columns=["dias_con_datos"])["dias_con_datos"][0]
            if previo == con_datos:
                continue
        hechos.append(consolidar(p, frecuencia, cfg, clasificador))
    return hechos
