# INPA V1 アカウント・バックエンド・DB確定仕様

本書は、実装開始前に未確定だったアカウントAPI、SNS名の表示許可、共有トークン、
オンボーディング、Pythonバックエンド、DB権限分離について確定した仕様です。
`00_v1_specification.md` を補完し、関連する詳細設計と矛盾する場合は本書を優先します。

## 1. アカウント関連API

以下のAPIをV1へ追加します。

```text
GET    /api/v1/profile
PATCH  /api/v1/profile
PATCH  /api/v1/privacy-defaults
POST   /api/v1/privacy-defaults/apply-to-visits

POST   /api/v1/account/password/change
POST   /api/v1/account/email-change/request
POST   /api/v1/account/email-change/confirm

GET    /api/v1/account/deletion
POST   /api/v1/account/deletion/request
POST   /api/v1/account/deletion/cancel
```

### 1.1 プロフィールと標準公開設定

- `PATCH /profile` は表示名、Xハンドル、Instagramハンドル、各SNS名の表示許可だけを更新します。
- メールアドレスとパスワードはプロフィールAPIで変更しません。
- 空のSNSハンドルは `NULL` へ正規化します。
- `PATCH /privacy-defaults` は標準公開対象と標準公開情報量を更新します。
- 既存予定への一括反映は、対象件数と変更内容を確認したうえで
  `POST /api/v1/privacy-defaults/apply-to-visits` から実行します。
- すべての更新系APIでログイン、メール確認、CSRF、入力値、Rate Limitを検証します。

### 1.2 パスワード変更

- 現在のパスワードと新しいパスワードを要求します。
- 変更完了時に、現在のセッションを含む全セッションを失効します。
- レスポンス時に現在のセッションCookieも削除し、再ログインを要求します。
- セキュリティ通知メールを送信し、`security_events` に記録します。

### 1.3 メールアドレス変更

- 変更要求時に現在のパスワードを再確認します。
- 新しいメールアドレスへ期限付き確認リンクを送ります。
- リンク確認までは現在のメールアドレスを変更しません。
- 確認時のトランザクション内でメールアドレスの重複を再検証します。
- 完了時に全セッションを失効し、新旧両方のメールアドレスへ通知します。
- 利用できないメールアドレスである場合も、他アカウントの存在を明示しない文言を返します。

### 1.4 退会申請と取消

- 退会申請時に現在のパスワードを再確認します。
- 申請と同時に全セッションを失効し、共有トークンを無効化し、予定を非公開にし、登録関係を解除します。
- 30日間の取消猶予を設けます。
- 退会猶予中は通常ログインを許可しません。
- 取消用ランダムトークンを発行し、DBにはpepperを加えたハッシュだけを保存します。
- 取消トークンの有効期限は退会処理予定日時 `scheduled_for` までとします。
- 取消リンクは確認済みメールアドレスへ送信し、取消APIはログインなしで利用可能とします。
- 取消トークンにはRate Limitを適用し、使用済み・期限切れを再利用できないようにします。
- 取消後も旧共有URLは復活させず、本人が新しい共有URLを発行します。
- 管理者による本人確認済みの復旧手段を別途用意します。

## 2. SNS名の表示許可

利用者ごとに、SNS名をプロフィール表示へ含めるかを個別に設定します。

```text
x_handle_visible: boolean, default false
instagram_handle_visible: boolean, default false
```

- 初期値は両方とも非表示です。
- ハンドルの登録と表示許可は別々の入力として扱います。
- 設定は共有予定表と登録済み利用者の予定表へ共通して適用します。
- 本人には設定状態にかかわらず自分の登録値を返せます。
- 本人以外へのAPIレスポンスでは、非表示のSNSフィールド自体を返しません。
- SNS名は公開プロフィール検索には使用しません。
- 外部プロフィールへのURLは許可された文字列から安全に組み立て、利用者入力をURLとして直接使用しません。
- 本人確認済みSNSアカウントであるかのような表示は行いません。

## 3. 共有トークンの一意性と再発行

1ユーザーにつき有効な共有トークンは同時に1件だけとし、アプリケーションとDBの両方で保証します。

`share_tokens` に次の生成列とユニーク制約を設けます。

```sql
active_user_id BIGINT UNSIGNED
  GENERATED ALWAYS AS (
    CASE WHEN status = 'active' THEN user_id ELSE NULL END
  ) STORED,
UNIQUE KEY uq_share_tokens_one_active_user (active_user_id)
```

失効済み行では生成列が `NULL` になるため履歴を複数保持でき、有効な行だけがユーザー単位で
一意になります。

再発行は次の処理を1トランザクションで行います。

```text
現在のactive tokenをロックして取得
-> 現在のtokenをrevokedへ変更
-> 新しいtokenをactiveで追加
-> commit
```

ユニーク制約違反が発生した場合は同時実行競合として扱い、トークンを複数有効化しません。
平文トークンは発行レスポンス以外に保存せず、旧URLはcommit直後から無効とします。

## 4. 登録完了とオンボーディング

登録時のプロフィール入力とオンボーディングの重複をなくし、次の導線に統一します。

```text
/register
  メールアドレス・Turnstile
-> 確認メール
-> /register/complete?token=...
  1. パスワード・規約同意
  2. 表示名・SNS名・SNS表示許可
  3. 標準公開対象・標準公開情報量
-> 正式ユーザーを作成してログイン
-> /onboarding
  4. 共有URL発行
  5. 最初の予定登録
```

- 正式ユーザーは登録完了のトランザクションで初めて作成します。
- `/onboarding` ではプロフィールと標準公開設定を再入力させません。
- 共有URL発行と最初の予定登録はスキップ可能です。
- 再開位置は、有効な共有トークンと予定の有無から判定し、専用のステップ番号へ依存しません。
- オンボーディング未完了でも通常画面を利用できます。

## 5. Pythonバックエンド

V1のバックエンド構成を次で確定します。

```text
Python
Flask 3系
Gunicorn
SQLAlchemy 2系
Alembic
PyMySQL
pytest
```

- INPAはMFUとは別のPython仮想環境、プロセス、設定、Linuxユーザーで稼働します。
- Flaskアプリケーションファクトリー `create_app()` と機能別Blueprintを使用します。
- ORM・SQL実行とトランザクション管理にはSQLAlchemy 2系を使用します。
- DB変更履歴にはAlembicを使用し、生成されたmigrationはレビューしてから適用します。
- 本番ではFlask開発サーバーを使わずGunicornを使用します。
- メールworkerは同じドメインロジックを利用しますが、Webプロセスとは別のsystemd serviceにします。

プロセス境界は次を基準とします。

```text
inpa-web.service
  利用者API、共有API、Vue配信

inpa-admin-api.service
  Internal Admin APIだけを登録
  /run/inpa/admin.sockで待受

inpa-mail-worker.service
  DBメールキュー処理
```

公開用FlaskアプリにはInternal Admin Blueprintを登録しません。Internal Admin APIは外部TCPで
待ち受けず、MFUの薄い接続層だけがUnix domain socket経由で呼び出します。

## 6. マイグレーションとDB権限

DBの初期構築、スキーマ変更、通常実行を別の権限で行います。

### 6.1 bootstrap

DB管理者が初回構築時だけ、次を実行します。

- `inpa_db` の作成（`utf8mb4` / `utf8mb4_0900_ai_ci`）
- DBロールまたはDBユーザーの作成
- 権限付与
- 秘密値ファイルの配置

bootstrapはAlembic migrationへ含めず、`deploy/mysql/` に再構築可能な原本を置きます。

### 6.2 migration用アカウント

`inpa_migrator` は `inpa_db` に限り、次の権限を持ちます。

```text
SELECT, INSERT, UPDATE, DELETE
CREATE, ALTER, DROP, INDEX, REFERENCES
```

`CREATE USER`、`GRANT OPTION`、`FILE`、グローバル管理権限、MFU DBへの権限は与えません。
データ移行を伴うmigrationに備えてDML権限も持たせます。

### 6.3 runtime用アカウント

`inpa_app` は原則として次の権限だけを持ちます。

```text
SELECT, INSERT, UPDATE, DELETE ON inpa_db.*
```

通常実行アカウントへ `CREATE`、`ALTER`、`DROP`、`INDEX` を与えません。MFU DBへの権限も
与えません。バックアップが必要な場合は、通常実行アカウントを流用せず専用アカウントを使用します。

### 6.4 秘密値と適用手順

```text
/etc/inpa/runtime.env    inpa_app用。Web、admin API、workerだけが参照
/etc/inpa/migration.env  inpa_migrator用。デプロイ処理だけが参照
```

- runtime serviceからmigration用秘密値を読めないファイル権限にします。
- CIではmigrationの整合性を検査しますが、本番DBの資格情報を渡しません。
- デプロイ時に `alembic upgrade head` を専用処理として実行してからアプリを切り替えます。
- Alembic標準の `alembic_version` を使用し、独自の `schema_migrations` は使用しません。
- 同一リリースで後方互換性のないカラム削除や名称変更を行わず、expand/contract方式で段階移行します。
