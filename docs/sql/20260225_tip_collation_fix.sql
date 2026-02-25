-- event_payments.payment_token と mfu_payment_request.token の照合順序を統一
ALTER TABLE event_payments
  MODIFY COLUMN payment_token CHAR(36)
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci
  NULL;

ALTER TABLE mfu_payment_request
  MODIFY COLUMN token CHAR(36)
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci
  NOT NULL;
