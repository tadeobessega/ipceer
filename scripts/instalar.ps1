# Instalación de IPCEER en Windows, de una sola vez.
#
# Abrir PowerShell (no hace falta como administrador) y pegar:
#   irm https://raw.githubusercontent.com/Subsepresupuesto/ipceer/main/scripts/instalar.ps1 | iex
#
# Hace: instala Python y Git si faltan (winget), clona el repo en Documentos\ipceer,
# crea el entorno de Python, extrae los ponderadores del INDEC, crea la carpeta de
# archivo y registra la tarea diaria (lunes a viernes, 11:00). Al final corre la tarea
# una vez para bajar lo que haya publicado hoy.

param(
  [string]$Destino = "$env:USERPROFILE\Documents\ipceer",
  [string]$Archivo = "$env:USERPROFILE\SEPA",
  [string]$Hora = "11:00"
)
$ErrorActionPreference = "Stop"

function Tiene($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

if (-not (Tiene python)) {
  Write-Host "Instalando Python..."
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
}
if (-not (Tiene git)) {
  Write-Host "Instalando Git..."
  winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
  $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
}

if (-not (Test-Path "$Destino\.git")) {
  git clone https://github.com/Subsepresupuesto/ipceer.git $Destino
} else {
  git -C $Destino pull
}
Set-Location $Destino

if (-not (Test-Path ".venv")) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
& .\.venv\Scripts\python.exe -m pip install -e ".[ponderadores]"

New-Item -ItemType Directory -Force -Path $Archivo | Out-Null

if (-not (Test-Path "config\ponderadores_clase.csv")) {
  Write-Host "Extrayendo ponderadores del INDEC (Metodología 32, Anexo II)..."
  & .\.venv\Scripts\python.exe scripts\extraer_ponderadores.py
}

$accion = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$Destino\scripts\tarea_diaria.ps1`" -Archivo `"$Archivo`""
schtasks /Create /F /TN "IPCEER diario" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST $Hora /TR $accion | Out-Null
# que corra al encender la PC si se perdió la hora programada
$t = Get-ScheduledTask -TaskName "IPCEER diario"
$t.Settings.StartWhenAvailable = $true
Set-ScheduledTask -InputObject $t | Out-Null

Write-Host "Tarea 'IPCEER diario' registrada ($Hora, lunes a viernes). Archivo: $Archivo"
Write-Host "Primera corrida (baja hasta ~2 GB, puede tardar)..."
& powershell -NoProfile -ExecutionPolicy Bypass -File "$Destino\scripts\tarea_diaria.ps1" -Archivo $Archivo
Write-Host "Listo. Log en $Destino\logs. Revisar config\ponderadores_clase.csv."
