"""Construye el tablero: un único HTML con los datos embebidos (igual que los sitios del CEER)."""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config


def construir(cfg: Config, datos: dict, destino: Path | None = None) -> Path:
    web = cfg.raiz / "web"
    plantilla = (web / "template.html").read_text(encoding="utf-8")
    logo_pos = (web / "logo_pos.datauri").read_text().strip()
    logo_neg = (web / "logo_neg.datauri").read_text().strip()
    js = json.dumps(datos, ensure_ascii=False).replace("</", "<\\/")
    html = (plantilla.replace("__DATA__", js).replace("__LOGO_POS__", logo_pos)
            .replace("__LOGO_NEG__", logo_neg))
    destino = destino or (cfg.raiz / "sitio" / "index.html")
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(html, encoding="utf-8")
    return destino
