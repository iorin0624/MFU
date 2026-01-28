-- 受領書機能: 相手先マスター（受領者アドレス帳）

CREATE TABLE IF NOT EXISTS receipt_recipients (
  id INT AUTO_INCREMENT PRIMARY KEY,
  issuer_user_id VARCHAR(255) NOT NULL,
  display_name VARCHAR(255) NOT NULL,
  email VARCHAR(255) NOT NULL,
  last_used_at DATETIME,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  is_deleted TINYINT(1) NOT NULL DEFAULT 0,
  UNIQUE KEY uniq_receipt_recipients (issuer_user_id, email),
  KEY idx_receipt_recipients_user (issuer_user_id),
  KEY idx_receipt_recipients_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
