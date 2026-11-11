from ipceer import frescos
from ipceer.otras_fuentes import leer_zonaprop


def test_zonaprop_toma_la_frase_de_alquiler_y_no_la_de_venta():
    t = ("El precio medio sube 0.4% en octubre. El alquiler medio de un departamento de 2 ambientes en la "
         "ciudad se ubica en $909.058 por mes, sube 1.2% en octubre.")
    r = leer_zonaprop(t)
    assert r["alquiler_2amb"] == 909058 and r["var_alquiler"] == 1.2
    assert leer_zonaprop("El precio medio sube 0.4%")["var_alquiler"] is None


def test_mag_pondera_por_kilos_y_saltea_subtotales():
    fila = "<tr><td>{}</td><td>1</td><td>2</td><td>3</td><td>4</td><td>{}</td><td>${}</td><td>{}</td><td>400</td></tr>"
    html = "<table>" + fila.format("NOVILLOS Mest.EyB 431/460", "10", "4.000.000,00", "1.000") + \
        fila.format("NOVILLOS Regulares", "10", "9.000.000,00", "3.000") + \
        fila.format("", "20", "13.000.000,00", "4.000") + \
        fila.format("NOVILLITOS EyB", "5", "5.000.000,00", "1.000") + "</table>"
    g = frescos.leer_mag(html)
    assert g["NOVILLOS"]["precio"] == 3250 and g["NOVILLITOS"]["precio"] == 5000
    assert g["TOTAL"]["kilos"] == 5000


def test_mes_de_los_nombres_del_mercado_central():
    assert frescos._mes_de_nombre("FRUTRAS_AGOSTO-26_0.zip") == "2026-08"
    assert frescos._mes_de_nombre("HORTALIZA_SEPTIENBRE_26_0.zip") == "2026-09"
    assert frescos._mes_de_nombre("HORTALIZAS_ABRIL2026.zip") == "2026-04"
