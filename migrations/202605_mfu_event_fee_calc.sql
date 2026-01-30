ALTER TABLE mfu_event
  ADD COLUMN studio_fee_yen INT NULL AFTER fee_yen,
  ADD COLUMN fee_rate_percent DECIMAL(6,2) NULL AFTER studio_fee_yen,
  ADD COLUMN admin_fee_yen INT NULL AFTER fee_rate_percent,
  ADD COLUMN fee_auto_calc TINYINT(1) NOT NULL DEFAULT 1 AFTER admin_fee_yen;
