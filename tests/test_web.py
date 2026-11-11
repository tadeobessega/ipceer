import polars as pl

from ipceer import web
from ipceer.config import cargar_config


def test_clases_web_por_categoria_y_nombre():
    cl = web.ClasesWeb(cargar_config().dir_config / "web" / "clases.yaml")
    casos = [("Carnes y pescados / Carne vacuna", "Asado x kg", "01.1.2"),
             ("Carnes y pescados / Pescados y mariscos", "Filet de merluza", "01.1.3"),
             ("Frutas y verduras / Frutas", "Banana x kg", "01.1.6"),
             ("Frutas y verduras / Verduras", "Papa x kg", "01.1.7"),
             ("Lácteos / Yogures", "Yogur con frutas", None),               # ya viene de SEPA
             ("Electro y tecnología / Celulares", "Funda para celular", None),  # accesorio
             ("Electro y tecnología / Celulares", "Celular Motorola G54", "08.2.2"),
             ("CALZADO / deportes running", "Zapatillas Energen Run 3", "03.2.1"),
             ("Mujer / Ver todo", "Jean 501 azul", "03.1.2"),
             ("Salud y Farmacia / Analgésicos", "Ibuprofeno 400 x10", "06.1.1"),
             ("Hogar / Dormitorio", "Sábanas 2 plazas", "05.2"),
             ("Electro / Heladeras y freezers", "Heladera No Frost 364 L", "05.3")]
    for cat, nombre, esperado in casos:
        assert cl.clase(cat, nombre) == esperado, (cat, nombre)


def test_filas_vtex_toma_el_precio_sin_descuento_como_lista():
    prod = [{"productId": "9", "productName": "Asado", "brand": "X", "categories": ["/Carnes/", "/Carnes/Vacuna/"],
             "items": [{"itemId": "77", "name": "Asado", "ean": "123", "measurementUnit": "kg", "unitMultiplier": 1.0,
                        "sellers": [{"sellerId": "1", "sellerDefault": True,
                                     "commertialOffer": {"Price": 9000.0, "ListPrice": 12000.0,
                                                         "PriceWithoutDiscount": 10000.0, "IsAvailable": True}}]}]}]
    f = web._filas_vtex(prod, "tienda", "2026-11-11", "x")[0]
    assert (f["precio"], f["precio_lista"], f["categoria"], f["unidad"]) == (9000.0, 10000.0, "Carnes / Vacuna", "kg")


def test_paneles_web(tmp_path):
    cfg = cargar_config()
    cfg.datos["rutas"] = dict(cfg.datos["rutas"]) | {"panel_diario": str(tmp_path / "panel" / "diario"),
                                                    "resultados": str(tmp_path / "res")}
    (tmp_path / "panel" / "diario").mkdir(parents=True)
    d = web.dir_web(cfg)
    for dia, precio in (("2026-11-10", 100.0), ("2026-11-11", 121.0)):
        pl.DataFrame([{"fecha": dia, "tienda": "farmacity", "sku": "1", "producto": "1", "nombre": "Ibuprofeno 400",
                       "marca": "X", "categoria": "Salud y Farmacia / Analgésicos", "ean": None, "unidad": "un",
                       "multiplicador": 1.0, "precio": precio, "precio_lista": precio, "disponible": True},
                      {"fecha": dia, "tienda": "carrefour", "sku": "2", "producto": "2", "nombre": "Asado x kg",
                       "marca": "X", "categoria": "Carnes y pescados / Carne vacuna", "ean": None, "unidad": "kg",
                       "multiplicador": 1.0, "precio": precio, "precio_lista": precio, "disponible": True}],
                     schema=web.ESQUEMA).write_parquet(d / f"{dia}.parquet")
    r = web.construir_paneles(cfg)
    assert r["dias"] == 2
    m = pl.read_parquet(tmp_path / "panel" / "web_mensual" / "2026-11.parquet")
    far = m.filter(pl.col("id_comercio") == "web:farmacity")
    assert far.height == 6 and set(far["clase"]) == {"06.1.1"}            # precio único: las seis regiones
    assert abs(far["precio_lista"][0] - 110.0) < 1e-6 and far["n_dias"][0] == 2   # media geométrica de los días
    car = m.filter(pl.col("id_comercio") == "web:carrefour")
    assert car["region"].to_list() == ["GBA"] and car["clase"][0] == "01.1.2"
