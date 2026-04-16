-- 配送追跡管理テーブル追加

START TRANSACTION;

CREATE TABLE IF NOT EXISTS shipment_tracking_target (
    id INT AUTO_INCREMENT PRIMARY KEY,
    carrier_code VARCHAR(32) NOT NULL,
    tracking_number VARCHAR(64) NOT NULL,
    label VARCHAR(255) NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    last_checked_at DATETIME NULL,
    last_check_success_at DATETIME NULL,
    last_error_text TEXT NULL,
    last_payload_json LONGTEXT NULL,
    last_current_status VARCHAR(255) NULL,
    last_current_status_detail TEXT NULL,
    last_latest_event_at DATETIME NULL,
    last_completed TINYINT(1) NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_shipment_tracking_target_carrier_number (carrier_code, tracking_number)
);

CREATE TABLE IF NOT EXISTS shipment_tracking_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    target_id INT NOT NULL,
    triggered_by VARCHAR(16) NOT NULL,
    checked_at DATETIME NOT NULL,
    success TINYINT(1) NOT NULL,
    changed TINYINT(1) NOT NULL,
    error_text TEXT NULL,
    payload_json LONGTEXT NULL,
    current_status VARCHAR(255) NULL,
    current_status_detail TEXT NULL,
    latest_event_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_shipment_tracking_log_target_id (target_id),
    CONSTRAINT fk_shipment_tracking_log_target_id FOREIGN KEY (target_id)
        REFERENCES shipment_tracking_target(id)
        ON DELETE RESTRICT
        ON UPDATE CASCADE
);

COMMIT;
