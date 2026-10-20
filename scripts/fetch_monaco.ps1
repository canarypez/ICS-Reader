# Download the Monaco Editor AMD build into web\vendor\monaco\vs.
# Kept out of git (.gitignore); build_exe.ps1 calls this when the assets are missing.
# ASCII only: Windows PowerShell 5.1 reads .ps1 as ANSI.
param(
  [string]$Version = "0.57.0"
)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root "web\vendor\monaco"
$loader = Join-Path $dest "vs\loader.js"
if (Test-Path $loader) {
  Write-Host "Monaco already present: $loader"
  exit 0
}

$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) {
  Write-Host "npm not found; skipping Monaco download (the reader falls back to a plain code view)." -ForegroundColor Yellow
  exit 0
}

$tmp = Join-Path $root "tmp\monaco-pack"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
New-Item -ItemType Directory -Force $tmp | Out-Null

try {
  Push-Location $tmp
  & npm pack "monaco-editor@$Version" --silent | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "npm pack failed ($LASTEXITCODE)" }
  $tgz = Get-ChildItem -Path $tmp -Filter "monaco-editor-*.tgz" | Select-Object -First 1
  if (-not $tgz) { throw "npm pack produced no tarball" }

  & tar -xzf $tgz.FullName "package/min/vs" "package/LICENSE"
  if ($LASTEXITCODE -ne 0) { throw "tar failed ($LASTEXITCODE)" }

  $src = Join-Path $tmp "package\min\vs"
  if (-not (Test-Path (Join-Path $src "loader.js"))) {
    throw "package/min/vs/loader.js is missing; this Monaco version no longer ships the AMD build"
  }
  New-Item -ItemType Directory -Force $dest | Out-Null
  $vsDest = Join-Path $dest "vs"
  if (Test-Path $vsDest) { Remove-Item $vsDest -Recurse -Force }
  Copy-Item $src $vsDest -Recurse
  Copy-Item (Join-Path $tmp "package\LICENSE") (Join-Path $dest "LICENSE") -Force
  Write-Host "Monaco $Version installed under web\vendor\monaco\vs"
} finally {
  Pop-Location
  if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
}
