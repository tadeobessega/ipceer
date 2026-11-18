# Estudio de D-55: restaurantes y educación con salarios

Scripts del estudio del 15/11/2026 que llevó a los modelos de `src/ipceer/modelos.py`
(METODOLOGIA.md, D-55). No forman parte del cálculo diario. Usan pandas, numpy, scipy,
statsmodels y scikit-learn (no están en las dependencias del proyecto).

Se corren desde una carpeta de trabajo con esta estructura:

- `data/`: `serie_ipc_aperturas.csv`, `serie_ipc_divisiones.csv` e `indice_salarios.csv` del INDEC, y
  `par.xlsx` (el Excel `Indice_Salarios_Paritarias.xlsx`).
- `tadeobessega/ipceer/` (para `config/ponderadores_clase.csv`) y `salarios/` (clon de
  marcos-sacca/salarios, para `coef_finales.py`).
- `work/`: estos scripts; los resultados quedan en `work/resultados/`.

Orden: `datos_servicios.py data tadeobessega/ipceer` (arma `work/base_servicios.csv`),
`modelo_servicios.py` (familia de modelos y selección honesta), `analisis5.py`, `marzo.py`,
`marzo2.py`, `rest2.py`, `rest3.py`, `final2.py`, `montecarlo_rest.py`, `ml_modelos.py` y
`coef_finales.py` (coeficientes congelados de `config.yaml`).
