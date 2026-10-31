# Construye la base histórica 2016-2018 (Zenodo) y las series oficiales del INDEC.
# Tarda varias horas (baja ~44 GB de a un día y borra cada archivo después de procesarlo).
# Es reanudable: si se corta, se vuelve a correr y sigue donde quedó.
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
& $Python -m ipceer.cli historico 2>&1 | AlLog
& $Python -m ipceer.cli consolidar 2>&1 | AlLog
& $Python -m ipceer.cli calcular 2>&1 | AlLog
& $Python -m ipceer.cli sitio 2>&1 | AlLog
"$(Get-Date -Format s) Fin" | AlLog
