# IPCEER · Índice de precios de bienes (CEER)

Índice de precios de alta frecuencia de los bienes que se venden en grandes supermercados de todo el país, construido con la base **SEPA / Precios Claros** (Secretaría de Industria y Comercio) y agregado con la canasta del **IPC del INDEC**.

Produce:
- **Inflación semanal**, mensualizada, y promedio de 4 semanas.
- **Inflación mensual** (meses cerrados) y, para el mes en curso, lo **relevado**, el **arrastre** y una **proyección**.
- Aperturas por **rubro** (clase COICOP) y por **región** del INDEC.
- Un **tablero** en un único HTML (`sitio/index.html`) con estética CEER y una sección de metodología detallada.

No es un IPC completo: no cubre servicios, tarifas, alquileres ni bienes que no se venden en supermercados. Ver la pestaña *Metodología* del tablero o `METODOLOGIA.md`.

## Cómo funciona

```
SEPA (portal, últimos días)  →  archivo local de zips  →  panel diario  →  paneles semanal y mensual  →  índices  →  sitio
     ipceer descargar              (IPCEER_ARCHIVO)        ipceer procesar     ipceer consolidar        ipceer calcular  ipceer sitio
```

`ipceer diario` corre todo en orden. Es lo que ejecuta la tarea programada.

**El portal solo publica los últimos días** (un archivo por día de la semana, que se pisa a la semana siguiente). Cada día que no se descarga se pierde: por eso la tarea tiene que correr todos los días hábiles. Bajar solo en días hábiles no pierde los fines de semana, porque el lunes siguen publicados los archivos del sábado y el domingo.

**No puede correr en GitHub Actions**: el portal responde 403 a los servidores de GitHub. Tiene que correr en una PC o servidor en Argentina.

## Instalación (en la PC que va a correr la tarea)

**Windows, en un paso:** abrir PowerShell y pegar

```powershell
irm https://raw.githubusercontent.com/Subsepresupuesto/ipceer/main/scripts/instalar.ps1 | iex
```

Instala Python y Git si faltan, clona el repo, extrae los ponderadores, registra la tarea diaria y la corre una vez. Lo que sigue es la instalación manual.

Requiere Python 3.10 o más nuevo y git.

```bash
git clone <este repo> ipceer
cd ipceer
python -m venv .venv
# Windows:  .venv\Scripts\activate      macOS/Linux:  source .venv/bin/activate
pip install -e ".[ponderadores,dev]"
```

### 1. Ponderadores por clase (una sola vez)

```bash
python scripts/extraer_ponderadores.py
```

Baja la Metodología INDEC n° 32 y extrae el Anexo II, Cuadro 7 (pesos por apertura y región) a `config/ponderadores_clase.csv`. Muestra un control contra los pesos por división publicados. **Revisar el CSV antes de usarlo.** Sin este archivo, el pipeline archiva y procesa igual, pero no calcula índices.

### 2. Carpeta de archivo

Los zips se guardan en la carpeta de la variable `IPCEER_ARCHIVO` (por defecto, `SEPA` en la carpeta del usuario, si se usa la tarea programada). **Cada zip diario pesa unos 310-325 MB** (medido el 30/10/2026): son unos 115 GB por año. El portal publica el archivo de cada día alrededor de las 16:20; la tarea de las 11:00 baja el del día anterior. Se lleva un `manifiesto.csv` con fecha, tamaño, hash SHA-256 y URL de cada archivo.

### 3. Tarea programada

**Windows** (PowerShell, ajustar la ruta):

```powershell
schtasks /Create /TN "IPCEER diario" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 11:00 `
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\ruta\ipceer\scripts\tarea_diaria.ps1 -Archivo D:\SEPA"
```

En el Programador de tareas, marcar *"Ejecutar la tarea lo antes posible después de perder un inicio programado"* por si la PC está apagada.

**macOS / Linux** (`crontab -e`):

```
0 11 * * 1-5 /ruta/ipceer/scripts/tarea_diaria.sh
```

Con `-Publicar` (Windows) o `--publicar` (macOS/Linux), la tarea además hace commit y push de `resultados/` y `sitio/`.

Los logs quedan en `logs/`.

## Comandos

| Comando | Qué hace |
|---|---|
| `ipceer descargar` | Archiva los zips publicados que faltan |
| `ipceer procesar` | Arma el panel diario de los zips sin procesar |
| `ipceer consolidar [--todos]` | Arma o actualiza los paneles semanales y mensuales (`--todos` rehace todo, por ejemplo tras cambiar el clasificador) |
| `ipceer calcular` | Calcula índices y escribe `resultados/` |
| `ipceer sitio` | Construye `sitio/index.html` |
| `ipceer diario` | Todo lo anterior |
| `ipceer reporte-clasificacion` | Productos sin clasificar más frecuentes, para mejorar las reglas |
| `ipceer validar --indec serie_ipc_divisiones.csv` | Compara alimentos y bebidas con el INDEC |

## Estructura

| Ruta | Qué es |
|---|---|
| `config/config.yaml` | Parámetros (rutas, limpieza, mínimos, precio de lista o efectivo) |
| `config/clasificador.yaml` | Reglas para clasificar productos en clases COICOP |
| `config/clasificacion_manual.csv` | Correcciones puntuales por código de producto |
| `config/ponderadores_*.csv` | Pesos del INDEC por región, división y clase (ver Metodología) |
| `config/provincias.csv`, `gba_localidades.csv` | Asignación de sucursales a regiones |
| `src/ipceer/` | Código del pipeline |
| `web/template.html` | Plantilla del tablero (estética CEER) |
| `resultados/` | CSV de índices, cobertura, calidad y `datos.json` del sitio |
| `sitio/index.html` | Tablero generado (un solo archivo, datos embebidos) |
| `tests/` | Tests con datos SEPA sintéticos |

## Tests

```bash
pytest -q
```

Los tests generan zips con la estructura de SEPA y precios sintéticos, y corren todo el pipeline.
