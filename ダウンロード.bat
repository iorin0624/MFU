@echo off
setlocal

REM =========================
REM 設定
REM =========================
set "HOST=192.168.103.16"
set "USER=root"
set "REMOTE_DIR=/mnt/mfu/app"

REM 目的地（日本語パスOK：コピー/移動はrobocopyが担当）
set "DEST=Y:\01マイドキュメント\GitHub\MFU"

REM 一時作業場所（必ずASCIIだけにする）
set "TMP=C:\mfu_tmp"

echo [INFO] HOST=%HOST% USER=%USER%
echo [INFO] REMOTE_DIR=%REMOTE_DIR%
echo [INFO] DEST="%DEST%"
echo [INFO] TMP ="%TMP%"

REM =========================
REM 事前チェック
REM =========================
if not exist "%DEST%\" (
  echo [ERROR] DEST が存在しません: "%DEST%"
  exit /b 1
)

where scp >nul 2>&1
if errorlevel 1 (
  echo [ERROR] scp が見つかりません（OpenSSH Client未導入）
  exit /b 2
)

REM TMP 初期化
if exist "%TMP%\" (
  echo [STEP] Cleanup TMP...
  rmdir /S /Q "%TMP%" >nul 2>&1
)
mkdir "%TMP%" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] TMP を作れません: "%TMP%"
  exit /b 3
)

REM =========================
REM 1) scp で /mnt/mfu/app を TMP に丸ごとDL
REM =========================
echo [STEP] Downloading remote app to TMP...
scp -r %USER%@%HOST%:%REMOTE_DIR% "%TMP%"
if errorlevel 1 (
  echo [ERROR] scp 失敗
  exit /b 10
)

if not exist "%TMP%\app\" (
  echo [ERROR] DL後に "%TMP%\app" が見つかりません
  exit /b 11
)

REM =========================
REM 2) app の中身だけを DEST 直下へ展開（MOVE）
REM =========================
echo [STEP] Flatten to DEST (move)...
robocopy "%TMP%\app" "%DEST%" /E /MOVE >nul
set "RC=%ERRORLEVEL%"
if %RC% GEQ 8 (
  echo [ERROR] robocopy 失敗 errorlevel=%RC%
  exit /b 12
)

REM TMP掃除
rmdir /S /Q "%TMP%" >nul 2>&1

echo [OK] 完了：/mnt/mfu/app の中身を "%DEST%" 直下へ展開しました
echo [DONE]
exit /b 0
