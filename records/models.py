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
            odometer_km DECIMAL(10, 1) NOT NULL,
            trip_km DECIMAL(8, 2) NULL,
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
        SELECT column_name, data_type, numeric_scale
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'bike_fuel_log'
        """
    )
    columns = {row[0]: row[1:] for row in cur.fetchall()}
    if "trip_km" not in columns:
        cur.execute("ALTER TABLE bike_fuel_log ADD COLUMN trip_km DECIMAL(8, 2) NULL")
    odometer_meta = columns.get("odometer_km")
    if odometer_meta is not None:
        data_type, numeric_scale = odometer_meta
        if data_type != "decimal" or (numeric_scale is not None and numeric_scale < 1):
            cur.execute(
                "ALTER TABLE bike_fuel_log MODIFY COLUMN odometer_km DECIMAL(10, 1) NOT NULL"
            )
    db.commit()
    if close_db:
        db.close()


def ensure_records_schema() -> None:
    db = get_db()
    try:
        ensure_uber_schema(db)
        ensure_maintenance_schema(db)
        ensure_fuel_schema(db)
    finally:
        db.close()


def now_ts() -> datetime:
    return datetime.now()
