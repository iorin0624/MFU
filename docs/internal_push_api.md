# Internal Push API

## Python からの利用例

```python
from app.utils.push import send_push

send_push(
    recipient_type="external_user_id",
    recipient_value=123,
    title="写真アップロード完了",
    body="アルバムに新しい写真が追加されました。",
    target_url="/albums/123",
    kind="album_upload_complete",
    sender_label="アルバム機能",
    dedup_key="album:123:upload_complete:user:123",
)
```

## curl 例

```bash
curl -X POST 'https://mfu.iori0624.jp/api/internal/push/send' \
  -H 'Content-Type: application/json' \
  -H 'X-MFU-Internal-Key: YOUR_KEY' \
  --data '{
    "recipient_type":"external_user_id",
    "recipient_value":123,
    "title":"写真アップロード完了",
    "body":"アルバムに新しい写真が追加されました。",
    "target_url":"/albums/123",
    "kind":"album_upload_complete",
    "sender_label":"アルバム機能",
    "dedup_key":"album:123:upload_complete:user:123",
    "create_in_app":true,
    "send_web_push":true
  }'
```

## リクエスト仕様

- `recipient_type`: 必須。`external_user_id` / `mfu_username` のみ。
- `recipient_value`: 必須。`external_user_id` は整数、`mfu_username` は文字列。
- `title`: 必須。
- `body`: 任意。`null` は空文字へ正規化。
- `target_url`: 必須。`/` 始まりの内部 URL のみ。
- `kind`: 任意。未指定時は `general`。
- `sender_label`: 任意。
- `dedup_key`: 必須。191 文字以内。
- `room_type`, `room_id`, `event_id`, `chat_event_id`, `chat_room_id`: 任意。
- `create_in_app`: 任意。既定値 `true`。
- `send_web_push`: 任意。既定値 `true`。

## レスポンス例

### 作成成功 + Push 送信成功

```json
{
  "ok": true,
  "created": true,
  "duplicate": false,
  "notification_id": 123,
  "delivery": {
    "in_app": "created",
    "web_push": "sent"
  }
}
```

### dedup_key 重複

```json
{
  "ok": true,
  "created": false,
  "duplicate": true,
  "notification_id": null,
  "delivery": {
    "in_app": "duplicate",
    "web_push": "skipped"
  }
}
```

## 手動確認の最小手順

1. `MFU_INTERNAL_API_KEY` を設定してアプリを起動する。
2. ブラウザで対象ユーザーの Push 購読を有効化する。
3. 上記 curl 例で通知を送る。
4. `/notifications` または `/mfu-notifications` で通知が見えることを確認する。
5. 同じ `dedup_key` で再送し、二重作成されないことを確認する。
6. `mfu_notification_deliveries` に `sent` / `failed` / `duplicate` / `skipped` が記録されることを確認する。
