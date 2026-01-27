@echo off
setlocal

set "HOST=192.168.103.16"
set "USER=root"
set "SRC=Y:\01マイドキュメント\GitHub\MFU"
set "DEST=/mnt/mfu/app"

echo [INFO] Upload SRC="%SRC%" -> %USER%@%HOST%:%DEST%/

REM ===== 安全な存在確認（末尾の \ を付けない）=====
if not exist "%SRC%" (
  echo [ERROR] SRC が存在しません: "%SRC%"
  exit /b 1
)

REM ===== 送らない物（秘密/ゴミ）=====
REM .env を送らない（超重要）
REM __pycache__ や *.pyc を送らない
echo [STEP] Uploading...

scp -r ^
  -o BatchMode=yes ^
  -o StrictHostKeyChecking=accept-new ^
  "%SRC%\*" %USER%@%HOST%:%DEST%/

if errorlevel 1 (
  echo [ERROR] scp 失敗
  exit /b 2
)

echo [OK] Upload done
exit /b 0
