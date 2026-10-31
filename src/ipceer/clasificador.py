"""Clasificación de productos en clases COICOP con reglas de texto (config/clasificador.yaml)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import polars as pl
import yaml

from .config import Config

SIN_CLASIFICAR = "sin_clasificar"


def normalizar_descripcion(texto: str | None) -> str:
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", texto.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return f" {t.strip()} "


@dataclass
class Regla:
    clase: str
    nombre: str
    incluir: list[re.Pattern]
    excluir: list[re.Pattern]

    def aplica(self, texto: str) -> bool:
        return any(p.search(texto) for p in self.incluir) and not any(p.search(texto) for p in self.excluir)


class Clasificador:
    def __init__(self, reglas: list[Regla], manual: dict[str, str] | None = None):
        self.reglas = reglas
        self.manual = manual or {}
        self.nombres = {r.clase: r.nombre for r in reglas}

    @classmethod
    def desde_config(cls, cfg: Config) -> "Clasificador":
        return cls.desde_archivos(cfg.dir_config / "clasificador.yaml",
                                  cfg.dir_config / "clasificacion_manual.csv")

    @classmethod
    def desde_archivos(cls, yaml_path: Path, manual_path: Path | None = None) -> "Clasificador":
        with open(yaml_path, encoding="utf-8") as f:
            datos = yaml.safe_load(f)
        reglas = [
            Regla(
                clase=str(r["clase"]),
                nombre=r.get("nombre", ""),
                incluir=[re.compile(p) for p in r.get("incluir", [])],
                excluir=[re.compile(p) for p in r.get("excluir", [])],
            )
            for r in datos["reglas"]
        ]
        manual = {}
        if manual_path and Path(manual_path).exists():
            m = pl.read_csv(manual_path, schema_overrides={"id_producto": pl.Utf8, "clase": pl.Utf8})
            manual = dict(zip(m["id_producto"], m["clase"]))
        return cls(reglas, manual)

    def clasificar_texto(self, descripcion: str | None) -> str:
        t = normalizar_descripcion(descripcion)
        for r in self.reglas:
            if r.aplica(t):
                return r.clase
        return SIN_CLASIFICAR

    def clasificar(self, df: pl.DataFrame) -> pl.DataFrame:
        """Agrega la columna `clase` a un DataFrame con id_producto y descripcion."""
        unicos = df.select("descripcion").unique()
        mapa = {d: self.clasificar_texto(d) for d in unicos["descripcion"].to_list()}
        df = df.with_columns(
            pl.col("descripcion").replace_strict(mapa, default=SIN_CLASIFICAR, return_dtype=pl.Utf8).alias("clase")
        )
        if self.manual:
            df = df.with_columns(
                pl.col("id_producto").replace_strict(self.manual, default=None, return_dtype=pl.Utf8)
                .fill_null(pl.col("clase")).alias("clase")
            )
        return df
