ALTER TABLE uploads
    ADD COLUMN access_token_hash CHAR(64) NULL AFTER auth_version;

UPDATE upload_modes
   SET auth_method = CASE WHEN require_password = 1 THEN 'password' ELSE 'none' END
 WHERE auth_method IS NULL
    OR auth_method NOT IN ('none', 'password', 'access_token', 'email_otp');
