# Builds dist\pxbake.exe — a standalone Windows app, no Python needed to run it.
# You need Python + internet for this step only.
#
# It builds inside .venv\ rather than your global site-packages. Not politeness:
# PyInstaller hard-refuses to run if the obsolete `pathlib` backport is installed
# anywhere on the path, and plenty of machines have it. A clean venv can't.
#
# --uac-admin marks the exe so Windows prompts for elevation on launch. Writing a
# raw disk requires it; without the manifest you'd get a permission error deep
# into a bake instead of a prompt up front.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python pxbake.py --selftest

if (-not (Test-Path .venv)) { python -m venv .venv }
$py = Resolve-Path .venv\Scripts\python.exe
& $py -m pip install --quiet --upgrade pip pyinstaller
& $py -m PyInstaller --noconfirm --clean --onefile --noconsole --uac-admin `
    --name pxbake pxbake.py

Write-Host "`nBuilt: $(Resolve-Path dist\pxbake.exe)" -ForegroundColor Green
