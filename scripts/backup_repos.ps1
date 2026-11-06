# Backup de todos los repos de GitHub de tadeobessega (y los extra de $Extra) en OneDrive.
#
# Uso (lo llama solo actualizar_todo.ps1; también se puede correr a mano):
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backup_repos.ps1
#
# Qué hace:
#   1. lista los repos de la cuenta con gh (incluye los privados);
#   2. mantiene un espejo completo de cada uno (todas las ramas, etiquetas e historia) en _espejos\;
#   3. si el repo cambió desde el último backup, guarda un .bundle con toda la historia
#      (se restaura con: git clone archivo.bundle carpeta);
#   4. conserva los backups de los últimos 30 días y el primero de cada mes para siempre;
#   5. sube un resumen (estado_backups.json) al release sepa-archivo para que lo muestre el panel.
# Corre una sola vez por día aunque la tarea se ejecute más veces.

param(
  [string]$Destino = $(if ($env:OneDrive) { "$env:OneDrive\Backups repos CEER" } else { "$env:USERPROFILE\OneDrive\Backups repos CEER" }),
  [string]$Cuenta = "tadeobessega",
  [string[]]$Extra = @("Subsepresupuesto/ipceer", "marcos-sacca/tasa-vial"),
  [int]$DiasDiarios = 30,
  [switch]$Forzar
)

$ErrorActionPreference = "Continue"
$Hoy = Get-Date -Format "yyyy-MM-dd"
New-Item -ItemType Directory -Force -Path $Destino, "$Destino\_espejos" | Out-Null
$Marca = "$Destino\_ultimo_backup.txt"
if (-not $Forzar -and (Test-Path $Marca) -and ((Get-Content $Marca -Raw).Trim() -eq $Hoy)) {
  Write-Output "Backup de repos: ya se hizo hoy ($Hoy)."
  exit 0
}
if (-not (Get-Command gh -ErrorAction SilentlyContinue) -or -not (Get-Command git -ErrorAction SilentlyContinue)) {
  Write-Output "Backup de repos: falta gh o git."
  exit 1
}
gh auth setup-git 2>&1 | Out-Null

$repos = @()
$lista = gh repo list $Cuenta --limit 300 --json nameWithOwner 2>$null | ConvertFrom-Json
if ($lista) { $repos += $lista | ForEach-Object { $_.nameWithOwner } }
foreach ($e in $Extra) {
  if ($repos -notcontains $e) { gh repo view $e --json name 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { $repos += $e } }
}

$resumen = @()
foreach ($r in $repos) {
  $nombre = $r -replace "/", "__"
  $espejo = "$Destino\_espejos\$nombre.git"
  $carpeta = "$Destino\$nombre"
  New-Item -ItemType Directory -Force -Path $carpeta | Out-Null
  $estado = "ok"
  try {
    if (Test-Path $espejo) {
      git -C $espejo remote update --prune 2>&1 | Out-Null
    } else {
      git clone --mirror "https://github.com/$r.git" $espejo 2>&1 | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { throw "no se pudo actualizar el espejo" }
    $refs = (git -C $espejo for-each-ref --format="%(objectname) %(refname)" 2>$null) -join "`n"
    if (-not $refs) {
      $estado = "vacío"
    } else {
      $huella = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($refs))).Replace("-", "").Substring(0, 16)
      $previa = if (Test-Path "$carpeta\_huella.txt") { (Get-Content "$carpeta\_huella.txt" -Raw).Trim() } else { "" }
      if ($huella -ne $previa -or -not (Get-ChildItem $carpeta -Filter *.bundle)) {
        $archivo = "$carpeta\${nombre}_$Hoy.bundle"
        git -C $espejo bundle create $archivo --all 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "no se pudo crear el bundle" }
        Set-Content -Path "$carpeta\_huella.txt" -Value $huella -Encoding utf8
        $estado = "nuevo"
      } else {
        $estado = "sin cambios"
      }
    }
    # retención: últimos $DiasDiarios días + el primero de cada mes
    $bundles = Get-ChildItem $carpeta -Filter *.bundle | Sort-Object Name
    $primeros = $bundles | Group-Object { $_.BaseName.Substring($_.BaseName.Length - 10, 7) } | ForEach-Object { $_.Group[0].FullName }
    foreach ($b in $bundles) {
      $f = [datetime]::ParseExact($b.BaseName.Substring($b.BaseName.Length - 10), "yyyy-MM-dd", $null)
      if ($f -lt (Get-Date).AddDays(-$DiasDiarios) -and $primeros -notcontains $b.FullName) { Remove-Item $b.FullName }
    }
  } catch {
    $estado = "error: $_"
  }
  $ultimo = Get-ChildItem $carpeta -Filter *.bundle -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -Last 1
  $resumen += [ordered]@{
    repo = $r
    estado = $estado
    ultimo_backup = $(if ($ultimo) { $ultimo.BaseName.Substring($ultimo.BaseName.Length - 10) } else { $null })
    copias = @(Get-ChildItem $carpeta -Filter *.bundle -ErrorAction SilentlyContinue).Count
    mb = $(if ($ultimo) { [math]::Round($ultimo.Length / 1MB, 1) } else { 0 })
  }
  Write-Output ("Backup {0}: {1}" -f $r, $estado)
}

$json = [ordered]@{
  corrida = (Get-Date -Format s)
  pc = $env:COMPUTERNAME
  carpeta = $Destino
  repos = $resumen
} | ConvertTo-Json -Depth 4
$tmp = "$env:TEMP\estado_backups.json"
[IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding $false))
gh release upload sepa-archivo $tmp --clobber -R tadeobessega/ipceer 2>&1 | Out-Null
Set-Content -Path $Marca -Value $Hoy -Encoding utf8
Write-Output "Backup de repos listo en $Destino"
