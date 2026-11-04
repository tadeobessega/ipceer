# Actualiza el IPCEER con todo lo nuevo y lo corre completo en esta PC.
#
# Uso (en PowerShell):
#   powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Documents\ipceer\scripts\actualizar_todo.ps1"
#
# Hace, en orden (cada paso sigue aunque falle el anterior; todo queda en logs\actualizar_<fecha>.log):
#   1. trae el código nuevo si hay un paquete ipceer-*.bundle en la carpeta del repo (y git está instalado);
#   2. instala o actualiza las dependencias de Python (fastexcel para la CBA del INDEC, shapely para el mapa);
#   3. baja las series del INDEC (IPC y canasta básica alimentaria y total);
#   4. combustibles: límites de provincias y departamentos (Georef), precios y volúmenes por estación
#      (Secretaría de Energía, ~140 MB), copia diaria de "Precios en surtidor" y procesamiento;
#   5. SEPA: baja los días publicados que falten y los procesa;
#   6. nombres de las cadenas (desde los últimos zips);
#   7. reconsolida semanas y meses (reclasifica todo con las reglas nuevas), salvo que esté corriendo
#      la base histórica: en ese caso solo consolida lo que falta;
#   8. calcula índices, rotación, cadenas, regiones, CBA, combustibles, validación e hilo;
#   9. arma el tablero (sitio\index.html) y deja un resumen en resultados\estado.json.

param(
  [string]$Archivo = "$env:USERPROFILE\Documents\SEPA"
)

$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
New-Item -ItemType Directory -Force -Path "$Repo\logs", $Archivo | Out-Null
$Log = "$Repo\logs\actualizar_$(Get-Date -Format yyyy-MM-dd_HHmm).log"
function Log($m) { $l = "$(Get-Date -Format s) $m"; $l | Out-File -Append -Encoding utf8 $Log; Write-Host $l }
function AlLog { process { "$_" | Out-File -Append -Encoding utf8 $Log } }
$env:IPCEER_ARCHIVO = $Archivo
$env:PYTHONIOENCODING = "utf-8"
$Python = "$Repo\.venv\Scripts\python.exe"
$Pasos = [ordered]@{}

function Paso($nombre, [scriptblock]$bloque) {
  Log ">> $nombre"
  $t0 = Get-Date
  try {
    & $bloque
    $ok = ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq $null)
  } catch {
    "$_" | AlLog
    $ok = $false
  }
  $seg = [int]((Get-Date) - $t0).TotalSeconds
  $Pasos[$nombre] = $(if ($ok) { "ok ($seg s)" } else { "con errores ($seg s), ver log" })
  Log "   $($Pasos[$nombre])"
}
function Ipceer { & $Python -m ipceer.cli @args 2>&1 | AlLog }

Log "Inicio. Repo: $Repo. Archivo SEPA: $Archivo"

# 1. código nuevo: primero desde GitHub (tadeobessega/ipceer); si no, desde un paquete .bundle
$conGit = [bool](Get-Command git -ErrorAction SilentlyContinue)
if ($conGit -and ((git remote get-url origin 2>$null) -match "tadeobessega/ipceer")) {
  Paso "Código nuevo (GitHub)" { git pull --ff-only origin main 2>&1 | AlLog }
}
$bundle = Get-ChildItem -Path $Repo -Filter "ipceer-*.bundle" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 1
if ($bundle -and (Get-Command git -ErrorAction SilentlyContinue)) {
  Paso "Código nuevo ($($bundle.Name))" { git pull --ff-only $bundle.FullName main 2>&1 | AlLog; if ($LASTEXITCODE -eq 0) { Remove-Item $bundle.FullName } }
} elseif ($bundle) {
  Log "Hay un paquete de código ($($bundle.Name)) pero git no está instalado: pedile a Claude que lo aplique."
}
Log "Versión del código: $(Get-Content "$Repo\.git\refs\heads\main" -ErrorAction SilentlyContinue)"

# 2. dependencias
Paso "Entorno de Python y dependencias" {
  if (-not (Test-Path $Python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv "$Repo\.venv" 2>&1 | AlLog } else { python -m venv "$Repo\.venv" 2>&1 | AlLog }
  }
  & $Python -m pip install --upgrade pip 2>&1 | AlLog
  & $Python -m pip install -e ".[ponderadores]" 2>&1 | AlLog
}
if (-not (Test-Path "$Repo\config\ponderadores_clase.csv")) {
  Paso "Ponderadores del INDEC" { & $Python scripts\extraer_ponderadores.py 2>&1 | AlLog }
}

# ¿está corriendo la base histórica 2016-2018?
$historico = $false
try { $historico = ((Get-ScheduledTask -TaskName "IPCEER historico" -ErrorAction Stop).State -eq "Running") } catch {}
if ($historico) { Log "La tarea 'IPCEER historico' está corriendo: no se reconsolida todo (lo hace ella al terminar)." }

# 3 a 9
Paso "Series del INDEC (IPC y CBA)" { Ipceer indec --forzar }
Paso "Combustibles (Energía y Georef)" { Ipceer combustibles }
Paso "SEPA: descarga" { Ipceer descargar }
Paso "SEPA: procesamiento" { Ipceer procesar }
Paso "Nombres de las cadenas" { Ipceer cadenas }
Paso "Municipios (Morón)" { Ipceer municipios }
if ($historico) {
  Paso "Consolidación de semanas y meses" { Ipceer consolidar }
} else {
  Paso "Consolidación de semanas y meses (todo)" { Ipceer consolidar --todos }
}
Paso "Cálculo" { Ipceer calcular }
Paso "Tablero" { Ipceer sitio }
Paso "Resumen" { Ipceer estado }

# 10. base histórica 2016-2018: si no está corriendo, se relanza (sigue donde quedó)
if (-not $historico) {
  try {
    schtasks /Run /TN "IPCEER historico" 2>&1 | AlLog
    Log "Base histórica 2016-2018: relanzada (tarda varias horas; dejá la PC prendida y sin suspender)."
  } catch { Log "No se pudo relanzar la base histórica: $_" }
}

# 11. estado para el control: archivo local, aviso en Windows y copia en GitHub (si gh está conectado)
Paso "Estado de la base" { & powershell -NoProfile -ExecutionPolicy Bypass -File "$Repo\scripts\control.ps1" -Subir -Pasos ($Pasos | ConvertTo-Json -Compress) 2>&1 | AlLog }

Log "Fin. Resumen de pasos:"
$Pasos.GetEnumerator() | ForEach-Object { Log ("   {0,-45} {1}" -f $_.Key, $_.Value) }
if (Test-Path "$Repo\resultados\estado.json") {
  Write-Host ""
  Write-Host "Estado de los datos (resultados\estado.json):"
  Get-Content "$Repo\resultados\estado.json" -Encoding UTF8 | Write-Host
}
Write-Host ""
Write-Host "Listo. Log completo: $Log"
Write-Host "El tablero quedó en $Repo\sitio\index.html. Avisale a Claude para que publique la versión nueva."
