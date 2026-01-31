ALTER TABLE user_passkeys
  ADD COLUMN label VARCHAR(128) NULL AFTER username;
