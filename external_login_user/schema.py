# -*- coding: utf-8 -*-
from __future__ import annotations
import logging
from flask import current_app
from . import bp
from app.utils.db import get_db

# MySQL エラーコード（任意）
try:
    from mysql.connector import errors as mysql_errors
except Exception:
    mysql_errors = None  # type: ignore

DDL_EXTERNAL = """
CREATE TABLE IF NOT EXISTS external_login_user (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  mfu_uuid         BINARY(16)      NOT NULL,
  social_id        VARCHAR(191)    NOT NULL,
  nickname         VARCHAR(50)     NOT NULL,
  x_id             VARCHAR(15)     NULL,
  instagram_id     VARCHAR(30)     NULL,
  email            VARCHAR(191)    NULL,
  created_at       TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_mfu_uuid   (mfu_uuid),
  UNIQUE KEY uniq_social_id  (social_id),
  KEY        idx_x_id        (x_id),
  KEY        idx_instagram   (instagram_id),
  KEY        idx_email       (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

# ★ 支払期間（pay_from / pay_until）をDDLに追加
DDL_EVENT = """
CREATE TABLE IF NOT EXISTS mfu_event (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  event_uuid      BINARY(16)      NOT NULL,
  title           VARCHAR(200)    NOT NULL,
  owner_user_id   BIGINT UNSIGNED NULL,
  starts_at       DATETIME        NULL,
  fee_yen         INT UNSIGNED    NULL,
  pay_from        DATETIME        NULL,
  pay_until       DATETIME        NULL,
  place_name      VARCHAR(200)    NULL,
  address         VARCHAR(255)    NULL,
  maps_url        VARCHAR(512)    NULL,
  sns_hashtag     VARCHAR(255)    NULL,
  google_form_url VARCHAR(512)    NULL,
  album_id        CHAR(36)        NULL,
  payment_uuid    CHAR(32)        NULL,
  created_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_event_uuid (event_uuid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

DDL_EVENT_MEMBER = """
CREATE TABLE IF NOT EXISTS mfu_event_member (
  id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  event_id           BIGINT UNSIGNED NOT NULL,
  user_id            BIGINT UNSIGNED NOT NULL,
  role               ENUM('viewer','editor') NOT NULL DEFAULT 'viewer',
  status             ENUM('pending','approved','rejected') NOT NULL DEFAULT 'pending',
  payment_status     ENUM('unpaid','pending','paid','refunded') NOT NULL DEFAULT 'unpaid',
  require_payment    TINYINT(1) NOT NULL DEFAULT 1,
  process            TINYINT(1) NOT NULL DEFAULT 0,
  paid_at            DATETIME NULL,
  payment_row_id     BIGINT UNSIGNED NULL,
  receipt_url        VARCHAR(512) NULL,
  paid_amount_yen    INT UNSIGNED NULL,
  custom_fee_yen     INT UNSIGNED NULL,
  joined_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_member (event_id, user_id),
  KEY idx_event (event_id),
  KEY idx_status (status),
  KEY idx_pstatus (payment_status),
  KEY idx_em_require_payment (require_payment)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

DDL_ALBUM_PROCESS = """
CREATE TABLE IF NOT EXISTS album_process (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  ext_user_id   BIGINT UNSIGNED NOT NULL,
  album_id      CHAR(36) NOT NULL,
  child_id      CHAR(36) NOT NULL,
  request_flag  TINYINT(1) NOT NULL DEFAULT 0,
  complete_flag TINYINT(1) NOT NULL DEFAULT 0,
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_album_process (ext_user_id, album_id, child_id),
  KEY idx_album_process_album (album_id, child_id),
  KEY idx_album_process_user (ext_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

DDL_PAYMENT_REQUEST = """
CREATE TABLE IF NOT EXISTS mfu_payment_request (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  token          CHAR(36) NOT NULL,
  event_id       BIGINT UNSIGNED NOT NULL,
  user_id        BIGINT UNSIGNED NOT NULL,
  nickname       VARCHAR(50)     NULL,
  x_id           VARCHAR(15)     NULL,
  instagram_id   VARCHAR(30)     NULL,
  amount_yen     INT UNSIGNED NOT NULL,
  status         ENUM('pending','used','canceled') NOT NULL DEFAULT 'pending',
  created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  used_at        DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_token (token),
  KEY idx_evt_user (event_id, user_id),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

def _ensure_index(cur, check_sql: str, create_sql: str):
    try:
        cur.execute(check_sql)
        has = cur.fetchone()[0]
    except Exception:
        has = 0
    if not has:
        try:
            cur.execute(create_sql)
        except Exception as e:
            if not (mysql_errors and getattr(e, "errno", None) == 1061):
                pass

@bp.record_once
def _on_bp_registered(state) -> None:
    app = state.app
    with app.app_context():
        try:
            db = get_db(); cur = db.cursor()
            # ベースDDL
            cur.execute(DDL_EXTERNAL)
            cur.execute(DDL_EVENT)
            cur.execute(DDL_EVENT_MEMBER)
            cur.execute(DDL_ALBUM_PROCESS)
            cur.execute(DDL_PAYMENT_REQUEST)

            # 後方互換ALTER（存在しなければADD）
            def _ensure_col(table: str, col: str, ddl_after_add: str) -> None:
                try:
                    cur.execute("""
                        SELECT COUNT(*) FROM information_schema.COLUMNS
                         WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s
                    """, (table, col))
                    has = cur.fetchone()[0]
                except Exception:
                    has = 0
                if not has:
                    try:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl_after_add}")
                    except Exception as e:
                        if not (mysql_errors and getattr(e, "errno", None) == 1060):
                            raise

            # 既存のゆるいALTER群
            _ensure_col("mfu_event_member", "paid_amount_yen", "paid_amount_yen INT UNSIGNED NULL AFTER paid_at")
            _ensure_col("mfu_event_member", "custom_fee_yen", "custom_fee_yen INT UNSIGNED NULL AFTER paid_amount_yen")
            _ensure_col("mfu_event_member", "process", "process TINYINT(1) NOT NULL DEFAULT 0 AFTER require_payment")

            # ★ 追加：支払期間 列（後方互換で追加）
            _ensure_col("mfu_event", "pay_from",  "pay_from DATETIME NULL AFTER fee_yen")
            _ensure_col("mfu_event", "pay_until", "pay_until DATETIME NULL AFTER pay_from")

            _ensure_col("mfu_payment_request", "nickname", "nickname VARCHAR(50) NULL AFTER user_id")
            _ensure_col("mfu_payment_request", "x_id", "x_id VARCHAR(15) NULL AFTER nickname")
            _ensure_col("mfu_payment_request", "instagram_id", "instagram_id VARCHAR(30) NULL AFTER x_id")

            _ensure_col("mfu_event", "google_form_url", "google_form_url VARCHAR(512) NULL AFTER maps_url")
            _ensure_col("mfu_event", "line_openchat_url",
                        "line_openchat_url VARCHAR(512) NULL AFTER maps_url")
            _ensure_col("mfu_event", "line_openchat_pass",
                        "line_openchat_pass VARCHAR(120) NULL AFTER line_openchat_url")
            _ensure_col("mfu_event", "sns_hashtag",
                        "sns_hashtag VARCHAR(255) NULL AFTER maps_url")
            _ensure_col("mfu_event", "google_form_url",
                        "google_form_url VARCHAR(512) NULL AFTER line_openchat_pass")
            _ensure_col("mfu_event", "memo_all",
                        "memo_all TEXT NULL AFTER google_form_url")
# === 支払方法の記録列（後方互換で追加）===
            _ensure_col("mfu_event_member", "bank_transfer",
                        "bank_transfer TINYINT(1) NOT NULL DEFAULT 0 AFTER payment_status")
            _ensure_col("mfu_event_member", "bank_dest_name",
                        "bank_dest_name VARCHAR(120) NULL AFTER bank_transfer")
            _ensure_col("mfu_event_member", "bank_remitter_name",
                        "bank_remitter_name VARCHAR(120) NULL AFTER bank_dest_name")
            _ensure_col("mfu_event_member", "bank_deposit_date",
                        "bank_deposit_date DATE NULL AFTER bank_remitter_name")

            _ensure_col("mfu_event_member", "paypay_transfer",
                        "paypay_transfer TINYINT(1) NOT NULL DEFAULT 0 AFTER bank_deposit_date")
            _ensure_col("mfu_event_member", "paypay_sent_date",
                        "paypay_sent_date DATE NULL AFTER paypay_transfer")
            _ensure_col("mfu_event_member", "paypay_sender_name",
                        "paypay_sender_name VARCHAR(120) NULL AFTER paypay_sent_date")


# --- 既存 _on_bp_registered 内の後方互換ALTERに追記 ---
            _ensure_col("mfu_event", "allow_square", "allow_square TINYINT(1) NOT NULL DEFAULT 1 AFTER fee_yen")
            _ensure_col("mfu_event", "allow_paypay", "allow_paypay TINYINT(1) NOT NULL DEFAULT 0 AFTER allow_square")
            _ensure_col("mfu_event", "allow_bank",   "allow_bank   TINYINT(1) NOT NULL DEFAULT 0 AFTER allow_paypay")
            _ensure_col("mfu_event", "paypay_display",
                        "paypay_display VARCHAR(200) NULL AFTER allow_bank")

# --- イベント別銀行口座テーブル（なければ作成）---
            try:
                cur.execute("""
                  CREATE TABLE IF NOT EXISTS mfu_event_bank_accounts (
                    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    event_id      BIGINT UNSIGNED NOT NULL,
                    label         VARCHAR(80)   NOT NULL,   -- 例: "三井住友（撮影用）"
                    bank_name     VARCHAR(80)   NOT NULL,   -- 例: "三井住友銀行"
                    branch_name   VARCHAR(80)   NULL,       -- 例: "渋谷支店"
                    account_type  ENUM('普通','当座','貯蓄','その他') NOT NULL DEFAULT '普通',
                    account_no    VARCHAR(32)   NOT NULL,   -- 例: "1234567"
                    holder        VARCHAR(120)  NOT NULL,   -- 例: "ｲｵﾘ ﾀﾛｳ"
                    note          VARCHAR(255)  NULL,       -- 備考（振込名義/期限など）
                    display_order INT           NOT NULL DEFAULT 0,
                    visible       TINYINT(1)    NOT NULL DEFAULT 1,
                    created_at    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    KEY idx_event (event_id),
                    CONSTRAINT fk_mfu_event_bank_accounts_event
                      FOREIGN KEY (event_id) REFERENCES mfu_event(id) ON DELETE CASCADE
                  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
                """)
            except Exception:
                pass


            # 既存インデックスガード
            _ensure_index(cur, """
                SELECT COUNT(*) FROM information_schema.STATISTICS
                 WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='mfu_event_member' AND INDEX_NAME='idx_status'
            """, "CREATE INDEX idx_status ON mfu_event_member(status)")
            _ensure_index(cur, """
                SELECT COUNT(*) FROM information_schema.STATISTICS
                 WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='mfu_event_member' AND INDEX_NAME='idx_pstatus'
            """, "CREATE INDEX idx_pstatus ON mfu_event_member(payment_status)")
            _ensure_index(cur, """
                SELECT COUNT(*) FROM information_schema.STATISTICS
                 WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='mfu_event_member' AND INDEX_NAME='idx_em_require_payment'
            """, "CREATE INDEX idx_em_require_payment ON mfu_event_member(require_payment)")

            db.commit(); cur.close(); db.close()
            app.logger.info("[external_login_user] ensured tables + migrations (with pay_from/pay_until)")
        except Exception as e:
            app.logger.exception("DDL/migration failed: %s", e)

# ★外部ログイン利用者の通知設定（既定は受け取る=1）
            _ensure_col("external_login_user", "notify_album_upload",
                        "notify_album_upload TINYINT(1) NOT NULL DEFAULT 1 AFTER email")
            _ensure_col("external_login_user", "notify_album_process",
                        "notify_album_process TINYINT(1) NOT NULL DEFAULT 1 AFTER notify_album_upload")

# roles/require_payment のゆるい追加ガード（既存のまま）
@bp.before_app_request
def _roles_schema_guard():
    try:
        db = get_db(); cur = db.cursor()
        cur.execute("SHOW COLUMNS FROM mfu_event_member")
        rows = cur.fetchall() or []
        cols = set()
        for r in rows:
            try:
                cols.add(r[0])
            except Exception:
                cols.add(r.get("Field"))

        def _add(sql):
            try:
                cur.execute(sql); db.commit()
            except Exception:
                try: db.rollback()
                except Exception: pass

        if "is_host" not in cols:
            _add("ALTER TABLE mfu_event_member ADD COLUMN is_host TINYINT(1) NOT NULL DEFAULT 0")
        if "is_subhost" not in cols:
            _add("ALTER TABLE mfu_event_member ADD COLUMN is_subhost TINYINT(1) NOT NULL DEFAULT 0 AFTER is_host")
        if "participant_role" not in cols:
            _add("ALTER TABLE mfu_event_member ADD COLUMN participant_role ENUM('none','camera','assistant','cosplayer') NOT NULL DEFAULT 'none' AFTER is_subhost")
        if "costume_label" not in cols:
            _add("ALTER TABLE mfu_event_member ADD COLUMN costume_label VARCHAR(120) NULL AFTER participant_role")
        if "require_payment" not in cols:
            _add("ALTER TABLE mfu_event_member ADD COLUMN require_payment TINYINT(1) NOT NULL DEFAULT 1")
    except Exception:
        current_app.logger.debug("roles/require_payment guard skipped", exc_info=True)


# schema.py 末尾あたりに追記
from app.utils.db import get_db

def ensure_email_verification_schema() -> None:
    db = get_db(); cur = db.cursor()
    try:
        # A. external_login_user に email_verified_at が無ければ追加
        cur.execute("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE()
               AND TABLE_NAME = 'external_login_user'
               AND COLUMN_NAME = 'email_verified_at'
        """)
        has_col = (cur.fetchone()[0] > 0)
        if not has_col:
            cur.execute("""
                ALTER TABLE external_login_user
                  ADD COLUMN email_verified_at DATETIME NULL AFTER email
            """)
            db.commit()

        # B. mfu_email_verification テーブルを作成
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mfu_email_verification (
              id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
              user_id    BIGINT UNSIGNED NOT NULL,
              email      VARCHAR(255)    NOT NULL,
              token      CHAR(64)        NOT NULL,
              expires_at DATETIME        NOT NULL,
              used_at    DATETIME        NULL,
              created_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE KEY uq_token (token),
              KEY idx_user (user_id),
              CONSTRAINT fk_emailv_user FOREIGN KEY (user_id)
                REFERENCES external_login_user(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        db.commit()
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

# schema.py 末尾付近（DDL群の定義の後）に追加
@bp.before_app_request
def _ensure_event_acl_table():
    try:
        db = get_db(); cur = db.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS mfu_event_admin_acl (
          id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          event_id   BIGINT UNSIGNED NOT NULL,
          username   VARCHAR(191)    NOT NULL,
          role       ENUM('viewer','manager') NOT NULL DEFAULT 'viewer',
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (id),
          UNIQUE KEY uniq_ev_user (event_id, username),
          KEY idx_ev (event_id),
          KEY idx_user (username)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """)
        db.commit()
    except Exception as e:
        try: db.rollback()
        except Exception: pass
        current_app.logger.exception("ensure mfu_event_admin_acl failed: %s", e)
    finally:
        try: cur.close(); db.close()
        except Exception: pass

# ===== ここから追記（schema.py） =====
def _ensure_event_bank_table(cur, db):
    """
    イベント別の振込先口座テーブルを（なければ）作成。
    """
    # すでにあれば何もしない
    cur.execute("""
        SELECT COUNT(*) FROM information_schema.TABLES
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'mfu_event_bank'
    """)
    if (cur.fetchone()[0] or 0) > 0:
        return

    # 作成（最小構成）
    cur.execute("""
        CREATE TABLE mfu_event_bank (
          id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          event_id         BIGINT UNSIGNED NOT NULL,
          label            VARCHAR(80)   NOT NULL,     -- 表示名: 例) 三井住友（撮影用）
          bank_name        VARCHAR(80)   NOT NULL,     -- 銀行名
          branch_name      VARCHAR(80)   NULL,         -- 支店名
          account_kind     ENUM('普通','当座','貯蓄','その他') NOT NULL DEFAULT '普通',
          account_number   VARCHAR(32)   NOT NULL,     -- 口座番号
          account_holder   VARCHAR(120)  NOT NULL,     -- 名義（カナ等）
          memo             VARCHAR(255)  NULL,
          sort_order       INT           NOT NULL DEFAULT 0,
          is_active        TINYINT(1)    NOT NULL DEFAULT 1,
          created_at       TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (id),
          KEY idx_event (event_id),
          CONSTRAINT fk_event_bank_event
            FOREIGN KEY (event_id) REFERENCES mfu_event(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    """)
    db.commit()
# ===== 追記ここまで =====
