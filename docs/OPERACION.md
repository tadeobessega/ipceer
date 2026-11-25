# IPCEER: cómo opera y plan de redundancia

Documento de traspaso (25/11/2026). Lo lee cualquier chat o persona que tome la operación. La
metodología está en `METODOLOGIA.md` (decisiones D-1 a D-70); este archivo es la operación.

## 1. Reglas de Tadeo (no negociables)

- El índice no puede morir. Nada crítico puede depender de que alguien abra un chat: todo corre solo,
  con tareas programadas en las PC y workflows de GitHub, y se cubren entre sí.
- No se pierde información: todo dato bajado o procesado queda en al menos dos lugares (PC + GitHub).
  Si algo falla, alerta (issue en GitHub, que le llega por correo, y aviso de Windows).
- Nunca estimar información oficial ni arrastrar datos viejos como si fueran nuevos; los escenarios se
  rotulan como tales.
- Todo cambio de método tiene que mejorar la dispersión contra el INDEC; si no, se pregunta antes.
- Respetar robots.txt y límites de pedidos (pausas). X y LinkedIn no se leen en forma automática.
- Textos sin raya (—). Español rioplatense. No tocar el logo del ipceer en los gráficos.
- No pasarle archivos para guardar: guardar todo donde corresponde (repo, PC, release).
- Cada cambio del tablero se publica también en el artifact de Claude (slug
  597bae9a-788b-43d7-8417-4fbdf866a44c, público). Eso solo lo puede hacer Claude; la página que no
  depende de Claude es la del Worker de Cloudflare (`worker/`, sirve `tablero.html` del release).

## 2. Piezas

| Pieza | Qué hace hoy |
|---|---|
| **HP15** (`HPTADEO`, `C:\Users\tadeo\Documents\ipceer`, archivo SEPA en `Documents\SEPA`) | Tarea de Windows "IPCEER diario" (`scripts/tarea_diaria.ps1` → `ipceer diario`): baja SEPA, INDEC y Energía, procesa, calcula, arma el tablero; `scripts/control.ps1 -Subir` sube `estado_pc.json`, `tablero.html`, `datos_pc.json` al release y los paneles diarios recientes a la rama `datos-base` (`scripts/subir_paneles.py`). Tareas: `scripts/programar_tareas.ps1`. |
| **GitHub** (`tadeobessega/ipceer`) | Workflows: `respaldo_sepa`, `procesar_sepa` (Plan A), `tablero` (calcula en GitHub y publica `tablero_github.html` y `datos_github.json`), `otras_fuentes` (frescos, alquileres, BCRA, prepagas, normas, boletines, EcoGo), `precios_web` (tiendas en línea), `telefonia` (telefonía, TV, streaming, cine, teatro, diarios, surtidor de combustibles), `vigilancia` (cada 3 h abre un issue si algo falló o no hay éxito en 30 h), `dato_consultora` (carga manual de consultoras desde el tablero), `tests`. |
| **Release `sepa-archivo`** | Almacén: `sepa_*.zip`, `proc_*.tar`, `otras.tar.gz` (+ `otras_prev.tar.gz`), `telefonia.tar.gz`, `surtidor.tar.gz`, `vintages.csv`, estados y tableros. |
| **Rama `datos-base`** | Paneles cerrados (semanal/mensual), `panel/diario/*.parquet` recientes, `datos_pc.json`, series que GitHub no puede bajar solo. |
| **Cloudflare Worker** | Sirve el tablero público desde el release. |

## 3. Problemas conocidos (al 25/11/2026)

1. **El portal de SEPA (datos.produccion.gob.ar) devuelve 403 a GitHub y a la nube.** Solo una PC
   en Argentina puede bajarlo. Si la PC está apagada, no entra ningún día nuevo. Es el punto único de
   falla que hay que eliminar con la segunda PC.
2. **La HP15 se queda sin memoria en `Cálculo`** (Polars/Rust "memory allocation failed"), aun con
   `POLARS_MAX_THREADS=2`. El cálculo publicado lo hace GitHub, así que no frena el tablero.
3. **La HP15 no corre desde el 23/11 09:37** (apagada o suspendida). Último día SEPA: 22/11.
4. La corrida `tablero` de GitHub a veces se cae sin log (runner perdido); el reintento anda.
5. `precios_web` en la HP15 falló el 23/11 (en GitHub anda).

## 4. Diseño objetivo: tres nodos que se cubren solos

Principio: **activo-activo e idempotente**. Las dos PC hacen el mismo trabajo y suben a GitHub; lo que
ya está arriba no se vuelve a subir; GitHub arma el tablero con lo que haya. Si un nodo se cae, los
otros ya están haciendo el trabajo, no hace falta "pasarle la posta".

### Nodos
- **PC nueva** (nombre a definir) y **HP15**: idénticas. Cada una, con su tarea de Windows:
  1. Baja SEPA del día (y los que falten) a su archivo local.
  2. Procesa y genera `panel/diario/<fecha>.parquet`.
  3. Sube lo que falte a GitHub: zip de SEPA al release si no está (`sepa_<fecha>.zip`), panel diario
     a `datos-base` (`subir_paneles.py`, ya idempotente: si existe, no sube).
  4. Corre los scrapers (tiendas en línea, telefonía, otras fuentes) y sube su salida con sufijo de
     nodo (p. ej. `precios_web_<nodo>.tar.gz`), sin pisar la de otros.
  5. Sube su latido: `estado_<nodo>.json` (último día, hora, errores).
  6. Dispara `tablero.yml` (`gh workflow run`) cuando subió algo nuevo.
- **GitHub**: sigue corriendo sus scrapers (los que no están bloqueados), arma y publica el tablero,
  fusiona las salidas de los nodos (unión, sin duplicados por fecha/producto/fuente) y vigila.

### Coordinación y autocuración (sin prompts)
- `vigilancia.yml` lee los latidos de los dos nodos: si uno pasa 30 h sin éxito, issue + correo; si
  **los dos** están caídos, issue urgente. Si falta un día de SEPA en GitHub y algún nodo lo tiene,
  ese nodo lo sube en su próxima corrida (cada nodo compara lo suyo con lo que hay arriba).
- Cada nodo, en cada corrida, revisa los latidos del otro: si el otro está atrasado, hace también
  los pasos que normalmente hace solo el otro (si en algún momento se reparten tareas).
- Tareas de Windows con "despertar el equipo", "correr apenas se pueda si se perdió", reintentos y
  verificación de las 18 h (ya existe en `programar_tareas.ps1`).
- Código: cada corrida hace `git pull` de `main` antes de trabajar, así un arreglo subido llega solo.
- Datos: nada vive solo en una PC. Antes de dar por terminado un día, el nodo verifica que el zip y el
  panel estén en GitHub.

### Paso a paso para el chat de la PC nueva
1. **Duplicar**: clonar el repo, instalar (`scripts/instalar.ps1`), crear el archivo SEPA, conectar
   `gh` (cuenta tadeobessega), programar tareas (`scripts/programar_tareas.ps1`). Bajar el historial
   necesario desde el release y `datos-base` (no reprocesar lo que ya está).
2. **Recuperar** los días que faltan (23/11 en adelante) desde esta PC y subirlos.
3. **Comparar** varios días seguidos: paneles diarios y resultados de la PC nueva contra la HP15 y
   GitHub (mismo número de precios, mismas variaciones). Documentarlo.
4. **Unificar**: latidos por nodo, salidas con sufijo, fusión en GitHub, vigilancia de dos nodos
   (diseño de arriba). Recién ahí mover scrapers de GitHub a las PC, de a uno, comparando antes de
   apagar el de GitHub.
5. Resolver la memoria del cálculo en PC (procesar por partes, `engine="streaming"`, menos hilos).
6. Dejar todo documentado acá y en `METODOLOGIA.md` (nuevas decisiones D-71 en adelante).

## 4 bis. Estado de la implementación (25/11/2026)

- Hecho en el repo (D-71): `control.ps1` sube con nombre de nodo y no pisa; sube los zips de SEPA que
  falten en el release; `vigilancia.yml` vigila los latidos de `hp15` y `thinkpad`;
  `scripts/nodo_nuevo.ps1` arma un nodo de cero.
- ThinkPad (`eco-9774`, usuario `308657`, equipo con dominio): falta correr `nodo_nuevo.ps1` (pide
  aprobar GitHub en el navegador y la contraseña de Windows para las tareas). Desde un chat remoto no
  hay terminal en esa PC: la instalación la tiene que lanzar Tadeo una vez, o un chat abierto en ella.
- ThinkPad, 25/11: la primera corrida se trabó en la Wayback (arreglado en D-72) y las tareas nunca
  arrancaron (267011, política del dominio): `scripts/reparar_tareas.ps1` (D-73).
- Pendiente: comparar varios días ThinkPad contra HP15 y GitHub (paso 3), mover scrapers de a uno
  (paso 4, segunda parte) y la memoria del cálculo (paso 5).

## 5. Acceso para Claude

- Repo: `git clone https://github.com/tadeobessega/ipceer` (GitHub CLI `gh` autenticado con la cuenta de Tadeo).
- Publicar el tablero en Claude: bajar `tablero_github.html` del release y publicarlo en el artifact
  597bae9a-788b-43d7-8417-4fbdf866a44c (compartido en modo público: `shared_live: public`).
- Rama de datos: `datos-base` (worktree aparte).
- Commits con la atribución que pida el entorno; nunca subir secretos.
