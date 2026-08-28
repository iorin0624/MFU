-- Child-album ownership for the participant Vue migration.
-- Existing rows intentionally remain NULL; ownership must never be inferred
-- from a display name.

ALTER TABLE album_children
  ADD COLUMN IF NOT EXISTS created_by_ext_user_id BIGINT UNSIGNED NULL AFTER mode,
  ADD COLUMN IF NOT EXISTS created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP AFTER created_by_ext_user_id,
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP AFTER created_at;

CREATE INDEX IF NOT EXISTS idx_album_children_creator
  ON album_children (created_by_ext_user_id, album_id);
