# MFU Chat Desktop

MFU Chat Desktop は、既存MFUチャットをWindows向けネイティブGUIで利用するためのクライアントです。既存 `/chat` 画面をWebViewで丸ごと表示せず、GUI用JSON API、既存HTTP API、既存Socket.IOイベントで通信します。

## 通常利用

通常利用者は `.py` を直接実行せず、ビルド済みの `MFUChatDesktop.exe` をダブルクリックして起動します。

初回起動後、MFUの既存アカウントでログインしてください。パスワード保存を選択した場合のみ、Windows資格情報マネージャー経由で保存します。

## Windows exe化

1. Python 3.11以上をインストール
2. `tools\mfu_chat_desktop\build\build_windows.bat` を実行
3. `dist\MFUChatDesktop\MFUChatDesktop.exe` を起動

ビルド設定は `build\mfu_chat_desktop.spec` です。コンソールは非表示、`resources/` と `.env.example` は同梱、`.env` は同梱しません。

## 開発起動

```powershell
cd tools\mfu_chat_desktop
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python main.py
```

`.env` 例:

```env
MFU_BASE_URL=https://mfu.iori0624.jp
MFU_SOCKET_PATH=/socket.io
APP_NAME=MFU Chat Desktop
```

## Web版表示に寄せた方針

チャット本文表示エリアは、既存Web版 `room.html` のチャットDOM/CSSを参考に、PySide6のWidgetとQSSで再現しています。

- タイムライン背景はWeb版と同じ淡い水色系
- 自分のメッセージは右寄せ、青い吹き出し
- 相手のメッセージは左寄せ、薄いグレーの吹き出し
- 日付区切り、アバター、送信者名、時刻、編集済み、削除済みを表示
- 返信引用、画像サムネイル、複数画像グリッド、リアクションチップ、スレッド返信数を表示
- 「未読へ」「最新へ」ボタンと入力中表示を配置

## 既知の差異

- Windows GUI版では、Web版の一部CSSアニメーションは簡略化しています。
- スマホ向けタッチ操作、スワイプ操作は対象外です。
- 既読者の詳細ポップアップは簡略化しています。
- 未読位置ジャンプは現時点では最新位置への簡易ジャンプです。
- QWebEngineViewは使わず、ネイティブWidgetで再現しているため、HTML/CSS完全一致ではありません。

## トレイ収納

- 起動中はシステムトレイにもアイコンを表示します。
- 最小化するとメインウィンドウを非表示にし、タスクバーから消します。
- `×` ボタンでも終了せず、通知領域へ収納します。
- 復帰はトレイアイコンのダブルクリック、またはトレイメニューの「開く」から行います。
- 完全終了はトレイメニューの「終了」から行ってください。
- トレイ収納中もSocket.IO接続を維持し、新着通知を受け取ります。
- トレイ収納中はpresenceを表示中ではない状態で送信します。

## サーバーAPI

- `GET /chat/api/gui/session`
- `POST /chat/api/gui/login`
- `GET /chat/api/gui/bootstrap`
- `GET /chat/api/gui/events`
- `GET /chat/api/gui/events/<event_id>/snapshot`
- `GET /chat/api/gui/events/<event_id>/messages`
- `GET /chat/api/gui/events/<event_id>/search`
- `GET /chat/api/gui/events/<event_id>/rooms`
- `GET /chat/api/gui/dm/inbox`
- `GET /chat/api/gui/dm/<dm_uuid>/snapshot`
- `GET /chat/api/gui/dm/<dm_uuid>/messages`
- `GET /chat/api/gui/dm/<dm_uuid>/search`

アップロード、編集、削除、ルーム管理、メンバー、メンション、ミュート、presence、スレッド取得は既存APIを再利用します。

## トラブルシュート

- ログインできない: `MFU_BASE_URL`、認証情報、MFA有無を確認してください。
- Socket.IOが接続できない: `MFU_SOCKET_PATH=/socket.io` とApache/gunicorn側のSocket.IOプロキシ設定を確認してください。
- 画像送信に失敗する: 形式、20MB制限、最大6枚制限を確認してください。HEIC/HEIFはGUI側でブロックします。
- 通知が出ない: Windows通知設定とアプリ内の通知ON/OFFを確認してください。
