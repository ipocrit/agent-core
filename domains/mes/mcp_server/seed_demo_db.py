"""生成 MES 风格的演示数据库（全部为虚构数据）。

用法：uv run seed_demo_db.py
会覆盖 data/mes_demo.db。
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "mes_demo.db"

SCHEMA = """
CREATE TABLE products (
    product_id   TEXT PRIMARY KEY,
    product_name TEXT NOT NULL
);

CREATE TABLE stations (
    station_id   TEXT PRIMARY KEY,
    station_name TEXT NOT NULL,
    process      TEXT NOT NULL,          -- 工序
    qtime_limit_min INTEGER              -- 进站前允许的最长等待（分钟），NULL 表示不管控
);

CREATE TABLE work_orders (
    wo_id       TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL REFERENCES products(product_id),
    plan_qty    INTEGER NOT NULL,
    status      TEXT NOT NULL,           -- RELEASED / CLOSED
    created_at  TEXT NOT NULL
);

CREATE TABLE lots (
    lot_id          TEXT PRIMARY KEY,
    wo_id           TEXT NOT NULL REFERENCES work_orders(wo_id),
    qty             INTEGER NOT NULL,
    current_station TEXT REFERENCES stations(station_id),
    status          TEXT NOT NULL,       -- WAIT / RUN / HOLD / DONE
    updated_at      TEXT NOT NULL
);

CREATE TABLE lot_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id      TEXT NOT NULL REFERENCES lots(lot_id),
    station_id  TEXT NOT NULL REFERENCES stations(station_id),
    event       TEXT NOT NULL,           -- TRACK_IN / TRACK_OUT / HOLD / RELEASE
    event_time  TEXT NOT NULL,
    note        TEXT
);

CREATE TABLE holds (
    hold_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id      TEXT NOT NULL REFERENCES lots(lot_id),
    station_id  TEXT NOT NULL REFERENCES stations(station_id),
    reason_code TEXT NOT NULL,           -- QTIME_OVER / EQP_DOWN / QUALITY
    reason      TEXT NOT NULL,
    hold_time   TEXT NOT NULL,
    released    INTEGER NOT NULL DEFAULT 0
);
"""

PRODUCTS = [("P-ARRAY-01", "Array 基板 A 型"), ("P-ARRAY-02", "Array 基板 B 型")]

STATIONS = [
    ("ST-CLN", "清洗", "CLEAN", None),
    ("ST-CVD", "化学气相沉积", "CVD", 120),
    ("ST-PHO", "曝光", "PHOTO", 240),
    ("ST-ETC", "刻蚀", "ETCH", None),
    ("ST-INS", "检测", "INSPECT", None),
]

WORK_ORDERS = [
    ("WO-2026-1001", "P-ARRAY-01", 200, "RELEASED", "2026-10-18 08:00"),
    ("WO-2026-1002", "P-ARRAY-02", 120, "RELEASED", "2026-10-18 09:30"),
]

LOTS = [
    ("LOT-A001", "WO-2026-1001", 25, "ST-CVD", "HOLD", "2026-10-19 02:40"),
    ("LOT-A002", "WO-2026-1001", 25, "ST-PHO", "RUN", "2026-10-19 03:10"),
    ("LOT-A003", "WO-2026-1001", 25, "ST-CVD", "WAIT", "2026-10-19 01:05"),
    ("LOT-B001", "WO-2026-1002", 20, "ST-ETC", "HOLD", "2026-10-19 00:20"),
    ("LOT-B002", "WO-2026-1002", 20, "ST-INS", "DONE", "2026-10-18 22:00"),
]

HISTORY = [
    ("LOT-A001", "ST-CLN", "TRACK_IN", "2026-10-18 23:00", None),
    ("LOT-A001", "ST-CLN", "TRACK_OUT", "2026-10-18 23:40", None),
    ("LOT-A001", "ST-CVD", "HOLD", "2026-10-19 02:40", "清洗出站后等待 180 分钟，超过 CVD Q-time 120 分钟"),
    ("LOT-A002", "ST-CLN", "TRACK_IN", "2026-10-18 23:10", None),
    ("LOT-A002", "ST-CLN", "TRACK_OUT", "2026-10-18 23:50", None),
    ("LOT-A002", "ST-CVD", "TRACK_IN", "2026-10-19 00:30", None),
    ("LOT-A002", "ST-CVD", "TRACK_OUT", "2026-10-19 01:50", None),
    ("LOT-A002", "ST-PHO", "TRACK_IN", "2026-10-19 03:10", None),
    ("LOT-A003", "ST-CLN", "TRACK_IN", "2026-10-19 00:00", None),
    ("LOT-A003", "ST-CLN", "TRACK_OUT", "2026-10-19 00:40", None),
    ("LOT-B001", "ST-CLN", "TRACK_IN", "2026-10-18 20:00", None),
    ("LOT-B001", "ST-CLN", "TRACK_OUT", "2026-10-18 20:30", None),
    ("LOT-B001", "ST-CVD", "TRACK_IN", "2026-10-18 21:00", None),
    ("LOT-B001", "ST-CVD", "TRACK_OUT", "2026-10-18 22:10", None),
    ("LOT-B001", "ST-ETC", "HOLD", "2026-10-19 00:20", "刻蚀设备 ETC-02 报警停机"),
    ("LOT-B002", "ST-INS", "TRACK_OUT", "2026-10-18 22:00", None),
]

HOLDS = [
    ("LOT-A001", "ST-CVD", "QTIME_OVER", "Q-time 超时：清洗出站至 CVD 进站超过 120 分钟", "2026-10-19 02:40", 0),
    ("LOT-B001", "ST-ETC", "EQP_DOWN", "设备异常：ETC-02 报警停机", "2026-10-19 00:20", 0),
]


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO products VALUES (?, ?)", PRODUCTS)
        conn.executemany("INSERT INTO stations VALUES (?, ?, ?, ?)", STATIONS)
        conn.executemany("INSERT INTO work_orders VALUES (?, ?, ?, ?, ?)", WORK_ORDERS)
        conn.executemany("INSERT INTO lots VALUES (?, ?, ?, ?, ?, ?)", LOTS)
        conn.executemany(
            "INSERT INTO lot_history (lot_id, station_id, event, event_time, note) VALUES (?, ?, ?, ?, ?)",
            HISTORY,
        )
        conn.executemany(
            "INSERT INTO holds (lot_id, station_id, reason_code, reason, hold_time, released) VALUES (?, ?, ?, ?, ?, ?)",
            HOLDS,
        )
    print(f"已生成演示数据库：{DB_PATH}")


if __name__ == "__main__":
    main()
