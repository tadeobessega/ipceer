# Programa las tareas del IPCEER en Windows para que corran solas todos los días, también con la
# PC bloqueada o sin la sesión iniciada (usan tu usuario y tu contraseña; Windows la guarda cifrada).
#
# Uso (una sola vez, en PowerShell):
#   powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Documents\ipceer\scripts\programar_tareas.ps1"
#
# Qué deja:
#   - "IPCEER diario": 07:30 y 19:30, todos los días. Trae el código nuevo de GitHub, baja SEPA,
#     INDEC y Energía, procesa, calcula, arma el tablero y deja el estado. Si la PC estaba apagada
#     a esa hora, corre apenas se prende; si está suspendida, la despierta.
#   - "IPCEER historico": 01:30, sigue con la base 2016-2018 en lotes de 30 días hasta terminar.
#   - "IPCEER control": al iniciar sesión y cada 4 horas, avisa en Windows si la base está atrasada.
#   - Conecta el repo a github.com/tadeobessega/ipceer (de ahí baja el código nuevo) y, si querés,
#     GitHub CLI para subir el estado: así GitHub te avisa por correo si la PC deja de actualizar.
param([string]$Archivo = "$env:USERPROFILE\Documents\SEPA")
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
$ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

Write-Host "1/4  Repo conectado a GitHub (tadeobessega/ipceer)"
if (Get-Command git -ErrorAction SilentlyContinue) {
  git remote set-url origin https://github.com/tadeobessega/ipceer.git
  git fetch origin main
  git pull --ff-only origin main
} else { Write-Warning "git no está instalado: instalalo con 'winget install Git.Git' y volvé a correr este script." }

Write-Host "2/4  Tu usuario y contraseña de Windows (la de la cuenta, no el PIN), para que las tareas corran con la PC bloqueada"
# se pide en esta misma ventana (sin cuadro de diálogo, que a veces queda escondido detrás)
$usuario = "$env:USERDOMAIN\$env:USERNAME"
Write-Host "   Usuario: $usuario"
$seg = Read-Host "   Contraseña (no se ve mientras escribís; Enter al terminar)" -AsSecureString
$clave = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($seg))

$ajustes = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -RunOnlyIfNetworkAvailable -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 30) `
  -ExecutionTimeLimit (New-TimeSpan -Hours 10)

Write-Host "3/4  Tareas"
$diario = New-ScheduledTaskAction -Execute $ps -WorkingDirectory $Repo `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Repo\scripts\actualizar_todo.ps1`" -Archivo `"$Archivo`""
$tDiario = @((New-ScheduledTaskTrigger -Daily -At "07:30"), (New-ScheduledTaskTrigger -Daily -At "19:30"))
Register-ScheduledTask -TaskName "IPCEER diario" -Action $diario -Trigger $tDiario -Settings $ajustes `
  -User $usuario -Password $clave -RunLevel Limited -Description "Actualiza la base y el tablero del IPCEER" -Force | Out-Null

$hist = New-ScheduledTaskAction -Execute $ps -WorkingDirectory $Repo `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Repo\scripts\historico.ps1`""
$ajustesH = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -RunOnlyIfNetworkAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 20)
Register-ScheduledTask -TaskName "IPCEER historico" -Action $hist -Trigger (New-ScheduledTaskTrigger -Daily -At "01:30") `
  -Settings $ajustesH -User $usuario -Password $clave -RunLevel Limited -Description "Base histórica 2016-2018 del IPCEER" -Force | Out-Null
$clave = $null

$ctl = New-ScheduledTaskAction -Execute $ps -WorkingDirectory $Repo `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Repo\scripts\control.ps1`""
$tCtl = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(9) -RepetitionInterval (New-TimeSpan -Hours 4)
Register-ScheduledTask -TaskName "IPCEER control" -Action $ctl -Trigger @((New-ScheduledTaskTrigger -AtLogOn -User $usuario), $tCtl) `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries) -User $usuario -RunLevel Limited `
  -Description "Avisa si la base del IPCEER está atrasada" -Force | Out-Null

# que la PC pueda despertarse para las tareas (temporizadores de activación)
try { powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1; powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1; powercfg /SETACTIVE SCHEME_CURRENT } catch {}

Write-Host "4/4  Aviso por correo si la PC deja de actualizar (GitHub CLI)"
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
  $r = Read-Host "GitHub CLI no está instalado. ¿Lo instalo con winget? (s/n)"
  if ($r -eq "s") { winget install --id GitHub.cli -e --accept-source-agreements --accept-package-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User") }
}
if (Get-Command gh -ErrorAction SilentlyContinue) {
  gh auth status 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { Write-Host "Se abre el navegador para conectar GitHub (cuenta tadeobessega)."; gh auth login --web -h github.com -p https }
}

Write-Host ""
Write-Host "Listo. Tareas programadas:"
Get-ScheduledTask -TaskName "IPCEER*" | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "Primera corrida ahora (queda corriendo en segundo plano; el log va a logs\actualizar_*.log)."
Start-ScheduledTask -TaskName "IPCEER diario"
