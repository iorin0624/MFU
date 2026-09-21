# INPA V1 API・セキュリティ設計

## 1. API境界

```text
利用者向け:        /api/v1/*
共有ページ向け:    /api/v1/share/*
MFU管理向け:       /internal/admin/v1/*
ヘルスチェック:    /health/live, /health/ready
```

利用者向けAPIとVueは同一オリジンにし、通常運用でCORSを不要にします。

利用者向けAPIとInternal Admin APIは別のFlaskアプリインスタンスと別のGunicornプロセスで
稼働させます。公開用アプリにはInternal Admin Blueprintを登録せず、Internal Admin APIは
Unix domain socketだけで待ち受けます。

## 2. 認証API

```text
POST   /api/v1/auth/register/request
POST   /api/v1/auth/register/complete
POST   /api/v1/auth/email/resend
POST   /api/v1/auth/login
POST   /api/v1/auth/logout
POST   /api/v1/auth/logout-all
POST   /api/v1/auth/password/forgot
POST   /api/v1/auth/password/reset
GET    /api/v1/auth/sessions
DELETE /api/v1/auth/sessions/{sessionId}
GET    /api/v1/auth/me
```

登録要求では管理者発行の招待tokenとTurnstile tokenを受け取り、バックエンドから検証します。
招待tokenは最初の登録要求で正規化メールアドレスへ固定し、登録完了時に同じtransaction内で
使用済みにします。使用済み、無効、期限切れ、別メールへ割当済みのtokenは受け付けません。

### 2.1 プロフィール・アカウントAPI

```text
GET    /api/v1/profile
PATCH  /api/v1/profile
PATCH  /api/v1/privacy-defaults
POST   /api/v1/account/password/change
POST   /api/v1/account/email-change/request
POST   /api/v1/account/email-change/confirm
GET    /api/v1/account/deletion
POST   /api/v1/account/deletion/request
POST   /api/v1/account/deletion/cancel
```

- プロフィールAPIは表示名、SNS名、SNSごとの表示許可だけを扱います。
- 公開設定は専用APIで4範囲×4項目のブール値を厳密に検証し、保存後は全予定の閲覧判定に即時適用します。
- パスワード変更とメール変更要求では現在のパスワードを再確認します。
- パスワードまたはメールアドレスの変更完了時は全sessionを失効します。
- メールアドレスは新アドレスの確認が完了するまで変更しません。
- 退会申請後は通常ログインを許可せず、メールで送った取消tokenによりログインなしで取り消します。
- 取消tokenはpepper付きhashで保存し、期限、未使用、Rate Limitを検証します。

## 3. 予定・カレンダーAPI

```text
GET    /api/v1/seasons
GET    /api/v1/visits
POST   /api/v1/visits
GET    /api/v1/visits/{id}
PATCH  /api/v1/visits/{id}
DELETE /api/v1/visits/{id}
GET    /api/v1/calendar?season_id=&year=&month=
GET    /api/v1/restrictions?season_id=&date=&park=
```

更新系は、ログイン、メール確認、CSRF、所有者、入力値を検証します。

## 4. 共有・関係API

```text
GET    /api/v1/share/{token}
POST   /api/v1/share-token
POST   /api/v1/share-token/rotate
DELETE /api/v1/share-token
GET    /api/v1/people/{publicId}
GET    /api/v1/follows
POST   /api/v1/follows/{publicId}
DELETE /api/v1/follows/{publicId}
GET    /api/v1/blocks
POST   /api/v1/blocks/{publicId}
DELETE /api/v1/blocks/{publicId}
POST   /api/v1/reports
```

`GET /share/{token}` は閲覧者の状態に応じてレスポンス項目を構築し、DB行をそのままJSON化しません。
共有tokenは1ユーザーにつき同時に1件だけ有効とし、再発行は旧tokenの失効と新tokenの作成を
同一トランザクションで行います。

## 5. 公開判定

```text
link / 匿名                 -> link行の許可項目
logged_in                  -> link + logged_in行の許可項目
mutual / 双方向follow      -> link + logged_in + mutual行の許可項目
private / 所有者本人     -> 全4行の許可項目
blockがどちらかに存在       -> 本人以外は非表示
```

返却フィールド：

```text
date:     visit_date
park:     park
costume:  costume
memo:     memo
```

常に返してよいのは、予定IDそのものではなく画面操作に必要な不透明IDと、許可された表示項目だけです。

X・Instagramハンドルは、所有者本人または各ハンドルの表示許可が有効な場合だけレスポンスへ
含めます。許可されていない場合は `null` を返すのではなくフィールド自体を省略します。

## 6. Internal Admin API

```text
GET    /internal/admin/v1/summary
GET    /internal/admin/v1/users
GET    /internal/admin/v1/users/{id}
POST   /internal/admin/v1/users/{id}/suspend
POST   /internal/admin/v1/users/{id}/unsuspend
POST   /internal/admin/v1/users/{id}/logout-all
POST   /internal/admin/v1/users/{id}/delete-request
GET    /internal/admin/v1/registration-invitations
POST   /internal/admin/v1/registration-invitations
PATCH  /internal/admin/v1/registration-invitations/{id}
POST   /internal/admin/v1/registration-invitations/{id}/revoke
GET    /internal/admin/v1/visits
DELETE /internal/admin/v1/visits/{id}
POST   /internal/admin/v1/share-tokens/{id}/revoke
GET    /internal/admin/v1/reports
PATCH  /internal/admin/v1/reports/{id}
GET    /internal/admin/v1/seasons
POST   /internal/admin/v1/seasons
PATCH  /internal/admin/v1/seasons/{id}
GET    /internal/admin/v1/restriction-periods
POST   /internal/admin/v1/restriction-periods
PATCH  /internal/admin/v1/restriction-periods/{id}
DELETE /internal/admin/v1/restriction-periods/{id}
GET    /internal/admin/v1/security-events
GET    /internal/admin/v1/mail-logs
POST   /internal/admin/v1/mail-logs/{id}/retry
GET    /internal/admin/v1/audit-logs
```

一覧APIは必ずページングし、上限を100件とします。破壊的操作は `Idempotency-Key` を要求し、MFU側でも
重要操作に必要な再認証を適用できます。

## 7. Internal Admin API署名

MFUは以下を署名対象にします。

```text
HTTP method
request path
Unix timestamp
random nonce
SHA-256(body)
MFU admin username
```

INPAはUnix socketの接続権限、HMAC、時刻差、nonce未使用を確認します。成功・拒否の双方を
`admin_audit_logs` または `security_events` に記録します。

## 8. Webセキュリティ

- Cookie: `Secure; HttpOnly; SameSite=Lax; Path=/`
- CSRF: セッションと結び付けたtokenを更新系で要求
- CSP: nonce方式を基本とし、Turnstile等の許可先だけを明示
- HSTS: 本番ドメインとメール・運用要件を確認後に有効化
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: no-referrer`
- frame埋め込み禁止
- APIエラーは内部例外やSQLを返さない
- パラメーター化SQLまたはORMを使用
- パスワードはArgon2id
- 秘密tokenはSHA-256 + サービス固有pepperで保存
- 登録招待tokenの平文は発行responseで一度だけ返し、DB・監査ログには保存しない
- 重要な比較はconstant-time比較

パスワード変更、メールアドレス変更完了、退会申請・取消はセキュリティイベントとして記録し、
確認済みメールアドレスへ通知します。退会取消後も旧共有tokenは復活させません。

## 9. TurnstileとRate Limit

初期制限値は設定として管理し、コードへ固定しません。

推奨初期値：

| 操作 | 単位 | 初期値 |
|---|---|---:|
| 登録メール送信 | メール | 15分に3回 |
| 登録メール送信 | IP | 1時間に10回 |
| ログイン失敗 | アカウント候補 | 15分に5回で追加Turnstile |
| パスワード再設定 | メール | 1時間に3回 |
| フォロー | アカウント | 1時間に30回 |
| ブロック | アカウント | 1時間に30回 |
| 通報 | アカウント | 24時間に10回 |
| 共有URL不一致 | IP/IPv6プレフィックス | 10分に60回で一時制限 |

成功レスポンスだけでなく拒否も記録し、管理画面から理由を確認できるようにします。

## 10. ログの機密情報

以下をログへ出しません。

- 共有トークン全文
- セッションCookie
- CSRF token
- メール確認・再設定token
- パスワード
- Turnstile token
- SMTP認証情報
- Internal Admin API HMAC

共有URLは `/<redacted:last4>` のようにマスクします。メールアドレスは通常画面以外の集計ログでは
ハッシュまたは部分マスクを使用します。

## 11. メールキュー

メール送信要求はトランザクション内で `mail_logs` にqueued状態で登録します。別workerが取得して送信し、
指数バックオフで再試行します。

```text
queued -> sending -> sent
                 -> retry_wait -> sending
                 -> failed
```

同じ用途のメールを二重送信しないため、用途・対象・token世代からidempotency keyを作ります。
送信に必要な宛先とテンプレート値は暗号化して保持し、送信完了または再送打切り後に消去します。
メール本文と認証tokenの平文を履歴として保存しません。
