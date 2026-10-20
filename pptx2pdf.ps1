# Convert a PPTX to PDF through PowerPoint (PowerPoint.Application) or WPS
# Presentation (KWPP.Application). ASCII only: this file is embedded in the exe
# and run by Windows PowerShell 5.1, whose default file encoding is ANSI.
#
# Called by docs.py:
#   powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File pptx2pdf.ps1 -Src a.pptx -Dst b.pdf
#
# Exit code 0 = $Dst exists and is non-empty; anything else = caller falls back
# to the built-in python-pptx HTML renderer.

param(
    [Parameter(Mandatory = $true)][string]$Src,
    [Parameter(Mandatory = $true)][string]$Dst
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

$Src = [IO.Path]::GetFullPath($Src)
$Dst = [IO.Path]::GetFullPath($Dst)
$outDir = [IO.Path]::GetDirectoryName($Dst)
if ($outDir -and -not (Test-Path -LiteralPath $outDir)) {
    [void](New-Item -ItemType Directory -Force -Path $outDir)
}

function Test-AppRunning([string[]]$Names) {
    foreach ($name in $Names) {
        if (Get-Process -Name $name -ErrorAction SilentlyContinue) { return $true }
    }
    return $false
}

# Returns $true only when $Dst was written. Never throws: COM errors mean
# "this program is not installed / cannot open the file", and the caller has a
# fallback. Quit() only when we started the program ourselves, so a student's
# already-open PowerPoint is left alone.
function Convert-WithCom([string]$ProgId, [string[]]$ProcessNames) {
    $wasRunning = Test-AppRunning $ProcessNames
    $app = $null
    try {
        $app = New-Object -ComObject $ProgId
    } catch {
        return $false
    }

    $pres = $null
    try {
        # Open(FileName, ReadOnly, Untitled, WithWindow)
        $pres = $app.Presentations.Open($Src, -1, 0, 0)
    } catch {
        try { [void]$app.Quit() } catch { }
        try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app) } catch { }
        return $false
    }

    $ok = $false
    try {
        # 32 = ppSaveAsPDF
        $pres.SaveAs($Dst, 32)
        $ok = Test-Path -LiteralPath $Dst
    } catch {
        $ok = $false
    }

    try { [void]$pres.Close() } catch { }
    if (-not $wasRunning) {
        try { [void]$app.Quit() } catch { }
    }
    try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($pres) } catch { }
    try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app) } catch { }
    return $ok
}

$ok = $false
$ok = Convert-WithCom 'PowerPoint.Application' @('POWERPNT')
if (-not $ok) {
    $ok = Convert-WithCom 'KWPP.Application' @('wpp', 'wps')
}

if (-not $ok -or -not (Test-Path -LiteralPath $Dst)) {
    Write-Output 'No PowerPoint/WPS COM conversion available.'
    exit 1
}
Write-Output 'OK'
exit 0
