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
    assert 1.5 < sep < 3.5  # tendencias sintéticas entre 1,2% y 4% mensual
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
    generar_rango(date(2025, 4, 21), date(2025, 4, 27), c.ruta("crudos"))
    generar_rango(date(2026, 9, 7), date(2026, 9, 20), c.ruta("crudos"))
    for z in zips_sin_procesar(c):
        lectura.procesar_zip_diario(z, fecha_de_zip(z), c)
    panel.actualizar_paneles(c, "semanal")
    s = indice.calcular(c, "semanal")["indices"]
    tot = s.filter(pl.col("region") == "Nacional", pl.col("codigo") == "TOTAL").sort("periodo")
    assert tot["periodo"].to_list() == ["2025-W17", "2026-W37", "2026-W38"]
    assert tot["salto"].to_list() == [False, True, False]
    assert tot["var_pct"][1] is None and tot["var_pct"][2] is not None
    assert 20 < tot["var_salto_pct"][1] < 60  # ~16,5 meses a ~2,3% mensual
    sal = resumen.saltos(tot, "semanal")
    assert len(sal) == 1 and 1.5 < sal[0]["var_mensual"] < 3.5
