# N5105の8091番でのSocket.IO分離検証

2026-11-06 22:09 JST、対象コミット `a2c52f5681f743c88bcd7d605839761a708c0f0c`（[PR #5](https://github.com/iorin0624/MFU/pull/5)）を `/opt/mfu-socketio-stage/app` に新規checkoutし、`mfu-socketio-stage.service` を起動した。8091番は **127.0.0.1限定**。本番8080番、PiのApache/443番、DNSやファイアウォールは変更していない。自動起動はまだ有効にしていない。

実機ではRedis疎通、`/socketio-health` 200、通常HTTP `/admin` 404、Engine.IO polling 200、WebSocket/Pollingでの `/download-progress` 接続・切断、匿名 `/admin-system`・`/media-clipboard`・`/media-hub-notifications`・`/photo-relay` 接続拒否、不正Origin 400を確認した。別のRedisクライアントから `flask-socketio` チャンネルへ一意な接続SID宛てに送った検証イベントが、8091番のWebSocketクライアントへ届いた。本番8080番は引き続き応答し、`mfu`、`redis-server` も稼働中。初回確認時のDB `Threads_connected` は83、専用サービスのワーカーRSSは約144 MB、再起動回数0。実ユーザーの認証済み接続、実際の8080番ワーカーからの送信、Pi経由の公開WebSocketは未検証。

## 調査済みの前提

- `/mnt/mfu` は本番データと未追跡ファイルを多数含む、コミット履歴のないGitディレクトリ。`git pull`、`git add .`、`git reset`、`git clean` を使わない。
- 本番アプリは `/mnt/mfu/app`、依存venvは `/mnt/mfu/venv`、`config.py` は `/mnt/mfu/config.py`。アプリの秘密設定は `mfu` ユーザーだけが読める `/mnt/mfu/app/.env` にある。検証用Git checkoutに秘密設定をコピーしない。
- `/mnt/mfu/venv` には Flask-SocketIO、simple-websocket、redis、gunicorn が存在。RedisはPONG、8091は未使用。8080の本番サービスはそのまま維持する。
- 本番の `__init__.py` と `chat/__init__.py` は改行コードを除けば現行GitHub mainと一致し、`chat/socketio_ext.py`、DB、通知、写真転送、セッション失効の主要ファイルはGit blobも一致した。専用プロセスの新イベント形式と本番8080番のRedis連携は、実機で別途確認する。

## 実施した初期配置（16スレッド・ループバック限定）

作業前に本番8080、Redis、バックアップジョブ、空き容量を確認した。以下の `<COMMIT>` は上記コミットID。`/opt/mfu-socketio-stage/app` は新規checkoutとして作成し、既存本番ディレクトリには触れていない。再実行時は既存の検証ディレクトリを上書きしないこと。

```sh
ssh server-103-16 'ss -ltn "( sport = :8080 or sport = :8091 )"; redis-cli ping; systemctl is-active mfu redis-server'
ssh server-103-16 'test ! -e /opt/mfu-socketio-stage && echo stage_path_available'
# 以下は2026-11-06の明示的な起動承認を受けて実施済み。再実行には状態確認が必要。
ssh server-103-16 'install -d -o mfu -g mfu -m 0750 /opt/mfu-socketio-stage'
ssh server-103-16 'sudo -u mfu git clone --no-checkout --single-branch --branch codex/socketio-dedicated-service https://github.com/iorin0624/MFU.git /opt/mfu-socketio-stage/app'
ssh server-103-16 'sudo -u mfu git -C /opt/mfu-socketio-stage/app checkout --detach <COMMIT>'
ssh server-103-16 'install -d -o root -g mfu -m 0750 /etc/mfu'
# /etc/mfu/socketio-stage.env と /etc/systemd/system/mfu-socketio-stage.service は
# deploy/socketio/ の例をレビューし、root 所有（前者は root:mfu 0640）で配置済み。
ssh server-103-16 'systemctl daemon-reload && systemctl start mfu-socketio-stage'
ssh server-103-16 'curl -fsS http://127.0.0.1:8091/socketio-health'
ssh server-103-16 'curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8091/admin'
ssh server-103-16 'curl -sS "http://127.0.0.1:8091/socket.io/?EIO=4&transport=polling" | head -c 120'
```

GitHubが非公開で `mfu` ユーザーに読み取り権限がない場合、上のcloneを無理に進めない。サーバーへGitHub認証情報を恒久配置せず、作業端末で対象コミットを固定したハッシュ確認済みの配備アーカイブを作成し、承認済みの安全な転送経路で**新規**検証ディレクトリへ展開する方式に切り替える。作業前に手順とファイル一覧をレビューする。

`/opt/mfu-socketio-stage/app` のソースは本番の `/mnt/mfu/app` とは別にする。`PYTHONPATH=/opt/mfu-socketio-stage:/mnt/mfu` により `app` は新checkout、`config` は既存の `/mnt/mfu/config.py` を読む。秘密設定はsystemd `EnvironmentFile=/mnt/mfu/app/.env` で参照するためGit checkout内に残らない。ただし本番DB・Redisを共有するので、アプリ起動時や接続時の副作用は段階検証し、**実ユーザー接続や負荷試験は別途承認まで行わない**。検証環境でのポート8091は127.0.0.1にしかbindしない。Pi/Nginxから接続する段階で初めて、接続元限定のLAN bind・ファイアウォールを検討する。

## 起動後に確認する順番

1. `systemctl status mfu-socketio-stage`、`journalctl -u mfu-socketio-stage -n 100 --no-pager` に秘密が出ていないか。`ss -ltn` が127.0.0.1:8091だけを示すか。
2. `/socketio-health` が200、通常ページ `/admin` が404、Engine.IO pollingが `0{...}` を返すか。Redisを**止めずに**、接続設定を誤らせた検証プロセスだけで起動拒否を試す。
3. SSHトンネルで限定テストアカウントからWebSocket/Pollingの接続・切断と認証拒否を試す。管理者、外部ユーザー、Windowsクライアントの各名前空間をテストデータで確認する。
4. HTTP側8080からRedis経由で8091のテスト用ルームにメッセージが届くか。通知・チャットの実データを使わない。汎用 `python-socketio` のRedisManagerを検証送信側に使う場合、既定チャンネル `socketio` ではなく `channel="flask-socketio"` を指定すること。
5. `Threads_connected`、CPU/RSS、既存8080の応答、Redis latencyを比較する。50人以上の負荷試験は隔離環境と追加承認が整うまで実行しない。

停止・取り下げは `systemctl stop mfu-socketio-stage`。本番8080、Apache、Piの443には触れていないため、初期検証のロールバックはこの停止のみで済む。配置したcheckoutや設定は記録を残してから削除判断する（自動削除しない）。
