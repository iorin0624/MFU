-- 受領書機能: 論理削除メタ情報の追加

ALTER TABLE receipts
  ADD COLUMN deleted_at DATETIME DEFAULT NULL AFTER is_deleted,
  ADD COLUMN deleted_by VARCHAR(255) DEFAULT NULL AFTER deleted_at;
