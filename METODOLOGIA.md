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
| Precios diarios de supermercados (unos 12 millones por día) | SEPA, Secretaría de Industria y Comercio (Res. 12/2016 y 678/2020), datos abiertos | Índice: es la base de todo | GitHub Actions baja los zips (`respaldo_sepa.yml`, tres veces por día) y los procesa (`procesar_sepa.yml`, D-33); la PC trae el resultado y guarda los zips |
| SEPA histórico 2016-2018 | Base Precios Claros en Zenodo | Prueba contra el INDEC (2017-01 a 2018-03) | PC (una vez, `ipceer historico`) |
| SEPA de días sueltos (abril de 2025) | Wayback Machine | Tramo aislado | PC (consulta diaria) |
| IPC por región, división, grupo y clase; CBA y CBT | INDEC | Ponderadores actualizados, validación, empalme | PC |
| Ponderadores de la canasta | INDEC, Metodología n° 32 (2019), Cuadros 6 y 7 | Agregación | PC (`scripts/extraer_ponderadores.py`) |
| Combustibles por estación (precio y volumen) | Secretaría de Energía (Res. 1104/2004) | Clase 07.2.2 y variante "completo" | PC |
| Tarifas reguladas (Edenor, Metrogas, AySA) | Cuadros tarifarios y normas, cargados a mano con fuente (`config/regulados/tarifas.csv`) | Canasta de servicios y variante "completo" | Carga manual |
| Canasta de servicios del AMBA | IIEP (UBA-CONICET), AFISPOP (`config/regulados/afispop.csv`) | Referencia y empalme | Carga manual |
| Hacienda en pie | Mercado Agroganadero de Cañuelas | Referencia mayorista, no entra al índice | GitHub Actions (`otras_fuentes.yml`); la PC trae el resultado (D-33) |
| Frutas y hortalizas | Mercado Central de Buenos Aires | Referencia mayorista, no entra al índice | GitHub Actions; la PC trae el resultado |
| Alquiler de un 2 ambientes en CABA | Informe Index Zonaprop | Variante "completo" (clase 04.1.1, GBA) | GitHub Actions; la PC trae el resultado |
| Medicamentos (copagos) | PAMI, datos abiertos | Referencia | GitHub Actions; la PC trae el resultado |
| Dólar mayorista, ICL, CER, UVA | BCRA, API v4.0 | Referencia | GitHub Actions; la PC trae el resultado |
| Catálogos de tiendas en línea (ropa, calzado, hogar, medicamentos, frescos por kilo) | Catálogos públicos VTEX y Shopify (`config/web/`) | Variantes "web" y "completo" | GitHub Actions (`precios_web.yml`, cuatro partes); la PC trae el resultado |
| Cuotas de medicina prepaga (valor de cada plan por edad y región, aumento y afiliados) | Superintendencia de Servicios de Salud, cuadros tarifarios (Res. SSS 645/2025), API pública de cuadrostarifarios.sssalud.gob.ar | Clase 06.4.1 en la variante "completo" y validación contra el INDEC (D-35) | GitHub Actions (`otras_fuentes.yml`); la PC trae el resultado |
| Normas de tarifas (luz, gas, agua, transporte, peajes, prepagas, medicamentos, comunicaciones, servicio doméstico) | Infoleg (Ministerio de Justicia), fichas y anexos | Detección diaria; los cuadros de Edenor y Edesur se leen solos (D-34) | GitHub Actions (`otras_fuentes.yml`); la PC trae el resultado |
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
  `(1+v)^(30,44/7) − 1`. Promedio de 4 semanas: media geométrica de los 4 relativos.
- **D-37 (12 de noviembre de 2026). El índice semanal usa el método anterior** (Jevons encadenado de
  todos los artículos, sin variedades, sin filtro de populares y sin comparación directa), configurable
  en `indice.semanal`. Motivo: el método mejorado (D-12 a D-14) se validó solo contra el INDEC mensual;
  aplicado a las semanas de octubre y noviembre de 2026 bajaba alimentos de GBA, que con el método
  anterior daba 0,29% y 0,35% en las dos semanas cerradas, en línea con lo que publicaban las
  consultoras (Econviews: 0,3% y 0,4%). Con un tramo semanal que empieza el 24 de octubre, la base de
  la comparación directa es esa primera semana parcial, y el filtro de populares deja pocos artículos
  por variedad. El mensual sigue con el método mejorado. Se vuelve a evaluar cuando haya meses de
  2026 publicados por el INDEC para comparar ambos.
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
  Octubre de 2026 (INDEC, publicado el 12/11: nivel general 2,2% nacional y 2,0% en GBA; alimentos
  2,0% nacional y 1,9% en GBA). Error = estimación menos INDEC, en puntos porcentuales:

  | Consultora | Rubro y región | Estimó | INDEC | Error |
  |---|---|---|---|---|
  | Analytica | Total, nacional | 2,0 | 2,2 | −0,2 |
  | BBVA Research (modelo, no relevamiento) | Total, nacional | 2,0 | 2,2 | −0,2 |
  | EcoGo (RPM) | Total, nacional | 1,8 y 2,1 (dos notas) | 2,2 | −0,4 y −0,1 |
  | Equilibra | Total, nacional | 1,8 | 2,2 | −0,4 |
  | LCG | Total, nacional | 1,8 | 2,2 | −0,4 |
  | Fundación Libertad y Progreso | Total, nacional | 1,8 | 2,2 | −0,4 |
  | REM del BCRA (mediana) | Total, nacional | 1,8 | 2,2 | −0,4 |
  | C&T | Total, GBA | 2,0 y 1,9 | 2,0 | 0,0 y −0,1 |
  | C&T | Alimentos, GBA | 1,9 | 1,9 | 0,0 |
  | EcoGo | Alimentos, nacional | 2,4 | 2,0 | +0,4 |
  | Equilibra | Alimentos, nacional | 2,1 | 2,0 | +0,1 |
  | LCG (supermercados del AMBA) | Alimentos | 2,3 | 2,0 nacional; 1,9 GBA | +0,3; +0,4 |

  Lectura: casi todas subestimaron el nivel general (en 0,4 puntos la mayoría) y sobrestimaron
  alimentos; la que más se acercó fue C&T, que releva en GBA. FIEL (Ciudad) y las mediciones de Bahía
  Blanca no tienen dato del INDEC equivalente. El octubre propio no se puede medir: el tramo de datos
  actual no tiene septiembre completo para comparar (D-14), así que no hay variación mensual nuestra
  de octubre. El tablero calcula esta tabla sola con cada dato nuevo del INDEC
  (`externos.errores_consultoras`, con la variación que publica el INDEC).
- **D-31. Validación.** Réplica (mismas clases, mismos pesos) y nivel general, por región y por clase
  (`resultados/validacion_indec.csv`).

### 5.1 Regulados, salud y rubros sin precios propios (D-34 a D-41)

- **D-34. Normas de tarifas desde Infoleg.** El Boletín Oficial no permite robots (robots.txt), así
  que se usa Infoleg, que carga las mismas normas con sus anexos y no lo prohíbe. Infoleg numera las
  normas con un id correlativo: todos los días se revisan los ids nuevos de a uno, con un segundo de
  pausa e identificándose, hasta una racha de 60 vacíos. Se guardan las normas de los organismos y
  temas de `config/regulados/normas.yaml` (`data/otras/normas/normas.csv`) y se muestran en la
  pestaña Regulados para cargarlas. En las resoluciones del ente regulador sobre Edenor y Edesur se
  leen los anexos en PDF y se toman los cargos fijo y variable de R1 y R2 "sin subsidio", vigentes
  desde la fecha del cuadro hasta fin de ese mes. Un valor leído entra al registro de tarifas solo
  si su variación contra el último valor aceptado del mismo concepto está entre −30% y +60%; si no,
  queda "a revisar". Si el mismo concepto y fecha está cargado a mano, manda la carga manual. Gas,
  agua, transporte y peajes todavía se detectan pero no se leen solos: sus cuadros tienen formatos
  distintos por empresa y jurisdicción (los colectivos de la Ciudad y de la Provincia no salen en
  Infoleg). Los precios estacionales de la energía (Secretaría de Energía) también se detectan.
- **D-35. Prepagas desde la Superintendencia de Servicios de Salud.** Desde la Res. SSS 645/2025 las
  prepagas declaran el valor de cada plan, por edad y región, dentro de los 5 días hábiles después del
  dato del INDEC, y la Superintendencia lo publica (cuadrostarifarios.sssalud.gob.ar, desde julio de
  2025). La API devuelve también la cantidad de cápitas (afiliados) de cada celda. La variación del
  mes es la media geométrica de los relativos de los mismos planes, edades y regiones entre un mes y
  el anterior, ponderada por las cápitas del mes anterior (relativos fuera de [0,5; 2] se
  descartan). Se bajan las 13 entidades más grandes (`config/regulados/prepagas.yaml`), de a 50
  filas por pedido (con más, el servidor corta), hasta 3 meses por corrida, siempre los dos últimos
  de nuevo porque se pueden corregir. Va a la clase 06.4.1 en todas las regiones, en la variante
  "completo" de la carrera. No entra al índice publicado hasta compararla con el INDEC (la serie
  06.4.1 de GBA existe desde 2017, así que hay 14 meses para probar).
- **Farmacias.** Farmacity, Farmaonline, Farmaplus y Farmacias del Pueblo ya se bajan todos los días
  con las tiendas en línea (unos 58.000 productos el 12/11/2026) y se clasifican en 06.1.1, 06.1.2 y
  06.1.3. Entran en las variantes "web" y "completo"; necesitan historia para probarse.
- **D-36. Rubros sin precios propios: qué los anticipa (sugerencias, nada adoptado).**
  `scripts/correlaciones_rubros.py` compara cada rubro del INDEC con indicadores disponibles antes
  que el IPC, con una regresión que se reestima cada mes con los 24 meses anteriores (fuera de
  muestra) contra el promedio de esos 24 meses (resultados en `resultados/correlaciones_rubros.json`).
  GBA, 2017-2026 (mejora del error fuera de muestra; correlación en julio 2024 a septiembre 2026):

  | Rubro | Mejor indicador | Mejora | Correlación reciente |
  |---|---|---|---|
  | Restaurantes (11.1) | Bienes del mismo mes | 61% | 0,62 |
  | Productos medicinales (06.1) | Bienes del mismo mes | 52% | 0,28 |
  | Servicios (total) | Nivel general del mes anterior; CER | 49%; 46% | 0,64; 0,79 |
  | Bienes y servicios varios (12) | Bienes del mismo mes | 46% | 0,50 |
  | Alquiler (04.1.1) | El mismo rubro del mes anterior; ICL | 40%; 26% | 0,87; 0,92 |
  | Telefonía e internet (08.3) | Nivel general del mes anterior | 35% | 0,33 |
  | Servicios recreativos (09.4) | Nivel general del mes anterior | 26% | 0,20 |
  | Ropa y calzado (03) | Bienes del mismo mes | 25% | 0,65 |
  | Prepagas (06.4.1) | Nivel general del mes anterior | 21% | 0,75 |
  | Transporte público (07.3) y tarifas (04.5) | Ninguno sirve | menos de 15% | cerca de 0 |
  | Educación (10) | El mismo mes del año anterior (marzo) | 7% | 0,83 |

  Lectura: los rubros que siguen al resto de los precios (restaurantes, medicamentos, servicios
  varios, ropa) se anticipan con los bienes que ya medimos; los indexados (alquiler, prepagas,
  servicios en general) con la inflación pasada o el ICL y el CER; los regulados (tarifas,
  transporte) no se anticipan con nada: dependen de decisiones que hay que leer en las normas
  (D-34). Advertencia: "bienes" acá es el del INDEC; con nuestro índice de bienes la relación va a
  ser más débil, porque el nuestro es más ruidoso.

- **D-38 (12 de noviembre de 2026). Gas leído solo, historia hacia atrás y base de regulados.**
  (a) En las resoluciones del ENARGAS sobre Metrogas y Naturgy BAN se lee la página "residenciales
  sin subsidios" del anexo: los 9 cargos fijos (R1, R2 1° a 3°, R3 1° a 4°, R4) de cada subzona
  (Metrogas: Ciudad y conurbano; Naturgy BAN: conurbano norte) y el primer cargo por m3 (R1-R2 1°,
  que en los cuadros de septiembre de 2026 es igual al de R2 2° y R2 3°; los demás se agrupan
  distinto según la empresa y no se toman). La fecha de vigencia sale del articulado ("entrarán en
  vigencia a partir del 1° de septiembre de 2026"). Las otras distribuidoras se registran pero no
  se leen (no son del AMBA). (b) Además de los ids nuevos, cada corrida revisa 900 ids hacia atrás
  hasta `historia_hasta_id` (402000, diciembre de 2023), para reconstruir la historia de las
  tarifas; lleva unas 30 corridas. (c) Como ahora se leen valores viejos, el control de
  plausibilidad compara contra el valor del mismo concepto más cercano en el tiempo (anterior o
  posterior) y permite entre −30% y +60% por cada mes de distancia. (d) El texto de cada anexo
  leído se archiva (`data/otras/normas/textos/`) para poder releerlo si cambia el método. (e) Todo
  valor se guarda con jurisdicción, servicio, empresa, categoría, subzona y componente, y
  `resultados/regulados_base.csv` junta carga manual y lectura automática (con su control), como
  base para medir más adelante la variación real de las tarifas por jurisdicción y la canasta
  completa. No se calcula ningún indicador nuevo con ella todavía. (f) El índice de regulados que
  va a la validación y a la variante "completo" usa solo los conceptos que ya estaban en la carga
  manual: lo leído solo le agrega meses, no conceptos (sumar conceptos sería un cambio de método
  sin probar). (g) La canasta de servicios valoriza el gas con Metrogas Ciudad R2 2° (60,5 m3 por
  mes son 726 al año, categoría R2 2°, de 651 a 800 m3) más 60,5 m3 al cargo por m3; antes faltaba
  el cargo fijo de R2.
- **D-39. Alquileres de GBA Norte, Oeste y Sur (Zonaprop).** Se bajan los informes mensuales de
  las tres zonas además del de la Ciudad y se lee el alquiler medio de un 2 ambientes y su variación
  (la frase de GBA es "El precio medio de alquiler de un departamento de 2 ambientes en GBA Norte se
  ubica en ..."). Se muestran en la pestaña Precios frescos y alquileres, pero el índice sigue
  usando solo la Ciudad hasta probar contra el INDEC si promediar las cuatro zonas (propuesta:
  media geométrica con 25% cada una) reduce el error de 04.1.1 en GBA.
- **D-40. Boletos y subte de la Ciudad y la Provincia, desde sus boletines.** Ciudad: la API pública
  del Boletín Oficial porteño devuelve cada edición entera (normas, sumarios y anexos) en un JSON;
  se recorre por número de edición (hacia adelante y 150 hacia atrás por corrida hasta diciembre de
  2023), con un segundo de pausa. Se registran las normas de tarifas (transporte, subte, taxis,
  estacionamiento, peajes, unidad tarifaria) y se leen: el cuadro mensual de colectivos de la
  Secretaría de Transporte (es una imagen: se lee con OCR, tesseract; secciones 0-3, 3-6, 6-12 y
  12-27 km de la tarifa plena; el mes sale de la referencia "Aprueba cuadros tarifarios - octubre
  2026") y el cuadro del subte de SBASE (texto; 1 a 20 viajes con SUBE registrada; el mes sale de la
  referencia "CUADROS TARIFARIOS NOVIEMBRE 2026" o, si no está, del 5° día hábil después de
  publicado). Provincia: en normas.gba.gob.ar se buscan "valores tarifarios" y "cuadro tarifario"
  desde 2024; se registran todas (colectivos, electricidad provincial, agua de ABSA, peajes de
  AUBASA, fluvial) y se lee la tabla de servicios urbanos de GBA (0-3 km, tarifa nueva y sin
  nominar) de las resoluciones del Ministerio de Transporte sobre autotransporte. Limitación: la
  Provincia actualiza mes a mes por fórmula sin una resolución nueva cada vez (entre 2025 y 2026 hay
  4 resoluciones: 81/25, 342/25, 66/26 y 122/26), así que los meses intermedios siguen en la carga
  manual. Los robots.txt de los dos sitios no restringen nada. Verificación: octubre de 2026 de
  colectivos de la Ciudad leído por OCR (0-3 km $920,48) igual a la carga manual; subte de
  noviembre $1.887,00 contra $1.888 de la nota periodística cargada a mano. Como la carga manual de
  noviembre (colectivo $956,38 y subte $1.888) venía de una nota periodística y no coincidía con los
  anexos oficiales ($956,69 y $1.887), se corrigió con el dato oficial. Si el anexo de colectivos no
  nombra el mes, se toma del articulado de la resolución ("a partir del 1° de julio de 2026"); si
  tampoco está, el valor queda registrado como "sin mes de vigencia" y no se usa. Las normas
  registradas que no se pudieron leer se reintentan en las corridas siguientes (también las de luz y
  gas de Infoleg detectadas antes de que existiera el lector de gas). Primera corrida (12/11/2026):
  colectivos de la Ciudad de junio, septiembre y octubre y subte de julio a noviembre de 2026; gas
  de octubre de Metrogas y Naturgy BAN; colectivos de la Provincia de mayo de 2026.
- **D-41. Precios de autos (Cámara del Comercio Automotor).** La guía oficial mensual de la CCA
  (PDF público, robots.txt sin restricciones) se archiva una vez por mes y se pasa a una tabla larga
  (marca, modelo, versión, año, valor, moneda) usando la posición de cada número en la página: cada
  valor va a la columna del año cuyo borde derecho tiene más cerca. Noviembre de 2026: 18.038
  valores, 70 marcas, 783 precios de 0 km (en dólares) y el resto usados (miles de pesos). Queda
  guardada para 07.1.1 (compra de vehículos); no entra a ninguna variante hasta tener al menos dos
  meses y compararla con el INDEC.

- **D-42. Carne vacuna con datos del INDEC (pestaña de frescos).** Dos gráficos que no salen del
  índice propio: (a) variación mensual del precio promedio de cinco cortes vacunos en GBA (asado,
  carne picada común, cuadril, nalga y paleta), del archivo de precios promedio del INDEC
  (`sh_ipc_precios_promedio.xls`, que ahora se baja con las demás series); (b) incidencia de
  "Carnes y derivados" (01.1.2) en la variación mensual del nivel general, para GBA y el total
  nacional: peso de la clase en la canasta base diciembre de 2016 por la diferencia de su índice
  entre t y t−1, sobre el índice del nivel general en t−1; el nacional suma las seis regiones con
  su peso. Control: con las 12 divisiones, la suma de las incidencias da exactamente la variación
  del nivel general (octubre de 2026 en GBA: 1,98% contra 1,98%). Muestran desde enero del año del
  último dato y llevan el logo ceer_ (no ipceer), porque son datos del INDEC.

## 6. Qué falta para el índice completo

Peso en la canasta nacional del INDEC (divisiones de `config/ponderadores_division.csv` ponderadas por
región) y estado:

| Rubro | Peso | Estado | Qué falta |
|---|---|---|---|
| Alimentos y bebidas no alcohólicas (01) | 27,0% | Medido con SEPA; dentro de él, carnes 8,9%, verduras 2,7% y frutas 1,3% están débiles | Comercios tradicionales (panaderías, carnicerías, verdulerías, almacenes) y frescos por kilo |
| Bebidas alcohólicas y tabaco (02) | 3,5% | Bebidas medidas; tabaco no | Precios de cigarrillos (listas de las tabacaleras) |
| Ropa y calzado (03) | 9,9% | Variante web, sin historia | Juntar historia y probar contra el INDEC |
| Vivienda, agua, electricidad, gas (04) | 9,4% | Electricidad y gas (Metrogas, Naturgy BAN) leídos solos de Infoleg (D-34, D-38); agua detectada, a mano; Zonaprop Ciudad en el índice y GBA guardado (D-39) | Factura base del agua (AySA), probar alquileres de GBA, expensas (sin fuente pública) |
| Equipamiento del hogar (05) | 6,4% | Limpieza medida; electrodomésticos en variante web | Historia web, servicio doméstico (salario por norma) |
| Salud (06) | 8,0% | Prepagas desde la Superintendencia (D-35) y farmacias en línea, en variantes | Historia para probarlas; servicios médicos y odontológicos |
| Transporte (07) | 11,0% | Combustibles medidos; colectivos y subte de la Ciudad leídos de su boletín; colectivos de la Provincia de sus resoluciones (D-40); guía de autos de la CCA guardada (D-41) | Meses de la Provincia sin resolución, colectivos y trenes nacionales (anexos de Infoleg), probar autos contra el INDEC |
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

## 8. Dónde corre cada parte (D-32, D-33)
  *Retirado en noviembre de 2026:* los dos gráficos pasaron a la página de coyuntura del CEER
  (publicaciones-ceer, pestaña Inflación), donde la carne se calcula también para el total del país.

- **D-33. Plan A (11 de noviembre de 2026): bajar, procesar y almacenar en internet; la PC solo calcula.**
  - `respaldo_sepa.yml` baja los zips de SEPA a las 04:47, 08:17 y 20:17 y los guarda en el release
    `sepa-archivo`.
  - `procesar_sepa.yml` corre después de cada respaldo (y a las 06:37 y 12:37): baja cada zip que no
    tiene su `proc_AAAA-MM-DD.tar`, lo procesa con el mismo código que la PC
    (`lectura.procesar_zip_diario`: panel diario, mapa por departamento y municipios) y sube el
    `.tar`. Procesa hasta 4 días por corrida, empezando por los más nuevos, desde 3 días antes del
    último día que tiene la PC (no reprocesa la historia). Los límites de departamentos los baja
    una vez de Georef, la misma fuente que usa la PC, y los guarda en el release.
  - La PC, en el paso "SEPA: procesamiento", primero trae los `.tar` y los desarma en `data/` (solo
    acepta las rutas esperadas). Procesa por su cuenta un zip solo si GitHub no lo procesó después
    de 2 días (`descarga.procesar_local_tras_dias`); así, si GitHub falla, el dato llega igual con un
    día de demora. Sigue bajando y guardando los zips (el archivo propio de SEPA), que no consume
    procesador.
  - `otras_fuentes.yml` (todos los días 09:41) baja Cañuelas, Mercado Central, Zonaprop (Ciudad y
    GBA), PAMI, BCRA, prepagas, normas de Infoleg, boletines de la Ciudad y la Provincia (con OCR) y
    la guía de autos de la CCA, y deja `otras.tar.gz`. La PC lo trae (`ipceer otras --desde-github`) en lugar
    de bajarlo; si GitHub no responde, lo baja ella.
  - `precios_web.yml` baja las tiendas en línea en cuatro partes; la PC trae el resultado.
  - En la PC quedan: la base de unos 15 GB (paneles), la consolidación semanal y mensual, la
    carrera, el cálculo, el tablero y el respaldo.
- Opciones siguientes (para decidir): B, todo el cálculo en GitHub con la base en Cloudflare R2;
  C, un servidor gratuito de Oracle Cloud (hoy 2 procesadores y 12 GB, con riesgo de que lo
  recuperen si está inactivo); D, otras máquinas como recolectoras.

## 9. Limitaciones

- Solo grandes cadenas; sin comercios tradicionales (sección 4.2, hallazgo 4).
- Pesos de variedades propios (D-12); pesos de clases de la ENGHo 2004/05 (igual que el INDEC).
- Sin ajuste por reduflación: un cambio de presentación es un producto nuevo.
- Estacionalidad dentro del mes: una semana sola mensualizada engaña; el indicador principal es el
  promedio de 4 semanas.
- Clasificación por reglas, con errores conocidos en frescos (D-07).
- La prueba contra el INDEC es de 2017-2018; la de 2026 empieza con noviembre.
