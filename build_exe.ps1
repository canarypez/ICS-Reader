# Build a single-file exe. Run from the os-course-reader directory.
# Output: dist\ICS-Reader.exe
$ErrorActionPreference = "Stop"

# Monaco is downloaded on demand and is not committed (see .gitignore).
if (-not (Test-Path "web\vendor\monaco\vs\loader.js")) {
  & powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\fetch_monaco.ps1"
}

pyinstaller --noconfirm --onefile --windowed --clean `
  --name "ICS-Reader" `
  --add-data "web;web" `
  --add-data "stubs;stubs" `
  --add-data "pdf_export.py;." `
  --add-data "pptx2pdf.ps1;." `
  --collect-all webview `
  --collect-all pythonnet `
  --collect-all clr_loader `
  --collect-all pypdfium2 `
  --collect-all pypdfium2_raw `
  --collect-all pptx `
  --hidden-import clr_loader `
  app.py

# $ErrorActionPreference does not cover a native command's exit code: without this
# check a failed pyinstaller still prints the "Done" line below.
# Keep this file ASCII-only: Windows PowerShell 5.1 reads .ps1 as ANSI, not UTF-8.
if ($LASTEXITCODE -ne 0) {
  Write-Host "pyinstaller failed (exit code $LASTEXITCODE)" -ForegroundColor Red
  exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Done: dist\ICS-Reader.exe"
Write-Host "On first run it reads/generates config.json next to the exe; lectures render into course\ next to the exe."
