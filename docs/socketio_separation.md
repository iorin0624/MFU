# Socket.IO専用サービス分離（準備済み・本番未反映）

## 結論と現状

このリポジトリには8091番で動く単一ワーカーのSocket.IO専用起動口と、Nginx/Apache/systemdの**設定例**を追加した。本番の設定変更・サービス再起動・高負荷試験は行っていない。150/250接続はまだ合格判定できない。8080番を残したまま段階的に検証する。

調査時（2026-11-06）の構成は、Pi `.15` のApache prefork `:443` → MFU SE02 `.16:8080` のGunicorn gthread 8 worker × 8 threads。Socket.IOの全ワーカーはRedis `127.0.0.1:6379/0` を共有するが、従来コードはRedisに接続できないとローカル配信へ静かに戻り、プロセス間通知を失うおそれがあった。`.16:8091` は調査時未使用、SE02の既存venvには `simple_websocket` / `flask_socketio` / `gunicorn` / `redis` があった。PiにはNginxとApache `mod_remoteip` が未導入/未有効。PiのApacheはメール、INPA、SIP、FreePBX、MFU、その他複数の443仮想ホストを持つ。

### 移行対象と送信元

| 名前空間 | 接続・受信 | 主な送信元 |
| --- | --- | --- |
| `/` | チャット/DM、既読、入力中、外部イベント未読 | `chat/__init__.py`、`external_login_user/notifications.py`、`external_login_user/session_revocation.py`、`utils/realtime.py` |
| `/admin-system` | システム状態、ノード、タイマー、管理ジョブ、Uber | `__init__.py`、`utils/realtime.py` |
| `/download-progress` | ZIPとショートカットの進捗購読 | `__init__.py`、`utils/zip_stream.py`、`utils/mobile_download.py` の送信経路 |
| `/media-clipboard` | Windows Media Hubの取得要求 | `utils/media_hub.py` |
| `/media-hub-notifications` | Windows通知の配信・受領 | `utils/notification_service.py`、通知配信処理 |
| `/photo-relay` | iPhone→Windows写真転送と再同期 | `utils/photo_relay.py` |
| `/x-ff-admin`, `/x-ff-agent` | Xフォロー管理画面とWindows収集エージェント | `x_ff_remote.py` |

`/` の接続は外部ユーザーの有効状態とチャット権限を検証する。管理者名前空間はサーバー側セッション有効性を検証し、Windows名前空間はそれぞれ端末トークンを検証する。専用プロセスでも同じFlaskアプリと `SECRET_KEY` を使うため署名済みセッションCookieを読める。`ProxyFix` は1段の信頼済みプロキシを前提とする。8091をLANに無制限開放するとX-Forwarded系の偽装余地があるので、**Pi `.15` からの接続だけをファイアウォールで許可**する。

`chat/__init__.py` の `load_test_*` イベントは `MFU_SOCKETIO_DEDICATED=1` と `SOCKETIO_LOAD_TEST_TOKEN` の両方がある場合だけ有効。実ユーザーのチャット/通知に送らず、テスト専用ルームだけへ送る。通常本番ではトークンを設定しない。

## ワーカー、Redis、DB

初期候補は `gthread` 1 worker / 320 threads / `async_mode="threading"`。専用起動時は主要な起動時DDL/ナビゲーション初期化、blueprint登録時DDL、メンテナンススケジュールをスキップし、HTTP本体側だけで実行する。現行の同期MySQL接続、外部HTTP、ファイルI/O、バックグラウンドタスクを大きく変えない。`gevent` はスレッドより高密度になり得るが、monkey patch と既存同期処理の相互作用が未検証なので初版では採用しない。Pollingセッションを複数ワーカーへ分散するとEngine.IOセッションが失われ得るため、Redisがあっても専用サービスはまず1 worker。320 threadsは250+管理者/余裕用の**試験設定**であり、CPU/メモリ・スレッド数を負荷試験で調整する。

専用プロセスとHTTP側は同一Redis URL。専用プロセスはRedis不通時に起動失敗する。HTTP側も切替時には `SOCKETIO_QUEUE_REQUIRED=1` にして同様にfail-closedにする。稼働中のRedis断では既存接続が直ちに安全配信へ切り替わるわけではないため、ヘルスとログを監視し、必要なら旧経路へロールバックする。

DBプールは現行HTTP側が最大 `8 × 10 = 80`、専用側は `MYSQL_POOL_SIZE=5` で最大85接続。調査時MariaDB `max_connections=200`、`Threads_connected=83`、過去最大134だったため、単純加算なら過去最大+5=139、余裕61。ただし他サービスの増加、接続バースト、同時処理待ちを負荷試験で測る。上限200は変更しない。クライアントの通知フォールバック30秒周期は非表示タブでは停止済みだが、チャットの30秒回復・在室pingが同時接続数に比例する。50→100→150でDB QPS/Threads_running/待ち時間を見て、必要ならジッターとイベント駆動に変更する。

`utils/socket_connection_metrics.py` は物理Engine.IO接続を名前空間間で重複排除し、別途名前空間別の接続数、拒否数、クライアント申告の再接続数、WebSocket/Polling、Redis読取異常を返す。管理者トップの「リアルタイム接続」に主要3機能を表示する。Redis不通時のローカル集計は全体値ではない。拒否数はアプリ認証拒否のみで、Nginx/Engine.IOレベルの失敗は含まない。再接続数はポータルWebクライアントが申告した回数で、Windows等の全クライアントを網羅しない。

## 検証環境（本番443を触らない）

1. SE02でコードを配備後、`deploy/socketio/socketio.env.example` を実環境用に設定。既存 `/mnt/mfu/.env` の `SECRET_KEY` がHTTP側と同一であることを値を表示せず確認。通常は `SOCKETIO_CORS_ALLOWED_ORIGINS=https://mfu.iori0624.jp`。18443番のブラウザ検証中だけ `https://mfu.iori0624.jp:18443` も許可し、検証後に削除する。`SOCKETIO_LOAD_TEST_TOKEN` は検証時だけ追加する。
2. `mfu-socketio.service.example` を `/etc/systemd/system/mfu-socketio.service` に配置し、Redis健全性とポート空きを再確認。8091はPi `.15` のみ接続許可し、インターネット/他LAN端末から拒否。ファイアウォール変更は別途検証する。
3. `sudo systemctl daemon-reload && sudo systemctl start mfu-socketio`、`curl -fsS http://192.168.103.16:8091/socketio-health`、`curl -i http://192.168.103.16:8091/admin` がそれぞれ200/404となること。`curl 'http://192.168.103.16:8091/socket.io/?EIO=4&transport=polling'` でEngine.IO openパケットを確認（ログにトークンを出さない）。
4. PiへNginxの必要モジュール（`stream`、`ssl_preread`、`http_ssl`）を導入し、`nginx-socketio-common.conf.example` と `nginx-test.conf.example` を**ループバック18443だけ**で `nginx -t`。Apacheの443はそのまま。SSHトンネル越しにWebSocket/Polling、通常MFU、ログインCookie、全名前空間を確認する。
5. Redis経由のプロセス間配信を、テスト専用ルームに接続したクライアントへHTTPプロセス側から `socketio.emit('load_test_probe', {'sequence': '...'}, room='mfu-socketio-load-test')` して確認。チャット、DM、通知、管理画面、ZIP進捗、Media Hub、写真転送、Xフォローをテストデータで回帰する。ログアウト/無効化後の再接続拒否、Origin拒否も確認。
6. **本番では高負荷試験しない。** 隔離した検証環境で `tools/socketio_load_test.py` を50→100→150→200→250→300の順に実行する。1人1テストアカウントCookie、テスト専用トークンを使用する。例：`python tools/socketio_load_test.py --url http://127.0.0.1:8091 --clients 50 --cookie-file /secure/test-cookies.txt --token-file /secure/load-token.txt --staging-only`。最初はWebSocket、次に `--transport polling` も実行。Cookie/トークンは出力しない。CPU、RSS、Gunicorn threads、Redis ops/latency、MariaDB QPS/Threads_connected、Apache BusyWorkers、通常ページ/PHPメール管理/ダウンロード応答を同時採取する。一斉ログインはテストユーザーのセッション作成経路を別途測る。

150人の合格条件：認証済み接続/再接続とイベント配信が安定し、p95/p99が許容範囲、通常MFU・PHPメール管理・ダウンロードが運用上問題ない応答を維持し、Apache/DB枠を使い切らないこと。具体的な遅延閾値は試験前に決める。250は設計目標、300は限界確認で合格必須ではない。

## 最終443構成の前提と切替

Piの現在のApacheは10以上のHTTPS vhostを持ち、mail/FreePBXのIP制限がクライアント実IPに依存する。単純なNginxリバースプロキシを前に置くとApacheから見たIPがlocalhostになり、制限やログを壊す。**その構成では切り替えない。**

設定例はNginx `stream` がSNIだけを見て、MFUはNginx TLS `127.0.0.1:4443`、それ以外は既存Apacheの全vhostを `127.0.0.1:8443` へ転送する。両経路へPROXY protocolで元IPを渡す。Apache :8443 は `mod_remoteip` の `RemoteIPProxyProtocol On` を全vhostで有効にする。MFUの通常HTTPだけはNginxからApacheの **別ポート8444に複製したMFU vhost** へ転送し、そこだけ `RemoteIPHeader X-Forwarded-For` / `RemoteIPInternalProxy 127.0.0.1` を使う。これでApacheの元IPベースの `Require ip` を維持できる。Apache 8443/8444はloopback bind、80は既存のまま。`nginx-final-*` と `apache-8443-delta` は**完全な現用設定ではなく差分例**であり、現行vhostを1件ずつ複製・検証する必要がある。

切替前にApache全vhost/ports/certbot hookをバックアップし、`apache2ctl -S`、`apache2ctl configtest`、`nginx -t`、各ドメインのIP許可/403・証明書/HTTP2・メールPHP・INPA・FreePBX・MFUの正常応答を比較する。証明書は既存 `/etc/letsencrypt/live/iori0624.jp/` をMFU側Nginxでも読み込む。現在のcertbotは `iori0624.jp` がmanual、`inpa.mydns.jp` がwebroot、deploy hookはApache等を再読込するため、Nginxの証明書再読込hookを別途追加し、80番のwebrootを保持する。Nginxへの秘密鍵読取権限を最小限にする。

承認済み切替窓でのみ、準備済み設定を有効化し以下の順序を使う（実ファイル名は現場で確認）。

```sh
sudo apache2ctl configtest
sudo nginx -t
sudo systemctl stop apache2
# ここでバックアップ済みのApache設定を、全443 vhost→8443とMFU clone→8444へ切り替える。
sudo apache2ctl configtest && sudo systemctl start apache2
curl -kfsS --resolve mfu.iori0624.jp:8444:127.0.0.1 https://mfu.iori0624.jp:8444/ >/dev/null
sudo nginx -t && sudo systemctl start nginx
```

443番停止中は短い中断が発生する。切替後、SNIごとに各HTTPSサイトを確認し、外部IPと自宅許可IPの両方からFreePBX/メール管理の403/許可を検証する。`X-Forwarded-For` 偽装ヘッダーが許可を迂回できないことを検証する。Nginxの `/socket.io/` が8091へ行き、通常MFUがApache 8444へ行くこと、Redisイベントが届くことを確認する。

### ロールバック

旧8080サービスは常時残す。切替失敗時は**まずNginxの443を止め**、バックアップからApacheの443 vhost/Listen設定を復元し、`apache2ctl configtest` 後Apacheを起動する。専用8091を停止し、HTTP側に追加した `SOCKETIO_QUEUE_REQUIRED` 以外の動作変更を確認する。設定を編集した順序とバックアップパスを作業記録に残す。接続中のSocket.IOクライアントは切断されるが、既存クライアントの再接続により旧8080経路へ戻る。未送達のメッセージは自動再送保証がないため、通知/チャットの実データと配信ログを照合する。

```sh
sudo systemctl stop nginx
# バックアップしたApacheの443設定一式を元のパスへ復元する。
sudo apache2ctl configtest && sudo systemctl restart apache2
sudo systemctl stop mfu-socketio
```

## 未検証事項

WindowsローカルのPythonにはFlask等のアプリ依存がないため、今回実行できたのは依存不要の単体テスト、構文チェック、フロントエンド型チェックに限る。Redisを跨ぐ実配信、認証Cookie、各名前空間、Nginx設定構文、Apache PROXY protocol、証明書更新、実負荷、端末別再接続は**本番反映前の必須検証**。これらが通らない限り443を切り替えない。
