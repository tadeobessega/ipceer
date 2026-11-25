# Arma un nodo nuevo del IPCEER en esta PC, idéntico a la HP15 (D-71). Se corre una sola vez.
#
# Uso: clic derecho sobre este archivo > "Ejecutar con PowerShell", o en PowerShell:
#   powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Documents\nodo_nuevo.ps1"
#
# Hace, en orden (todo queda en Documents\ipceer_instalacion.log):
#   1. instala Python 3.12, Git y GitHub CLI si faltan (winget, en el usuario);
#   2. clona el repo en Documents\ipceer (o lo actualiza);
#   3. entorno de Python y dependencias; ponderadores del INDEC;
#   4. conecta GitHub CLI con la cuenta tadeobessega (se abre el navegador: lo aprueba Tadeo);
#   5. trae el historial que no se reprocesa: paneles cerrados y diarios recientes de la rama
#      datos-base, y otras fuentes, surtidor, telefonía, precios web y vintages del release;
#   6. marca las reglas de clasificación como ya aplicadas, para que la primera corrida NO
#      reconsolide todo desde los pocos días diarios que hay en esta PC;
#   7. fija el nombre del nodo (IPCEER_NODO, por defecto "thinkpad"): sube con sufijo y no pisa a la HP15;
#   8. programa las tareas (scripts\programar_tareas.ps1: pide la contraseña de Windows) y arranca la
#      primera corrida, que baja de SEPA los días publicados que falten y los sube a GitHub.
param(
  [string]$Nodo = "thinkpad",
  [string]$Destino = "$env:USERPROFILE\Documents\ipceer",
  [string]$Archivo = "$env:USERPROFILE\Documents\SEPA"
)
$ErrorActionPreference = "Continue"
$R = "tadeobessega/ipceer"; $TAG = "sepa-archivo"
$LogI = "$env:USERPROFILE\Documents\ipceer_instalacion.log"
function Log($m) { $l = "$(Get-Date -Format s) $m"; $l | Out-File -Append -Encoding utf8 $LogI; Write-Host $l }
function Tiene($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function Refrescar { $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User") }
function Bajar($patron, $salida) {
  for ($i = 1; $i -le 5; $i++) { gh release download $TAG -R $R -p $patron -O $salida --clobber 2>&1 | Out-Null; if ($LASTEXITCODE -eq 0) { return $true }; Start-Sleep ($i * 15) }
  Log "   no se pudo bajar $patron"; return $false
}

Log "== Nodo nuevo del IPCEER: $Nodo ($env:COMPUTERNAME, $env:USERNAME)"

Log "1/8 Programas"
foreach ($p in @(@("python","Python.Python.3.12"), @("git","Git.Git"), @("gh","GitHub.cli"))) {
  if (-not (Tiene $p[0])) {
    Log "   instalando $($p[1])"
    winget install -e --id $p[1] --scope user --accept-package-agreements --accept-source-agreements 2>&1 | Out-File -Append -Encoding utf8 $LogI
    if ($LASTEXITCODE -ne 0) { winget install -e --id $p[1] --accept-package-agreements --accept-source-agreements 2>&1 | Out-File -Append -Encoding utf8 $LogI }
    Refrescar
  }
  if (-not (Tiene $p[0])) { Log "   ERROR: no quedó instalado $($p[0]). Si la PC no deja instalar (equipo administrado), hay que pedirlo a sistemas."; }
}
# el alias de la Microsoft Store no es Python de verdad
$Py = (Get-Command py -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" }

Log "2/8 Repo en $Destino"
if (-not (Test-Path "$Destino\.git")) { git clone "https://github.com/$R.git" $Destino 2>&1 | Out-File -Append -Encoding utf8 $LogI }
else { git -C $Destino pull --ff-only origin main 2>&1 | Out-File -Append -Encoding utf8 $LogI }
if (-not (Test-Path "$Destino\.git")) { Log "ERROR: no se pudo clonar el repo. Fin."; Read-Host "Enter para cerrar"; exit 1 }
Set-Location $Destino

Log "3/8 Entorno de Python"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  if ($Py -like "*py.exe") { & $Py -3.12 -m venv .venv } else { & $Py -m venv .venv }
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip 2>&1 | Out-File -Append -Encoding utf8 $LogI
& .\.venv\Scripts\python.exe -m pip install -e ".[ponderadores,dev]" 2>&1 | Out-File -Append -Encoding utf8 $LogI
if (-not (Test-Path "config\ponderadores_clase.csv")) { & .\.venv\Scripts\python.exe scripts\extraer_ponderadores.py 2>&1 | Out-File -Append -Encoding utf8 $LogI }
New-Item -ItemType Directory -Force -Path $Archivo, "data", "resultados", "logs" | Out-Null

Log "4/8 GitHub CLI (cuenta tadeobessega)"
cmd /c "gh auth status >nul 2>&1"
if ($LASTEXITCODE -ne 0) {
  Write-Host ""; Write-Host "Se abre el navegador: entrá con la cuenta tadeobessega y pegá el código que aparece acá." -ForegroundColor Yellow
  gh auth login --web -h github.com -p https
}
gh auth setup-git 2>&1 | Out-Null
cmd /c "gh auth status >nul 2>&1"
if ($LASTEXITCODE -ne 0) { Log "   ERROR: GitHub CLI no quedó conectado; el nodo no va a poder subir. Correr 'gh auth login' y volver a correr este script." }

Log "5/8 Historial desde GitHub (no se reprocesa lo que ya está)"
$base = "$env:USERPROFILE\Documents\ipceer-base"
if (-not (Test-Path "$base\.git")) { git clone --depth 1 -b datos-base "https://github.com/$R.git" $base 2>&1 | Out-File -Append -Encoding utf8 $LogI }
else { git -C $base pull --ff-only 2>&1 | Out-File -Append -Encoding utf8 $LogI }
foreach ($d in "panel","geo","municipios","combustibles") {
  if (Test-Path "$base\$d") { robocopy "$base\$d" "data\$d" /E /XC /XN /XO /NFL /NDL /NJH /NJS | Out-Null }
}
if (Test-Path "$base\otras") { robocopy "$base\otras" "data\otras" /E /XC /XN /XO /NFL /NDL /NJH /NJS | Out-Null }
if (Test-Path "$base\datos_pc.json") { Copy-Item "$base\datos_pc.json" "resultados\datos_base.json" -Force }
$tmp = "$env:TEMP\ipceer_nodo"; New-Item -ItemType Directory -Force -Path $tmp | Out-Null
if ((Bajar "otras.tar.gz" "$tmp\otras.tar.gz")) { tar -xzf "$tmp\otras.tar.gz" -C data }
New-Item -ItemType Directory -Force -Path "data\combustibles", "data\otras", "data\web" | Out-Null
if ((Bajar "surtidor.tar.gz" "$tmp\surtidor.tar.gz")) { tar -xzf "$tmp\surtidor.tar.gz" -C data\combustibles }
if ((Bajar "telefonia.tar.gz" "$tmp\telefonia.tar.gz")) { tar -xzf "$tmp\telefonia.tar.gz" -C data\otras }
if (-not (Test-Path "resultados\vintages.csv")) { Bajar "vintages.csv" "resultados\vintages.csv" | Out-Null }
$activos = @(gh release view $TAG -R $R --json assets -q ".assets[].name" 2>$null)
foreach ($n in ($activos | Where-Object { $_ -match '^web_20[0-9-]+\.parquet$' })) {
  $dest = "data\web\gh_$($n.Substring(4))"
  if (-not (Test-Path $dest)) { Bajar $n $dest | Out-Null }
}
Log ("   paneles diarios: " + (Get-ChildItem data\panel\diario\*.parquet -ErrorAction SilentlyContinue).Count +
     "; semanales: " + (Get-ChildItem data\panel\semanal -ErrorAction SilentlyContinue).Count +
     "; mensuales: " + (Get-ChildItem data\panel\mensual -ErrorAction SilentlyContinue).Count)

Log "6/8 Reglas de clasificación ya aplicadas (evita reconsolidar todo con historia parcial)"
$reglas = (Get-FileHash "config\clasificador.yaml").Hash + (Get-FileHash "config\clasificacion_manual.csv" -ErrorAction SilentlyContinue).Hash
if (-not (Test-Path "resultados\reglas_clasificacion.hash")) { $reglas | Out-File -Encoding ascii "resultados\reglas_clasificacion.hash" }

Log "7/8 Nombre del nodo: $Nodo"
[Environment]::SetEnvironmentVariable("IPCEER_NODO", $Nodo, "User"); $env:IPCEER_NODO = $Nodo
[Environment]::SetEnvironmentVariable("IPCEER_ARCHIVO", $Archivo, "User")
[Environment]::SetEnvironmentVariable("POLARS_MAX_THREADS", "2", "User")

Log "8/8 Tareas programadas y primera corrida"
& powershell -NoProfile -ExecutionPolicy Bypass -File "$Destino\scripts\programar_tareas.ps1" -Archivo $Archivo
Get-ScheduledTask -TaskName "IPCEER*" -ErrorAction SilentlyContinue | ForEach-Object { Log "   tarea $($_.TaskName): $($_.State)" }
Log "== Listo. La primera corrida sigue en segundo plano (log en $Destino\logs\actualizar_*.log)."
Read-Host "Enter para cerrar"
