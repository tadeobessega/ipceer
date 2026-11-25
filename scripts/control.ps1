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
$archPasos = "$Repo\resultados\ultima_corrida_pasos.json"
if (-not $Pasos -and (Test-Path $archPasos)) { $Pasos = Get-Content $archPasos -Raw -Encoding UTF8 }
if ($Pasos) { try { ($Pasos | ConvertFrom-Json).PSObject.Properties | Where-Object { $_.Value -like "con errores*" } | ForEach-Object { $errores += $_.Name } } catch {} }
$estado = [ordered]@{
  ultimo_dia = $ultimo; atraso_dias = $atraso; corrida = (Get-Date -Format s); pc = $env:COMPUTERNAME
  pasos_con_errores = $errores; historico_faltan = $(if ($hist) { $hist.faltan } else { $null })
}
$json = $estado | ConvertTo-Json
$destino = "$Repo\resultados\estado_pc.json"
[IO.File]::WriteAllText($destino, $json, (New-Object System.Text.UTF8Encoding $false))
Write-Output $json

# fallas en GitHub: issues abiertos por los flujos o por la vigilancia (también llegan por correo)
$fallasGh = @()
if (Get-Command gh -ErrorAction SilentlyContinue) {
  try { $fallasGh = @(gh issue list -R tadeobessega/ipceer --state open --limit 20 --json title -q ".[].title" 2>$null | Where-Object { $_ -match "^(Falló|Fallaron|Vigilancia)" }) } catch {}
}
$estado["fallas_github"] = $fallasGh
$json = $estado | ConvertTo-Json
[IO.File]::WriteAllText($destino, $json, (New-Object System.Text.UTF8Encoding $false))

# aviso en Windows
$msg = $null
if ($atraso -ge 2) { $msg = "La base de precios tiene datos hasta el $ultimo ($atraso días de atraso)." }
elseif ($errores.Count) { $msg = "La última actualización tuvo errores en: $($errores -join ', '). Ver logs." }
elseif ($fallasGh.Count) { $msg = "GitHub avisó fallas: $($fallasGh -join '; '). La PC ya recalculó todo; revisar el issue y cerrarlo." }
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
  # D-71: cada PC es un nodo con nombre (variable de usuario IPCEER_NODO). Sin la variable es la
  # HP15, que sigue subiendo con los nombres de siempre. Los demás nodos suben con sufijo y no pisan.
  $Nodo = if ($env:IPCEER_NODO) { $env:IPCEER_NODO.ToLower() } else { "hp15" }
  $Py = if (Test-Path "$Repo\.venv\Scripts\python.exe") { "$Repo\.venv\Scripts\python.exe" } else { "python" }
  # paneles diarios recientes a datos-base: GitHub no puede bajar SEPA (403) y los necesita.
  # Idempotente: lo que ya está arriba (lo haya subido cualquier nodo) no se vuelve a subir.
  & $Py "$PSScriptRoot\subir_paneles.py" 2>&1 | Write-Output
  cmd /c "gh auth status >nul 2>&1"
  if ($LASTEXITCODE -eq 0) {
    $R = "tadeobessega/ipceer"
    # zips de SEPA de los últimos 14 días que no estén en el release: así cada día queda en PC + GitHub
    $ArchivoSepa = if ($env:IPCEER_ARCHIVO) { $env:IPCEER_ARCHIVO } else { "$env:USERPROFILE\Documents\SEPA" }
    $arriba = @(gh release view sepa-archivo -R $R --json assets -q ".assets[].name" 2>$null)
    if ($LASTEXITCODE -eq 0 -and $arriba.Count) {
      Get-ChildItem "$ArchivoSepa\sepa_*.zip" -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -gt (Get-Date).AddDays(-14) } |
        Where-Object { $arriba -notcontains $_.Name } | ForEach-Object {
          Write-Output "Subiendo $($_.Name) al release"
          gh release upload sepa-archivo $_.FullName -R $R 2>&1 | Write-Output
        }
    }
    # latido del nodo (lo lee vigilancia.yml)
    Copy-Item $destino "$env:TEMP\estado_$Nodo.json" -Force
    gh release upload sepa-archivo "$env:TEMP\estado_$Nodo.json" --clobber -R $R 2>&1 | Write-Output
    $suf = if ($Nodo -eq "hp15") { "pc" } else { $Nodo }
    if ($Nodo -eq "hp15") {
      # compatibilidad: estado_pc.json sigue siendo el de la HP15
      Copy-Item $destino "$env:TEMP\estado_pc.json" -Force
      gh release upload sepa-archivo "$env:TEMP\estado_pc.json" --clobber -R $R 2>&1 | Write-Output
    }
    # tablero: lo sirve el Worker "ipceer" de Cloudflare (ipceer.tadeobessega.workers.dev); solo la HP15
    # sube tablero.html; los otros nodos suben tablero_<nodo>.html para comparar
    if (Test-Path "$Repo\sitio\index.html") {
      $nt = if ($Nodo -eq "hp15") { "tablero.html" } else { "tablero_$Nodo.html" }
      Copy-Item "$Repo\sitio\index.html" "$env:TEMP\$nt" -Force
      gh release upload sepa-archivo "$env:TEMP\$nt" --clobber -R $R 2>&1 | Write-Output
    }
    # datos del último cálculo: GitHub los usa para las partes que no puede recalcular (D-49)
    if (Test-Path "$Repo\resultados\datos.json") {
      Copy-Item "$Repo\resultados\datos.json" "$env:TEMP\datos_$suf.json" -Force
      gh release upload sepa-archivo "$env:TEMP\datos_$suf.json" --clobber -R $R 2>&1 | Write-Output
    }
  } else { Write-Output "gh no está conectado: el estado no se sube a GitHub (correr 'gh auth login')." }
}
