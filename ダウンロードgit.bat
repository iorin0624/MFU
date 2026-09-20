@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "SCRIPT_VERSION=20260921a"
set "BRANCH=main"
set "REMOTE=origin"
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

echo [INFO] MFU GitHub download
echo [INFO] SCRIPT_VERSION=%SCRIPT_VERSION%
echo [INFO] REPOSITORY=%SCRIPT_DIR%
echo [INFO] This script only downloads updates from GitHub.
echo [INFO] It does not copy files directly from the production server.
echo.

where git >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Git was not found in PATH.
  goto FAIL
)

if not exist "%SCRIPT_DIR%\.git\" (
  echo [ERROR] The batch file is not in the repository root.
  goto FAIL
)

pushd "%SCRIPT_DIR%"
if errorlevel 1 (
  echo [ERROR] Could not open the repository folder.
  goto FAIL
)

for /f "delims=" %%B in ('git branch --show-current 2^>nul') do set "CURRENT_BRANCH=%%B"
if not defined CURRENT_BRANCH (
  echo [ERROR] Git is in detached HEAD state.
  popd
  goto FAIL
)
if /I not "%CURRENT_BRANCH%"=="%BRANCH%" (
  echo [ERROR] Current branch is "%CURRENT_BRANCH%". Expected "%BRANCH%".
  popd
  goto FAIL
)

set "DIRTY="
for /f "delims=" %%S in ('git status --porcelain --untracked-files^=all') do set "DIRTY=1"
if defined DIRTY (
  echo [ERROR] Local changes exist. Nothing was downloaded.
  echo [HINT] Commit, stash, or remove these changes first:
  git status --short
  popd
  goto FAIL
)

for /f "delims=" %%H in ('git rev-parse --short HEAD') do set "BEFORE=%%H"

echo [STEP] Fetching %REMOTE%/%BRANCH%...
git fetch --prune "%REMOTE%"
if errorlevel 1 (
  echo [ERROR] git fetch failed.
  popd
  goto FAIL
)

echo [STEP] Updating by fast-forward only...
git pull --ff-only --no-rebase "%REMOTE%" "%BRANCH%"
if errorlevel 1 (
  echo [ERROR] Fast-forward update failed.
  echo [HINT] No reset or automatic rebase was performed.
  popd
  goto FAIL
)

for /f "delims=" %%H in ('git rev-parse --short HEAD') do set "AFTER=%%H"

echo.
if /I "%BEFORE%"=="%AFTER%" (
  echo [OK] Already up to date: %AFTER%
) else (
  echo [OK] Updated: %BEFORE% -^> %AFTER%
  echo [INFO] Downloaded commits:
  git log --oneline "%BEFORE%..%AFTER%"
)
git status --short --branch
popd
goto OK

:OK
echo.
echo [OK] GitHub download completed safely.
echo.
pause
exit /b 0

:FAIL
echo.
echo [FAIL] Stopped without overwriting local work.
echo.
pause
exit /b 1
