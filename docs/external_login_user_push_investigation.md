# external_login_user の Push 通知構造・問題点 調査メモ

## 1. 全体構造（どこで何をしているか）

### 1-1. クライアント（external_login_user 側）
- ベーステンプレート `base_extlogin.html` で Service Worker を `/sw.js`（scope=`/`）に登録している。
- 旧 scope (`/external-login/`, `/chat/`) の Service Worker をアンレジストしてから再登録する移行処理を持つ。
- `ext_index.html` の「プッシュ通知の有効」ボタンで、
  - `/chat/api/push/bootstrap` から CSRF と VAPID 公開鍵を取得
  - `PushManager.subscribe(...)`
  - `/chat/api/push/subscribe` へ endpoint/keys/sw_scope を送信
  - 無効化時は `/chat/api/push/unsubscribe` を呼ぶ

### 1-2. Service Worker
- `static/sw.js` が Push イベントを受け取り、`title/body/url` を使って通知表示する。
- 通知クリック時は `data.url` に遷移（同一オリジン制限あり）。
- Push受信時に既存タブへ `postMessage`（`MFU_PUSH_NAV`）し、画面側で localStorage に保留遷移情報を書き、可視化タイミングで遷移する実装。

### 1-3. サーバー（chat API 側）
- Push購読情報は `chat_push_subscriptions` に保存（`actor_type`,`actor_id`,`endpoint_hash` をユニーク）。
- `/chat/api/push/bootstrap` はログイン済み actor に対して `csrf_token`,`vapid_public_key`,`sw_url` を返す。
- `/chat/api/push/subscribe` は endpoint/p256dh/auth を保存（actor単位）。
- `/chat/api/push/unsubscribe` は endpoint_hash で削除。
- 実際の送信は `_send_push_to_actor()` で `pywebpush` を使う。
  - `CHAT_VAPID_PUBLIC_KEY / PRIVATE_KEY` 必須
  - 429/5xx はリトライ
  - 404/410 は購読削除

### 1-4. external_login_user への通知生成
- external_login_user は chat actor として `actor_type='line'`, `actor_id=<ext_user_id>` で扱われる。
- チャットメッセージ配信時、Push送信に加えて `create_notification_external()` で `mfu_notifications` へ通知を保存（通知一覧画面のデータソース）。
- つまり「Web Push（即時通知）」と「DB通知（通知一覧）」の二層構造。

---

## 2. 問題点・リスク（現状コードから確認できたもの）

### 問題1: broadcast 送信のURL生成で未定義変数を参照
- `broadcast_push()` の payload URL 生成で `effective_room_id` を使っているが、この関数内で定義されていない。
- 実行時に `NameError` となり、external_login_user 向け含む Push ブロードキャストが失敗する可能性が高い。

### 問題2: Push無効時のフォールバックが画面ごとに分散
- Push通知自体が届かない場合でも DB通知は残る設計だが、通知検知（未読更新反映）は Socket.IO / polling 依存。
- Push有効化導線が `ext_index.html` 依存で、他画面からの初回導線が弱い。
- ユーザー体験として「Pushは来ないが通知一覧にはある」状態が発生しやすい。

### 問題3: Service Worker 登録処理が複数箇所に重複
- `base_extlogin.html` と `ext_index.html` の双方に「旧scope解除→`/sw.js`登録」ロジックがあり、保守時の差分混入リスクがある。

### 問題4: 通知一覧APIの可視性判定が重い
- `api_notifications_list()` はページ取得後に可視性フィルタし、さらに total 計算のため全件再走査している。
- 通知件数が増えると external_login_user の通知画面表示が遅くなる可能性がある。

### 問題5: VAPID未設定時のサイレント機能停止
- `_send_push_to_actor()` は鍵未設定時に `0` を返して終了するため、運用上は「Pushが飛ばないがエラーに見えにくい」状態になりうる。
- UI側にはアラートがあるが、サーバー運用者向けの明示的ヘルスシグナルが薄い。

---

## 3. まとめ
- external_login_user 向け Push は **chat基盤を共用**しており、
  1) ブラウザPush（`chat_push_subscriptions` + `pywebpush`）
  2) アプリ内通知（`mfu_notifications`）
  の二段構成。
- 致命度が高いのは `broadcast_push()` の未定義変数参照。
- 次点で、SW登録ロジック重複・通知一覧APIの走査コスト・VAPID未設定時の可観測性不足が運用課題。
