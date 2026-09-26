@echo off
setlocal
cd /d %~dp0\..

set "BUILD_PYTHON=build\.venv\Scripts\python.exe"
if not exist "%BUILD_PYTHON%" (
  echo [MFU Photo Relay] Creating the Python 3.10 compatibility build environment...
  py -3.10 -m venv build\.venv
  if errorlevel 1 goto :error
)

echo [MFU Photo Relay] Installing requirements...
"%BUILD_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :error
"%BUILD_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [MFU Photo Relay] Cleaning previous build output...
if exist "build\_pyinstaller_work" rmdir /s /q "build\_pyinstaller_work"
if exist "dist\MFUPhotoRelay" rmdir /s /q "dist\MFUPhotoRelay"

echo [MFU Photo Relay] Building executable...
"%BUILD_PYTHON%" -m PyInstaller build\mfu_photo_relay.spec --clean --noconfirm --workpath build\_pyinstaller_work --distpath dist
if errorlevel 1 goto :error

echo [MFU Photo Relay] Aligning the bundled Visual C++ runtime...
for /f "usebackq delims=" %%D in (`"%BUILD_PYTHON%" -c "import pathlib,PySide6; print(pathlib.Path(PySide6.__file__).resolve().parent)"`) do set "PYSIDE_DIR=%%D"
for %%F in (concrt140.dll msvcp140.dll msvcp140_1.dll msvcp140_2.dll msvcp140_codecvt_ids.dll vcruntime140.dll vcruntime140_1.dll) do (
  if exist "%PYSIDE_DIR%\%%F" copy /y "%PYSIDE_DIR%\%%F" "dist\MFUPhotoRelay\_internal\%%F" >nul
)

echo.
echo Build finished:
echo %cd%\dist\MFUPhotoRelay\MFUPhotoRelay.exe
if exist "dist\MFUPhotoRelay" explorer "dist\MFUPhotoRelay"
pause
exit /b 0

:error
echo Build failed. Please check the error above.
pause
exit /b 1
