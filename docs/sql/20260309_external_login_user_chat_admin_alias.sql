SET @has_col := (
  SELECT COUNT(*)
    FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME = 'external_login_user'
     AND COLUMN_NAME = 'chat_admin_alias'
);
SET @ddl := IF(
  @has_col = 0,
  'ALTER TABLE external_login_user ADD COLUMN chat_admin_alias TINYINT(1) NOT NULL DEFAULT 0 AFTER admin_note',
  'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
