CREATE TABLE IF NOT EXISTS photo_relay_devices (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  device_uuid CHAR(36) NOT NULL,
  username VARCHAR(191) NOT NULL,
  label VARCHAR(120) NOT NULL,
  token_hash CHAR(64) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at DATETIME NULL,
  last_used_at DATETIME NULL,
  last_connected_at DATETIME NULL,
  revoked_at DATETIME NULL,
  removed_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_photo_relay_device_uuid (device_uuid),
  UNIQUE KEY uq_photo_relay_token_hash (token_hash),
  KEY ix_photo_relay_device_owner (username, removed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS photo_relay_jobs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_uuid CHAR(36) NOT NULL,
  device_id BIGINT UNSIGNED NOT NULL,
  username VARCHAR(191) NOT NULL,
  status VARCHAR(24) NOT NULL DEFAULT 'receiving',
  expected_files INT UNSIGNED NULL,
  total_files INT UNSIGNED NOT NULL DEFAULT 0,
  completed_files INT UNSIGNED NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ready_at DATETIME NULL,
  completed_at DATETIME NULL,
  expires_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_photo_relay_job_uuid (job_uuid),
  KEY ix_photo_relay_job_device (device_id, status, created_at),
  CONSTRAINT fk_photo_relay_job_device FOREIGN KEY (device_id)
    REFERENCES photo_relay_devices(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS photo_relay_files (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_id BIGINT UNSIGNED NOT NULL,
  client_file_id VARCHAR(128) NOT NULL,
  original_filename VARCHAR(255) NOT NULL,
  stored_filename VARCHAR(255) NOT NULL,
  mime_type VARCHAR(64) NOT NULL,
  size_bytes BIGINT UNSIGNED NOT NULL,
  sha256 CHAR(64) NOT NULL,
  sequence_index INT UNSIGNED NOT NULL DEFAULT 0,
  capture_at VARCHAR(64) NULL,
  source_modified_at VARCHAR(64) NULL,
  converted_to_jpeg TINYINT(1) NOT NULL DEFAULT 0,
  status VARCHAR(24) NOT NULL DEFAULT 'queued',
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  last_error VARCHAR(500) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_photo_relay_client_file (job_id, client_file_id),
  KEY ix_photo_relay_file_status (job_id, status, sequence_index),
  CONSTRAINT fk_photo_relay_file_job FOREIGN KEY (job_id)
    REFERENCES photo_relay_jobs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
