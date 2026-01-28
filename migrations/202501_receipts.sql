-- 受領書機能: 追加テーブル

CREATE TABLE IF NOT EXISTS receipts (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  receipt_no VARCHAR(32) NOT NULL UNIQUE,
  issuer_user_id VARCHAR(255) NOT NULL,
  recipient_name VARCHAR(255) NOT NULL,
  recipient_email VARCHAR(255) NOT NULL,
  issue_date DATE NOT NULL,
  pay_date DATE NOT NULL,
  amount INT NOT NULL,
  description VARCHAR(255) NOT NULL,
  payment_method VARCHAR(64) NOT NULL,
  status VARCHAR(32) NOT NULL,
  current_version_id BIGINT UNSIGNED,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  is_deleted TINYINT(1) NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS receipt_versions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  receipt_id BIGINT UNSIGNED NOT NULL,
  version_no INT NOT NULL,
  original_pdf_path TEXT NOT NULL,
  final_pdf_path TEXT NOT NULL,
  hash_original VARCHAR(64) NOT NULL,
  hash_final VARCHAR(64) NOT NULL,
  created_at DATETIME NOT NULL,
  FOREIGN KEY (receipt_id) REFERENCES receipts(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS receipt_tokens (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  receipt_version_id BIGINT UNSIGNED NOT NULL,
  token_hash VARCHAR(64) NOT NULL,
  expires_at DATETIME NOT NULL,
  used_at DATETIME DEFAULT NULL,
  created_at DATETIME NOT NULL,
  FOREIGN KEY (receipt_version_id) REFERENCES receipt_versions(id),
  INDEX idx_receipt_tokens_hash (token_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS otp_sessions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  token_id BIGINT UNSIGNED NOT NULL,
  otp_hash VARCHAR(64) NOT NULL,
  otp_salt VARCHAR(32) NOT NULL,
  expires_at DATETIME NOT NULL,
  failed_count INT NOT NULL DEFAULT 0,
  locked_until DATETIME DEFAULT NULL,
  verified_at DATETIME DEFAULT NULL,
  created_at DATETIME NOT NULL,
  FOREIGN KEY (token_id) REFERENCES receipt_tokens(id),
  INDEX idx_otp_token (token_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS signatures (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  receipt_version_id BIGINT UNSIGNED NOT NULL,
  signed_at DATETIME NOT NULL,
  signer_name_input VARCHAR(255) NOT NULL,
  signer_email VARCHAR(255) NOT NULL,
  signature_type VARCHAR(16) NOT NULL,
  signature_image_path TEXT,
  ip VARCHAR(64),
  user_agent TEXT,
  op_id VARCHAR(64) NOT NULL,
  FOREIGN KEY (receipt_version_id) REFERENCES receipt_versions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS audit_logs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  at DATETIME NOT NULL,
  actor_type VARCHAR(32) NOT NULL,
  actor_id VARCHAR(255),
  action VARCHAR(64) NOT NULL,
  receipt_id BIGINT UNSIGNED,
  version_id BIGINT UNSIGNED,
  token_id BIGINT UNSIGNED,
  result VARCHAR(255),
  ip VARCHAR(64),
  user_agent TEXT,
  prev_hash VARCHAR(64),
  hash VARCHAR(64) NOT NULL,
  INDEX idx_audit_receipt (receipt_id),
  INDEX idx_audit_token (token_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
