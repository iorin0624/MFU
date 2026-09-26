# MFU写真リアルタイム転送

## 構成

1. iPhoneショートカットが写真をHTTPSでMFUへアップロードする。
2. MFUはJPEG・PNGを保持し、HEIC・HEIFをJPEG（品質95）へ変換する。
3. 選択された全ファイルが揃うまでは`staged`としてサーバー内に保持し、Windowsへは通知しない。
4. iPhoneの完了通知と枚数照合後、MFUが`/photo-relay`名前空間のWebSocketへ一括して到着イベントを送る。
5. Windows常駐アプリが認証付きHTTPSで本体を取得し、SHA-256とサイズを検証して指定フォルダーへ原子的に保存する。
6. WindowsのACK後、MFUは一時ファイルを削除する。オフライン時は7日間キューに保持する。
7. Windowsアプリは30秒ごとに未受信キューを再確認し、WebSocket通知の取りこぼしや一時エラーを自動回復する。

## ショートカットAPI

すべて`Authorization: Bearer mfu_up_...`を使用する。`ios_shortcut_upload`スコープだけを許可する。

撮影日時とファイル更新日時は、ショートカット内でISO 8601形式へ整形して送信する。

- `GET /api/photo-relay/v1/devices`
- `POST /api/photo-relay/v1/jobs`
- `POST /api/photo-relay/v1/files`
- `POST /api/photo-relay/v1/jobs/<job_uuid>/done`
- `GET /api/photo-relay/v1/jobs/<job_uuid>`

ジョブ作成時にiPhoneで選択した枚数を`expected_file_count`として送信し、完了時にサーバー受信枚数と照合する。0枚または不足時は完了応答を返さない。

## Windows認証

Windowsアプリは`/desktop/photo-relay/login/start`をChromeで開き、`127.0.0.1`の一時コールバックで専用トークンを受け取る。トークンはWindows DPAPIで暗号化して保存され、ログアウトまたは端末登録解除でサーバー側も失効する。

本番ではMFUへログイン後、`/desktop/photo-relay/download/windows`からビルド済みZIPを取得できる。
