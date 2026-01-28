-- 受領書機能: 支払者プロフィールとスナップショット拡張、署名方式整理

CREATE TABLE IF NOT EXISTS payer_profiles (
  issuer_user_id VARCHAR(255) NOT NULL PRIMARY KEY,
  payer_name VARCHAR(255) NOT NULL,
  payer_address VARCHAR(255) NOT NULL,
  payer_phone VARCHAR(64) NOT NULL,
  payer_email VARCHAR(255),
  payer_invoice_no VARCHAR(64),
  payer_bank_name VARCHAR(128),
  payer_bank_branch VARCHAR(128),
  payer_bank_account VARCHAR(128),
  payer_bank_holder VARCHAR(128),
  payer_note TEXT,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE receipts
  ADD COLUMN payer_invoice_no VARCHAR(64) AFTER payer_email,
  ADD COLUMN payer_bank_name VARCHAR(128) AFTER payer_invoice_no,
  ADD COLUMN payer_bank_branch VARCHAR(128) AFTER payer_bank_name,
  ADD COLUMN payer_bank_account VARCHAR(128) AFTER payer_bank_branch,
  ADD COLUMN payer_bank_holder VARCHAR(128) AFTER payer_bank_account,
  ADD COLUMN payer_note TEXT AFTER payer_bank_holder;
