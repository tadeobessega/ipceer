"""Modelos de restaurantes y educación (D-55)."""
import math

import polars as pl
import pytest

from ipceer import modelos
from ipceer.config import REGIONES

PM = {
    "usar": True, "desde_mes": "2026-11",
    "restaurantes": {"con_salario": {"alim": 0.2, "ngex": 0.6, "salario": 0.2}, "sin_salario": {"alim": 0.1, "ngex": 0.9}},
    "educacion": {"con_paritarias": {"const": 1.0, "ngex": 0.2, "ene": -2.0, "dic": -1.0, "paritarias_t1": 0.3},
                  "sin_paritarias": {"const": 2.0, "ngex": 0.4, "ene": -3.0, "dic": -2.0},
                  "marzo": {"const": 0.08, "ng12": 0.47, "educ_abr_feb": -0.4}},
}
CFG = {"indice": {"modelos": PM}}


def _base(periodos, alim=2.0, ngex=2.5):
    filas = []
    for p in periodos:
        for r in REGIONES:
            filas.append({"periodo": p, "region": r, "nivel": "grupo", "codigo": "01.1", "var_pct": alim})
            filas.append({"periodo": p, "region": r, "nivel": "total", "codigo": "TOTAL", "var_pct": ngex})
    return pl.DataFrame(filas)


@pytest.fixture
def parche(monkeypatch):
    gastro = {f"2026-{m:02d}": 3.0 for m in range(3, 11)}   # hasta octubre
    par = {"2026-10": 2.0, "2026-11": 1.5}
    monkeypatch.setattr(modelos, "salarios", lambda cfg: {"gastro": gastro, "paritarias": par})
    ind = {}
    for r in REGIONES:
        for k in range(12):  # mar-2026 a feb-2027 nivel general; abr-2026 a feb-2027 educación
            m = modelos._mes(2026, 3 + k)
            ind[("0", r, m)] = 2.0
            if k:
                ind[("10", r, m)] = 1.0
    monkeypatch.setattr(modelos, "_indec", lambda cfg: ind)
    return gastro, par


def test_restaurantes_y_educacion_mensual(parche):
    mens, info = modelos.mensual(CFG, _base(["2026-10", "2026-11", "2026-12", "2027-01", "2027-03"]))
    g = {(p, c, r): (lr, f) for p, c, r, lr, f in mens.iter_rows()}
    assert ("2026-10", "11.1", "GBA") not in g                     # antes de desde_mes no se modela
    lr, f = g[("2026-11", "11.1", "GBA")]
    assert f == "modelo_restaurantes" and math.isclose(lr, math.log1p((0.2 * 2 + 0.6 * 2.5 + 0.2 * 3) / 100))
    lr, f = g[("2027-01", "11.1", "GBA")]                            # salario de nov: falta -> sin salario
    assert f == "modelo_restaurantes_sin_salario" and math.isclose(lr, math.log1p((0.1 * 2 + 0.9 * 2.5) / 100))
    lr, f = g[("2026-11", "10.1", "Cuyo")]                           # paritarias de octubre
    assert f == "modelo_educacion" and math.isclose(lr, math.log1p((1 + 0.2 * 2.5 + 0.3 * 2.0) / 100))
    lr, f = g[("2026-12", "10.3", "GBA")]
    assert math.isclose(lr, math.log1p((1 - 1 + 0.2 * 2.5 + 0.3 * 1.5) / 100))
    lr, f = g[("2027-01", "10.5", "GBA")]                            # sin paritarias de diciembre
    assert f == "modelo_educacion_sin_paritarias" and math.isclose(lr, math.log1p((2 - 3 + 0.4 * 2.5) / 100))
    lr, f = g[("2027-03", "10.2", "GBA")]                            # marzo con el INDEC acumulado
    v = math.exp(0.08 + 0.47 * 12 * math.log1p(0.02) - 0.4 * 11 * math.log1p(0.01)) - 1
    assert f == "modelo_educacion_marzo" and math.isclose(lr, math.log1p(v))
    assert {c for _, c, _ in g} == {"11.1", "10.1", "10.2", "10.3", "10.5"}
    assert info["detalle"] and info["desde_mes"] == "2026-11"


def test_marzo_sin_acumulado_queda_sin_dato(parche, monkeypatch):
    monkeypatch.setattr(modelos, "_indec", lambda cfg: {})
    mens, _ = modelos.mensual(CFG, _base(["2027-03"]))
    assert mens.filter(pl.col("codigo") == "10.1").is_empty()       # no se inventa lo acumulado
    assert not mens.filter(pl.col("codigo") == "11.1").is_empty()


def test_educacion_semanal_por_devengado(parche):
    mens, _ = modelos.mensual(CFG, _base(["2026-11", "2026-12"]))
    sem_base = _base(["2026-W45", "2026-W46", "2026-W47", "2026-W48", "2026-W49", "2026-W50"], alim=0.5, ngex=0.6)
    sem = modelos.semanal(CFG, sem_base, mens)
    ed = sem.filter((pl.col("codigo") == "10.1") & (pl.col("region") == "GBA")).sort("periodo")
    lr = dict(ed.select("periodo", "logrel").iter_rows())
    # el 1/11/2026 es domingo: la semana 44 (26/10 a 1/11) tiene 1/7 del salto y la 45 el resto
    nov = mens.filter((pl.col("codigo") == "10.1") & (pl.col("region") == "GBA") & (pl.col("periodo") == "2026-11"))["logrel"][0]
    assert lr["2026-W44"] > 0 and lr["2026-W45"] > lr["2026-W44"]
    assert lr["2026-W46"] == pytest.approx(0, abs=1e-12)
    assert "2026-W53" not in lr and "2027-W01" not in lr                 # enero sin modelo: no se arrastra
    assert lr["2026-W44"] + lr["2026-W45"] == pytest.approx(nov, rel=1e-9)
    rs = sem.filter((pl.col("codigo") == "11.1") & (pl.col("periodo") == "2026-W47") & (pl.col("region") == "GBA"))["logrel"][0]
    s_sem = ((1.03) ** (7 / 30.44) - 1) * 100
    assert rs == pytest.approx(math.log1p((0.2 * 0.5 + 0.6 * 0.6 + 0.2 * s_sem) / 100))


def test_lectura_de_acuerdos_y_nowcast(tmp_path):
    (tmp_path / "nowcast__acuerdos_nov_dic_2026.csv").write_text(
        "Convenio,Var % nov,Var % dic,Confianza\nComercio,1.9,1.9,A\nGastronómicos,1.0,2.5,B\n", encoding="utf-8")
    (tmp_path / "estudio__nowcast_v2.csv").write_text("Concepto,Valor\nIndice m/m nov (estimado),9\n", encoding="utf-8")
    (tmp_path / "estudio__nowcast_v10.csv").write_text(
        "Concepto,Valor\nIndice m/m nov (estimado),1.5\nIndice m/m dic (estimado),2.1\nsesgo,0.1\n", encoding="utf-8")
    assert modelos._acuerdos(tmp_path, "2026-10") == {"2026-11": 1.0, "2026-12": 2.5}
    assert modelos._acuerdos(tmp_path, "2026-11") == {"2026-12": 2.5}
    assert modelos._nowcast_indice(tmp_path, "2026-10") == {"2026-11": 1.5, "2026-12": 2.1}
