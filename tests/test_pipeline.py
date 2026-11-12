import shutil
import sys
from datetime import date
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from sintetico import generar_rango, ponderadores_ficticios  # noqa: E402

from ipceer import indice, lectura, panel, resumen  # noqa: E402
from ipceer.clasificador import Clasificador  # noqa: E402
from ipceer.config import cargar_config  # noqa: E402
from ipceer.descarga import fecha_de_zip, zips_sin_procesar  # noqa: E402
from ipceer.regiones import asignar_regiones  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    raiz = tmp_path_factory.mktemp("repo")
    shutil.copytree(REPO / "config", raiz / "config")
    shutil.copytree(REPO / "web", raiz / "web")
    ponderadores_ficticios(raiz / "config" / "ponderadores_clase.csv")
    c = cargar_config(raiz)
    generar_rango(date(2026, 8, 31), date(2026, 10, 14), c.ruta("crudos"), solo_habiles=True)
    for z in zips_sin_procesar(c):
        lectura.procesar_zip_diario(z, fecha_de_zip(z), c)
    for f in ("semanal", "mensual"):
        panel.actualizar_paneles(c, f)
    return c


def test_clasificador_casos_dificiles():
    cl = Clasificador.desde_archivos(REPO / "config" / "clasificador.yaml")
    casos = {
        "Jabon en Polvo Ariel 800 gr": "05.6.1",
        "Jabon de Tocador Dove 90 gr": "12.1.3",
        "Chocolate con Leche Milka 100 gr": "01.1.8",
        "Leche Entera La Serenisima 1 lt": "01.1.4",
        "Dulce de Leche Clasico 400 gr": "01.1.4",
        "Alfajor de Dulce de Leche": "01.1.8",
        "Fideos al Huevo Tallarines 500 gr": "01.1.1",
        "Atun al Natural en Agua 170 gr": "01.1.3",
        "Agua Mineral sin Gas 2 lt": "01.2.2",
        "Cerveza sin Alcohol 473 ml": "01.2.2",
        "Vinagre de Vino 500 ml": "01.1.9",
        "Vino Tinto Malbec 750 ml": "02.1.2",
        "Fernet Branca 750 ml": "02.1.1",
        "Cerveza Rubia Lata 473 ml": "02.1.3",
        "Alimento para Perros Adultos 3 kg": "09.3.4",
        "Aceite de Girasol 1.5 lt": "01.1.5",
        "Crema Dental Colgate 90 gr": "12.1.3",
        "Crema de Leche 200 ml": "01.1.4",
        "Yerba Mate con Palo 1 kg": "01.2.1",
        "Milanesas de Soja 4 un": "01.1.7",
        "Caldo de Gallina 6 cubos": "01.1.9",
        "Papel Higienico Doble Hoja 4 un": "12.1.3",
        "Rollo de Cocina 3 un": "05.6.1",
        # descripciones reales de SEPA (oct-2026), con abreviaturas
        "ARBANIT ALF DDL 50G": "01.1.8",
        "ILOLAY YOG NAT 350GR": "01.1.4",
        "VACALIN DDL LIGH400G": "01.1.4",
        "AXE DEO BS MUSK 150M": "12.1.3",
        "YERBA L.TRANQUERA X1KG.ESPECIAL": "01.2.1",
        "SAL FINA CELUSAL X250G.SALERO": "01.1.9",
        "VINO UVITA X1L.BLANCO": "02.1.2",
        "MAYONESA CADA DIA X2.900ML": "01.1.9",
    }
    errores = {d: (cl.clasificar_texto(d), c) for d, c in casos.items() if cl.clasificar_texto(d) != c}
    assert not errores, errores


def test_propagacion_por_ean():
    cl = Clasificador.desde_archivos(REPO / "config" / "clasificador.yaml")
    df = pl.DataFrame({"id_producto": ["779001", "779001", "779002"], "es_ean": ["1", "1", "0"],
                       "descripcion": ["Galletitas Dulces Vainilla 300 g", "XYZ VAI 300G", "XYZ VAI 300G"]})
    assert cl.clasificar(df)["clase"].to_list() == ["01.1.1", "01.1.1", "sin_clasificar"]


def test_regiones(cfg):
    suc = pl.DataFrame({
        "sucursales_provincia": ["AR-B", "AR-B", "AR-C", "AR-X", "AR-B", "Mendoza"],
        "sucursales_latitud": ["-34.65", "-34.92", "-34.6", "-31.4", None, None],
        "sucursales_longitud": ["-58.62", "-57.95", "-58.4", "-64.2", None, None],
        "sucursales_localidad": ["Moron", "La Plata", "CABA", "Cordoba", "Castelar", "Godoy Cruz"],
    })
    reg = asignar_regiones(suc, cfg)["region"].to_list()
    assert reg == ["GBA", "Pampeana", "GBA", "Pampeana", "GBA", "Cuyo"]


def test_resolver_codigo():
    cods = {"01.1.1", "02.1", "05", "12.1.3"}
    assert indice.resolver_codigo("01.1.1", cods) == "01.1.1"
    assert indice.resolver_codigo("02.1.1", cods) == "02.1"
    assert indice.resolver_codigo("05.6.1", cods) is None  # nunca sube a división
    assert indice.resolver_codigo("09.3.4", cods) is None


def test_paneles(cfg):
    sem = sorted(p.stem for p in cfg.ruta("panel_semanal").glob("*.parquet"))
    men = sorted(p.stem for p in cfg.ruta("panel_mensual").glob("*.parquet"))
    assert men == ["2026-08", "2026-09", "2026-10"]
    assert sem[0] == "2026-W36" and len(sem) == 7
    p = pl.read_parquet(cfg.ruta("panel_semanal") / "2026-W37.parquet")
    assert p["dias_con_datos"][0] == 5 and p["dias_periodo"][0] == 7  # solo días hábiles


def test_indices(cfg):
    m = indice.calcular(cfg, "mensual")["indices"]
    tot = m.filter(pl.col("region") == "Nacional", pl.col("codigo") == "TOTAL").sort("periodo")
    sep = tot.filter(pl.col("periodo") == "2026-09")["var_pct"][0]
    assert 1.0 < sep < 3.5  # tendencias sintéticas entre 1,2% y 4% mensual (precios pegajosos)
    carnes = m.filter(pl.col("region") == "Nacional", pl.col("codigo") == "01.1.2", pl.col("periodo") == "2026-10")
    frutas = m.filter(pl.col("region") == "Nacional", pl.col("codigo") == "01.1.6", pl.col("periodo") == "2026-10")
    assert carnes["var_pct"][0] > frutas["var_pct"][0]
    s = indice.calcular(cfg, "semanal")
    assert s["indices"].filter(pl.col("periodo") == "2026-W42")["parcial"].all()
    assert set(s["cobertura"]["region"]) >= {"Nacional", "GBA"}


def test_mensualizacion_y_proyeccion(cfg):
    assert resumen.mensualizar(1.0, 7) == pytest.approx((1.01 ** (365.25 / 12 / 7) - 1) * 100)
    s = indice.calcular(cfg, "semanal")["indices"]
    serie = resumen.serie_semanal(s.filter(pl.col("region") == "Nacional", pl.col("codigo") == "TOTAL"))
    assert serie[4]["prom4"] is not None and serie[2]["prom4"] is None
    p = resumen.proyeccion_mes(serie, "2026-10")
    # W40 (jueves 1/10), W41 y W42 (en curso) caen en octubre
    assert p["semanas_mes"] == 5 and p["semanas_observadas"] == 3
    assert p["arrastre"] <= p["proyeccion"]


def test_salto_wayback(tmp_path):
    """Una semana suelta de abril de 2025 (como la de la Wayback Machine) y semanas de 2026."""
    raiz = tmp_path / "repo"
    shutil.copytree(REPO / "config", raiz / "config")
    shutil.copytree(REPO / "web", raiz / "web")
    ponderadores_ficticios(raiz / "config" / "ponderadores_clase.csv")
    c = cargar_config(raiz)
    generar_rango(date(2017, 3, 6), date(2017, 3, 19), c.ruta("crudos"))
    generar_rango(date(2025, 4, 21), date(2025, 4, 27), c.ruta("crudos"))
    generar_rango(date(2026, 9, 7), date(2026, 9, 20), c.ruta("crudos"))
    for z in zips_sin_procesar(c):
        lectura.procesar_zip_diario(z, fecha_de_zip(z), c)
    panel.actualizar_paneles(c, "semanal")
    s = indice.calcular(c, "semanal")["indices"]
    tot = s.filter(pl.col("region") == "Nacional", pl.col("codigo") == "TOTAL").sort("periodo")
    assert tot["periodo"].to_list() == ["2017-W10", "2017-W11", "2025-W17", "2026-W37", "2026-W38"]
    assert tot["tramo"].to_list() == [0, 0, 1, 1, 1]  # 2017 -> 2025: más de 24 meses, tramo nuevo
    assert tot["indice"][2] == 100  # el tramo nuevo arranca en 100
    assert tot["salto"].to_list() == [False, False, False, True, False]
    assert tot["var_pct"][2] is None and tot["var_pct"][3] is None and tot["var_pct"][4] is not None
    assert 20 < tot["var_salto_pct"][3] < 60  # ~16,5 meses a ~2,3% mensual
    sal = resumen.saltos(tot.filter(pl.col("tramo") == 1), "semanal")
    assert len(sal) == 1 and 1.5 < sal[0]["var_mensual"] < 3.5


def test_historico_zenodo(tmp_path, monkeypatch):
    """Un día con el formato de la base de Zenodo se convierte al panel diario."""
    import gzip
    from ipceer import historico
    raiz = tmp_path / "repo"
    shutil.copytree(REPO / "config", raiz / "config")
    c = cargar_config(raiz)
    src = tmp_path / "src"
    src.mkdir()
    def gz(nombre, texto):
        with gzip.open(src / nombre, "wt", encoding="utf-8") as f:
            f.write(texto)
    gz("sucursales.csv.gz", "comercio_id,bandera_id,sucursal_id,sucursal_provincia,sucursal_localidad,sucursal_latitud,sucursal_longitud\r\n"
       "8,1,3,AR-X,CORDOBA,-31.36,-64.16\r\n10,1,1,AR-C,CABA,-34.6,-58.4\r\n")
    gz("productos.csv.gz", "producto_sepa_id,producto_ean,producto_descripcion,producto_cantidad_presentacion,"
       "producto_unidad_medida_presentacion,producto_marca,categoria_secom_id,categoria_rubro_id,categoria_rubro_desc,"
       "categoria_categ_id,categoria_categ_desc,categoria_subcateg_id,categoria_subcateg_desc,categoria_requerido,categoria_ean\r\n"
       '7790480089390,t,"Yerba Mate La Tranquera 1 Kg",1,KG,"LA TRANQUERA",1,2,Almacén,12,"Infusiones",8,"Yerba",t,t\r\n')
    gz("precios.csv.gz", "comercio_id,bandera_id,sucursal_id,producto_sepa_id,precio_lista,precio_unitario_promo1,precio_unitario_promo2,precio_fecha\r\n"
       "8,1,3,7790480089390,40.5,NaN,NaN,736755\r\n10,1,1,7790480089390,42,35.0,NaN,736755\r\n")
    monkeypatch.setattr(historico, "_bajar", lambda url, destino, *a, **k: shutil.copy(src / f"{url}.csv.gz", destino))
    d = date(2017, 3, 1)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    ruta = historico.procesar_dia(d, {t: t for t in ("sucursales", "productos", "precios")}, c, tmp)
    p = pl.read_parquet(ruta).sort("region")
    assert p["region"].to_list() == ["GBA", "Pampeana"]
    assert p["precio_lista"].to_list() == pytest.approx([42.0, 40.5]) and p["precio_efectivo"][0] == pytest.approx(35.0)
    assert p["es_ean"][0] == "1" and p["cantidad"][0] == "1.0"
    cl = Clasificador.desde_archivos(REPO / "config" / "clasificador.yaml").clasificar(p)
    assert cl["clase"][0] == "01.2.1"


def test_indicadores_y_hilo(cfg):
    from ipceer import hilo, indicadores, resumen
    sem = indice.calcular(cfg, "semanal")
    ind = indicadores.calcular(cfg)
    # rotación: porcentajes coherentes y con precios pegajosos no cambia todo
    rot = [r for r in ind["rotacion"] if r["region"] == "Nacional" and r["codigo"] == "TOTAL"]
    assert rot
    for r in rot:
        assert r["sube"] + r["baja"] + r["igual"] == pytest.approx(100)
        assert 0 < r["cambia"] < 100
    # cadenas: en GBA la más barata es la mayorista sintética (nivel 0,91) y la más cara Express Tres (1,09)
    ult = ind["semanas"][-1]
    gba = sorted([c for c in ind["cadenas"] if c["region"] == "GBA" and c["semana"] == ult], key=lambda c: c["nivel"])
    assert gba[0]["cadena"] == "Mayorista Cinco" and gba[-1]["cadena"] == "Express Tres"
    assert gba[-1]["nivel"] / gba[0]["nivel"] == pytest.approx(1.09 / 0.91, rel=0.03)
    # regiones: Patagonia es la más cara
    reg = sorted([r for r in ind["regiones"] if r["semana"] == ult], key=lambda r: r["nivel"])
    assert reg[-1]["region"] == "Patagonia"
    # CBA: costo positivo, ranking con la mayorista primero
    cba = [c for c in ind["cba"]["cadenas"] if c["region"] == "GBA" and c["semana"] == ult and c["en_ranking"]]
    assert cba and min(cba, key=lambda c: c["costo"])["cadena"] == "Mayorista Cinco"
    # hilo
    datos = resumen.armar_datos(cfg, None, sem["indices"], sem["cobertura"], indicadores=ind)
    h = hilo.armar(cfg, datos, sem["indices"], datos["indicadores"])
    assert h["tuits"][0]["texto"].startswith("El ipceer midió una inflación semanal de bienes del ")
    assert "—" not in "".join(t["texto"] for t in h["tuits"])
    assert any(t["imagen"] == "cadenas" for t in h["tuits"])


def test_combustibles(cfg):
    from ipceer import combustibles
    from sintetico_combustibles import csv_precios, geometria
    d = combustibles.directorio(cfg)
    cajas = geometria(d / "geo")
    csv_precios(d / "precios-y-volumenes.csv", cajas)
    combustibles.procesar(cfg)
    datos, idx = combustibles.construir(cfg)
    # índice: 2,5% mensual de tendencia más saltos semestrales del 4%
    nac = idx.filter(pl.col("region") == "Nacional").sort("periodo")
    v = nac.filter(pl.col("periodo") == "2024-03")["var_pct"][0]
    assert 2.0 < v < 3.2
    assert set(idx["region"].unique()) >= {"GBA", "Pampeana", "Cuyo", "Patagonia", "Nacional"}
    # niveles: Chubut más caro que CABA en nafta súper; y se abarata en términos relativos
    pr = datos["provincias"]
    p0 = pr["26"]["nafta_super"]["p"][0] / pr["02"]["nafta_super"]["p"][0]
    p1 = pr["26"]["nafta_super"]["p"][-1] / pr["02"]["nafta_super"]["p"][-1]
    assert p0 > 1.05 and p1 < p0
    # departamentos asignados por coordenadas y geometría para el mapa
    assert len(datos["departamentos"]) == 15 and len(datos["geo"]["departamentos"]) == 15
    assert datos["sin_departamento_pct"] == 0


def test_regulados_no_completa_meses(tmp_path):
    """Un mes sin tarifa registrada corta la serie: no hay variación inventada."""
    from ipceer import externos
    cfg = cargar_config(Path(__file__).resolve().parents[1])
    idx = externos.indice_regulados(cfg)
    nac = idx.filter(pl.col("region") == "Nacional").sort("periodo")
    # agosto 2026 no está registrado: septiembre no tiene variación y empieza un tramo nuevo
    sep = nac.filter(pl.col("periodo") == "2026-09").row(0, named=True)
    assert sep["var_pct"] is None
    assert "2026-08" not in nac["periodo"].to_list()
