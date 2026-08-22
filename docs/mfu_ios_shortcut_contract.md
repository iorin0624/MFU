# MFU写真保存ショートカット API仕様

ショートカット名は `MFU写真保存`、保存先アルバム名は `MFU` とする。

## 起動入力

MFUの「SCでDL」ボタンは、次のURLスキームでショートカットを起動する。

```text
shortcuts://run-shortcut?name=MFU写真保存&input=text&text=<URLエンコード済み起動URL>
```

ショートカット入力は次の形式の起動URLとなる。

```text
https://mfu.iori0624.jp/mobile-download/open/mfu_launch_<one-time-token>
```

## ショートカット処理

1. ショートカット入力のURLから、末尾の `mfu_launch_...` を取得する。
2. `POST https://mfu.iori0624.jp/mobile-download/api/exchange` をJSONで呼ぶ。

```json
{
  "launch_token": "mfu_launch_...",
  "platform": "ios_shortcut"
}
```

3. 応答の `access_token` と `manifest.files` を保持する。
4. `manifest.files` を先頭から順番に反復する。
5. 各 `download_url` を、HTTPヘッダー `Authorization: Bearer <access_token>` 付きで取得する。
6. 取得した写真・動画を、そのまま「写真アルバムに保存」アクションで `MFU` アルバムへ保存する。
7. 全画像の処理後、次を呼ぶ。

```text
POST https://mfu.iori0624.jp/mobile-download/api/jobs/<job_id>/complete
Authorization: Bearer <access_token>
Content-Type: application/json

{}
```

8. `○枚をMFUアルバムへ保存しました` と結果を表示する。途中失敗時は成功件数と失敗件数を表示する。

## 有効期限と制約

- 起動トークンは10分、一度だけ交換可能。
- 交換後のアクセストークンは1時間有効。
- 1ジョブ最大1,000枚。
- JPG/JPEGは原本を返す。
- PNG/HEIC/HEIFはサーバーでJPEGへ変換して返す。
- アルバム動画は変換済みMP4を優先し、未変換時はMP4/MOV/M4V原本を返す。
- ZIP、RAW、WebMは対象外。
- アップロード側で非公開になった写真は取得できない。
- イベント連携アルバムは、各画像取得時にも退会状態と参加承認状態を再確認する。
