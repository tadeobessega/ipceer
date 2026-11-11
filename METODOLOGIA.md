# Metodología

La versión completa está en la pestaña *Metodología* del tablero (`sitio/index.html`), generada a partir de `web/template.html` con los parámetros vigentes. Este archivo resume las decisiones y, sobre todo, **de dónde sale cada ponderador**.

## Fuente

Base SEPA (Precios Claros), Secretaría de Industria y Comercio. Res. 12/2016 y 678/2020. Datos abiertos, CC BY 4.0: `datos.produccion.gob.ar/dataset/sepa-precios`. Precios diarios por sucursal de los comercios minoristas grandes. No incluye comercios chicos ni servicios.

## Cálculo

1. **Unidad**: producto-cadena-región (mismo código, presentación, comercio y bandera, región).
2. **Precio diario**: media geométrica entre sucursales de la cadena en la región (precio de lista). Se descartan sucursales con precio más de 4 veces por encima o por debajo de la mediana.
3. **Precio del período** (semana ISO o mes): media geométrica de los precios diarios.
4. **Índice elemental** (clase × región): Jevons encadenado. Media geométrica de los relativos de los productos presentes en ambos períodos. Se descartan relativos fuera de [0,33; 3]. Si hay menos de 8 productos, se usa el relativo nacional de la clase.
5. **Agregación**: Laspeyres con ponderadores fijos del INDEC por región. Nacional: regiones ponderadas por su peso en el gasto × peso cubierto.
6. **Semanal**: mensualizada = (1+v)^(30,44/7) − 1. Promedio de 4 semanas = media geométrica de los 4 relativos.
7. **Mes en curso**: promedio de los niveles semanales del mes contra el del mes anterior. Arrastre: las semanas que faltan quedan en el último nivel. Proyección: crecen al ritmo del promedio de 4 semanas.

## Ponderadores: fuentes

Todos provienen de la canasta del IPC del INDEC, **base diciembre 2016 = 100**. Se calcularon con la **ENGHo 2004/05**, con gastos actualizados a precios de diciembre de 2016. Es la canasta que el INDEC sigue usando: la basada en la ENGHo 2017/18 estaba prevista para enero de 2026 y se postergó.

| Archivo | Contenido | Fuente | Estado |
|---|---|---|---|
| `config/ponderadores_region.csv` | Peso de cada región en el total nacional (GBA 0,447; Pampeana 0,342; Noreste 0,045; Noroeste 0,069; Cuyo 0,052; Patagonia 0,046) | INDEC, Metodología n° 32 (2019), Cuadro 6 | Cargado y verificado |
| `config/ponderadores_division.csv` | Peso de las 12 divisiones en cada canasta regional | INDEC, informe técnico mensual del IPC, cuadro "Ponderaciones de la canasta, según divisiones, a diciembre de 2016" | Cargado y verificado |
| `config/ponderadores_clase.csv` | Peso de cada apertura (grupo y clase) en cada canasta regional | INDEC, Metodología n° 32, Anexo II, Cuadro 7 "Estructura de ponderaciones regionales del IPC" | **Se genera con `scripts/extraer_ponderadores.py`**. Hay que revisarlo a mano |

Sobre el Cuadro 7: se verificó la fila de Pan y cereales (01.1.1: GBA 4,05; Pampeana 5,02; Noreste 6,59; Noroeste 6,49; Cuyo 5,21; Patagonia 4,61). El resto se extrae del PDF en la máquina que corre el pipeline. El script compara la suma de los grupos de cada división con el peso publicado de la división.

**Clases sin peso propio**: se usa el del grupo que las contiene (nunca el de la división). Las que no tienen peso ni en su grupo quedan fuera y se informan en `resultados/mapeo_clases_*.csv`.

**Cobertura**: la suma de los pesos de las clases relevadas es la parte de la canasta del IPC que representa el índice (`resultados/cobertura_*.csv`).

## Limitaciones principales

- No es un IPC: cubre solo bienes de grandes cadenas.
- Ponderadores de 2004/05 (la misma limitación que el IPC oficial).
- Los productos no se ponderan dentro de cada rubro (SEPA no tiene cantidades vendidas).
- Un cambio de presentación cuenta como producto nuevo: no hay ajuste por reduflación ni se capta la sustitución.
- Hay estacionalidad dentro del mes (las listas se actualizan a comienzo de mes), así que una semana sola mensualizada engaña. Por eso el indicador principal es el promedio de 4 semanas.
- La clasificación se hace con reglas y puede tener errores.

## Ponderación con los índices del INDEC y carrera de métodos

Lo que se publica es inflación: la variación de los precios propios de cada rubro, sumada con las
ponderaciones del INDEC. Los ponderadores de la canasta (base diciembre 2016) se llevan al último mes
publicado por el INDEC multiplicándolos por el índice oficial de cada rubro; de ahí en adelante se
actualizan con las variaciones propias hasta que sale el dato oficial siguiente
(`indice.calcular(..., ponderacion="indec")`, el valor por defecto).

`ipceer carrera` calcula en cada corrida todas las variantes de método (`src/ipceer/carrera.py`),
también las descartadas, y las compara con el nivel general del INDEC y con la réplica del INDEC para
las mismas clases. `ipceer carrera --historico` hace la prueba 2017-2018 de las variantes cuya
definición cambió. Resultados: `resultados/carrera.json`, `resultados/carrera_historico.json` y
`resultados/pronosticos.csv` (lo que cada variante estimó para cada mes, con la fecha del cálculo; no
se reescribe). La variante publicada se cambia solo si otra baja el error contra el INDEC.

Prueba 2017-01 a 2018-03 (15 meses), error mensual en puntos porcentuales contra la réplica y contra
el nivel general: publicado 0,69 y 0,89; ponderación anterior 0,69 y 0,88; con promociones 0,73 y
0,92; media recortada 0,69 y 0,92; mediana 1,54 y 1,80; por sucursales 0,70 y 0,88; sin frutas ni
verduras 0,79 y 0,86; sin frescos 0,86 y 0,93.

Los precios de tiendas en línea (`src/ipceer/web.py`, `config/web/`) los baja GitHub Actions una vez
por día y la PC trae el resultado. No tienen historia: entran solo en sus variantes de la carrera.
