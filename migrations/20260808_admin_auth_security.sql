CREATE TABLE IF NOT EXISTS admin_auth_state (
    username VARCHAR(191) PRIMARY KEY,
    auth_version BIGINT NOT NULL DEFAULT 1,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS admin_auth_sessions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    sid_hash CHAR(64) NOT NULL UNIQUE,
    username VARCHAR(191) NOT NULL,
    auth_version BIGINT NOT NULL,
    auth_method VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL,
    last_seen_at DATETIME NOT NULL,
    expires_at DATETIME NOT NULL,
    mfa_verified_at DATETIME NOT NULL,
    ip VARCHAR(64), user_agent VARCHAR(255), revoked_at DATETIME,
    revoke_reason VARCHAR(191),
    INDEX idx_admin_session_user (username, revoked_at, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS admin_auth_attempts (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(191) NOT NULL, ip VARCHAR(64) NOT NULL,
    stage VARCHAR(32) NOT NULL, success TINYINT(1) NOT NULL,
    attempted_at DATETIME NOT NULL,
    INDEX idx_admin_attempt_user (username, stage, attempted_at),
    INDEX idx_admin_attempt_ip (ip, stage, attempted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS admin_qr_login_challenges (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    token_hash CHAR(64) NOT NULL UNIQUE,
    desktop_nonce_hash CHAR(64) NOT NULL,
    username VARCHAR(191) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    created_at DATETIME NOT NULL, expires_at DATETIME NOT NULL,
    desktop_ip VARCHAR(64), desktop_user_agent VARCHAR(255),
    approved_at DATETIME, approved_by_sid_hash CHAR(64), consumed_at DATETIME,
    INDEX idx_admin_qr_status (status, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO admin_auth_state (username, auth_version) VALUES ('admin', 1);
