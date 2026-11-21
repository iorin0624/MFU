# iOS Share Extension 実装（JPEG保存専用）

## プロジェクト構成案

```
ios/JpegShareSample
├─ HostApp
│  └─ Info.plist
└─ ShareExtension
   ├─ Info.plist
   ├─ ShareViewController.swift
   ├─ URLExtractor.swift
   ├─ JpegDownloader.swift
   └─ PhotoSaver.swift
```

- **HostApp**: 署名・配布用の親アプリ。写真追加権限の文言を保持します。
- **ShareExtension**: Safari の共有シートから起動し、URL解釈→JPEG判定→保存を行います。

## 実装ポイント

- 受け取りタイプは `public.url` / `public.plain-text` のみ。
- URLSession で GET し、以下の判定を実施。
  - `HTTP 200` かつ `Content-Type=image/jpeg` または `image/jpg`、かつ先頭バイト `FF D8`。
  - `401/403/410` は期限切れ/権限なしエラー。
  - `Content-Type=text/html` は閲覧ページURLエラー。
  - それ以外は汎用エラー。
- いったん一時ファイルに保存し、PhotoKit の `creationRequestForAssetFromImage` で保存。
- 保存後に一時ファイルを削除。
- UIは1画面（URL短縮表示、保存ボタン、進捗、結果メッセージ）。

## 導入手順（Xcode）

1. iOS App プロジェクトを作成。
2. Share Extension ターゲットを追加。
3. 本ディレクトリ内の Swift ファイルを Share Extension ターゲットへ追加。
4. `HostApp/Info.plist` の `NSPhotoLibraryAddUsageDescription` を親アプリの Info.plist に反映。
5. `ShareExtension/Info.plist` の `NSExtension` 設定を拡張ターゲットの Info.plist に反映。
6. Share Extension の Principal Class に `ShareViewController` を指定。

## 動作確認手順

1. Safariで公開JPEG URLを開く。
2. 共有シートから本拡張を選択。
3. URL表示を確認し「写真に保存」をタップ。
4. 「ダウンロード中」→「保存中」→「保存しました」が表示され、写真アプリに追加されることを確認。
5. `text/html` を返す閲覧ページURLを共有した場合、
   「閲覧ページURLです。保存用URLが必要です」が表示されることを確認。
6. 期限切れリンク等（401/403/410）で「期限切れ/権限なし」を確認。
