import json
import sqlite3
from pathlib import Path

import pandas as pd

from .config import cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY, name TEXT, market TEXT, industry TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS holidays (date TEXT PRIMARY KEY, name TEXT);

CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, value REAL,
    market TEXT, available_at TEXT, PRIMARY KEY (symbol, date));
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

CREATE TABLE IF NOT EXISTS index_prices (name TEXT, date TEXT, close REAL, PRIMARY KEY (name, date));

CREATE TABLE IF NOT EXISTS institutional (
    symbol TEXT, date TEXT, foreign_net REAL, trust_net REAL, dealer_net REAL, total_net REAL,
    available_at TEXT, PRIMARY KEY (symbol, date));

CREATE TABLE IF NOT EXISTS margin (
    symbol TEXT, date TEXT, margin_balance REAL, short_balance REAL, available_at TEXT,
    PRIMARY KEY (symbol, date));

CREATE TABLE IF NOT EXISTS flags (
    symbol TEXT, kind TEXT, start_date TEXT, end_date TEXT, detail TEXT, available_at TEXT,
    PRIMARY KEY (symbol, kind, start_date));

CREATE TABLE IF NOT EXISTS exdiv (
    symbol TEXT, date TEXT, kind TEXT, cash_dividend REAL, available_at TEXT, PRIMARY KEY (symbol, date));

CREATE TABLE IF NOT EXISTS revenue (
    symbol TEXT, ym TEXT, revenue REAL, yoy REAL, mom REAL, cum_yoy REAL, available_at TEXT,
    PRIMARY KEY (symbol, ym));

CREATE TABLE IF NOT EXISTS futures_oi (date TEXT, item TEXT, oi_net REAL, PRIMARY KEY (date, item));

CREATE TABLE IF NOT EXISTS macro_calendar (
    id INTEGER PRIMARY KEY, title TEXT, country TEXT, scheduled_at TEXT, impact TEXT, event_key TEXT,
    forecast TEXT, previous TEXT, forecast_value REAL, pre_period TEXT, actual_value REAL,
    actual_period TEXT, surprise REAL, surprise_class TEXT, actual_source TEXT, actual_at TEXT,
    available_at TEXT, updated_at TEXT, UNIQUE (title, country, scheduled_at));

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, uid TEXT UNIQUE, kind TEXT, source TEXT, license_class TEXT,
    title TEXT, body TEXT, url TEXT, symbols TEXT, published_at TEXT, available_at TEXT,
    processed INTEGER DEFAULT 0, category TEXT, sentiment REAL, importance REAL, summary TEXT,
    broker TEXT, rating TEXT, target_price REAL, cluster_id INTEGER);
CREATE INDEX IF NOT EXISTS idx_events_avail ON events(available_at);

CREATE TABLE IF NOT EXISTS news_clusters (
    id INTEGER PRIMARY KEY, title TEXT, symbols TEXT, category TEXT, sentiment REAL,
    importance REAL, first_seen TEXT, last_seen TEXT, count INTEGER, sources TEXT);

CREATE TABLE IF NOT EXISTS market_regime (
    date TEXT PRIMARY KEY, state TEXT, score REAL, inputs TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS event_reactions (
    event_key TEXT, release_date TEXT, value REAL, surprise REAL, bucket TEXT, basis TEXT,
    target TEXT, ret_1d REAL, ret_5d REAL, PRIMARY KEY (event_key, release_date, target, basis));

CREATE TABLE IF NOT EXISTS scenarios (
    id INTEGER PRIMARY KEY, macro_id INTEGER UNIQUE, event_key TEXT, created_at TEXT,
    content TEXT, analysis_id INTEGER, matched TEXT, matched_at TEXT);

CREATE TABLE IF NOT EXISTS candidates (
    date TEXT, symbol TEXT, screen TEXT, score REAL, features TEXT, PRIMARY KEY (date, symbol, screen));

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY, created_at TEXT, role TEXT, provider TEXT, model TEXT,
    prompt_version TEXT, input TEXT, output TEXT, ok INTEGER, error TEXT,
    tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, latency_s REAL);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY, grp TEXT, mode TEXT, issued_at TEXT, asof_date TEXT, symbol TEXT,
    name TEXT, direction TEXT, horizon TEXT, strategy TEXT, entry_low REAL, entry_high REAL,
    stop REAL, target1 REAL, target2 REAL, time_stop_days INTEGER, entry_window_days INTEGER,
    confidence REAL, thesis TEXT, invalidation TEXT, sources TEXT, risk_notes TEXT,
    priced_in REAL, size_pct REAL, score REAL, analysis_id INTEGER,
    UNIQUE (grp, mode, asof_date, symbol));

CREATE TABLE IF NOT EXISTS tracking (
    rec_id INTEGER PRIMARY KEY, state TEXT, fill_date TEXT, fill_price REAL, stop_current REAL,
    t1_hit INTEGER DEFAULT 0, realized_r REAL DEFAULT 0, exit_date TEXT, exit_price REAL,
    exit_reason TEXT, r_multiple REAL, bench_return REAL, mfe_r REAL, mae_r REAL,
    invalidated_at TEXT, last_date TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS invalidation_alerts (
    id INTEGER PRIMARY KEY, rec_id INTEGER, created_at TEXT, event_id INTEGER, reason TEXT,
    severity TEXT, analysis_id INTEGER, UNIQUE (rec_id, event_id));

CREATE TABLE IF NOT EXISTS user_actions (
    id INTEGER PRIMARY KEY, rec_id INTEGER, action TEXT, price REAL, at TEXT, note TEXT);

CREATE TABLE IF NOT EXISTS signal_stats (
    kind TEXT, key TEXT, n INTEGER, win_rate REAL, avg_r REAL, t_stat REAL, status TEXT,
    updated_at TEXT, PRIMARY KEY (kind, key));

CREATE TABLE IF NOT EXISTS alerts_sent (key TEXT PRIMARY KEY, sent_at TEXT);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY, job TEXT, started_at TEXT, finished_at TEXT, ok INTEGER, message TEXT);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or cfg()["db_path"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def upsert(conn: sqlite3.Connection, table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    conn.executemany(sql, [[r.get(c) for c in cols] for r in rows])
    conn.commit()
    return len(rows)


def insert_ignore(conn: sqlite3.Connection, table: str, row: dict) -> int | None:
    """回傳新列 id；已存在（違反 UNIQUE）則回傳 None。"""
    cols = list(row.keys())
    cur = conn.execute(
        f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [row[c] for c in cols])
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def df(conn: sqlite3.Connection, sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def kv_get(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(conn: sqlite3.Connection, key: str, value) -> None:
    from .timeutil import iso
    conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, json.dumps(value, ensure_ascii=False), iso()))
    conn.commit()


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
