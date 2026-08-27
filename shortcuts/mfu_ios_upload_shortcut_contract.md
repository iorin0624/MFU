# MFU写真アップロード ショートカット仕様

## サーバーAPI

すべてHTTPSで、次のヘッダーを送る。

```http
Authorization: Bearer mfu_up_...
```

APIキーはMFUの `/admin/ios-shortcut-upload` で端末ごとに発行する。

### 1. テンプレート一覧

```http
GET /api/ios-upload/v1/config
```

`default_mode` がサーバー既定値、`modes` が選択肢となる。画面には `label`、アップロード時には `mode` を使用する。

### 2. アップロード枠作成

```http
POST /api/ios-upload/v1/create
Content-Type: application/json

{
  "title": "撮影タイトル",
  "date": "20260827",
  "mode": "選択したmode"
}
```

### 3. 写真送信

写真ごとに繰り返す。

```http
POST /api/ios-upload/v1/original
Content-Type: multipart/form-data

uuid=<createで返されたuuid>
file=<写真ファイル>
```

HEIC/HEIFはサーバー側で内容を判定し、安全なJPEGへ変換して保存する。応答の `converted_to_jpeg` で変換有無が分かる。

### 4. 完了

```http
POST /api/ios-upload/v1/done
Content-Type: application/json

{"uuid": "作成済みuuid"}
```

応答例:

```json
{
  "ok": true,
  "completion_url": "https://mfu.iori0624.jp/upload/done/...",
  "view_url": "https://mfu.iori0624.jp/view/...",
  "uploaded_count": 12,
  "message": "テンプレートから生成された案内文",
  "notification_started": true
}
```

`message` をクリップボードへコピーし、「結果を表示」で利用者へ見せる。完了APIを再送してもiPhone用アップロードの通知は重複送信しない。

## ショートカットのアクション構成

1. 共有シートから「画像」と「メディア」を受け取る。
2. 入力が空なら「写真を選択」で複数選択する。
3. `テキスト` アクションへ端末専用APIキーを設定する。
4. `入力を要求` で撮影タイトルを入力する。
5. `現在の日付` を `yyyyMMdd` で整形し、日付入力の初期値にする。
6. 日付を `入力を要求` で確認し、8桁の数字でなければ中止する。
7. config APIを呼び、`modes` を「リストから選択」する。`default_mode` と同じ項目を先頭へ並べる。
8. create APIを呼び、返された `uuid` を保持する。
9. ショートカット入力の写真を順番に繰り返し、original APIへフォーム送信する。
10. done APIを呼ぶ。
11. 応答の `message` を「クリップボードにコピー」する。
12. アップロード枚数・閲覧URL・案内文を「結果を表示」で表示する。

APIキーを埋め込んだショートカットは他人へ共有しない。別の端末には別のキーを発行する。
