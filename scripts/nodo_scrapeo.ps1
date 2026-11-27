# Pasa el relevamiento a esta PC (D-76): la tarea diaria corre todos los días a las 08:00 y a las 13:00
# y releva las tiendas en línea acá, además de SEPA. Incluye el arreglo de D-73 (tareas con la sesión
# del usuario, sin contraseña guardada: en equipos con dominio las otras nunca arrancaban).
#
# Uso: clic derecho > "Ejecutar con PowerShell". Anda desde Documentos o desde ipceer\scripts.
$ErrorActionPreference = "Continue"
$Repo = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path "$Repo\.git")) { $Repo = "$env:USERPROFILE\Documents\ipceer" }
New-Item -ItemType Directory -Force -Path "$Repo\logs" | Out-Null
$Log = "$Repo\logs\nodo_scrapeo_$(Get-Date -Format yyyy-MM-dd_HHmm).log"
function Log($m) { $l = "$(Get-Date -Format s) $m"; $l | Out-File -Append -Encoding utf8 $Log; Write-Host $l }
$usuario = "$env:USERDOMAIN\$env:USERNAME"
Log "PC $env:COMPUTERNAME, usuario $usuario, repo $Repo"

# 1. código nuevo
git -C $Repo pull --ff-only origin main 2>&1 | ForEach-Object { Log "   git: $_" }

# 2. esta PC releva (lo leen actualizar_todo.ps1 y control.ps1)
[Environment]::SetEnvironmentVariable("IPCEER_SCRAPEO", "1", "User"); $env:IPCEER_SCRAPEO = "1"
if (-not [Environment]::GetEnvironmentVariable("IPCEER_NODO", "User")) { [Environment]::SetEnvironmentVariable("IPCEER_NODO", "thinkpad", "User") }
$env:IPCEER_NODO = [Environment]::GetEnvironmentVariable("IPCEER_NODO", "User")
Log "Nodo $env:IPCEER_NODO con relevamiento propio (IPCEER_SCRAPEO=1)"

# 3. tarea diaria: todos los días 08:00 y 13:00, con la sesión del usuario (también con la PC bloqueada)
$ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$accion = New-ScheduledTaskAction -Execute $ps -WorkingDirectory $Repo `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Repo\scripts\actualizar_todo.ps1`""
$disparos = @((New-ScheduledTaskTrigger -Daily -At "08:00"), (New-ScheduledTaskTrigger -Daily -At "13:00"))
$ajustes = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 5)
$p = New-ScheduledTaskPrincipal -UserId $usuario -LogonType Interactive -RunLevel Limited
try {
  Register-ScheduledTask -TaskName "IPCEER diario" -Action $accion -Trigger $disparos -Settings $ajustes -Principal $p `
    -Description "IPCEER: SEPA, tiendas en línea, cálculo y tablero (08:00 y 13:00)" -Force -ErrorAction Stop | Out-Null
  Log "IPCEER diario: todos los días 08:00 y 13:00"
} catch { Log "ERROR al registrar IPCEER diario: $($_.Exception.Message)" }

# 4. las demás tareas, con la sesión del usuario; la base histórica 2016-2018 la sigue la HP15
foreach ($n in "IPCEER verificacion 18 h", "IPCEER control") {
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue; if (-not $t) { continue }
  $s = $t.Settings; $s.RunOnlyIfNetworkAvailable = $false; $s.StartWhenAvailable = $true
  $s.DisallowStartIfOnBatteries = $false; $s.StopIfGoingOnBatteries = $false
  try { Set-ScheduledTask -TaskName $n -Principal $p -Settings $s -ErrorAction Stop | Out-Null; Log "$n: con la sesión del usuario" }
  catch { Log "ERROR en $n: $($_.Exception.Message)" }
}
if (Get-ScheduledTask -TaskName "IPCEER historico" -ErrorAction SilentlyContinue) {
  Disable-ScheduledTask -TaskName "IPCEER historico" | Out-Null; Log "IPCEER historico: desactivada en esta PC (la hace la HP15)"
}

# 5. que no se suspenda enchufada
powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0; Log "Suspensión con cargador: nunca"

# 6. primera corrida y verificación
Start-ScheduledTask -TaskName "IPCEER diario"
Start-Sleep -Seconds 45
$i = Get-ScheduledTaskInfo -TaskName "IPCEER diario"; $e = (Get-ScheduledTask -TaskName "IPCEER diario").State
Log "IPCEER diario: estado $e, última corrida $($i.LastRunTime), resultado $($i.LastTaskResult), próxima $($i.NextRunTime)"
$nuevo = Get-ChildItem "$Repo\logs\actualizar_*.log" -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -gt (Get-Date).AddMinutes(-2) }
if ($e -eq "Running" -or $nuevo) { Log "OK: la tarea arrancó sola y sigue en segundo plano." }
else { Log "La tarea no arrancó: avisale a Claude (lee este log en logs\)." }
Read-Host "Enter para cerrar"
