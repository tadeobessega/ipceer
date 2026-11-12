import polars as pl

from ipceer import carrera, indice

from test_pipeline import cfg  # noqa: F401  (fixture con datos sintéticos)


def test_opciones_del_calculo(cfg):
    base = indice.calcular(cfg, "mensual")["indices"]
    tot = lambda df: df.filter(pl.col("region") == "Nacional", pl.col("codigo") == "TOTAL", pl.col("periodo") == "2026-10")["var_pct"][0]
    sin = indice.calcular(cfg, "mensual", excluir=("01.1.2",))["indices"]
    assert sin.filter(pl.col("codigo") == "01.1.2", pl.col("var_pct").is_not_null()).is_empty()
    assert tot(sin) != tot(base)
    for op in ({"agregador": "mediana"}, {"agregador": "recortada"}, {"peso": "sucursales"}, {"precio": "efectivo"},
               {"ponderacion": "propia"}, {"estratificar": True}, {"populares": 2}, {"directo": True},
               {"estratificar": True, "populares": 2, "directo": True}):
        v = tot(indice.calcular(cfg, "mensual", **op)["indices"])
        assert v is not None and abs(v - tot(base)) < 3


def test_carrera_corre_todas_y_anota(cfg):
    r = carrera.actual(cfg)
    ids = [v["id"] for v in r["variantes"]]
    assert {"base", "anterior", "mejorado", "pond_propia", "efectivo", "mediana", "sin_fyv", "web", "completo"} <= set(ids)
    assert "GBA" in next(v for v in r["variantes"] if v["id"] == "base")["alimentos"]
    base = next(v for v in r["variantes"] if v["id"] == "base")
    assert base["meses"] and all("error" not in v for v in r["variantes"])
    pron = pl.read_csv(cfg.ruta("resultados") / "pronosticos.csv")
    assert {"calculado", "mes", "variante", "var_pct"} <= set(pron.columns) and pron.height
    carrera.actual(cfg)  # la segunda corrida del día no duplica filas
    assert pl.read_csv(cfg.ruta("resultados") / "pronosticos.csv").height == pron.height


def test_puntuar_y_calibraciones():
    p = carrera.puntuar({"2017-01": 1.0, "2017-02": 2.0}, {"2017-01": 1.5, "2017-02": 2.5})
    assert p["sesgo"] == -0.5 and p["rmse"] == 0.5 and p["n"] == 2
    hist = {"base": {"meses": [{"mes": f"2017-{i:02d}", "propio": 1 + i / 10, "general": 1.5 + i / 10} for i in range(1, 11)]}}
    c = carrera._calibraciones(hist)
    assert abs(c["base_mas_sesgo"]["f"](2.0) - 2.5) < 1e-9 and c["base_mas_sesgo"]["vs_general"]["rmse"] < 1e-6


def test_variedades():
    from pathlib import Path
    from ipceer import variedades
    r = variedades.reglas(Path(__file__).resolve().parents[1] / "config")
    assert variedades.asignar("Fideos Tirabuzon Matarazzo x 500 Gr", "01.1.1", r) == "fideos_secos"
    assert variedades.asignar("Leche Entera Sachet La Serenisima 1 Lt", "01.1.4", r) == "leche_fluida"
    assert variedades.asignar("Bebida de Manzana sin Alcohol", "01.1.6", r) != "fruta_fresca"
    assert variedades.asignar("Papas Fritas Lays Clasicas x 305 Gr", "01.1.7", r) != "papa"
    assert variedades.asignar("Yerba Mate Playadito 1 Kg", "01.2.1", r) == "yerba"


def test_normas_infoleg():
    from pathlib import Path
    from ipceer import normas
    d = Path(__file__).parent / "datos"
    n = normas.leer_norma((d / "infoleg_426258.html").read_text(encoding="utf-8"), 426258)
    assert n["numero"] == "26/2026" and n["organismo"] == "ENTE NACIONAL REGULADOR DEL GAS Y LA ELECTRICIDAD"
    assert n["titulo"] == "EDENOR S.A." and n["publicada"] == "29-may-2026" and n["resumen"].startswith("APROBAR")
    assert normas.leer_norma("<html>sin norma</html>", 1) is None
    f = normas.leer_cuadro_electricidad((d / "cuadro_edenor_202606.txt").read_text(encoding="utf-8"))
    v = {x["concepto"]: x["valor"] for x in f}
    assert v["Edenor T1-R2 (151 a 400 kWh) sin subsidio: cargo fijo"] == 3544.12
    assert v["Edenor T1-R2 (151 a 400 kWh) sin subsidio: cargo variable"] == 147.156
    assert v["Edenor T1-R1 (0 a 150 kWh) sin subsidio: cargo fijo"] == 1661.69
    assert all(x["vigente_desde"] == "2026-06-01" for x in f) and len(f) == 4
    ok = normas.plausible([{"concepto": "a", "valor": 110.0}, {"concepto": "b", "valor": 300.0}], {"a": 100.0, "b": 100.0})
    assert ok[0]["control"] == "ok" and ok[1]["control"].startswith("revisar")
    assert normas.anexos(426258, '<a href="res26-2.pdf">') == [
        "https://servicios.infoleg.gob.ar/infolegInternet/anexos/425000-429999/426258/res26-2.pdf"]


def test_cuadros_de_gas_y_plausibilidad_en_el_tiempo():
    from pathlib import Path
    from ipceer import normas
    d = Path(__file__).parent / "datos"
    m = normas.leer_cuadro_gas((d / "cuadro_metrogas_202609.txt").read_text(encoding="utf-8").split("\n\f"),
                               "Metrogas", "2026-09-01")
    v = {x["concepto"]: x["valor"] for x in m}
    assert v["Metrogas R1 sin subsidio (Ciudad de Buenos Aires): cargo fijo"] == 4432.10
    assert v["Metrogas R2 2° sin subsidio (conurbano): cargo fijo"] == 14580.56
    assert v["Metrogas R1-R2 1° sin subsidio (Ciudad de Buenos Aires): cargo por metro cúbico"] == 316.16
    assert len(m) == 20
    n = normas.leer_cuadro_gas((d / "cuadro_naturgy_202609.txt").read_text(encoding="utf-8").split("\n\f"),
                               "Naturgy BAN", "2026-09-01")
    v = {x["concepto"]: x["valor"] for x in n}
    assert v["Naturgy BAN R1 sin subsidio (conurbano norte): cargo fijo"] == 3632.34
    assert v["Naturgy BAN R1-R2 1° sin subsidio (conurbano norte): cargo por metro cúbico"] == 300.66
    assert normas.vigencia_de_norma("los cuadros ... entrarán en vigencia a partir del 1° de septiembre de 2026.") == "2026-09-01"
    # un valor de hace dos años contra el actual: se permite el cambio acumulado de los meses de distancia
    viejo = normas.plausible([{"concepto": "a", "valor": 20.0, "vigente_desde": "2024-09-01"}],
                             [{"concepto": "a", "valor": 300.0, "vigente_desde": "2026-09-01"}])
    assert viejo[0]["control"] == "ok"
    malo = normas.plausible([{"concepto": "a", "valor": 3000.0, "vigente_desde": "2026-10-01"}],
                            [{"concepto": "a", "valor": 300.0, "vigente_desde": "2026-09-01"}])
    assert malo[0]["control"].startswith("revisar")


def test_boletines_ciudad_y_provincia():
    from pathlib import Path
    from ipceer import boletines as b
    d = Path(__file__).parent / "datos"
    c = b.leer_colectivo_caba((d / "caba_colectivo_202610_ocr.txt").read_text(encoding="utf-8"))
    assert c == {"mes": "2026-10-01", "0-3": 920.48, "3-6": 1022.81, "6-12": 1101.59, "12-27": 1180.44}
    c8 = b.leer_colectivo_caba((d / "caba_colectivo_202608_ocr.txt").read_text(encoding="utf-8"))
    assert c8["mes"] == "2026-08-01" and c8["0-3"] == 852.9
    assert b.vigencia_en_texto("los cuadros tarifarios regirán a partir del 1° de julio de 2026.") == "2026-07-01"
    t = (d / "caba_subte_253.txt").read_text(encoding="utf-8")
    assert b.leer_subte(t) == 1887.0 and b.mes_de_referencia(t) == "2026-11-01"
    p = b.leer_colectivo_pba((d / "pba_colectivo_122_26.txt").read_text(encoding="utf-8"))
    assert p == {"mes": "2026-05-01", "0-3": 968.57, "0-3 sin nominar": 1937.14}
    assert b.rubro_caba("Resolución N° 210/SECT/26", "Aprueba los cuadros tarifarios para el servicio público de transporte colectivo de pasajeros por automotor") == "colectivo"
    assert b.rubro_caba("Resolución de Directorio N° 253/SBASE/26", "Aprueba los nuevos cuadros tarifarios para la explotación del Servicio SUBTE") == "subte"
    assert b.rubro_caba("Resolución N° 214/SECT/26", "Establece el límite máximo de velocidad") is None
    assert b.rubro_pba("Resolución 122/2026 del Ministerio de Transporte Resumen Modifica la Resolución N° 81/25 (actualización de los valores tarifarios de autotransporte público provincial") == "colectivo"


def test_zonaprop_gba():
    from ipceer.otras_fuentes import leer_zonaprop
    z = leer_zonaprop("El precio medio de alquiler de un departamento de 2 ambientes en GBA Oeste se ubica en $651.251 por mes, sube 1.3% en septiembre")
    assert z["alquiler_2amb"] == 651251.0 and z["var_alquiler"] == 1.3


def test_prepagas_indice(tmp_path):
    import polars as pl
    from ipceer import prepagas
    from ipceer.config import cargar_config
    from pathlib import Path
    import shutil
    shutil.copytree(Path(__file__).resolve().parents[1] / "config", tmp_path / "config")
    c = cargar_config(tmp_path)
    d = prepagas._dir(c)
    base = dict(rnemp=1, codigo_plan=10, tipo_plan="I", region="CABA", rango_etario_hasta=99, nombre_comercial="X",
                nombre_plan="P", permite_copago=True, comercializable=True)
    pl.DataFrame([dict(base, rango_etario_desde=0, periodo=202609, valor_capital=100.0, cantidad_capitas=300, tasa_aumento_mensual=0.02),
                  dict(base, rango_etario_desde=30, periodo=202609, valor_capital=200.0, cantidad_capitas=100, tasa_aumento_mensual=0.02)]
                 ).write_parquet(d / "202609.parquet")
    pl.DataFrame([dict(base, rango_etario_desde=0, periodo=202610, valor_capital=102.0, cantidad_capitas=300, tasa_aumento_mensual=0.02),
                  dict(base, rango_etario_desde=30, periodo=202610, valor_capital=206.0, cantidad_capitas=100, tasa_aumento_mensual=0.03)]
                 ).write_parquet(d / "202610.parquet")
    i = prepagas.indice(c)
    assert i["periodo"].to_list() == ["2026-10"]
    v = i["var_pct"][0]
    assert 2.0 < v < 3.0 and abs(v - ((1.02 ** 0.75 * 1.03 ** 0.25) - 1) * 100) < 1e-6
    r = prepagas.relativos_externos(c)
    assert set(r["region"].to_list()) >= {"GBA", "Patagonia"} and r["codigo"][0] == "06.4.1"


def test_plan_a_paquetes():
    import importlib.util
    from pathlib import Path
    from ipceer.descarga import miembros_validos
    f = "2026-11-12"
    ok = miembros_validos([f"panel/diario/{f}.parquet", f"geo/diario/{f}.parquet", f"municipios/06568/panel/diario/{f}.parquet",
                           "panel/cadenas.csv", "cadenas.csv", "../../malo.parquet", f"panel/diario/2026-11-11.parquet", "/abs.parquet"], f)
    assert ok == [f"panel/diario/{f}.parquet", f"geo/diario/{f}.parquet", f"municipios/06568/panel/diario/{f}.parquet", "panel/cadenas.csv"]
    spec = importlib.util.spec_from_file_location("pgh", Path(__file__).resolve().parents[1] / "scripts" / "procesar_sepa_gh.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assets = {"sepa_2026-11-10.zip", "sepa_2026-11-11.zip", "proc_2026-11-10.tar", "sepa_2026-09-01.zip"}
    assert m.pendientes(assets, None) == ["2026-11-11", "2026-09-01"]
    assert m.pendientes(assets, "2026-11-01") == ["2026-11-11"]
