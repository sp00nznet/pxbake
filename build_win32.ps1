# Builds dist\pimox-builder.exe — a standalone Windows app, no Python needed to run it.
# You need Python + internet for this step only.
#
# It builds inside .venv\ rather than your global site-packages. Not politeness:
# PyInstaller hard-refuses to run if the obsolete `pathlib` backport is installed
# anywhere on the path, and plenty of machines have it. A clean venv can't.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python pimox_builder.py --selftest

if (-not (Test-Path .venv)) { python -m venv .venv }
$py = Resolve-Path .venv\Scripts\python.exe
& $py -m pip install --quiet --upgrade pip pyinstaller
& $py -m PyInstaller --noconfirm --clean --onefile --noconsole `
    --name pimox-builder pimox_builder.py

Write-Host "`nBuilt: $(Resolve-Path dist\pimox-builder.exe)" -ForegroundColor Green
