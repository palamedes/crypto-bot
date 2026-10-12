"""SQLite storage: candle cache, paper bots, trades, equity snapshots and the prediction log."""
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    product TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (product, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    strategy TEXT NOT NULL,
    starting_cash REAL NOT NULL,
    cash REAL NOT NULL,
    position REAL NOT NULL DEFAULT 0,
    entry_price REAL,
    start_price REAL NOT NULL,
    created_at INTEGER NOT NULL,
    last_candle_ts INTEGER,
    fee_rate REAL,  -- NULL means the config default
    slippage REAL,
    stop_atr REAL,  -- trailing stop distance in ATRs; NULL means no stop
    stop_distance REAL,  -- the open position's stop distance in price units, fixed when it bought
    peak_price REAL,  -- highest price seen since it bought
    stopped_out INTEGER NOT NULL DEFAULT 0,  -- 1 after a stop-out until the signal stops saying BUY
    name TEXT,
    settings TEXT,  -- JSON BotSettings; NULL means all defaults
    stop_floor REAL,  -- the open position's fixed stop-loss price
    take_profit_price REAL,
    entry_candle_ts INTEGER  -- the candle whose signal opened the position (for max hold)
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    ts INTEGER NOT NULL,
    signal_candle_ts INTEGER NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    fee REAL NOT NULL,
    cash_after REAL NOT NULL,
    position_after REAL NOT NULL,
    pnl REAL,
    score REAL NOT NULL,
    reason TEXT NOT NULL,
    kind TEXT  -- what made the bot trade: 'signal', 'stop', 'take_profit' or 'max_hold'; NULL on old rows
);

CREATE TABLE IF NOT EXISTS equity (
    bot_id INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    ts INTEGER NOT NULL,
    equity REAL NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (bot_id, ts)
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    strategy TEXT NOT NULL,
    candle_ts INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    price REAL NOT NULL,
    action TEXT NOT NULL,
    score REAL NOT NULL,
    horizon INTEGER NOT NULL,
    target_ts INTEGER NOT NULL,
    features TEXT NOT NULL,
    components TEXT NOT NULL,
    outcome_price REAL,
    outcome_return REAL,
    correct INTEGER,
    evaluated_at INTEGER,
    UNIQUE (product, timeframe, strategy, candle_ts)
);
CREATE INDEX IF NOT EXISTS predictions_pending ON predictions (product, timeframe, evaluated_at);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# Columns added after a table first shipped: (table, column, type). Applied to older database files on startup.
MIGRATIONS = [
    ("bots", "fee_rate", "REAL"),
    ("bots", "slippage", "REAL"),
    ("bots", "stop_atr", "REAL"),
    ("bots", "stop_distance", "REAL"),
    ("bots", "peak_price", "REAL"),
    ("bots", "stopped_out", "INTEGER NOT NULL DEFAULT 0"),
    ("bots", "name", "TEXT"),
    ("bots", "settings", "TEXT"),
    ("bots", "stop_floor", "REAL"),
    ("bots", "take_profit_price", "REAL"),
    ("bots", "entry_candle_ts", "INTEGER"),
    ("trades", "kind", "TEXT"),
]


class Database:
    """A single shared connection guarded by a lock; plenty for one local user."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            for table, column, type_ in MIGRATIONS:
                columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")
            self.conn.commit()

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows) -> None:
        with self.lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def query(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params=()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def get_state(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM state WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_state(self, key: str, value) -> None:
        self.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
