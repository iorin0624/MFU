# 受領書機能（MFU実装メモ）

## 追加仕様（最小構成）
- 受領書は `receipts` + `receipt_versions` で版管理し、原本PDFは生成後に不変。再発行は新バージョンを作成。
- 署名リンクは 48 時間有効のワンタイムトークン。使用後に無効化。
- 署名は「署名文字列」「手書きサイン（PNG）」のいずれか（両方可）。
- 署名時にメール OTP（6桁、10分、5回失敗でロック）を必須。
- 監査ログはハッシュ連鎖で追記型保存。

## 追加ファイル一覧 & 統合ポイント
- `receipts/__init__.py` / `receipts/routes.py`
  - 受領書の新規作成・送信・再発行・署名・PDF生成を担当する Blueprint。
- `receipts/template/*.html`
  - 発行者 UI、受領者の署名 UI、PDF（原本/署名証跡）テンプレート。
- `migrations/202501_receipts.sql`
  - 受領書関連テーブル追加 SQL。
- `docs/receipts_feature.md`
  - 本仕様メモ。

統合ポイント:
- `__init__.py` に `receipts_bp` を追加登録。
- `templates/base.html` のナビに「受領書」メニューを追加。

## 状態遷移（テキスト図）
```
[draft]
   | 署名依頼送信
   v
[sent] --(OTP+署名完了)--> [finalized]
   | 再発行
   v
[reissued] --(署名依頼送信)--> [sent]
   \
    \--(破棄)--> [void]
```

## DBマイグレーションSQL
- `migrations/202501_receipts.sql` を参照。

## 簡易テスト観点
1. 署名リンク期限切れ（48h）: 期限切れ表示になること。
2. OTP失敗5回でロック: 失敗後にロックされること。
3. 再発行: 新しいバージョンと原本PDFが生成され、旧リンクは無効のまま残ること。
4. 改ざん検知: audit_logs の `prev_hash` / `hash` を再計算し整合確認できること。

## 運用メモ
- バックアップ対象: DB（receipt/署名/監査ログ）、PDF（原本/確定）、署名画像。
- PDF保管パス例: `/mnt/mfu/receipts_pdf_archive/<receipt_no>/v<version>/original.pdf`。
- 旧ルート `/mnt/mfu/app/receipts` は移行期間のみ参照（移行後は新ルート優先）。
- 署名画像は `signatures.signature_image_path` にファイル保存。
