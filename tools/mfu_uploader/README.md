# MFU Uploader

Windows/macOS 共通の MFU アップローダーです。

- Version: `1.10.1`
- Release series: `v10`
- Windows/macOS は同じ `main.py` と `MFUUploader.spec` からビルドします。

## 概要

- Python / PySide6 製
- Chrome または既定ブラウザで MFU にログイン
- `127.0.0.1` の一時 callback で MFU Uploader 専用トークンを取得
- 固定 API key は使わない
- 設定保存先
  - Windows: `%APPDATA%\MFU\MFU Uploader\settings.json`
  - macOS: `~/Library/Application Support/MFU/MFU Uploader/settings.json`

## Build

Windows:

```bat
build_windows.bat
```

Output:

```text
dist_v10\MFUUploader\MFUUploader.exe
```

macOS:

```bash
chmod +x build_macos.sh
./build_macos.sh
```

Output:

```text
dist_v10/MFUUploader.app
```

タイトルバーとウィンドウ右下にアプリのバージョンが表示されます。
Windows のファイル情報と macOS のアプリ情報にも同じバージョンが設定されます。

## ImageMagick

JPEG/JPG の WebP サムネイルは、Windows/macOS ともアプリ内蔵の Qt 画像機能で生成します。
JPEG/JPG の送信に ImageMagick のインストールは必要ありません。

PNG・TIFF・HEIC など、JPEG/JPG 以外のサムネイル生成には `magick` を使用します。
アプリ内の `magick.exe` / `magick` 欄で変更できます。

自動探索候補:

- Windows: `C:\Program Files\ImageMagick-7.1.2-Q16\magick.exe`
- macOS: `/opt/homebrew/bin/magick`, `/usr/local/bin/magick`
- `PATH` 上の `magick`

## Upload Route

- `自動（LAN優先）`: アップロード開始前に LAN API へ認証付き接続確認を行い、接続できなければ公開URLへ切り替えます。
- `公開URL`: `API Base URL` を使用します。
- `LAN直接`: 専用LAN API（標準は `http://192.168.103.16:8081`）だけを使用し、接続できない場合は開始しません。

デフォルトの LAN API URL は `http://192.168.103.16:8081` です。
LAN API が HTTP の場合、信頼できる LAN 内での使用を前提とします。

## Upload Modes

### 一括アップロード

ファイル選択、または画面全体へのドラッグ&ドロップでファイルを追加してからアップロードします。
JPEG/JPG の原本送信中に WebP サムネイルを並行生成します。
原本は設定した送信並列数（デフォルト4、最大8）で送信します。
クライアントとサーバーで SHA-256 とファイルサイズを照合し、一時的な失敗は合計3回まで自動再送します。
同じ転送IDの再送はサーバーで重複登録しません。3回失敗後は同じアップロード枠へ失敗ファイルだけ再送できます。
完了後はWeb完了画面を開き、請求書アドレス帳から宛先を選択してテンプレート本文を送信できます。

### リアルタイム送信

監視フォルダと対象拡張子を指定して `監視開始` を押すと、1つのアップロード枠を作成します。
新しい対象ファイルが作成されるたび、同じUUIDへ順次アップロードします。

- 最初の1ファイル送信後だけ `/done` を送信して通知します。
- 監視停止時には `/done` を送信しません。
- 2ファイル目以降と監視停止時は、通知を発生させない専用APIで不足サムネイルのサーバー補完を確認します。
- 対象拡張子は `.jpg,.jpeg,.png` のようにカンマ区切りで複数指定できます。
- 監視中に画面へD&Dしたファイルやフォルダも、同じUUIDの送信キューへ追加されます。
