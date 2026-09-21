# INPA

INPA（インパーク予定共有サービス）は、MFUと同じ物理サーバー上で稼働しつつ、
アプリケーション、認証、Cookie、データベース、運用単位を分離する独立サービスです。

本番配置の基準パスは `/mnt/mfu/app/inpa` とします。

## 本番トポロジー

```text
利用者
  -> server-103-15（Apache TLS終端・リバースプロキシ）
  -> SE02 / server-103-16（INPA Web、Internal Admin API、mail worker）
  -> server-103-17（inpa_db）
```

SE02の公開Gunicorn listenerはTCP/8090でserver-103-15からだけ受け付けます。Internal Admin APIは
SE02内の `/run/inpa/admin.sock` だけで待ち受け、外部TCPポートを開きません。

## 配置方針

```text
/mnt/mfu/app/inpa/
├── backend/               INPA利用者向けAPIと内部管理API
├── frontend/              Vue 3 / TypeScript / Vite
├── mfu_admin_bridge/      MFU管理画面から内部管理APIを呼ぶ薄い接続層
├── migrations/            inpa_db専用Alembic環境と初期スキーマ設計
├── deploy/                systemd・Webサーバー設定の原本と導入スクリプト
├── docs/                  仕様、画面、API、実装計画
├── scripts/               保守・バックアップ・移行補助
├── tests/                 バックエンド、フロントエンド、結合テスト
└── .env.example           秘密値を含まない設定例
```

稼働時にOSの所定位置へ必要となるsystemd unitやWebサーバー設定も、原本と導入手順は
`inpa/deploy/` で一元管理します。秘密値を含む `.env` はGit管理せず、INPA専用Linux
ユーザーだけが読める権限にします。

## 設計文書

- [V1確定仕様](docs/00_v1_specification.md)
- [V1仕様レビュー](docs/01_spec_review.md)
- [画面設計](docs/02_screen_design.md)
- [API・セキュリティ設計](docs/03_api_security_design.md)
- [実装計画](docs/04_implementation_plan.md)
- [アカウント・バックエンド・DB確定仕様](docs/05_account_backend_database_decisions.md)
- [基盤配置手順](docs/06_foundation_deployment.md)
- [初期DB設計](migrations/0001_initial_schema.sql)

## 境界

- MFU利用者アカウントとINPAアカウントは共有しません。
- MFUのCookieやセッションをINPAへ渡しません。
- MFUは `INPA Internal Admin API` を通じてのみ管理します。
- INPAのDBユーザーにはMFU既存DBの権限を与えません。
- INPA利用者向けAPIはMFU DBへ接続しません。
