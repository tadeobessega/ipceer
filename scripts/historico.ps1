# Construye la base histórica 2016-2018 (Zenodo) en lotes de 30 días.
# Después de cada lote recalcula y arma el tablero, así la comparación con el INDEC se puede
# ir mirando mientras sigue. Primero baja un día por semana (miércoles) de todo el período y
# después completa el resto, así desde los primeros lotes hay datos para todos los meses.
# Es reanudable: si se corta, se vuelve a correr y sigue donde quedó.
param([int]$Lote = 30)
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
New-Item -ItemType Directory -Force -Path "$Repo\logs" | Out-Null
$Log = "$Repo\logs\historico_$(Get-Date -Format yyyy-MM-dd).log"
function AlLog { process { "$_" | Out-File -Append -Encoding utf8 $Log } }
$env:IPCEER_ARCHIVO = "$env:USERPROFILE\Documents\SEPA"
$env:PYTHONIOENCODING = "utf-8"
$Python = "$Repo\.venv\Scripts\python.exe"
"$(Get-Date -Format s) Inicio" | AlLog
& $Python -m ipceer.cli indec --forzar 2>&1 | AlLog
while ($true) {
  & $Python -m ipceer.cli historico --lote $Lote 2>&1 | AlLog
  & $Python -m ipceer.cli consolidar 2>&1 | AlLog
  & $Python -m ipceer.cli calcular 2>&1 | AlLog
  & $Python -m ipceer.cli sitio 2>&1 | AlLog
  $estado = Get-Content "$Repo\resultados\historico_estado.json" -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
  "$(Get-Date -Format s) Lote terminado. Faltan $($estado.faltan) días." | AlLog
  if (-not $estado -or $estado.faltan -le 0 -or $estado.procesados_en_este_lote -le 0) { break }
}
"$(Get-Date -Format s) Fin" | AlLog
