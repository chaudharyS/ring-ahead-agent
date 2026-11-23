import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app import bus, config

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  customer_name TEXT NOT NULL,
  customer_phone TEXT,
  restaurant TEXT NOT NULL,
  address_line TEXT NOT NULL,
  address_type TEXT NOT NULL,
  unit TEXT,
  dropoff_note TEXT,
  dropoff_pref TEXT NOT NULL,
  contains_alcohol INTEGER NOT NULL DEFAULT 0,
  call_opt_out INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'placed',
  eta_minutes INTEGER NOT NULL,
  rule_action TEXT,
  rule_score INTEGER,
  rule_reasons TEXT,
  handoff_status TEXT,
  handoff_decision TEXT,
  decision_source TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS address_history (
  address_line TEXT PRIMARY KEY,
  success_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL,
  trigger TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  status TEXT,
  ended_reason TEXT,
  duration_s REAL,
  outcome TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT,
  type TEXT NOT NULL,
  payload TEXT,
  ts TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Session:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.events: list[dict] = []

    def log_event(self, order_id: str | None, type_: str, payload: dict | None = None) -> None:
        payload = payload or {}
        ts = now()
        cur = self.conn.execute(
            "INSERT INTO events (order_id, type, payload, ts) VALUES (?, ?, ?, ?)",
            (order_id, type_, json.dumps(payload), ts),
        )
        self.events.append({"id": cur.lastrowid, "order_id": order_id, "type": type_, "payload": payload, "ts": ts})


@contextmanager
def session():
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    s = Session(conn)
    try:
        yield s
        conn.commit()
    finally:
        conn.close()
    # Publish only after commit, so UI refetches see the new state.
    for event in s.events:
        bus.publish(event)


def init_db() -> None:
    with session() as s:
        s.conn.executescript(SCHEMA)
