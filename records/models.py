from datetime import datetime

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


def ensure_records_schema() -> None:
    db = get_db()
    try:
        ensure_uber_schema(db)
        ensure_maintenance_items_schema(db)
        ensure_maintenance_schema(db)
        ensure_fuel_schema(db)
    finally:
        db.close()


def now_ts() -> datetime:
    return datetime.now()
