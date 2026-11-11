# Verificación de las 18 h: ¿corrió bien hoy la actualización del IPCEER?
# Mira logs\corridas.csv. Si hoy no hay ninguna corrida terminada "ok", lanza la actualización
# completa en ese momento (salvo que ya haya una en curso). Deja su propia línea en
# logs\verificacion.log con lo que encontró y lo que hizo.
$Repo = Split-Path -Parent $PSScriptRoot
$log = "$Repo\logs\verificacion.log"
function Anotar($m) { "$(Get-Date -Format s) $m" | Out-File -Append -Encoding utf8 $log }
$hoy = (Get-Date).ToString("yyyy-MM-dd")
$filas = @()
if (Test-Path "$Repo\logs\corridas.csv") { $filas = @(Import-Csv "$Repo\logs\corridas.csv" -Encoding UTF8 | Where-Object { $_.inicio -like "$hoy*" }) }
$ok = @($filas | Where-Object { $_.resultado -eq "ok" })
$enCurso = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -like "*actualizar_todo.ps1*" }
if ($ok.Count) { Anotar "Hoy hubo $($filas.Count) corrida(s), $($ok.Count) sin errores. No hace falta correr."; exit 0 }
if ($enCurso) { Anotar "Hay una corrida en curso (PID $($enCurso.ProcessId -join ', ')). No se lanza otra."; exit 0 }
$motivo = if ($filas.Count) { "hoy hubo $($filas.Count) corrida(s) pero ninguna terminó bien: " + (($filas | ForEach-Object { "$($_.resultado) [$($_.pasos_con_errores)]" }) -join '; ') } else { "hoy no corrió ninguna vez" }
Anotar "Se lanza la actualización: $motivo."
$env:IPCEER_ORIGEN = "verificacion 18 h"
& powershell -NoProfile -ExecutionPolicy Bypass -File "$Repo\scripts\actualizar_todo.ps1"
Anotar "Terminó la actualización lanzada por la verificación (código $LASTEXITCODE)."
