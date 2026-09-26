# MFU写真転送（Windows）

iPhoneショートカットからMFUへ送信された写真を、WebSocket通知を使ってWindowsの指定フォルダーへリアルタイム保存する常駐アプリです。

## ログイン

1. `MFUPhotoRelay.exe` を起動します。
2. タスクトレイの「MFUへログイン」を選びます。
3. ChromeでMFUへログインし、「MFU写真転送を許可しますか？」で許可します。
4. アプリは`127.0.0.1`の一時コールバックで専用トークンだけを受け取ります。

ChromeのCookieやMFUのパスワードはアプリへ保存しません。専用トークンはWindows DPAPIで暗号化して保存します。

## 保存設定

- そのままの名前
- `yyyy-mm-dd_HHmmss`（EXIF撮影日時、送信された更新日時、受信日時の順）
- 連番（初期値5桁、開始番号設定可）

JPEG・PNGはそのまま転送されます。HEIC・HEIFはMFUサーバーでJPEGへ変換されます。

WebSocket通知を取り逃した場合や一時的に保存できなかった場合も、30秒ごとにサーバーの未受信キューを再確認して自動的に再取得します。

## ビルド

```bat
tools\mfu_photo_relay\build\build_windows.bat
```

出力先：`tools\mfu_photo_relay\dist\MFUPhotoRelay\MFUPhotoRelay.exe`

対象PCとの互換性を優先し、ビルド環境はPython 3.10・PySide6 6.8.3へ固定しています。ビルド時にはQtと同じVisual C++ランタイムを配布フォルダー直下へ揃えます。
