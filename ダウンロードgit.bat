@echo off
setlocal enabledelayedexpansion

REM =========================
REM 設定
REM =========================
set "HOST=192.168.103.16"
set "USER=root"
set "REMOTE_DIR=/mnt/mfu/app"

REM Git作業フォルダ（ここがGitリポジトリであること）
set "DEST=Y:\01マイドキュメント\GitHub\MFU.2"

REM scp一時作業（ASCIIのみ）
set "TMP=C:\mfu_tmp_dl"

REM Gitコミットメッセージ
set "MSG=sync from 103.16 (/mnt/mfu/app)"

REM SSH/SCPオプション（初回はknown_hosts登録、以後無言）
set "SSHOPTS=-o StrictHostKeyChecking=accept-new"

echo [INFO] HOST=%HOST% USER=%USER%
echo [INFO] REMOTE_DIR=%REMOTE_DIR%
echo [INFO] DEST="%DEST%"
echo [INFO] TMP ="%TMP%"
echo.

REM =========================
REM 事前チェック
REM =========================
if not exist "%DEST%" (
  echo [ERROR] DEST が存在しません: "%DEST%"
  goto FAIL
)

where scp >nul 2>&1 || (echo [ERROR] scp がありません & goto FAIL)
where ssh >nul 2>&1 || (echo [ERROR] ssh がありません & goto FAIL)
where git >nul 2>&1 || (echo [ERROR] git がありません（Git for Windows入れて） & goto FAIL)

REM Gitリポジトリ確認
if not exist "%DEST%\.git" (
  echo [ERROR] "%DEST%" はGitリポジトリではありません（.git が無い）
  goto FAIL
)

REM =========================
REM 1) TMP 初期化
REM =========================
if exist "%TMP%\" rmdir /S /Q "%TMP%" >nul 2>&1
mkdir "%TMP%" >nul 2>&1 || (echo [ERROR] TMP作成失敗 & goto FAIL)

REM =========================
REM 2) scp で /mnt/mfu/app をTMPへDL
REM =========================
echo [STEP] Downloading to TMP...
scp %SSHOPTS% -r %USER%@%HOST%:%REMOTE_DIR% "%TMP%"
if errorlevel 1 (
  echo [ERROR] scp 失敗
  goto FAIL
)

if not exist "%TMP%\app\" (
  echo [ERROR] DL後に "%TMP%\app" が見つかりません
  goto FAIL
)

REM =========================
REM 3) app中身をDEST直下へ展開（MOVE）
REM =========================
echo [STEP] Flatten into DEST...
robocopy "%TMP%\app" "%DEST%" /E /MOVE >nul
set "RC=%ERRORLEVEL%"
if %RC% GEQ 8 (
  echo [ERROR] robocopy 失敗 errorlevel=%RC%
  goto FAIL
)

REM TMP掃除
rmdir /S /Q "%TMP%" >nul 2>&1

REM =========================
REM 4) Git add/commit/push
REM =========================
echo [STEP] Git add/commit/push...
pushd "%DEST%"

REM 念のため .env などを誤コミットしない（既に.gitignoreで管理推奨）
REM ここでは強制除外はしない。必要なら指示して。

git status --porcelain >nul 2>&1
if errorlevel 1 (
  echo [ERROR] git status 失敗（リポジトリ壊れ/権限/パス）
  popd
  goto FAIL
)

REM 変更をステージ
git add -A
if errorlevel 1 (
  echo [ERROR] git add 失敗
  popd
  goto FAIL
)

REM 変更が無ければコミットしない
for /f %%A in ('git status --porcelain') do set "HAVECHG=1"
if not defined HAVECHG (
  echo [INFO] 変更なし：commit/pushはスキップ
  popd
  goto OK
)

git commit -m "%MSG%"
if errorlevel 1 (
  echo [ERROR] git commit 失敗（user.name/email未設定や認証が原因のこと多い）
  popd
  goto FAIL
)

git push
if errorlevel 1 (
  echo [ERROR] git push 失敗（GitHub認証/権限/リモート設定確認）
  popd
  goto FAIL
)

popd
goto OK

:OK
echo.
echo [OK] Download -> Flatten -> GitHub push 完了
echo.
pause
exit /b 0

:FAIL
echo.
echo [FAIL] 途中で失敗しました。上のエラー行が原因です。
echo.
pause
exit /b 1
