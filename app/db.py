"""SQLite schema, connection helpers and transaction boundaries."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_code TEXT NOT NULL REFERENCES companies(code),
    batch_number INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    route_count INTEGER NOT NULL,
    stop_count INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    pruned_stop_rows INTEGER NOT NULL DEFAULT 0,
    UNIQUE (company_code, batch_number)
);

CREATE TABLE IF NOT EXISTS batch_stops (
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    route_code TEXT NOT NULL,
    seq INTEGER NOT NULL,
    stop_code TEXT,
    stop_name TEXT NOT NULL,
    PRIMARY KEY (batch_id, route_code, seq)
);

CREATE INDEX IF NOT EXISTS idx_batch_stops_route
    ON batch_stops (route_code, batch_id);

CREATE TABLE IF NOT EXISTS route_ownership (
    route_code TEXT PRIMARY KEY,
    owner_company TEXT NOT NULL REFERENCES companies(code),
    first_company TEXT NOT NULL REFERENCES companies(code),
    first_batch_id INTEGER NOT NULL REFERENCES batches(id),
    first_reported_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfer_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL CHECK (status IN ('open', 'committed', 'cancelled')),
    created_at TEXT NOT NULL,
    committed_at TEXT,
    cancelled_at TEXT
);

-- 一次只办一张换线单。
CREATE UNIQUE INDEX IF NOT EXISTS ux_transfer_orders_one_open
    ON transfer_orders (status) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS transfer_lines (
    order_id INTEGER NOT NULL REFERENCES transfer_orders(id) ON DELETE CASCADE,
    route_code TEXT NOT NULL,
    from_company TEXT NOT NULL REFERENCES companies(code),
    to_company TEXT NOT NULL REFERENCES companies(code),
    frozen_batch_id INTEGER REFERENCES batches(id),
    PRIMARY KEY (order_id, route_code)
);

CREATE INDEX IF NOT EXISTS idx_transfer_lines_frozen
    ON transfer_lines (frozen_batch_id, route_code);

-- 下游取数结果：每条在供线路当前由哪个批次供数。
CREATE TABLE IF NOT EXISTS route_serving (
    route_code TEXT PRIMARY KEY,
    company_code TEXT NOT NULL REFERENCES companies(code),
    source_batch_id INTEGER NOT NULL REFERENCES batches(id),
    is_frozen INTEGER NOT NULL DEFAULT 0 CHECK (is_frozen IN (0, 1)),
    updated_at TEXT NOT NULL
);
"""


def connect(settings: Settings) -> sqlite3.Connection:
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level=None,  # 手动控制事务边界
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(settings: Settings) -> None:
    conn = connect(settings)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO companies (code, name) VALUES (?, ?)",
            settings.seed_companies,
        )
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialised write transaction. Any exception rolls everything back."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Snapshot read: every statement inside sees the same committed state.

    A deferred ``BEGIN`` takes no write lock, so a long-ish read never blocks
    concurrent reports; under WAL the snapshot stays valid even while another
    connection commits in the meantime. Never nest this inside
    :func:`transaction` on the same connection.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
