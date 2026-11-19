"""El ipceer con las semanas de las consultoras (D-67).

El ipceer semanal va de lunes a domingo (semana ISO). LCG y EcoGo cierran la semana el miércoles
(de jueves a miércoles) y Econviews el jueves (de viernes a jueves). Para comparar lo mismo, se arma la
variación del ipceer con esas semanas a partir del índice diario por división (Jevons contra un día
base fijo, `diario.py`), con el mismo criterio de devengado que el semanal: el nivel de una semana es el
promedio de los niveles de sus siete días y la variación es el cociente de dos semanas seguidas.

Solo se calcula con las dos semanas completas (14 días seguidos con dato): no se completa ningún día.
Cada semana se identifica con la semana ISO de su último día, que es como se cargan los datos de las
consultoras en config/externos.csv. Es una comparación aparte: el ipceer publicado no cambia.
"""
from __future__ import annotations

from datetime import date, timedelta

DEFINICIONES = {
    "iso": {"fin": 6, "nombre": "Lunes a domingo (ipceer)"},
    "mie": {"fin": 2, "nombre": "Jueves a miércoles (LCG, EcoGo)"},
    "jue": {"fin": 3, "nombre": "Viernes a jueves (Econviews)"},
}
SEMANA_DE = {"LCG": "mie", "EcoGo": "mie", "Econviews": "jue"}


def calcular(diario: dict, regiones=("Nacional", "GBA"), codigos=("01",)) -> list[dict]:
    """Solo alimentos: el índice diario es de bienes de SEPA y no tiene los servicios del nivel general."""
    series = (diario or {}).get("series") or {}
    out = []
    for reg in regiones:
        for cod in codigos:
            s = {d: v for d, v in (series.get(reg) or {}).get(cod) or [] if v is not None}
            if len(s) < 14:
                continue
            dias = sorted(date.fromisoformat(d) for d in s)
            for clave, df in DEFINICIONES.items():
                for f in (d for d in dias if d.weekday() == df["fin"]):
                    a = [s.get((f - timedelta(i)).isoformat()) for i in range(7)]
                    b = [s.get((f - timedelta(i + 7)).isoformat()) for i in range(7)]
                    if None in a or None in b:
                        continue
                    a_, w, _ = f.isocalendar()
                    out.append({"region": reg, "codigo": cod, "semana_tipo": clave, "desde": (f - timedelta(6)).isoformat(),
                                "hasta": f.isoformat(), "semana": f"{a_}-W{w:02d}",
                                "var": round((sum(a) / sum(b) - 1) * 100, 4)})
    return out
