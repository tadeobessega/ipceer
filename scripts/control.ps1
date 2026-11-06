# Control de la base del IPCEER: ¿está al día?
# - Escribe resultados\estado_pc.json (último día con datos, última corrida, pasos con errores).
# - Si la base está atrasada (último día con datos de hace 2 días o más) o la última corrida tuvo
#   errores, muestra un aviso de Windows (cuando hay alguien con la sesión iniciada).
# - Con -Subir, y si GitHub CLI (gh) está conectado, sube el estado al release "sepa-archivo" de
#   tadeobessega/ipceer: así el flujo de GitHub avisa por correo (issue) si la PC deja de actualizar.
param([switch]$Subir, [string]$Pasos = "")
$Repo = Split-Path -Parent $PSScriptRoot
$dias = Get-ChildItem "$Repo\data\panel\diario\*.parquet" -ErrorAction SilentlyContinue | ForEach-Object { $_.BaseName } | Sort-Object
$ultimo = if ($dias) { $dias[-1] } else { "" }
$atraso = if ($ultimo) { [int]((Get-Date).Date - [datetime]::ParseExact($ultimo, "yyyy-MM-dd", $null)).TotalDays } else { 999 }
$hist = Get-Content "$Repo\resultados\historico_estado.json" -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
$errores = @()
if ($Pasos) { ($Pasos | ConvertFrom-Json).PSObject.Properties | Where-Object { $_.Value -like "con errores*" } | ForEach-Object { $errores += $_.Name } }
$estado = [ordered]@{
  ultimo_dia = $ultimo; atraso_dias = $atraso; corrida = (Get-Date -Format s); pc = $env:COMPUTERNAME
  pasos_con_errores = $errores; historico_faltan = $(if ($hist) { $hist.faltan } else { $null })
}
$json = $estado | ConvertTo-Json
$destino = "$Repo\resultados\estado_pc.json"
[IO.File]::WriteAllText($destino, $json, (New-Object System.Text.UTF8Encoding $false))
Write-Output $json

# aviso en Windows
$msg = $null
if ($atraso -ge 2) { $msg = "La base de precios tiene datos hasta el $ultimo ($atraso días de atraso)." }
elseif ($errores.Count) { $msg = "La última actualización tuvo errores en: $($errores -join ', '). Ver logs." }
if ($msg) {
  try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $t = $xml.GetElementsByTagName("text"); $t.Item(0).InnerText = "IPCEER"; $t.Item(1).InnerText = $msg
    $app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
  } catch { Write-Output "No se pudo mostrar el aviso: $_" }
}

# copia del estado en GitHub
if ($Subir -and (Get-Command gh -ErrorAction SilentlyContinue)) {
  cmd /c "gh auth status >nul 2>&1"
  if ($LASTEXITCODE -eq 0) {
    Copy-Item $destino "$env:TEMP\estado_pc.json" -Force
    gh release upload sepa-archivo "$env:TEMP\estado_pc.json" --clobber -R tadeobessega/ipceer 2>&1 | Write-Output
    # tablero: lo sirve el Worker "ipceer" de Cloudflare (ipceer.tadeobessega.workers.dev)
    if (Test-Path "$Repo\sitio\index.html") {
      Copy-Item "$Repo\sitio\index.html" "$env:TEMP\tablero.html" -Force
      gh release upload sepa-archivo "$env:TEMP\tablero.html" --clobber -R tadeobessega/ipceer 2>&1 | Write-Output
    }
  } else { Write-Output "gh no está conectado: el estado no se sube a GitHub (correr 'gh auth login')." }
}
