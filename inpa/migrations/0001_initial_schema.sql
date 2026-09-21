-- INPA V1 initial schema (MySQL 8.0+)
-- Application timestamps are stored in UTC. visit_date and arrival_time represent Asia/Tokyo.
-- The database and DB accounts are created by deploy/mysql bootstrap scripts.
-- Schema revision state is managed by Alembic's alembic_version table.
-- This file is immutable schema source for Alembic revision 0001_initial_schema,
-- not a DB bootstrap script. Later schema changes must use new Alembic revisions.

CREATE TABLE users (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  public_id CHAR(26) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  email VARCHAR(254) NOT NULL,
  email_normalized VARCHAR(254) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  display_name VARCHAR(40) NOT NULL,
  x_handle VARCHAR(15) NULL,
  instagram_handle VARCHAR(30) NULL,
  x_handle_visible TINYINT(1) NOT NULL DEFAULT 0,
  instagram_handle_visible TINYINT(1) NOT NULL DEFAULT 0,
  default_visibility VARCHAR(16) NOT NULL DEFAULT 'link',
  default_detail_level VARCHAR(16) NOT NULL DEFAULT 'park',
  email_verified_at DATETIME(6) NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  deleted_at DATETIME(6) NULL,
  deletion_scheduled_at DATETIME(6) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_users_public_id (public_id),
  UNIQUE KEY uq_users_email_normalized (email_normalized),
  KEY ix_users_status_created (status, created_at),
  CONSTRAINT chk_users_visibility CHECK (default_visibility IN ('link','logged_in','following','mutual','private')),
  CONSTRAINT chk_users_detail CHECK (default_detail_level IN ('date','park','memo','full')),
  CONSTRAINT chk_users_x_handle_visible CHECK (x_handle_visible IN (0,1)),
  CONSTRAINT chk_users_instagram_handle_visible CHECK (instagram_handle_visible IN (0,1)),
  CONSTRAINT chk_users_status CHECK (status IN ('pending','active','suspended','deletion_pending','deleted'))
) ENGINE=InnoDB;

CREATE TABLE registration_requests (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  public_id CHAR(26) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  email VARCHAR(254) NOT NULL,
  email_normalized VARCHAR(254) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  requested_ip VARBINARY(16) NULL,
  requested_user_agent VARCHAR(512) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  expires_at DATETIME(6) NOT NULL,
  consumed_at DATETIME(6) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_registration_requests_public_id (public_id),
  UNIQUE KEY uq_registration_requests_token (token_hash),
  KEY ix_registration_requests_email_expiry (email_normalized, expires_at),
  KEY ix_registration_requests_cleanup (expires_at, consumed_at)
) ENGINE=InnoDB;

CREATE TABLE user_sessions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  public_id CHAR(26) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  csrf_secret_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  ip_address VARBINARY(16) NULL,
  user_agent VARCHAR(512) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  last_seen_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  expires_at DATETIME(6) NOT NULL,
  absolute_expires_at DATETIME(6) NOT NULL,
  revoked_at DATETIME(6) NULL,
  revoke_reason VARCHAR(64) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_user_sessions_public_id (public_id),
  UNIQUE KEY uq_user_sessions_token_hash (token_hash),
  KEY ix_user_sessions_user_active (user_id, revoked_at, expires_at),
  CONSTRAINT fk_user_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE email_verifications (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  purpose VARCHAR(24) NOT NULL DEFAULT 'email_change',
  token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  requested_email VARCHAR(254) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  expires_at DATETIME(6) NOT NULL,
  used_at DATETIME(6) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_email_verifications_token (token_hash),
  KEY ix_email_verifications_user (user_id, purpose, expires_at),
  CONSTRAINT fk_email_verifications_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT chk_email_verifications_purpose CHECK (purpose IN ('email_change'))
) ENGINE=InnoDB;

CREATE TABLE password_resets (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  expires_at DATETIME(6) NOT NULL,
  used_at DATETIME(6) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_password_resets_token (token_hash),
  KEY ix_password_resets_user (user_id, expires_at),
  CONSTRAINT fk_password_resets_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE seasons (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  public_id CHAR(26) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  name VARCHAR(80) NOT NULL,
  slug VARCHAR(80) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_seasons_public_id (public_id),
  UNIQUE KEY uq_seasons_slug (slug),
  KEY ix_seasons_active_dates (is_active, start_date, end_date),
  CONSTRAINT chk_seasons_dates CHECK (start_date <= end_date)
) ENGINE=InnoDB;

CREATE TABLE visits (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  public_id CHAR(26) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  season_id BIGINT UNSIGNED NOT NULL,
  visit_date DATE NOT NULL,
  park VARCHAR(16) NULL,
  arrival_time TIME NULL,
  costume VARCHAR(100) NULL,
  memo VARCHAR(500) NULL,
  visibility VARCHAR(16) NOT NULL,
  detail_level VARCHAR(16) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_visits_public_id (public_id),
  UNIQUE KEY uq_visits_user_season_date (user_id, season_id, visit_date),
  KEY ix_visits_season_date (season_id, visit_date),
  KEY ix_visits_user_date (user_id, visit_date),
  CONSTRAINT fk_visits_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_visits_season FOREIGN KEY (season_id) REFERENCES seasons(id) ON DELETE RESTRICT,
  CONSTRAINT chk_visits_park CHECK (park IS NULL OR park IN ('land','sea','both','undecided')),
  CONSTRAINT chk_visits_visibility CHECK (visibility IN ('link','logged_in','following','mutual','private')),
  CONSTRAINT chk_visits_detail CHECK (detail_level IN ('date','park','memo','full'))
) ENGINE=InnoDB;

CREATE TABLE follows (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  follower_user_id BIGINT UNSIGNED NOT NULL,
  followed_user_id BIGINT UNSIGNED NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_follows_direction (follower_user_id, followed_user_id),
  KEY ix_follows_followed (followed_user_id, follower_user_id),
  CONSTRAINT fk_follows_follower FOREIGN KEY (follower_user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_follows_followed FOREIGN KEY (followed_user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT chk_follows_not_self CHECK (follower_user_id <> followed_user_id)
) ENGINE=InnoDB;

CREATE TABLE blocks (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  blocker_user_id BIGINT UNSIGNED NOT NULL,
  blocked_user_id BIGINT UNSIGNED NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_blocks_direction (blocker_user_id, blocked_user_id),
  KEY ix_blocks_blocked (blocked_user_id, blocker_user_id),
  CONSTRAINT fk_blocks_blocker FOREIGN KEY (blocker_user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_blocks_blocked FOREIGN KEY (blocked_user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT chk_blocks_not_self CHECK (blocker_user_id <> blocked_user_id)
) ENGINE=InnoDB;

CREATE TABLE share_tokens (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  public_id CHAR(26) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  token_last4 CHAR(4) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  active_user_id BIGINT UNSIGNED GENERATED ALWAYS AS (
    CASE WHEN status = 'active' THEN user_id ELSE NULL END
  ) STORED,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  last_used_at DATETIME(6) NULL,
  revoked_at DATETIME(6) NULL,
  revoke_reason VARCHAR(64) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_share_tokens_public_id (public_id),
  UNIQUE KEY uq_share_tokens_hash (token_hash),
  UNIQUE KEY uq_share_tokens_one_active_user (active_user_id),
  KEY ix_share_tokens_user_status (user_id, status),
  CONSTRAINT fk_share_tokens_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT chk_share_tokens_status CHECK (status IN ('active','revoked')),
  CONSTRAINT chk_share_tokens_revocation CHECK (
    (status = 'active' AND revoked_at IS NULL) OR
    (status = 'revoked' AND revoked_at IS NOT NULL)
  )
) ENGINE=InnoDB;

CREATE TABLE reports (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  public_id CHAR(26) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  reporter_user_id BIGINT UNSIGNED NULL,
  target_user_id BIGINT UNSIGNED NULL,
  target_visit_id BIGINT UNSIGNED NULL,
  category VARCHAR(32) NOT NULL,
  detail VARCHAR(1000) NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'open',
  handled_by VARCHAR(128) NULL,
  handled_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_reports_public_id (public_id),
  KEY ix_reports_status_created (status, created_at),
  KEY ix_reports_target_user (target_user_id, created_at),
  CONSTRAINT fk_reports_reporter FOREIGN KEY (reporter_user_id) REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT fk_reports_target_user FOREIGN KEY (target_user_id) REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT fk_reports_target_visit FOREIGN KEY (target_visit_id) REFERENCES visits(id) ON DELETE SET NULL,
  CONSTRAINT chk_reports_status CHECK (status IN ('open','in_progress','resolved','dismissed')),
  CONSTRAINT chk_reports_target CHECK (target_user_id IS NOT NULL OR target_visit_id IS NOT NULL)
) ENGINE=InnoDB;

CREATE TABLE security_events (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NULL,
  event_type VARCHAR(64) NOT NULL,
  severity VARCHAR(16) NOT NULL DEFAULT 'info',
  result VARCHAR(16) NOT NULL,
  ip_address VARBINARY(16) NULL,
  user_agent VARCHAR(512) NULL,
  correlation_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
  metadata_json JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY ix_security_events_type_created (event_type, created_at),
  KEY ix_security_events_user_created (user_id, created_at),
  KEY ix_security_events_severity_created (severity, created_at),
  CONSTRAINT fk_security_events_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT chk_security_events_severity CHECK (severity IN ('info','warning','critical')),
  CONSTRAINT chk_security_events_result CHECK (result IN ('success','failure','blocked'))
) ENGINE=InnoDB;

CREATE TABLE mail_logs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  public_id CHAR(26) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  user_id BIGINT UNSIGNED NULL,
  mail_type VARCHAR(48) NOT NULL,
  recipient_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  recipient_ciphertext VARBINARY(1024) NULL,
  template_data_ciphertext MEDIUMBLOB NULL,
  idempotency_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'queued',
  attempt_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  next_attempt_at DATETIME(6) NULL,
  sent_at DATETIME(6) NULL,
  last_error_code VARCHAR(64) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_mail_logs_public_id (public_id),
  UNIQUE KEY uq_mail_logs_idempotency (idempotency_key),
  KEY ix_mail_logs_queue (status, next_attempt_at, created_at),
  KEY ix_mail_logs_user_created (user_id, created_at),
  CONSTRAINT fk_mail_logs_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT chk_mail_logs_status CHECK (status IN ('queued','sending','retry_wait','sent','failed'))
) ENGINE=InnoDB;

CREATE TABLE rate_limit_counters (
  bucket_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  action_name VARCHAR(64) NOT NULL,
  window_started_at DATETIME(6) NOT NULL,
  window_seconds INT UNSIGNED NOT NULL,
  request_count INT UNSIGNED NOT NULL DEFAULT 0,
  blocked_until DATETIME(6) NULL,
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (bucket_key, action_name, window_started_at),
  KEY ix_rate_limit_cleanup (window_started_at),
  KEY ix_rate_limit_blocked (blocked_until)
) ENGINE=InnoDB;

CREATE TABLE admin_api_nonces (
  nonce_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  expires_at DATETIME(6) NOT NULL,
  PRIMARY KEY (nonce_hash),
  KEY ix_admin_api_nonces_expiry (expires_at)
) ENGINE=InnoDB;

CREATE TABLE admin_audit_logs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  admin_username VARCHAR(128) NOT NULL,
  action VARCHAR(64) NOT NULL,
  target_type VARCHAR(64) NOT NULL,
  target_id VARCHAR(128) NULL,
  idempotency_key VARCHAR(128) NULL,
  before_json JSON NULL,
  after_json JSON NULL,
  ip_address VARBINARY(16) NULL,
  result VARCHAR(16) NOT NULL,
  correlation_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_admin_audit_idempotency (idempotency_key),
  KEY ix_admin_audit_admin_created (admin_username, created_at),
  KEY ix_admin_audit_target_created (target_type, target_id, created_at),
  KEY ix_admin_audit_action_created (action, created_at),
  CONSTRAINT chk_admin_audit_result CHECK (result IN ('success','failure','denied'))
) ENGINE=InnoDB;

CREATE TABLE account_deletion_requests (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  requested_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  scheduled_for DATETIME(6) NOT NULL,
  cancel_token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  cancel_token_used_at DATETIME(6) NULL,
  cancelled_at DATETIME(6) NULL,
  completed_at DATETIME(6) NULL,
  request_reason VARCHAR(500) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_account_deletion_cancel_token (cancel_token_hash),
  KEY ix_account_deletion_due (completed_at, cancelled_at, scheduled_for),
  KEY ix_account_deletion_user (user_id, requested_at),
  CONSTRAINT fk_account_deletion_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;
