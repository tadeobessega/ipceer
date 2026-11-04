"""Datos SINTÉTICOS de combustibles (precios y volúmenes por estación) y geometría de prueba
con el formato de Georef, para tests y demo. No representan precios ni límites reales."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

# provincia: (nombre, lon0, lat0, ancho, alto, nivel de precio, inflación mensual extra)
PROVINCIAS = {
    "02": ("Ciudad Autónoma de Buenos Aires", -58.53, -34.70, 0.2, 0.17, 1.00, 0.000),
    "06": ("Buenos Aires", -63.0, -40.0, 5.5, 6.0, 1.01, 0.000),
    "14": ("Córdoba", -65.5, -35.0, 3.0, 5.0, 1.03, 0.001),
    "50": ("Mendoza", -70.0, -37.5, 3.0, 5.0, 1.04, 0.002),
    "26": ("Chubut", -71.5, -46.0, 6.0, 3.5, 1.08, -0.003),
}
PRODS = [("Nafta (súper) entre 92 y 95 Ron", 1.0), ("Nafta (premium) de más de 95 Ron", 1.22),
         ("Gas Oil Grado 2", 1.04), ("Gas Oil Grado 3", 1.25), ("GNC", 0.55)]


def _rect(x0, y0, w, h):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h], [x0, y0]]]}


def geometria(destino: Path) -> dict:
    """Escribe provincias.geojson y departamentos.geojson (3 departamentos por provincia)."""
    destino.mkdir(parents=True, exist_ok=True)
    provs, deptos, cajas = [], [], {}
    for pid, (nombre, x0, y0, w, h, *_ ) in PROVINCIAS.items():
        provs.append({"type": "Feature", "geometry": _rect(x0, y0, w, h),
                      "properties": {"id": pid, "nombre": nombre, "centroide": {"lon": x0 + w / 2, "lat": y0 + h / 2}}})
        for k in range(3):
            did = f"{pid}{(k + 1) * 7:03d}"
            g = _rect(x0 + k * w / 3, y0, w / 3, h)
            cajas[did] = (x0 + k * w / 3, y0, w / 3, h, pid)
            deptos.append({"type": "Feature", "geometry": g, "properties": {
                "id": did, "nombre": f"Departamento {k + 1} de {nombre}", "provincia": {"id": pid, "nombre": nombre},
                "centroide": {"lon": x0 + (k + 0.5) * w / 3, "lat": y0 + h / 2}}})
    (destino / "provincias.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": provs}), encoding="utf-8")
    (destino / "departamentos.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": deptos}), encoding="utf-8")
    return cajas


def csv_precios(destino: Path, cajas: dict, desde=(2024, 1), meses: int = 33, seed: int = 1) -> Path:
    rng = random.Random(seed)
    lineas = ["indice_tiempo,anio,mes,empresa,cuit,direccion,localidad,provincia,region,idproducto,producto,"
              "idtipohorario,tipohorario,precio,volumen,idempresa,empresabandera,latitud,longitud"]
    estaciones = []
    for did, (x0, y0, w, h, pid) in cajas.items():
        for e in range(4 if pid != "02" else 8):
            estaciones.append((f"{did}-{e}", pid, x0 + rng.random() * w, y0 + rng.random() * h,
                               1 + 0.03 * math.sin(len(estaciones) * 1.7)))
    for t in range(meses):
        a, m = desde[0] + (desde[1] - 1 + t) // 12, (desde[1] - 1 + t) % 12 + 1
        for eid, pid, lon, lat, efecto in estaciones:
            nombre, *_r, nivel, extra = PROVINCIAS[pid]
            for k, (prod, rel) in enumerate(PRODS):
                if prod == "GNC" and pid not in ("02", "06"):
                    continue
                infl = (1.025 + extra) ** t * (1.04 if t % 6 == 0 and t else 1)
                precio = 1000 * rel * nivel * efecto * infl
                vol = round(rng.uniform(40, 120), 3)
                for turno in ("Diurno", "Nocturno"):
                    p = precio * (1.0 if turno == "Diurno" else 1.02)
                    lineas.append(f"{a}-{m:02d}-01,{a},{m},Empresa {eid},30-1-{eid},Ruta {eid},Localidad {eid[:5]},"
                                  f"{nombre},Region,{k},{prod},{1 if turno == 'Diurno' else 2},{turno},{p:.2f},{vol},"
                                  f"{eid},Bandera,{lat:.5f},{lon:.5f}")
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return destino
