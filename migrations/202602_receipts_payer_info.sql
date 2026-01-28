-- 受領書機能: 支払者情報の追加

ALTER TABLE receipts
  ADD COLUMN payer_name VARCHAR(255) NOT NULL AFTER issuer_user_id,
  ADD COLUMN payer_address VARCHAR(255) NOT NULL AFTER payer_name,
  ADD COLUMN payer_phone VARCHAR(64) NOT NULL AFTER payer_address,
  ADD COLUMN payer_email VARCHAR(255) NOT NULL AFTER payer_phone;
