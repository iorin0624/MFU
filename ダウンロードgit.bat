@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_VERSION=20260714a"
set "HOST=server-103-16"
set "REMOTE_DIR=/mnt/mfu/app"
set "TMP=C:\mfu_tmp_dl"
set "MSG=backup from server-103-16 (/mnt/mfu/app)"
set "BRANCH=main"
set "SSHOPTS=-o StrictHostKeyChecking=accept-new"

echo [INFO] SCRIPT_VERSION=%SCRIPT_VERSION%

set "DEST="
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

if exist "%SCRIPT_DIR%\.git\" set "DEST=%SCRIPT_DIR%"
if not defined DEST if exist "%CD%\.git\" set "DEST=%CD%"

if not defined DEST (
  echo [ERROR] DEST not detected
  echo [HINT] Put this bat in the repo root, or run it from the repo root
  goto FAIL
)

echo [INFO] HOST=%HOST%
echo [INFO] REMOTE_DIR=%REMOTE_DIR%
echo [INFO] DEST=%DEST%
echo [INFO] TMP=%TMP%
echo.

where scp >nul 2>&1 || (
  echo [ERROR] scp not found
  goto FAIL
)
where ssh >nul 2>&1 || (
  echo [ERROR] ssh not found
  goto FAIL
)
where git >nul 2>&1 || (
  echo [ERROR] git not found
  goto FAIL
)
where powershell >nul 2>&1 || (
  echo [ERROR] powershell not found
  goto FAIL
)
where robocopy >nul 2>&1 || (
  echo [ERROR] robocopy not found
  goto FAIL
)

if not exist "%DEST%\.git" (
  echo [ERROR] DEST is not a git repository
  goto FAIL
)

if exist "%TMP%\" rmdir /S /Q "%TMP%" >nul 2>&1
mkdir "%TMP%" >nul 2>&1 || (
  echo [ERROR] TMP create failed
  goto FAIL
)

echo [STEP] Download to TMP
scp %SSHOPTS% -r %HOST%:%REMOTE_DIR% "%TMP%"
if errorlevel 1 (
  echo [ERROR] scp failed
  goto FAIL
)

if not exist "%TMP%\app\" (
  echo [ERROR] TMP\app not found after scp
  goto FAIL
)

echo [STEP] Copy app into DEST
robocopy "%TMP%\app" "%DEST%" /E /R:1 /W:1 /COPY:DAT /DCOPY:DAT /NFL /NDL /NJH /NJS /NP >nul
set "RC=%ERRORLEVEL%"
echo [INFO] robocopy rc=%RC%
if %RC% GEQ 8 (
  echo [ERROR] robocopy failed
  goto FAIL
)

if exist "%TMP%\" rmdir /S /Q "%TMP%" >nul 2>&1

echo [STEP] Git add/commit/pull/push
pushd "%DEST%"
if errorlevel 1 (
  echo [ERROR] pushd failed
  goto FAIL
)

git add -A
if errorlevel 1 (
  echo [ERROR] git add failed
  popd
  goto FAIL
)

for /r "%DEST%" %%F in (.env) do (
  if exist "%%~fF" (
    set "ENVABS=%%~fF"
    set "CHECK_GIT=!ENVABS:\.git\=!"
    if /I "!CHECK_GIT!"=="!ENVABS!" (
      set "RELPATH=!ENVABS:%DEST%\=!"
      set "RELPATH=!RELPATH:\=/!"
      if defined RELPATH (
        echo [INFO] exclude .env : !RELPATH!
        git restore --staged -- "!RELPATH!" >nul 2>&1
        if errorlevel 1 git reset -q HEAD -- "!RELPATH!" >nul 2>&1
      )
    )
  )
)

git diff --cached --quiet
if errorlevel 1 (
  git commit -m "%MSG%"
  if errorlevel 1 (
    echo [ERROR] git commit failed
    popd
    goto FAIL
  )
) else (
  echo [INFO] no staged changes
)

git -c rebase.autoStash=true pull --rebase origin %BRANCH%
if errorlevel 1 (
  echo [ERROR] git pull --rebase failed
  popd
  goto FAIL
)

git push origin %BRANCH%
if errorlevel 1 (
  echo [ERROR] git push failed
  popd
  goto FAIL
)

popd
goto OK

:OK
echo.
echo [OK] completed
echo [OK] local copy keeps .env, GitHub excludes .env
echo.
pause
exit /b 0

:FAIL
echo.
echo [FAIL] stopped due to error above
echo.
pause
exit /b 1
