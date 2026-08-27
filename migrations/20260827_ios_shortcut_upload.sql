CREATE TABLE IF NOT EXISTS ios_shortcut_upload_completions (
    upload_id INT NOT NULL PRIMARY KEY,
    completed_at DATETIME NOT NULL,
    CONSTRAINT fk_ios_shortcut_upload_completion_upload
      FOREIGN KEY (upload_id) REFERENCES uploads(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
