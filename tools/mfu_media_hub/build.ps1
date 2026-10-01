$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$entry = Join-Path $PSScriptRoot "main.py"
$dist = Join-Path $PSScriptRoot "dist"
$work = Join-Path $PSScriptRoot "build"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }
& $python -m PyInstaller --noconfirm --clean --windowed --onedir `
  --name "MFU Media Hub" `
  --paths $root `
  --distpath $dist `
  --workpath $work `
  --specpath $PSScriptRoot `
  --collect-submodules socketio `
  --collect-submodules engineio `
  $entry
Write-Host "Built: $dist\MFU Media Hub"
