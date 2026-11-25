# Repara las tareas del IPCEER en una PC donde nunca corrieron (D-73).
#
# Síntoma: Get-ScheduledTaskInfo "IPCEER diario" da LastTaskResult 267011 (0x41303, "la tarea
# todavía no se ejecutó") aun después de las 06:00 y las 14:00. Pasa en equipos con dominio donde una
# política no deja correr tareas con usuario y contraseña guardados ("iniciar sesión como proceso por
# lotes"). Arreglo: las tareas corren con la sesión del usuario (también con la PC bloqueada), sin
# contraseña guardada, y no esperan a que Windows "detecte" la red (la tarea ya reintenta si no hay).
#
# Uso: powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Documents\ipceer\scripts\reparar_tareas.ps1"
$ErrorActionPreference = "Continue"
$Repo = Split-Path -Parent $PSScriptRoot
$Log = "$Repo\logs\reparar_tareas_$(Get-Date -Format yyyy-MM-dd_HHmm).log"
New-Item -ItemType Directory -Force -Path "$Repo\logs" | Out-Null
function Log($m) { $l = "$(Get-Date -Format s) $m"; $l | Out-File -Append -Encoding utf8 $Log; Write-Host $l }
$usuario = "$env:USERDOMAIN\$env:USERNAME"
Log "Usuario: $usuario  PC: $env:COMPUTERNAME"

# 1. diagnóstico: qué dice el historial del Programador de tareas (si está activado)
try {
  Get-WinEvent -LogName "Microsoft-Windows-TaskScheduler/Operational" -MaxEvents 500 -ErrorAction Stop |
    Where-Object { $_.Message -like "*IPCEER*" } | Select-Object -First 20 |
    ForEach-Object { Log "evento $($_.Id) $($_.TimeCreated.ToString('s')): $(($_.Message -split "`n")[0])" }
} catch { Log "Historial del Programador de tareas no disponible: $($_.Exception.Message)" }
try { $pol = (whoami /priv | Select-String "SeBatchLogonRight"); Log "Derecho de proceso por lotes: $(if ($pol) { $pol } else { 'no figura en la sesión' })" } catch {}

# 2. las tareas pasan a correr con la sesión del usuario
$p = New-ScheduledTaskPrincipal -UserId $usuario -LogonType Interactive -RunLevel Limited
foreach ($n in "IPCEER diario", "IPCEER historico", "IPCEER verificacion 18 h", "IPCEER control") {
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if (-not $t) { Log "No existe la tarea $n"; continue }
  $s = $t.Settings
  $s.RunOnlyIfNetworkAvailable = $false
  $s.StartWhenAvailable = $true
  $s.DisallowStartIfOnBatteries = $false
  $s.StopIfGoingOnBatteries = $false
  try { Set-ScheduledTask -TaskName $n -Principal $p -Settings $s -ErrorAction Stop | Out-Null; Log "Tarea $n: con la sesión del usuario" }
  catch { Log "ERROR al cambiar $n: $($_.Exception.Message)" }
}

# 3. que la PC no se suspenda enchufada (si se suspende, la tarea no corre hasta que se despierte)
try { powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0; Log "Suspensión con cargador: nunca" } catch {}

# 4. prueba: arranca la tarea diaria y verifica que haya corrido
Start-ScheduledTask -TaskName "IPCEER diario"
Start-Sleep -Seconds 45
$i = Get-ScheduledTaskInfo -TaskName "IPCEER diario"
$estado = (Get-ScheduledTask -TaskName "IPCEER diario").State
Log "IPCEER diario: estado $estado, última corrida $($i.LastRunTime), resultado $($i.LastTaskResult)"
$nuevo = Get-ChildItem "$Repo\logs\actualizar_*.log" -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -gt (Get-Date).AddMinutes(-2) }
if ($estado -eq "Running" -or $nuevo) { Log "OK: la tarea arrancó sola. Sigue en segundo plano (log en logs\actualizar_*.log)." }
else { Log "La tarea sigue sin arrancar. Pasame el contenido de $Log." }
Read-Host "Enter para cerrar"
