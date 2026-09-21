# INPA 基盤配置手順

この手順は、INPAの実装基盤を各サーバーへ配置するためのものです。初回Alembic revision、
登録・メール確認・ログイン機能を含む構成を前提にします。公開切替はSMTPとTurnstileの本番値を
設定し、結合テストが完了してから行います。

## 1. server-103-17: MySQL

1. MySQL管理者として `deploy/mysql/bootstrap.sql.example` を複製します。
2. `REPLACE_AT_DEPLOY` をGitに保存しない別々のランダムパスワードへ置き換えます。
3. `inpa_db`、`inpa_app`、`inpa_migrator` を作成します。
4. `SHOW GRANTS` で、各アカウントにMFU DBの権限がないことを確認します。
5. migration用秘密値をSE02の `/etc/inpa/migration.env` だけへ配置します。

`inpa_app` は通常のDMLだけ、`inpa_migrator` は `inpa_db` 限定のDDLとDMLだけを持ちます。

## 2. SE02 / server-103-16: アプリケーション

1. `/mnt/mfu/app/inpa` にGit checkoutを配置します。
2. Python 3.12以上で `backend/.venv` を作成し、`pip install -e '.[dev]'` を実行します。
3. `frontend/` で `pnpm install --frozen-lockfile` と `pnpm build` を実行します。ビルド時に
   `VITE_TURNSTILE_SITE_KEY` を設定し、公開用サイトキーだけを静的ファイルへ含めます。
4. rootで `deploy/install.sh` を実行します。
5. `/etc/inpa/runtime.env` をroot所有・`0640`で作成し、`inpa`グループだけに読み取りを許可します。
6. `deploy/install.sh` は `mfu`利用者を `inpa_admin` グループへ加えます。反映後、MFUプロセスを再起動します。
7. `inpa-app-firewall.service`、`inpa-web.service`、`inpa-admin-api.service`を有効化します。

メールworkerはDBキューを処理します。SMTP疎通を確認してから `/etc/inpa/enable-mail-worker` を
作成し、有効化します。

## 3. server-103-15: リバースプロキシ

1. `inpa.mydns.jp` 用のTLS VirtualHostを作成します。
2. HTTP-01検証パスを除きHTTPからHTTPSへ転送します。
3. Certbot webroot方式で `inpa.mydns.jp` の証明書を発行します。
4. `deploy/certbot/20-reload-apache` をCertbot deploy hookとして配置し、更新成功時にApacheをreloadします。
5. `deploy/apache/inpa.conf` をそのTLS VirtualHostへincludeします。
6. Apache設定を検証してからreloadします。
7. server-103-15以外からSE02のTCP/8090へ接続できないことを確認します。

## 4. 起動確認

公開前の基盤確認では、以下だけを確認します。

```text
GET https://inpa.mydns.jp/health/live  -> 200
GET https://inpa.mydns.jp/health/ready -> 200（MySQL migration完了後）
```

`/internal/admin/v1/*` は外部から到達してはなりません。MFU bridgeのHMAC認証は、管理API実装
フェーズで追加します。
