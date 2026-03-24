-- GPSチェックイン機能廃止 + QRチェックイン常時有効化
-- 対象: external_login_user

START TRANSACTION;

-- 1) 既存イベントを QR 常時有効へ寄せる
UPDATE mfu_event
   SET checkin_qr_enabled = 1;

-- 2) 無効/空トークンを再生成（64桁hexへ統一）
UPDATE mfu_event
   SET checkin_qr_token = LOWER(HEX(RANDOM_BYTES(32)))
 WHERE checkin_qr_token IS NULL
    OR CHAR_LENGTH(TRIM(checkin_qr_token)) <> 64
    OR NOT (TRIM(checkin_qr_token) REGEXP '^[0-9A-Fa-f]{64}$');

-- 3) デフォルトを常時ONへ変更
SET @sql = (
  SELECT IF(
    EXISTS(
      SELECT 1 FROM information_schema.COLUMNS
       WHERE TABLE_SCHEMA = DATABASE()
         AND TABLE_NAME = 'mfu_event'
         AND COLUMN_NAME = 'checkin_qr_enabled'
    ),
    'ALTER TABLE mfu_event MODIFY COLUMN checkin_qr_enabled TINYINT(1) NOT NULL DEFAULT 1',
    'SELECT 1'
  )
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 4) GPS専用カラムを削除（存在確認つき）
SET @sql = (
  SELECT IF(EXISTS(
      SELECT 1 FROM information_schema.COLUMNS
       WHERE TABLE_SCHEMA = DATABASE()
         AND TABLE_NAME = 'mfu_event_member'
         AND COLUMN_NAME = 'checkin_lat'
    ),
    'ALTER TABLE mfu_event_member DROP COLUMN checkin_lat',
    'SELECT 1'
  )
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
  SELECT IF(EXISTS(
      SELECT 1 FROM information_schema.COLUMNS
       WHERE TABLE_SCHEMA = DATABASE()
         AND TABLE_NAME = 'mfu_event_member'
         AND COLUMN_NAME = 'checkin_lng'
    ),
    'ALTER TABLE mfu_event_member DROP COLUMN checkin_lng',
    'SELECT 1'
  )
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
  SELECT IF(EXISTS(
      SELECT 1 FROM information_schema.COLUMNS
       WHERE TABLE_SCHEMA = DATABASE()
         AND TABLE_NAME = 'mfu_event_member'
         AND COLUMN_NAME = 'checkin_method'
    ),
    'ALTER TABLE mfu_event_member DROP COLUMN checkin_method',
    'SELECT 1'
  )
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
  SELECT IF(EXISTS(
      SELECT 1 FROM information_schema.COLUMNS
       WHERE TABLE_SCHEMA = DATABASE()
         AND TABLE_NAME = 'mfu_event'
         AND COLUMN_NAME = 'event_lat'
    ),
    'ALTER TABLE mfu_event DROP COLUMN event_lat',
    'SELECT 1'
  )
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
  SELECT IF(EXISTS(
      SELECT 1 FROM information_schema.COLUMNS
       WHERE TABLE_SCHEMA = DATABASE()
         AND TABLE_NAME = 'mfu_event'
         AND COLUMN_NAME = 'event_lng'
    ),
    'ALTER TABLE mfu_event DROP COLUMN event_lng',
    'SELECT 1'
  )
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
  SELECT IF(EXISTS(
      SELECT 1 FROM information_schema.COLUMNS
       WHERE TABLE_SCHEMA = DATABASE()
         AND TABLE_NAME = 'mfu_event'
         AND COLUMN_NAME = 'checkin_radius_m'
    ),
    'ALTER TABLE mfu_event DROP COLUMN checkin_radius_m',
    'SELECT 1'
  )
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

COMMIT;
