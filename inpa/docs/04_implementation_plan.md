# INPA V1 実装計画

## 1. 実装方針

一度に全機能を本番公開せず、基盤、認証、予定共有、利用者関係、MFU管理、運用の順に完成させます。
各段階で自動テストとスマートフォン実機確認を行います。

## 2. フェーズ0: 確定と外部準備

- 正式サービス名と本番ドメインを確定
- `inpa.mydns.jp` のDNS管理可否を確認
- `noreply@inpa.mydns.jp` の送信経路を確認
- SPF・DKIM・DMARCを設計
- Turnstile site key / secret keyを発行
- 利用規約、プライバシーポリシー、問い合わせ窓口を確定
- ログ・退会データの保存期間を最終承認

完了条件：外部依存と未確定値が一覧化され、秘密値の投入方法が決まっていること。

## 3. フェーズ1: 独立実行基盤

- `inpa/backend` のPythonプロジェクト作成
- アプリケーションファクトリーと `/health` 実装
- `inpa/frontend` のVue 3 + TypeScript + Vite作成
- INPA専用Linuxユーザー、DBユーザー、`inpa_db` 作成手順
- 独立systemd serviceとメールworker service
- Webサーバーの同一オリジン配信設定
- 環境変数と秘密値の権限設定
- journald、ローテーション、監視

完了条件：MFUを停止せずINPAだけ起動・停止・更新できること。

## 4. フェーズ2: DBと認証

- 初期マイグレーション適用機構
- ユーザー登録、Turnstile検証、メール確認
- Argon2idパスワード
- ログイン、ログアウト、セッション一覧、全失効
- パスワード再設定
- Rate Limit、セキュリティイベント
- メールキューと再送worker
- 登録・ログイン・再設定の列挙耐性テスト

完了条件：メール確認済みユーザーだけが認証済み機能を利用できること。

## 5. フェーズ3: プロフィール・シーズン・予定

- プロフィールと標準公開設定
- シーズン取得
- 予定の追加・編集・削除
- 1ユーザー・1シーズン・1日制約
- 公開対象・公開情報量のAPI判定
- 自分の予定一覧と月間表示

完了条件：権限テストで他人の予定を変更できず、非公開フィールドがAPIへ混入しないこと。

## 6. フェーズ4: 共有URL

- 80bit短縮トークン発行
- token hash検索
- 共有HTMLのOG・robots・referrer設定
- 匿名共有API
- URLコピー、QR、無効化、再発行
- ログのtokenマスク
- SNS内ブラウザー実機確認

完了条件：旧URLが再発行直後から無効になり、SNSプレビューへ予定情報が出ないこと。

## 7. フェーズ5: 登録・相互・ブロック・統合カレンダー

- 一方向登録と解除
- 双方向関係から相互判定
- ブロック時の双方登録解除
- 登録済み利用者画面
- 権限付き人物予定表
- 統合カレンダーと同日人数
- 関係方向ごとの公開制御テスト

完了条件：link/logged_in/following/mutual/privateの組合せテストがすべて通ること。

## 8. フェーズ6: MFU管理接続

- INPA Internal Admin API
- Unix domain socketとHMAC署名
- `inpa/mfu_admin_bridge` のMFU Blueprint
- ダッシュボード、利用者、予定、通報、シーズン、メール、セキュリティ、監査画面
- 停止、解除、全ログアウト、予定削除、token失効
- 管理操作の冪等性と監査ログ

完了条件：MFU DB権限なしで管理でき、すべての変更がINPA側監査ログへ残ること。

## 9. フェーズ7: 退会・通報・運用

- 通報受付と対応状態
- 退会申請、30日猶予、取消、完全削除job
- ログ削除job
- DBバックアップ、暗号化、復元手順
- 障害時のロールバック
- メトリクスとアラート
- 利用規約・プライバシーポリシー表示

完了条件：INPAだけのバックアップから別環境へ復元できること。

## 10. フェーズ8: 公開前試験

- 単体・API・権限・E2Eテスト
- iOS Safari / Android Chrome
- X / Instagram / LINE内ブラウザー
- 低速回線・メール遅延・Turnstile障害
- CSRF、XSS、IDOR、token列挙、Rate Limit回避の確認
- アクセシビリティ
- バックアップ復元試験
- MFUとINPAの個別再起動試験

## 11. 推奨ディレクトリ詳細

```text
inpa/
├── backend/
│   ├── pyproject.toml
│   ├── inpa_app/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── db.py
│   │   ├── auth/
│   │   ├── users/
│   │   ├── visits/
│   │   ├── sharing/
│   │   ├── relationships/
│   │   ├── security/
│   │   ├── mail/
│   │   └── internal_admin/
│   └── wsgi.py
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   └── src/
│       ├── api/
│       ├── assets/
│       ├── components/
│       ├── composables/
│       ├── router/
│       ├── stores/
│       ├── styles/
│       └── views/
├── mfu_admin_bridge/
├── migrations/
├── deploy/
│   ├── apache/
│   ├── systemd/
│   └── install.sh
├── docs/
├── scripts/
└── tests/
    ├── backend/
    ├── frontend/
    └── e2e/
```

## 12. デプロイ戦略

1. CIでバックエンドテスト、Vue lint/typecheck/test/buildを実行します。
2. `/mnt/mfu/app/inpa` のリリース候補へ配置します。
3. DBマイグレーションの前方互換性を確認して適用します。
4. INPA APIとworkerだけを再起動します。
5. readiness確認後に新しいVue assetsへ切り替えます。
6. MFU管理接続をスモークテストします。
7. 問題時はアプリを直前版へ戻します。DBは破壊的変更を同一リリースで行いません。

## 13. 実装開始前に利用者確認が必要な項目

- 正式なサービス名と最終ドメイン
- 予定1日1件の扱いでよいか
- セッション期限（初期案: 最終利用30日、最長90日）
- 退会取消猶予（初期案: 30日）
- ログ保存期間
- メール通知の範囲（セキュリティのみか、登録・ブロック等も含むか）
- QRコード表示をV1へ含めるか

