# MFU Media Clipboard

Instagramの投稿・ストーリー / Threads / X の URL をクリップボードから検出し、MFU Image Viewer に遠隔保存する Windows 常駐アプリです。

## 使い方

1. `main.py` または exe を起動します。
2. タスクトレイメニューの「ログイン」から MFU にログインします。
   - 普段使っている Chrome で認可画面を開きます。
   - Chrome でログイン後、「MFU Media Clipboard を許可しますか？」画面で許可します。
   - アプリは `127.0.0.1` の一時 callback で専用APIトークンを受け取ります。
3. Instagramの投稿・ストーリー / Threads / X の URL をコピーします。
4. タスクトレイ通知をクリックし、確認画面から取得します。

タスクトレイの「URLを指定して取得」では、Instagram / Threads / X のURLを改行区切りで最大20件まで指定できます。複数URLはサーバー負荷を抑えるため1件ずつ順番に取得し、各URLの選択・保存が終わると次へ進みます。クリップボード内の複数URLも同じように認識します。

画像は保存先フォルダー、開始番号、桁数を指定して保存できます。開始番号は Web 版と同じ API で自動取得します。動画は Web 版と同じ動画保存 API を使います。
「動画を写真で取得」では、動画の先頭付近のフレームをJPEGとして保存できます。

## 開発起動

```powershell
cd tools\mfu_media_clipboard
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

## Windows exe 化

```powershell
tools\mfu_media_clipboard\build\build_windows.bat
```

出力先:

```text
tools\mfu_media_clipboard\dist\MFUMediaClipboard\MFUMediaClipboard.exe
```

## 設定

MFU Media Clipboard 専用の API トークンを使います。MFU Chat Desktop や Chrome の Cookie はアプリに保存しません。

- base URL: `MFU_BASE_URL`、または既存設定の `base_url`
- 専用設定: `%APPDATA%\MFU\MFU Media Clipboard\settings.json`
- ログアウトするとサーバー側トークンも失効します。

Chrome 上でログインするため、既存のパスキーログインと通常ログインのフォールバックをそのまま利用できます。
