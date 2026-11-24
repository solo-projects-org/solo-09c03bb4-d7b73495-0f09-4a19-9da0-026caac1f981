"""SQLite 持久化层。

表设计要点：
- holding_file：每期完整持仓（期初/期末各一行，重复上传整表替换并触发重算）。
- announcement_batch：公告整表版本。status=active 的只有一份；
  换报校验/折算失败时新批次记为 failed，原 active 批次不动，原表仍可查。
- result_batch / result_lot：每次生效的（持仓或公告）变动产生一个结果批次，
  result_lot 按期初来源逐行保存折算后的代码、股数与折算路径。
"""

from __future__ import annotations

import sqlite3
from flask import current_app, g

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS holding_file (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    period      TEXT NOT NULL CHECK (period IN ('begin', 'end')),
    as_of_date  TEXT NOT NULL,
    filename    TEXT NOT NULL,
    row_count   INTEGER NOT NULL,
    is_current  INTEGER NOT NULL DEFAULT 1,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_holding_current ON holding_file(period, is_current);

CREATE TABLE IF NOT EXISTS holding_row (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id  INTEGER NOT NULL REFERENCES holding_file(id),
    code     TEXT NOT NULL,
    shares   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_holding_row_file ON holding_row(file_id);

CREATE TABLE IF NOT EXISTS announcement_batch (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    status         TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'failed')),
    filename       TEXT NOT NULL,
    row_count      INTEGER NOT NULL,
    note           TEXT,
    uploaded_at    TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_at  TEXT
);

CREATE TABLE IF NOT EXISTS announcement (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id       INTEGER NOT NULL REFERENCES announcement_batch(id),
    effective_date TEXT NOT NULL,
    old_code       TEXT NOT NULL,
    new_code       TEXT NOT NULL,
    ratio          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_announcement_batch ON announcement(batch_id);

CREATE TABLE IF NOT EXISTS result_batch (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_batch_id INTEGER NOT NULL REFERENCES announcement_batch(id),
    begin_file_id        INTEGER NOT NULL REFERENCES holding_file(id),
    end_file_id          INTEGER REFERENCES holding_file(id),
    begin_date           TEXT NOT NULL,
    end_date             TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    trigger_note         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS result_lot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    result_batch_id INTEGER NOT NULL REFERENCES result_batch(id),
    origin_code     TEXT NOT NULL,
    end_code        TEXT NOT NULL,
    adjusted_shares TEXT NOT NULL,
    path_json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_result_lot_batch ON result_lot(result_batch_id);
CREATE INDEX IF NOT EXISTS idx_result_lot_code ON result_lot(result_batch_id, end_code);

-- 预置一份空的 active 公告批次，保证任何时候都有可引用的有效公告表；
-- 首次上传公告时整表替换它。
INSERT INTO announcement_batch (status, filename, row_count, note)
SELECT 'active', '(系统初始空表)', 0, '系统初始化，尚未上传公告'
WHERE NOT EXISTS (SELECT 1 FROM announcement_batch);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db


def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db(app.config["DATABASE"])
