# Metodología del IPCEER

Este archivo es el registro completo del método: qué se mide, con qué datos, cómo se calcula y **cada
decisión tomada, con su motivo y la evidencia**. La pestaña *Metodología* del tablero
(`web/template.html`) lo presenta para el público con los parámetros vigentes. Si algo del código no
está acá, es un error de este documento: hay que agregarlo.

Regla general: **todo cambio de método tiene que mejorar la dispersión contra el INDEC** (error
cuadrático medio mensual contra la réplica y contra el nivel general). Si no la mejora, no se adopta
sin consultarlo, explicando qué cambia, por qué y cuánto.

## 1. Qué mide

La **inflación mensual y semanal** de los precios al consumidor, con datos propios de alta frecuencia,
antes de que publique el INDEC, y métricas que el INDEC no publica (rotación y difusión de precios,
precios por cadena y por región, índice diario, canasta básica por cadena). No es un índice de largo
plazo: cuando el INDEC publica un mes, su dato reemplaza al propio (ver D-19).

Se publican tres números (D-16):

- **IPCEER total**: los rubros que cubrimos, con los pesos del INDEC. Hoy son sobre todo bienes de
  supermercado; no es el nivel general del IPC.
- **IPCEER-alimentos nacional**: división 01 (alimentos y bebidas no alcohólicas), las seis regiones.
- **IPCEER-alimentos GBA**: división 01 en Gran Buenos Aires.

## 2. Datos: qué se usa, de dónde sale y dónde se baja

| Dato | Fuente | Uso | Dónde se baja |
|---|---|---|---|
| Precios diarios de supermercados (unos 12 millones por día) | SEPA, Secretaría de Industria y Comercio (Res. 12/2016 y 678/2020), datos abiertos | Índice: es la base de todo | PC (tarea diaria) y GitHub Actions (`respaldo_sepa.yml`, dos veces por día, al release `sepa-archivo`); la PC trae del release lo que le falte |
| SEPA histórico 2016-2018 | Base Precios Claros en Zenodo | Prueba contra el INDEC (2017-01 a 2018-03) | PC (una vez, `ipceer historico`) |
| SEPA de días sueltos (abril de 2025) | Wayback Machine | Tramo aislado | PC (consulta diaria) |
| IPC por región, división, grupo y clase; CBA y CBT | INDEC | Ponderadores actualizados, validación, empalme | PC |
| Ponderadores de la canasta | INDEC, Metodología n° 32 (2019), Cuadros 6 y 7 | Agregación | PC (`scripts/extraer_ponderadores.py`) |
| Combustibles por estación (precio y volumen) | Secretaría de Energía (Res. 1104/2004) | Clase 07.2.2 y variante "completo" | PC |
| Tarifas reguladas (Edenor, Metrogas, AySA) | Cuadros tarifarios y normas, cargados a mano con fuente (`config/regulados/tarifas.csv`) | Canasta de servicios y variante "completo" | Carga manual |
| Canasta de servicios del AMBA | IIEP (UBA-CONICET), AFISPOP (`config/regulados/afispop.csv`) | Referencia y empalme | Carga manual |
| Hacienda en pie | Mercado Agroganadero de Cañuelas | Referencia mayorista, no entra al índice | GitHub Actions (`otras_fuentes.yml`) y PC |
| Frutas y hortalizas | Mercado Central de Buenos Aires | Referencia mayorista, no entra al índice | GitHub Actions y PC |
| Alquiler de un 2 ambientes en CABA | Informe Index Zonaprop | Variante "completo" (clase 04.1.1, GBA) | GitHub Actions y PC |
| Medicamentos (copagos) | PAMI, datos abiertos | Referencia | GitHub Actions y PC |
| Dólar mayorista, ICL, CER, UVA | BCRA, API v4.0 | Referencia | GitHub Actions y PC |
| Catálogos de tiendas en línea (ropa, calzado, hogar, medicamentos, frescos por kilo) | Catálogos públicos VTEX y Shopify (`config/web/`) | Variantes "web" y "completo" | GitHub Actions (`precios_web.yml`, cuatro partes); la PC trae el resultado |
| Estimaciones de consultoras | Notas publicadas, con enlace (`config/externos.csv`) | Comparación | Carga manual |

## 3. Cálculo

### 3.1 Unidad y precio (D-03 a D-06)

- **D-03. Unidad**: producto-cadena-región (mismo código, presentación, comercio, bandera y región).
  Comparar dentro de la cadena evita que la entrada o salida de cadenas en SEPA se lea como cambio de precio.
- **D-04. Precio diario**: media geométrica entre las sucursales de la cadena en la región. Se descartan
  precios de sucursal más de 4 veces por encima o por debajo de la mediana (errores de carga).
- **D-05. Precio del período** (semana ISO o mes): media geométrica de los precios diarios. Igual que
  el INDEC, se compara el promedio del mes con el del mes anterior (no punta contra punta). Un producto
  entra si tiene precio al menos 5 días del mes o 2 de la semana (o la mitad de los días con datos).
- **D-06. Precio de lista**, no el precio con promociones. Motivo: en la prueba 2017-2018 el precio con
  promociones dio más error (0,73 contra 0,69 contra la réplica) y el INDEC no toma descuentos
  condicionados. Se sigue calculando como variante "efectivo".

### 3.2 Clasificación y regiones (D-07, D-08)

- **D-07.** Cada descripción se clasifica en una clase COICOP con reglas de texto
  (`config/clasificador.yaml`, correcciones en `config/clasificacion_manual.csv`). Lo no clasificado
  queda fuera. Error conocido (diagnóstico de noviembre de 2026): en Frutas y en Verduras entran
  productos que no son frescos (bebidas de manzana, tortas de durazno, papas fritas de paquete,
  aceitunas). La estratificación (D-12) lo atenúa porque esos productos caen en variedades propias o
  en "otros"; las reglas del clasificador se tienen que seguir depurando.
- **D-08. Regiones**: las seis del INDEC. Una sucursal de la provincia de Buenos Aires va a GBA si está
  a menos de 40 km del Obelisco (aproximación a los 24 partidos).

### 3.3 Índice elemental (D-09, D-12, D-13, D-14)

Método vigente desde el 11 de noviembre de 2026 ("mejorado", D-15):

1. **D-12. Estratificación por variedad.** Dentro de cada clase de alimentos y bebidas, cada artículo
   se asigna a una variedad (`config/variedades.yaml`: pan fresco, galletitas, fideos secos, leche
   fluida, quesos, carne vacuna, yerba, gaseosas, etc.). Se calcula el Jevons (media geométrica de los
   relativos) dentro de cada variedad y después la media ponderada de las variedades de la clase.
   Una variedad con menos de 3 artículos apareados no entra ese período; los artículos sin variedad
   forman "otros" con peso 5. Si ninguna variedad llega al mínimo, la clase usa el Jevons de todos sus
   artículos. Las clases no alimentarias no tienen variedades: equivale al Jevons simple.
   - Motivo: el INDEC calcula su nivel elemental por variedad, con ponderadores de gasto (Metodología
     n° 32, apartados 5.1 y 5.2). En SEPA, una clase como Pan y cereales tiene 4.800 galletitas dulces
     y 114 panes frescos en GBA: el promedio simple de todos los artículos le da a las galletitas el
     peso que el INDEC le da al pan.
   - **Los pesos de las variedades son propios, no del INDEC**: el INDEC no publica ponderadores por
     variedad. Son una aproximación a partir de la composición de la CBA y de la canasta del IPC, fijada
     antes de la prueba nacional y no ajustada con ella. Con pesos iguales entre variedades
     ("estrato_igual" en el diagnóstico) el resultado es parecido. Si el INDEC publica sus pesos, se
     reemplazan.
2. **D-13. Artículos populares.** Solo entran los códigos de barras que venden al menos 3 comercios
   distintos en la región en el período. Motivo: el encuestador del INDEC elige "el artículo más
   vendido" de cada variedad (apartado 3.3); los productos que vende una sola cadena (marcas propias,
   presentaciones raras) suben menos y tienen precios más quietos. Es el mismo umbral que ya se usaba
   para comparar cadenas.
3. **D-14. Comparación directa, sin encadenar.** La variación de un período es el cociente entre la
   comparación directa contra la base y la del período anterior contra la misma base:
   `ln R(t) = J(base→t) − J(base→t−1)`. La base es el último período de diciembre del año anterior
   dentro del mismo tramo; si el tramo no tiene un diciembre previo, su primer período. Motivo: el
   INDEC sigue una muestra fija de artículos desde la base; encadenar mes a mes con artículos que
   entran y salen acumula deriva (en la prueba, el encadenado medía 0,1 puntos por mes menos).
4. **D-09.** Se descartan relativos fuera de [0,33; 3]. Si una clase tiene menos de 8 artículos
   comparables en la región, usa la variación nacional de la clase; si tampoco alcanza, queda **sin
   dato** ese período (no se arrastra el nivel anterior ni se supone variación cero).

### 3.4 Agregación (D-10, D-11)

- **D-10. Ponderadores del INDEC**, base diciembre 2016 (ENGHo 2004/05): región (Cuadro 6), división y
  clase (Cuadro 7). Clases sin peso propio usan el de su grupo, nunca el de la división. La canasta
  ENGHo 2017/18 se anunció para enero de 2026 y el Gobierno la postergó (febrero de 2026): el INDEC
  sigue con la de 2004/05, así que se usa esa.
- **D-11. Ponderación "indec"**: los pesos se llevan al último mes publicado por el INDEC con el índice
  oficial de la serie más específica de cada rubro, y desde ahí con las variaciones propias. La
  variación de un agregado es la suma de las variaciones propias de cada clase con esos pesos; una
  clase sin dato sale del eslabón. En 2017-2018 da prácticamente lo mismo que actualizar los pesos con
  el nivel propio (0,695 contra 0,687 contra la réplica). **Pendiente de decisión de Tadeo**: dejarla
  o volver a la ponderación propia.
- Nacional: regiones con su peso en el gasto por el peso cubierto en cada una.

### 3.5 Semanal, mes en curso y escenarios (D-17, D-18)

- **D-17.** Semana ISO (lunes a domingo), asignada al mes de su jueves. Mensualizada:
  `(1+v)^(30,44/7) − 1`. Promedio de 4 semanas: media geométrica de los 4 relativos. El índice semanal
  usa el mismo método elemental (D-12 a D-14); su base directa es la última semana de diciembre del año
  anterior o la primera del tramo.
- **D-18.** Mes en curso: **relevado** (promedio de las semanas observadas contra el mes anterior).
  **Arrastre** (las semanas que faltan quedan en el último nivel) y **proyección** (crecen al ritmo
  del promedio de 4 semanas) son **escenarios rotulados**: nunca reemplazan al dato relevado.

### 3.6 Empalme con el INDEC y reglas sobre datos oficiales (D-19, D-20)

- **D-19.** Lo propio es provisorio: cubre los meses que el INDEC todavía no publicó (unos 45 días).
  Cuando sale el dato oficial, se usa ese dato y sus pesos.
- **D-20.** Nunca se estima información oficial ni se arrastran datos del pasado a períodos presentes.
  Un rubro sin datos queda sin datos. Las proyecciones y los ajustes son escenarios con su nombre.

## 4. Diagnóstico del sesgo a la baja (noviembre de 2026)

### 4.1 El problema

En la prueba 2017-01 a 2018-03 el método anterior medía 23,0% acumulado contra 28,4% de la réplica
del INDEC (mismas clases, mismos pesos) y 33,1% del nivel general: **0,3 puntos por mes menos que la
réplica y 0,5 menos que el nivel general**. Las dos brechas tienen causas distintas:

- **Cobertura** (réplica contra nivel general, unos 0,24 puntos por mes): no medimos servicios,
  tarifas, alquileres ni transporte, que en 2017 subieron mucho más que los bienes. Se resuelve
  sumando esos rubros (pendientes, sección 6) o comparando cada subíndice con su par del INDEC (D-16).
- **Método y fuente** (nuestro índice contra la réplica, unos 0,3 puntos por mes): es lo que se
  diagnosticó.

### 4.2 Qué se probó

`scripts/diagnostico_sesgo.py` recalcula cada clase de alimentos de una región de varias formas y la
compara con la misma clase del INDEC (resultados en `resultados/diagnostico_sesgo_*.json`). Error
mensual (puntos porcentuales) en alimentos y bebidas, 2017-01 a 2018-03:

| Forma | GBA sesgo | GBA error | Pampeana sesgo | Pampeana error | Hipótesis que prueba |
|---|---|---|---|---|---|
| Anterior (Jevons encadenado de todo) | −0,41 | 0,85 | −0,20 | 0,93 | |
| Estratificado con pesos | −0,39 | 0,79 | −0,16 | 0,88 | Variedades con muchos artículos pesan de más |
| Estratificado sin "otros" | −0,37 | 0,87 | −0,13 | 0,93 | Errores de clasificación |
| Solo populares (3 comercios o más) | −0,28 | 0,87 | −0,10 | 0,99 | Artículos poco vendidos suben menos |
| Sin artículos con precio quieto 3 meses | −0,36 | 0,84 | −0,13 | 0,97 | Precios desactualizados en SEPA |
| Sin cadenas quietas (más de 85% sin cambio) | −0,40 | 0,84 | −0,19 | 0,93 | Cadenas que no actualizan SEPA |
| Con promociones | −0,42 | 0,94 | −0,20 | 0,97 | Promociones |
| Directo contra diciembre | −0,31 | 0,81 | −0,14 | 0,94 | Deriva del encadenamiento |
| Directo + estratificado | −0,32 | 0,77 | −0,14 | 0,89 | |
| Directo + estratificado + populares | −0,16 | 0,82 | +0,02 | 0,85 | Todo junto |
| Valor unitario (precio por kilo de todo) | +2,5 | 17,6 | +1,5 | 13,2 | Entradas y salidas de productos: descartado, demasiado ruido |

Hallazgos:

1. **El sesgo es parejo en casi todas las clases** (Pan y cereales −0,51; Lácteos −0,34; Café y yerba
   −0,40; Azúcar y dulces −0,30 por mes en GBA). Aceites es la excepción (0,0). No es un problema de
   ponderación entre rubros: es del promedio dentro de cada rubro y de la fuente.
2. **Tres causas de método explican más de la mitad** y se corrigen (D-12 a D-14): el peso de las
   variedades con muchos artículos, los artículos poco vendidos y la deriva del encadenamiento.
3. **Tres comercios tienen 70% a 75% de sus precios sin cambios en tres meses** (ids 61, 64 y 65 en
   2017) y otros dos rondan 45%: son datos desactualizados en SEPA. Sacarlos casi no mueve el índice
   (ya pesan poco), pero se controla en `estancados_por_comercio`.
4. **Lo que queda, unos 0,15 puntos por mes en GBA, no es corregible con SEPA**: el INDEC releva también
   almacenes, panaderías, carnicerías y verdulerías (Metodología n° 32, apartados 3.1 y 5.1, con la
   proporción de cada tipo de negocio de la ENGHo), y en 2017 esos precios subieron más que los de las
   grandes cadenas. En SEPA hay 114 panes frescos contra 4.800 galletitas en GBA. Se necesitan fuentes
   de comercios tradicionales (sección 6).
5. **Frutas y verduras** son las peores clases (sesgo −1,8 y −0,7 por mes, error de 4 a 5 puntos): pocos
   frescos reales en SEPA y errores de clasificación (D-07).
6. **Promociones**: incluirlas empeora (D-06). **Media recortada y mediana**: empeoran.

### 4.3 Prueba nacional con el cálculo real (carrera de métodos)

Con `ipceer carrera --historico`, todas las regiones, 2017-01 a 2018-03 (sesgo / error en puntos
porcentuales por mes):

| Variante | Contra la réplica | Contra el nivel general | Alimentos GBA | Alimentos nacional |
|---|---|---|---|---|
| Anterior | −0,30 / 0,67 | −0,54 / 0,89 | −0,42 / 0,84 | −0,29 / 0,72 |
| Estratificado | −0,29 / 0,64 | −0,53 / 0,87 | −0,42 / 0,79 | −0,29 / 0,66 |
| Populares | −0,18 / 0,66 | −0,42 / 0,82 | −0,27 / 0,85 | −0,17 / 0,75 |
| Directo | −0,22 / 0,66 | −0,46 / 0,85 | −0,33 / 0,83 | −0,22 / 0,74 |
| **Mejorado (las tres)** | **−0,05 / 0,63** | **−0,30 / 0,75** | **−0,14 / 0,86** | **−0,05 / 0,72** |

Acumulado 2017-01 a 2018-03: anterior 23,0%, mejorado 27,4%, réplica del INDEC 28,4%, nivel general 33,1%.

- **D-15. Se adopta el método "mejorado"** (estratificar, populares = 3, directo) como publicado
  (`config/config.yaml`): baja el error contra la réplica (0,67 a 0,63) y contra el nivel general
  (0,89 a 0,75) y casi elimina el sesgo contra la réplica (−0,30 a −0,05). Cumple la regla de mejorar
  la dispersión. Advertencias: (a) son 15 meses de 2017-2018, con otra inflación y otros controles de
  precios; (b) el diagnóstico de GBA y Pampeana se vio antes de elegir la combinación, así que la prueba
  nacional no es del todo fuera de muestra. La validación de verdad son los meses de 2026 que publique
  el INDEC, empezando por noviembre (octubre no se puede medir: no hay septiembre). El método anterior
  sigue corriendo como variante "anterior".
- El sesgo contra el nivel general (−0,30) es la brecha de cobertura: no es un error del índice de
  bienes, y se cierra sumando servicios (sección 6), no ajustando los precios.

### 4.4 ¿Conviene limitar el índice a alimentos? (D-16)

No da considerablemente mejor. Contra su propia referencia del INDEC, alimentos nacional tiene error
0,72 y alimentos GBA 0,86, contra 0,63 del total contra la réplica y 0,75 contra el nivel general. GBA
solo es más ruidoso (menos artículos por clase y más peso de los comercios tradicionales). **D-16**: se
publican los tres (total, alimentos nacional y alimentos GBA), cada uno comparado con su par del INDEC;
el total no se presenta como nivel general.

## 5. Carrera de métodos, validación y comparación con consultoras (D-21, D-22, D-29, D-31)

- **D-21.** En cada corrida se calculan todas las variantes (`src/ipceer/carrera.py`), también las
  descartadas: publicado, anterior, estratificado, populares, directo, mejorado, ponderación propia,
  con promociones, media recortada, mediana, por sucursales, sin frutas ni verduras, sin frescos, con
  tiendas en línea, completo (tiendas, tarifas, combustibles y alquileres). Cada una se mide contra el
  nivel general y la réplica, y sus subíndices de alimentos contra la división 01 del INDEC (GBA y
  nacional). Lo que cada variante estimó para un mes queda en `resultados/pronosticos.csv` con la
  fecha del cálculo y no se reescribe. La firma de cada variante incluye el método vigente y las
  variedades: si cambian, la prueba histórica se rehace sola.
- **D-22.** Dos ajustes estadísticos (publicado más el sesgo medio; calibrado por regresión) se calculan
  como **experimentales**, con error medido dejando afuera de a un mes. No se publican como dato.
- **D-29. Consultoras.** Cada dato publicado se carga con enlace, fecha y nota en `config/externos.csv`
  y se compara con la medida propia equivalente (alimentos con alimentos, total con total, semanal con
  semanal). Antecedentes: EcoGo (RPM, scraping semanal con ponderadores del IPC de la Ciudad) midió 2,0%
  en septiembre de 2026 contra 1,9% del INDEC, y 2,4% en octubre de 2025 contra 2,3% del INDEC; LCG
  (8.000 alimentos de 5 supermercados en línea, un relevamiento los miércoles) es más volátil y en
  octubre de 2025 marcó más de 3% en alimentos. Las consultoras que relevan góndola en línea sobrestiman
  en semanas de ofertas al revés: LCG atribuyó su −0,2% de la primera semana de noviembre de 2026 al
  CyberMonday. Econviews publica su relevamiento en X, que no se puede leer de forma automática: se
  carga a mano cuando aparece en notas.
- **D-31. Validación.** Réplica (mismas clases, mismos pesos) y nivel general, por región y por clase
  (`resultados/validacion_indec.csv`).

## 6. Qué falta para el índice completo

Peso en la canasta nacional del INDEC (divisiones de `config/ponderadores_division.csv` ponderadas por
región) y estado:

| Rubro | Peso | Estado | Qué falta |
|---|---|---|---|
| Alimentos y bebidas no alcohólicas (01) | 27,0% | Medido con SEPA; dentro de él, carnes 8,9%, verduras 2,7% y frutas 1,3% están débiles | Comercios tradicionales (panaderías, carnicerías, verdulerías, almacenes) y frescos por kilo |
| Bebidas alcohólicas y tabaco (02) | 3,5% | Bebidas medidas; tabaco no | Precios de cigarrillos (listas de las tabacaleras) |
| Ropa y calzado (03) | 9,9% | Variante web, sin historia | Juntar historia y probar contra el INDEC |
| Vivienda, agua, electricidad, gas (04) | 9,4% | Parcial: tarifas a mano, Zonaprop CABA | Cuadros tarifarios automáticos (ENRE, ENARGAS, AySA), cargo fijo de gas, factura base del agua, alquileres fuera de CABA, expensas |
| Equipamiento del hogar (05) | 6,4% | Limpieza medida; electrodomésticos en variante web | Historia web, servicio doméstico (salario por norma) |
| Salud (06) | 8,0% | Referencia PAMI | Cuotas de prepagas (aumentos autorizados), precios de farmacia |
| Transporte (07) | 11,0% | Combustibles medidos; boleto a mano | Tarifas de colectivos y trenes automáticas, precios de autos |
| Comunicación (08) | 2,8% | No medido | Planes de telefonía e internet |
| Recreación y cultura (09) | 7,3% | Muy poco (diarios, útiles) | Servicios digitales, turismo, libros |
| Educación (10) | 2,3% | No medido | Cuotas de colegios privados |
| Restaurantes y hoteles (11) | 9,0% | No medido | Precios de menús (cartas en línea, delivery) |
| Bienes y servicios varios (12) | 3,6% | Cuidado personal medido | Servicios personales (peluquería) |

## 7. Indicadores que el INDEC no mide (D-30)

Rotación de precios (parte que sube y baja, tamaño medio, difusión ponderada), precios por cadena y
por región (modelo de efectos fijos producto-cadena), inflación por cadena, canasta básica por cadena,
mapa por provincia y partido, índice diario contra un día base para eventos, hilo semanal. Detalle de
cada uno en la pestaña Metodología del tablero.

## 8. Dónde corre cada parte (D-32)

- **GitHub Actions**: respaldo de SEPA (dos veces por día), otras fuentes (diario), tiendas en línea
  (diario, cuatro partes que después se juntan; cada parte reintenta la subida si falla). Todo queda
  en el release `sepa-archivo`.
- **PC**: base de unos 15 GB (paneles diarios, semanales y mensuales), INDEC, combustibles,
  consolidación, carrera, cálculo, tablero y respaldo. GitHub Actions no la reemplaza: no tiene disco
  persistente para la base y su horario es inexacto. Las opciones para mover más a internet están en
  discusión (ver la respuesta del 11/11/2026).

## 9. Limitaciones

- Solo grandes cadenas; sin comercios tradicionales (sección 4.2, hallazgo 4).
- Pesos de variedades propios (D-12); pesos de clases de la ENGHo 2004/05 (igual que el INDEC).
- Sin ajuste por reduflación: un cambio de presentación es un producto nuevo.
- Estacionalidad dentro del mes: una semana sola mensualizada engaña; el indicador principal es el
  promedio de 4 semanas.
- Clasificación por reglas, con errores conocidos en frescos (D-07).
- La prueba contra el INDEC es de 2017-2018; la de 2026 empieza con noviembre.
