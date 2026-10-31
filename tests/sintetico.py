"""Generador de zips con la estructura de SEPA y datos SINTÉTICOS, para tests y demo.

Los precios siguen una tendencia por clase, con saltos a comienzo de mes, ruido,
promociones y faltantes. No representan precios reales.
"""

from __future__ import annotations

import io
import math
import random
import zipfile
from datetime import date, timedelta
from pathlib import Path

PRODUCTOS = [
    ("Leche Entera Sachet", "La Vaquita", "1", "lt", "01.1.4", 1500),
    ("Yogur Bebible Frutilla", "Lacteo", "900", "gr", "01.1.4", 2100),
    ("Queso Cremoso Horma", "Lacteo", "1", "kg", "01.1.4", 9800),
    ("Huevos Blancos Maple", "Granja", "30", "un", "01.1.4", 6900),
    ("Fideos Tirabuzon", "Pastas Sur", "500", "gr", "01.1.1", 1400),
    ("Arroz Largo Fino", "Grano", "1", "kg", "01.1.1", 1900),
    ("Pan Lactal Blanco", "Panera", "550", "gr", "01.1.1", 3100),
    ("Galletitas Crackers", "Crocante", "300", "gr", "01.1.1", 1600),
    ("Harina 000", "Molino", "1", "kg", "01.1.1", 1100),
    ("Carne Picada Comun", "", "1", "kg", "01.1.2", 8900),
    ("Pollo Entero Fresco", "", "1", "kg", "01.1.2", 4200),
    ("Salchichas Tipo Viena", "Frigo", "6", "un", "01.1.2", 2300),
    ("Atun al Natural", "Mar", "170", "gr", "01.1.3", 2600),
    ("Aceite de Girasol", "Sol", "1.5", "lt", "01.1.5", 3900),
    ("Manteca", "Lacteo", "200", "gr", "01.1.5", 3200),
    ("Banana Ecuador", "", "1", "kg", "01.1.6", 2400),
    ("Manzana Roja", "", "1", "kg", "01.1.6", 2900),
    ("Papa Negra", "", "1", "kg", "01.1.7", 1300),
    ("Tomate Redondo", "", "1", "kg", "01.1.7", 2800),
    ("Pure de Tomate", "Huerta", "520", "gr", "01.1.7", 1100),
    ("Azucar Comun", "Ingenio", "1", "kg", "01.1.8", 1500),
    ("Chocolate con Leche", "Dulzura", "100", "gr", "01.1.8", 2700),
    ("Mayonesa Clasica", "Salsera", "500", "gr", "01.1.9", 2500),
    ("Sal Fina", "Salina", "500", "gr", "01.1.9", 900),
    ("Yerba Mate con Palo", "Monte", "1", "kg", "01.2.1", 4700),
    ("Cafe Molido", "Tostado", "250", "gr", "01.2.1", 5200),
    ("Gaseosa Cola", "Burbuja", "2.25", "lt", "01.2.2", 3300),
    ("Agua Mineral sin Gas", "Manantial", "2", "lt", "01.2.2", 1200),
    ("Cerveza Rubia Lata", "Malteria", "473", "ml", "02.1.1", 1700),
    ("Vino Tinto Malbec", "Bodega", "750", "ml", "02.1.1", 5400),
    ("Detergente Lavavajillas", "Brillo", "750", "ml", "05.6.1", 2300),
    ("Lavandina Concentrada", "Blanca", "1", "lt", "05.6.1", 1300),
    ("Jabon en Polvo", "Espuma", "800", "gr", "05.6.1", 4100),
    ("Shampoo Brillo", "Pelo", "400", "ml", "12.1.3", 4400),
    ("Papel Higienico Doble Hoja", "Suave", "4", "un", "12.1.3", 2900),
    ("Pasta Dental Menta", "Sonrisa", "90", "gr", "12.1.3", 2100),
    ("Alimento para Perros Adultos", "Can", "3", "kg", "09.3.4", 11000),
    ("Producto Generico Sin Rubro", "Otro", "1", "un", None, 1000),
]

# inflación mensual de tendencia por clase (sintética)
TENDENCIA = {"01.1.1": 2.1, "01.1.2": 3.0, "01.1.3": 1.6, "01.1.4": 2.4, "01.1.5": 1.9, "01.1.6": 1.2,
             "01.1.7": 4.0, "01.1.8": 2.2, "01.1.9": 1.8, "01.2.1": 2.6, "01.2.2": 2.0, "02.1.1": 1.7,
             "05.6.1": 1.9, "12.1.3": 2.3, "09.3.4": 2.5, None: 2.0}

COMERCIOS = [  # id, bandera, nombre, sucursales (provincia, lat, lon, localidad)
    (10, 1, "Super Uno", [("AR-C", -34.60, -58.42, "CABA"), ("AR-B", -34.65, -58.62, "Moron"),
                          ("AR-B", -34.92, -57.95, "La Plata"), ("AR-X", -31.42, -64.18, "Cordoba")]),
    (11, 1, "Hiper Dos", [("AR-S", -32.95, -60.65, "Rosario"), ("AR-M", -32.89, -68.83, "Mendoza"),
                          ("AR-T", -26.82, -65.22, "San Miguel de Tucuman"), ("AR-H", -27.45, -58.98, "Resistencia")]),
    (12, 2, "Express Tres", [("AR-Q", -38.95, -68.06, "Neuquen"), ("AR-U", -42.77, -65.04, "Puerto Madryn"),
                             ("AR-A", -24.79, -65.41, "Salta"), ("AR-N", -27.37, -55.90, "Posadas"),
                             ("AR-B", -34.44, -58.56, "Tigre"), ("AR-C", -34.58, -58.44, "CABA")]),
]


def precio(base: float, clase, d: date, comercio: int, sucursal: int, prod: int, rng: random.Random) -> float:
    dias = (d - date(2026, 1, 1)).days
    mensual = TENDENCIA[clase] / 100
    # dos tercios del aumento del mes se aplican en la primera semana (estacionalidad intramensual)
    meses = d.year * 12 + d.month - (2026 * 12 + 1)
    frac = min(1.0, (d.day - 1) / 7) * 2 / 3 + max(0.0, (d.day - 8) / 23) / 3
    nivel = (1 + mensual) ** (meses + min(frac, 1.0))
    efecto = 1 + 0.03 * math.sin(comercio * 7 + sucursal * 3 + prod)
    ruido = math.exp(rng.gauss(0, 0.004))
    return round(base * nivel * efecto * ruido * (1 + 0.0 * dias), 2)


def generar_dia(d: date, destino: Path, seed: int = 0) -> Path:
    rng = random.Random(f"{seed}-{d.isoformat()}")
    externo = io.BytesIO()
    with zipfile.ZipFile(externo, "w", zipfile.ZIP_DEFLATED) as zout:
        for cid, bid, nombre, sucs in COMERCIOS:
            com = "﻿id_comercio|id_bandera|comercio_cuit|comercio_razon_social|comercio_bandera_nombre\n"
            com += f"{cid}|{bid}|30-0000000{cid}-0|{nombre} SA|{nombre}\n"
            su = "﻿id_comercio|id_bandera|id_sucursal|sucursales_nombre|sucursales_provincia|" \
                 "sucursales_localidad|sucursales_latitud|sucursales_longitud\n"
            pr = "﻿id_comercio|id_bandera|id_sucursal|id_producto|productos_ean|productos_descripcion|" \
                 "productos_cantidad_presentacion|productos_unidad_medida_presentacion|productos_marca|" \
                 "productos_precio_lista|productos_precio_referencia|productos_cantidad_referencia|" \
                 "productos_unidad_medida_referencia|productos_precio_unitario_promo1|productos_leyenda_promo1\n"
            for sid, (prov, lat, lon, loc) in enumerate(sucs, start=1):
                su += f"{cid}|{bid}|{sid}|Suc {sid}|{prov}|{loc}|{lat}|{lon}\n"
                for pid, (desc, marca, cant, uni, clase, base) in enumerate(PRODUCTOS):
                    for variante in range(4):  # varias marcas/variedades por producto
                        if rng.random() < 0.06:
                            continue  # faltante
                        ean = f"779{pid:03d}{variante:02d}{cid:05d}"
                        p = precio(base * (1 + 0.15 * variante), clase, d, cid, sid, pid * 10 + variante, rng)
                        promo = f"{p * 0.8:.2f}" if rng.random() < 0.1 else ""
                        texto = f"{desc} Var{variante} {cant}{uni}"
                        pr += f"{cid}|{bid}|{sid}|{ean}|1|{texto}|{cant}|{uni}|{marca}|{p:.2f}|||||{promo}|\n"
            pr += "Ultima actualizacion: 2026-01-01\n"
            interno = io.BytesIO()
            with zipfile.ZipFile(interno, "w", zipfile.ZIP_DEFLATED) as zi:
                zi.writestr("comercio.csv", com)
                zi.writestr("sucursales.csv", su)
                zi.writestr("productos.csv", pr)
            zout.writestr(f"{d.isoformat()}/sepa_{cid}_{bid}.zip", interno.getvalue())
    destino.mkdir(parents=True, exist_ok=True)
    ruta = destino / f"sepa_{d.isoformat()}.zip"
    ruta.write_bytes(externo.getvalue())
    return ruta


def ponderadores_ficticios(ruta: Path) -> None:
    """Ponderadores de prueba con el formato de config/ponderadores_clase.csv. NO son los del INDEC."""
    filas = {
        "01.1.1": ("Pan y cereales", [4.05, 5.02, 6.59, 6.49, 5.21, 4.61]),
        "01.1.2": ("Carnes y derivados", [6.1, 8.2, 10.5, 10.1, 7.9, 7.6]),
        "01.1.3": ("Pescados y mariscos", [0.4, 0.4, 0.3, 0.3, 0.3, 0.4]),
        "01.1.4": ("Leche, productos lácteos y huevos", [3.2, 3.7, 4.2, 4.3, 3.6, 3.5]),
        "01.1.5": ("Aceites, grasas y manteca", [0.6, 0.8, 1.1, 1.0, 0.8, 0.7]),
        "01.1.6": ("Frutas", [1.3, 1.4, 1.6, 1.5, 1.4, 1.5]),
        "01.1.7": ("Verduras, tubérculos y legumbres", [2.0, 2.5, 3.2, 3.4, 2.6, 2.4]),
        "01.1.8": ("Azúcar, dulces, chocolate, golosinas, etc.", [1.6, 1.8, 2.0, 2.1, 1.8, 1.7]),
        "01.1.9": ("Otros alimentos", [0.9, 1.0, 1.1, 1.0, 1.0, 1.0]),
        "01.2.1": ("Café, té, yerba y cacao", [1.0, 1.3, 1.8, 1.4, 1.2, 1.1]),
        "01.2.2": ("Aguas minerales, bebidas gaseosas y jugos", [2.2, 2.5, 3.0, 3.1, 2.1, 2.0]),
        "02.1": ("Bebidas alcohólicas", [2.0, 2.3, 2.2, 1.9, 2.2, 2.2]),
        "05.6.1": ("Artículos de limpieza", [1.8, 1.8, 2.3, 1.8, 1.8, 1.9]),
        "12.1.3": ("Artículos de cuidado personal", [2.0, 2.0, 1.9, 1.9, 2.0, 1.7]),
    }
    lineas = ["codigo,nombre,GBA,Pampeana,Noreste,Noroeste,Cuyo,Patagonia"]
    for cod, (nom, vals) in filas.items():
        lineas.append(f'{cod},"{nom}",' + ",".join(str(v) for v in vals))
    ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")


def generar_rango(desde: date, hasta: date, destino: Path, solo_habiles: bool = False) -> list[Path]:
    out, d = [], desde
    while d <= hasta:
        if not solo_habiles or d.weekday() < 5:
            out.append(generar_dia(d, destino))
        d += timedelta(days=1)
    return out
