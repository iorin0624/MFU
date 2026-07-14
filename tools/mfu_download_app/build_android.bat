@echo off
setlocal

where flutter >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Flutter SDK was not found in PATH.
  exit /b 1
)

call flutter pub get
if errorlevel 1 exit /b 2

call dart run flutter_launcher_icons
if errorlevel 1 exit /b 3

call flutter analyze
if errorlevel 1 exit /b 4

call flutter test
if errorlevel 1 exit /b 5

call flutter build apk --release
if errorlevel 1 exit /b 6

echo.
echo Built: build\app\outputs\flutter-apk\app-release.apk
exit /b 0
