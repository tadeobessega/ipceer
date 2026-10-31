# Tarea diaria de IPCEER para Windows.
# Archiva los zips de SEPA publicados que falten, procesa, calcula y arma el sitio.
#
# La primera vez también prepara todo: crea el entorno de Python (.venv), instala
# dependencias y extrae los ponderadores por clase del INDEC si faltan.
#
# Se registra importando scripts\tarea_ipceer.xml en el Programador de tareas, o con:
#   schtasks /Create /TN "IPCEER diario" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 11:00 `
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\ruta\ipceer\scripts\tarea_diaria.ps1"

param(
  # Carpeta donde se guardan los zips de SEPA (el archivo histórico).
  [string]$Archivo = "$env:USERPROFILE\Documents\SEPA",
  # Si se pasa -Publicar, sube resultados y sitio al repo (git commit + push).
  [switch]$Publicar
)

$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
New-Item -ItemType Directory -Force -Path "$Repo\logs", $Archivo | Out-Null
$Log = "$Repo\logs\diario_$(Get-Date -Format yyyy-MM-dd).log"
function Log($m) { "$(Get-Date -Format s) $m" | Out-File -Append -Encoding utf8 $Log }
$env:IPCEER_ARCHIVO = $Archivo
$env:PYTHONIOENCODING = "utf-8"
$Python = "$Repo\.venv\Scripts\python.exe"

# --- preparación (solo la primera vez) ---
if (-not (Test-Path $Python)) {
  Log "Creando entorno de Python"
  if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv "$Repo\.venv" *>> $Log }
  else { python -m venv "$Repo\.venv" *>> $Log }
}
& $Python -c "import ipceer, pdfplumber" 2>$null
if ($LASTEXITCODE -ne 0) {
  Log "Instalando dependencias"
  & $Python -m pip install --upgrade pip *>> $Log
  & $Python -m pip install -e ".[ponderadores]" *>> $Log
}
if (-not (Test-Path "$Repo\config\ponderadores_clase.csv")) {
  Log "Extrayendo ponderadores del INDEC"
  & $Python scripts\extraer_ponderadores.py *>> $Log
}

# --- corrida diaria ---
Log "Inicio"
& $Python -m ipceer.cli diario *>> $Log
Log "Fin (código $LASTEXITCODE)"

if ($Publicar) {
  git add resultados sitio *>> $Log
  git diff --cached --quiet
  if ($LASTEXITCODE -ne 0) {
    git commit -m "Datos $(Get-Date -Format yyyy-MM-dd)" *>> $Log
    git push *>> $Log
  }
}
