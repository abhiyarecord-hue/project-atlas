# project-atlas installer (Windows PowerShell)
# Usage: powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"
$src  = Join-Path $PSScriptRoot "project-atlas"
$dest = Join-Path $HOME ".claude\skills\project-atlas"

$py = $null
foreach ($c in @("python", "py", "python3")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}
if (-not $py) { Write-Host "Python 3 not found. Install it from https://python.org first." -ForegroundColor Red; exit 1 }

New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item -Path (Join-Path $src "*") -Destination $dest -Recurse -Force
Write-Host "Skill copied to $dest" -ForegroundColor Green

& $py (Join-Path $dest "atlas.py") install-hook --root $HOME

Write-Host ""
Write-Host "Done. Open Claude Code in any project and it will be indexed automatically." -ForegroundColor Cyan
