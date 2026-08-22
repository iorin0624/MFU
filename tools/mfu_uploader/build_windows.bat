@echo off
setlocal
cd /d "%~dp0"

python -m pip install -r requirements.txt
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --distpath dist_v10 ^
  --workpath build_v10 ^
  MFUUploader.spec

echo.
echo Built: %~dp0dist_v10\MFUUploader\MFUUploader.exe
pause
