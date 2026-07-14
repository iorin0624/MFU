@echo off
setlocal
cd /d "%~dp0"

python -m pip install -r requirements.txt
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --name MFUWindowsUploader ^
  --hidden-import PySide6.QtCore ^
  --hidden-import PySide6.QtGui ^
  --hidden-import PySide6.QtWidgets ^
  main.py

echo.
echo Built: %~dp0dist\MFUWindowsUploader\MFUWindowsUploader.exe
pause
