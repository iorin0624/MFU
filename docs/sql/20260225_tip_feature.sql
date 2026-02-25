ALTER TABLE mfu_event
  ADD COLUMN tip_enabled TINYINT(1) NOT NULL DEFAULT 0 AFTER fee_yen;

ALTER TABLE mfu_payment_request
  ADD COLUMN kind VARCHAR(32) NOT NULL DEFAULT 'event_fee' AFTER buyer_email,
  ADD COLUMN tip_event_id BIGINT UNSIGNED NULL AFTER kind;
