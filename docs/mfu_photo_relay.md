# MFU写真リアルタイム転送

## 構成

1. iPhoneショートカットが写真をHTTPSでMFUへアップロードする。
2. MFUはJPEG・PNGを保持し、HEIC・HEIFをJPEG（品質95）へ変換する。
3. MFUが`/photo-relay`名前空間のWebSocketへ到着イベントを送る。
4. Windows常駐アプリが認証付きHTTPSで本体を取得し、SHA-256とサイズを検証して指定フォルダーへ原子的に保存する。
5. WindowsのACK後、MFUは一時ファイルを削除する。オフライン時は7日間キューに保持する。

## ショートカットAPI

すべて`Authorization: Bearer mfu_up_...`を使用する。`ios_shortcut_upload`スコープだけを許可する。

撮影日時とファイル更新日時は、ショートカット内でISO 8601形式へ整形して送信する。

- `GET /api/photo-relay/v1/devices`
- `POST /api/photo-relay/v1/jobs`
- `POST /api/photo-relay/v1/files`
- `POST /api/photo-relay/v1/jobs/<job_uuid>/done`
- `GET /api/photo-relay/v1/jobs/<job_uuid>`

## Windows認証

Windowsアプリは`/desktop/photo-relay/login/start`をChromeで開き、`127.0.0.1`の一時コールバックで専用トークンを受け取る。トークンはWindows DPAPIで暗号化して保存され、ログアウトまたは端末登録解除でサーバー側も失効する。

本番ではMFUへログイン後、`/desktop/photo-relay/download/windows`からビルド済みZIPを取得できる。
