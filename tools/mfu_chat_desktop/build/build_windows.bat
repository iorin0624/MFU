@echo off
setlocal
cd /d %~dp0\..

echo [MFU Chat Desktop] Installing requirements...
python -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [MFU Chat Desktop] Cleaning previous build output...
if exist "build\_pyinstaller_work" rmdir /s /q "build\_pyinstaller_work"
if exist "build\mfu_chat_desktop" rmdir /s /q "build\mfu_chat_desktop"

echo [MFU Chat Desktop] Building executable...
python -m PyInstaller build\mfu_chat_desktop.spec --clean --noconfirm --workpath build\_pyinstaller_work --distpath dist
if errorlevel 1 goto :error

echo.
echo Build finished.
echo Run this executable:
echo %cd%\dist\MFUChatDesktop\MFUChatDesktop.exe
echo.
if exist "dist\MFUChatDesktop" explorer "dist\MFUChatDesktop"
pause
exit /b 0

:error
echo.
echo Build failed. Please check the error above.
pause
exit /b 1
