"""Carga de configuración y de tablas auxiliares (regiones, ponderadores)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import polars as pl
import yaml

REGIONES = ["GBA", "Pampeana", "Noreste", "Noroeste", "Cuyo", "Patagonia"]


def raiz_repo() -> Path:
    """Raíz del repo: variable IPCEER_RAIZ o, si no está, el directorio de trabajo."""
    return Path(os.environ.get("IPCEER_RAIZ", Path.cwd())).resolve()


@dataclass
class Config:
    raiz: Path
    datos: dict

    @property
    def dir_config(self) -> Path:
        return self.raiz / "config"

    def ruta(self, clave: str) -> Path:
        valor = self.datos["rutas"][clave]
        if clave == "crudos" and os.environ.get("IPCEER_ARCHIVO"):
            valor = os.environ["IPCEER_ARCHIVO"]
        p = Path(valor).expanduser()
        if not p.is_absolute():
            p = self.raiz / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    def __getitem__(self, clave):
        return self.datos[clave]


def cargar_config(raiz: Path | None = None) -> Config:
    raiz = Path(raiz) if raiz else raiz_repo()
    with open(raiz / "config" / "config.yaml", encoding="utf-8") as f:
        datos = yaml.safe_load(f)
    return Config(raiz=raiz, datos=datos)


def ponderadores_region(cfg: Config) -> dict[str, float]:
    df = pl.read_csv(cfg.dir_config / "ponderadores_region.csv")
    return dict(zip(df["region"], df["ponderacion"]))


def ponderadores_clase(cfg: Config) -> pl.DataFrame:
    """Ponderadores por apertura COICOP y región (formato largo).

    Columnas: codigo, nombre, region, ponderacion (en % de la canasta regional).
    Sale de config/ponderadores_clase.csv, generado con
    scripts/extraer_ponderadores.py a partir de la Metodología INDEC n° 32 (Anexo II).
    """
    ruta = cfg.dir_config / "ponderadores_clase.csv"
    if not ruta.exists():
        raise FileNotFoundError(
            f"Falta {ruta}. Generalo con `python scripts/extraer_ponderadores.py` "
            "(ver README, sección Ponderadores)."
        )
    df = pl.read_csv(ruta, schema_overrides={"codigo": pl.Utf8})
    faltan = [r for r in REGIONES if r not in df.columns]
    if faltan:
        raise ValueError(f"{ruta} no tiene las columnas {faltan}")
    largo = df.unpivot(
        index=["codigo", "nombre"], on=REGIONES, variable_name="region", value_name="ponderacion"
    )
    return largo.with_columns(pl.col("ponderacion").cast(pl.Float64))
