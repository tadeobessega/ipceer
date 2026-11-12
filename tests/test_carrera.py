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
