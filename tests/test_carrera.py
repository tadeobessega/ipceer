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
