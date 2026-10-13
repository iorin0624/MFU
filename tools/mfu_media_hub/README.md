# MFU Media Hub

`MFU写真転送` と `MFU Media Clipboard` を一つのWindows常駐アプリに統合したクライアントです。

- 写真転送の永続キューとACK・30秒再同期を維持
- Media Clipboardの取得・保存指示と進捗をWebSocketで送受信
- 取得中枚数を進捗ダイアログとトレイのツールチップに表示
- 写真保存通知のクリックでExplorerを開き、保存したファイルを選択
- 通知センターは未読数だけ15秒ごとに取得し、トレイからWeb版を開く（Windows内の通知一覧・ポップアップは廃止）
- 旧アプリの設定・端末UUID・トークンを初回起動時に移行
- 統合後のトークンはWindows DPAPIで暗号化

## ビルド

```powershell
python -m pip install -r requirements.txt
.\build.ps1
```

QtのDLLを確実に同梱するため、配布形式は`onedir`です。EXE単体ではなく、出力フォルダーごと配布してください。
