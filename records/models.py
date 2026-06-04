from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from app.utils.db import get_db


def ensure_uber_schema(db=None) -> None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS uber_daily (
            id INT AUTO_INCREMENT PRIMARY KEY,
            work_date DATE NOT NULL UNIQUE,
            deliveries INT NOT NULL DEFAULT 0,
            net_yen INT NOT NULL DEFAULT 0,
            promo_yen INT NOT NULL DEFAULT 0,
            other_yen INT NOT NULL DEFAULT 0,
            tip_yen INT NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            INDEX(work_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'uber_daily'
        """
    )
    columns = {row[0] for row in cur.fetchall()}
    if "freee_exported_at" not in columns:
        cur.execute(
            "ALTER TABLE uber_daily ADD COLUMN freee_exported_at DATETIME NULL AFTER updated_at"
        )
    if "freee_deal_id" not in columns:
        cur.execute(
            "ALTER TABLE uber_daily ADD COLUMN freee_deal_id BIGINT NULL AFTER freee_exported_at"
        )
    if "freee_api_synced_at" not in columns:
        cur.execute(
            "ALTER TABLE uber_daily ADD COLUMN freee_api_synced_at DATETIME NULL AFTER freee_deal_id"
        )
    if "freee_api_status" not in columns:
        cur.execute(
            "ALTER TABLE uber_daily ADD COLUMN freee_api_status VARCHAR(32) NULL AFTER freee_api_synced_at"
        )
    if "freee_api_error" not in columns:
        cur.execute(
            "ALTER TABLE uber_daily ADD COLUMN freee_api_error TEXT NULL AFTER freee_api_status"
        )
    db.commit()
    if close_db:
        db.close()


def ensure_freee_schema(db=None) -> None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS freee_oauth_tokens (
            id INT AUTO_INCREMENT PRIMARY KEY,
            provider VARCHAR(32) NOT NULL DEFAULT 'freee',
            access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            expires_at DATETIME NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            UNIQUE KEY uniq_provider (provider)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS freee_accounting_settings (
            id INT AUTO_INCREMENT PRIMARY KEY,
            company_id BIGINT NULL,
            partner_id BIGINT NULL,
            partner_code VARCHAR(191) NULL,
            account_item_id BIGINT NULL,
            tax_code INT NULL,
            walletable_type VARCHAR(64) NULL,
            walletable_id BIGINT NULL,
            deal_payment_mode VARCHAR(32) NOT NULL DEFAULT 'settled',
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    db.commit()
    if close_db:
        db.close()


def ensure_uber_ocr_queue_schema(db=None) -> None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS uber_ocr_queue (
            id INT AUTO_INCREMENT PRIMARY KEY,
            status VARCHAR(32) NOT NULL,
            work_date DATE NOT NULL,
            deliveries INT NOT NULL DEFAULT 0,
            net_yen INT NOT NULL DEFAULT 0,
            promo_yen INT NOT NULL DEFAULT 0,
            other_yen INT NOT NULL DEFAULT 0,
            tip_yen INT NOT NULL DEFAULT 0,
            warnings_json TEXT NULL,
            image_path VARCHAR(512) NOT NULL,
            mime_type VARCHAR(128) NULL,
            original_filename VARCHAR(255) NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            INDEX(status),
            INDEX(created_at),
            INDEX(work_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    db.commit()
    if close_db:
        db.close()


def ensure_maintenance_items_schema(db=None) -> None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS maintenance_items (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(191) NOT NULL,
            target_km INT NULL,
            sort_order INT NOT NULL,
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            INDEX(sort_order),
            INDEX(is_active)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute("SELECT COUNT(*) FROM maintenance_items")
    has_rows = cur.fetchone()[0] > 0
    if not has_rows:
        now = now_ts()
        seed_items = [
            ("オイル交換", 1),
            ("プラグ交換", 2),
            ("リアタイヤ交換", 3),
            ("フロントタイヤ交換", 4),
            ("Vベルト交換", 5),
            ("ウェイトローラー交換", 6),
            ("エアフィルター交換", 7),
            ("ブレーキフルード交換", 8),
            ("リアブレーキパッド交換", 9),
            ("フロントブレーキパッド交換", 10),
        ]
        cur.executemany(
            """
            INSERT INTO maintenance_items (
                name,
                target_km,
                sort_order,
                is_active,
                created_at,
                updated_at
            ) VALUES (%s, %s, %s, 1, %s, %s)
            """,
            [(name, None, order, now, now) for name, order in seed_items],
        )
    db.commit()
    if close_db:
        db.close()


def ensure_maintenance_schema(db=None) -> None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bike_maintenance_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            event_date DATE NOT NULL,
            odometer_km INT NOT NULL,
            item VARCHAR(191) NOT NULL,
            note TEXT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            INDEX(event_date),
            INDEX(item),
            INDEX(odometer_km)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'bike_maintenance_log'
        """
    )
    columns = {row[0] for row in cur.fetchall()}
    if "item_id" not in columns:
        cur.execute(
            "ALTER TABLE bike_maintenance_log ADD COLUMN item_id INT NULL AFTER odometer_km"
        )
    cur.execute(
        """
        SELECT index_name
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 'bike_maintenance_log'
          AND index_name = 'idx_bike_maintenance_item_id'
        """
    )
    if not cur.fetchone():
        cur.execute(
            "CREATE INDEX idx_bike_maintenance_item_id ON bike_maintenance_log (item_id)"
        )
    db.commit()
    if close_db:
        db.close()


def ensure_fuel_schema(db=None) -> None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bike_fuel_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            fill_date DATE NOT NULL,
            odometer_km DECIMAL(10, 1) NULL,
            trip_km DECIMAL(10, 1) NULL,
            liters DECIMAL(8, 2) NOT NULL,
            yen_per_liter INT NULL,
            is_full TINYINT(1) NOT NULL DEFAULT 1,
            note TEXT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            INDEX(fill_date),
            INDEX(odometer_km)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        SELECT column_name, data_type, numeric_precision, numeric_scale, is_nullable
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'bike_fuel_log'
        """
    )
    columns = {row[0]: row[1:] for row in cur.fetchall()}
    if "trip_km" not in columns:
        cur.execute("ALTER TABLE bike_fuel_log ADD COLUMN trip_km DECIMAL(10, 1) NULL")

    def needs_decimal_10_1_nullable(meta: tuple | None) -> bool:
        if meta is None:
            return True
        data_type, precision, scale, is_nullable = meta
        if data_type != "decimal":
            return True
        if precision is not None and precision != 10:
            return True
        if scale is not None and scale != 1:
            return True
        return is_nullable != "YES"

    if needs_decimal_10_1_nullable(columns.get("odometer_km")):
        cur.execute(
            "ALTER TABLE bike_fuel_log MODIFY COLUMN odometer_km DECIMAL(10, 1) NULL"
        )
    if needs_decimal_10_1_nullable(columns.get("trip_km")):
        cur.execute("ALTER TABLE bike_fuel_log MODIFY COLUMN trip_km DECIMAL(10, 1) NULL")
    db.commit()
    if close_db:
        db.close()


def ensure_records_kv_schema(db=None) -> None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS records_kv (
            `key` VARCHAR(191) NOT NULL PRIMARY KEY,
            value_decimal DECIMAL(10, 1) NULL,
            updated_at DATETIME NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    db.commit()
    if close_db:
        db.close()


def _quantize_decimal(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _ensure_current_odometer_row(cur) -> Decimal:
    cur.execute(
        "SELECT value_decimal FROM records_kv WHERE `key` = %s",
        ("current_odometer_km",),
    )
    row = cur.fetchone()
    if row and row[0] is not None:
        return _quantize_decimal(Decimal(row[0]))
    cur.execute(
        "SELECT MAX(odometer_km) FROM bike_fuel_log WHERE odometer_km IS NOT NULL"
    )
    max_row = cur.fetchone()
    if max_row and max_row[0] is not None:
        initial_value = _quantize_decimal(Decimal(max_row[0]))
    else:
        initial_value = Decimal("0.0")
    now = now_ts()
    cur.execute(
        """
        REPLACE INTO records_kv (`key`, value_decimal, updated_at)
        VALUES (%s, %s, %s)
        """,
        ("current_odometer_km", initial_value, now),
    )
    return initial_value


def get_current_odometer_km(*, db=None) -> Decimal:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    ensure_records_kv_schema(db)
    cur = db.cursor()
    current_value = _ensure_current_odometer_row(cur)
    db.commit()
    if close_db:
        db.close()
    return current_value


def set_current_odometer_km(value: Decimal, *, db=None) -> Decimal:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    ensure_records_kv_schema(db)
    cur = db.cursor()
    normalized = _quantize_decimal(Decimal(value))
    now = now_ts()
    cur.execute(
        """
        REPLACE INTO records_kv (`key`, value_decimal, updated_at)
        VALUES (%s, %s, %s)
        """,
        ("current_odometer_km", normalized, now),
    )
    db.commit()
    if close_db:
        db.close()
    return normalized


def ensure_records_schema() -> None:
    db = get_db()
    try:
        ensure_uber_schema(db)
        ensure_freee_schema(db)
        ensure_uber_ocr_queue_schema(db)
        ensure_maintenance_items_schema(db)
        ensure_maintenance_schema(db)
        ensure_fuel_schema(db)
        ensure_records_kv_schema(db)
    finally:
        db.close()


def now_ts() -> datetime:
    return datetime.now()


def list_maintenance_items(
    *,
    include_inactive: bool = False,
    db=None,
) -> list[dict]:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    cur = db.cursor(dictionary=True)
    if include_inactive:
        cur.execute(
            """
            SELECT id, name, target_km, sort_order, is_active
            FROM maintenance_items
            ORDER BY sort_order, id
            """
        )
    else:
        cur.execute(
            """
            SELECT id, name, target_km, sort_order
            FROM maintenance_items
            WHERE is_active = 1
            ORDER BY sort_order, id
            """
        )
    rows = cur.fetchall()
    if close_db:
        db.close()
    return rows


def insert_maintenance_item(
    name: str,
    target_km: int | None,
    sort_order: int | None,
    is_active: bool,
    *,
    db=None,
) -> int:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    cur = db.cursor()
    if sort_order is None:
        cur.execute("SELECT COALESCE(MAX(sort_order), 0) FROM maintenance_items")
        sort_order = int(cur.fetchone()[0] or 0) + 10
    now = now_ts()
    cur.execute(
        """
        INSERT INTO maintenance_items (
            name,
            target_km,
            sort_order,
            is_active,
            created_at,
            updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (name, target_km, sort_order, 1 if is_active else 0, now, now),
    )
    item_id = cur.lastrowid
    db.commit()
    if close_db:
        db.close()
    return int(item_id)


def update_maintenance_item(
    item_id: int,
    name: str,
    target_km: int | None,
    sort_order: int,
    is_active: bool,
    *,
    db=None,
) -> None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    cur = db.cursor()
    now = now_ts()
    cur.execute(
        """
        UPDATE maintenance_items
        SET name = %s,
            target_km = %s,
            sort_order = %s,
            is_active = %s,
            updated_at = %s
        WHERE id = %s
        """,
        (name, target_km, sort_order, 1 if is_active else 0, now, item_id),
    )
    db.commit()
    if close_db:
        db.close()
