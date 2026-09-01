ALTER TABLE image_viewer_files
  ADD COLUMN captured_at DATETIME(6) NULL AFTER file_mtime,
  ADD INDEX idx_iv_files_folder_capture (folder_id, captured_at),
  ADD INDEX idx_iv_files_folder_registered (folder_id, created_at),
  ADD INDEX idx_iv_files_folder_content_updated (folder_id, file_mtime);
