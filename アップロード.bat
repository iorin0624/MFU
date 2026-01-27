@echo off
setlocal enabledelayedexpansion

REM ===== 接続先 =====
set "HOST=192.168.103.16"
set "USER=root"

REM ===== ローカル（日本語OK） =====
set "SRC=Y:\01マイドキュメント\GitHub\MFU"

REM ===== リモート =====
set "DEST=/mnt/mfu/app"

REM ===== 一時（ASCIIのみ） =====
set "TMP=C:\mfu_tmp_up"

REM ===== SSH/SCP共通オプション（鍵認証前提：パスワード入力を省略）=====
set "SSHOPTS=-o BatchMode=yes -o StrictHostKeyChecking=accept-new"

echo [INFO] SRC ="%SRC%"
echo [INFO] TMP ="%TMP%"
echo [INFO] DEST=%USER%@%HOST%:%DEST%
echo.

REM ===== 事前チェック =====
if not exist "%SRC%" (
  echo [ERROR] SRC が存在しません: "%SRC%"
  exit /b 1
)

where scp >nul 2>&1
if errorlevel 1 (
  echo [ERROR] scp が見つかりません（OpenSSH Client未導入）
  exit /b 2
)

where ssh >nul 2>&1
if errorlevel 1 (
  echo [ERROR] ssh が見つかりません（OpenSSH Client未導入）
  exit /b 3
)

REM ===== TMP 初期化 =====
if exist "%TMP%\" (
  echo [STEP] Cleanup TMP...
  rmdir /S /Q "%TMP%" >nul 2>&1
)
mkdir "%TMP%" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] TMP を作れません: "%TMP%"
  exit /b 4
)

REM ===== SRC -> TMP へコピー（除外あり）=====
REM 注意: .env は送らない（危険）
echo [STEP] Stage files to TMP (exclude .env, __pycache__, *.pyc, .git)...
robocopy "%SRC%" "%TMP%" /E ^
  /XD ".git" "__pycache__" ^
  /XF ".env" "*.pyc" >nul
set "RC=%ERRORLEVEL%"
if %RC% GEQ 8 (
  echo [ERROR] robocopy 失敗 errorlevel=%RC%
  exit /b 10
)

REM ===== アップロード（中身だけ）=====
echo [STEP] Uploading to server...
scp %SSHOPTS% -r "%TMP%\*" %USER%@%HOST%:%DEST%/
if errorlevel 1 (
  echo [ERROR] scp 失敗（鍵認証が未設定だと BatchMode で失敗します）
  exit /b 20
)

REM ===== TMP掃除 =====
rmdir /S /Q "%TMP%" >nul 2>&1

echo [OK] Upload

pause

exit /b 0


