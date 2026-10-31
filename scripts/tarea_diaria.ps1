# Tarea diaria de IPCEER para Windows.
# Archiva los zips de SEPA publicados que falten, procesa, calcula y arma el sitio.
#
# Registrar (una sola vez, en PowerShell, ajustando las rutas):
#   schtasks /Create /TN "IPCEER diario" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 11:00 `
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\ruta\ipceer\scripts\tarea_diaria.ps1"
#
# Si la PC está apagada a esa hora, en el Programador de tareas marcar
# "Ejecutar la tarea lo antes posible después de perder un inicio programado".
# El portal mantiene cada archivo una semana, así que perder un día no es grave.

param(
  # Carpeta donde se guardan los zips de SEPA (el archivo histórico).
  [string]$Archivo = "$env:USERPROFILE\SEPA",
  # Si se pasa -Publicar, sube resultados y sitio al repo (git commit + push).
  [switch]$Publicar
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
New-Item -ItemType Directory -Force -Path "$Repo\logs", $Archivo | Out-Null
$Log = "$Repo\logs\diario_$(Get-Date -Format yyyy-MM-dd).log"
$env:IPCEER_ARCHIVO = $Archivo

$Python = if (Test-Path "$Repo\.venv\Scripts\python.exe") { "$Repo\.venv\Scripts\python.exe" } else { "python" }
& $Python -m ipceer.cli diario *>> $Log

if ($Publicar) {
  git add resultados sitio *>> $Log
  git diff --cached --quiet
  if ($LASTEXITCODE -ne 0) {
    git commit -m "Datos $(Get-Date -Format yyyy-MM-dd)" *>> $Log
    git push *>> $Log
  }
}
