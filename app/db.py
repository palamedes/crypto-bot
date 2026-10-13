"""SQLite storage: candle cache, paper bots, trades, equity snapshots and the prediction log."""
import contextlib
import fcntl
import sqlite3
import sys
import threading
from pathlib import Path

from . import config

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
    fetched_at INTEGER,  -- when this row was last fetched; a candle is final only if that was after it closed
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
    fee_rate REAL,  -- taker fee (market orders); NULL means the config default
    slippage REAL,
    stop_atr REAL,  -- trailing stop distance in ATRs; NULL means no stop
    stop_distance REAL,  -- the open position's stop distance in price units, fixed when it bought
    peak_price REAL,  -- highest price seen since it bought
    stopped_out INTEGER NOT NULL DEFAULT 0,  -- 1 after a stop-out until the signal stops saying BUY
    name TEXT,
    settings TEXT,  -- JSON BotSettings; NULL means all defaults
    stop_floor REAL,  -- the open position's fixed stop-loss price
    take_profit_price REAL,
    entry_candle_ts INTEGER,  -- the candle whose signal opened the position (for max hold)
    bot_type TEXT,  -- indicators, breakout, grid, dca or rebalance; NULL means indicators
    state TEXT,  -- JSON working state of grid / DCA / rebalance bots
    maker_fee REAL,  -- fee on resting limit orders (grid levels, DCA orders); NULL means the config default
    paused INTEGER NOT NULL DEFAULT 0,  -- 1: holds whatever it has and makes no trades
    error_count INTEGER NOT NULL DEFAULT 0,  -- consecutive failures; the bot is paused after too many
    last_error TEXT
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
    ts INTEGER NOT NULL,  -- when the value was observed: a candle's close, or the wall clock for starts and exits
    equity REAL NOT NULL,
    price REAL NOT NULL,
    kind TEXT,  -- 'close', 'start' or 'exit'
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
    ("bots", "bot_type", "TEXT"),
    ("bots", "state", "TEXT"),
    ("bots", "maker_fee", "REAL"),
    ("bots", "paused", "INTEGER NOT NULL DEFAULT 0"),
    ("bots", "error_count", "INTEGER NOT NULL DEFAULT 0"),
    ("bots", "last_error", "TEXT"),
    ("trades", "kind", "TEXT"),
    ("candles", "fetched_at", "INTEGER"),
    ("equity", "kind", "TEXT"),
]


class AlreadyRunning(SystemExit):
    pass


class Database:
    """A single shared connection guarded by a lock; plenty for one local user.

    Statements commit one by one unless they run inside `transaction()`, which commits them together (or rolls
    them all back), so a crash can't leave a bot's money moved with no trade row. Only one process may open a
    database file: a second copy would run its own engine against the same bots and double every trade."""

    def __init__(self, path: Path | str):
        self._lock_file = None
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._claim(Path(path))
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)  # we commit ourselves
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._depth = 0  # nesting depth of transaction()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            for table, column, type_ in MIGRATIONS:
                columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")
            self._shift_equity_times()

    def _claim(self, path: Path) -> None:
        lock_path = path.with_name(path.name + ".lock")
        handle = open(lock_path, "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            msg = (f"Crypto Bot is already running against {path}. Stop that copy first, or set CRYPTO_BOT_DB to a "
                   "different file to run a second one.")
            print(msg, file=sys.stderr)
            raise AlreadyRunning(msg) from None
        self._lock_file = handle  # held for the life of the process

    def close(self) -> None:
        with self.lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.close()
        if self._lock_file:
            self._lock_file.close()

    def _shift_equity_times(self) -> None:
        """Rows written before `kind` existed were keyed by the candle's open but valued at its close. Move them
        to the close so every row is keyed by when its value was observed (newest first, so nothing collides)."""
        if self.conn.execute("SELECT 1 FROM state WHERE key = 'equity_times_shifted'").fetchone():
            return
        for bot in self.conn.execute("SELECT id, timeframe FROM bots").fetchall():
            seconds = config.TIMEFRAMES.get(bot["timeframe"], (0,))[0]
            if not seconds:
                continue
            rows = self.conn.execute(
                "SELECT ts FROM equity WHERE bot_id = ? AND kind IS NULL AND ts % ? = 0 ORDER BY ts DESC",
                (bot["id"], seconds)).fetchall()
            for row in rows:
                self.conn.execute("UPDATE OR REPLACE equity SET ts = ?, kind = 'close' WHERE bot_id = ? AND ts = ?",
                                  (row["ts"] + seconds, bot["id"], row["ts"]))
        self.conn.execute("UPDATE equity SET kind = 'start' WHERE kind IS NULL")
        self.conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('equity_times_shifted', '1')")

    @contextlib.contextmanager
    def transaction(self):
        """Group statements so they all land or none do. Nests: only the outermost level commits."""
        with self.lock:
            if self._depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("COMMIT")

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)  # autocommits unless inside transaction()

    def executemany(self, sql: str, rows) -> None:
        with self.lock:
            self.conn.executemany(sql, rows)

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
