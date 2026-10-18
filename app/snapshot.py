"""Saving the bots into the repo, so a clone on another computer carries on with them.

`./crypto-bot save` writes snapshot/: the database as SQL text, without the cached candles (they're Coinbase's, and
are fetched again on start), plus the trained ML models. Commit and push it. On a computer with no database yet,
`./crypto-bot` brings it back before it starts; `./crypto-bot restore` brings it back over an existing one, after
moving that aside. The bots then pick up where the save left them. The time in between is downtime, as after any
restart: they don't trade on candles that closed while nothing was running.

Run the bots on one computer at a time: two copies started from the same save go their own ways.
"""
import fcntl
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from . import config

SNAPSHOT_DIR = config.ROOT / "snapshot"
DUMP = "bots.sql"
SAVED_AT = "snapshot_saved_at"  # state key: when the save was made


def save(db_path: Path = config.DB_PATH, out: Path = SNAPSHOT_DIR) -> dict:
    """Write the snapshot. Safe while the app runs: it copies a consistent moment of the database."""
    if not db_path.exists():
        raise SystemExit(f"There's no database at {db_path} to save.")
    src = sqlite3.connect(db_path)
    copy = sqlite3.connect(":memory:")
    src.backup(copy)
    src.close()
    copy.execute("DELETE FROM candles")
    copy.execute("DELETE FROM state WHERE key LIKE 'backfilled_from:%'")  # the cache is empty, so it starts afresh
    copy.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (SAVED_AT, str(int(time.time()))))
    copy.commit()
    counts = {t: copy.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("bots", "trades")}
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / f"{DUMP}.tmp"
    with open(tmp, "w") as f:
        for line in copy.iterdump():
            f.write(f"{line}\n")
    copy.close()
    tmp.replace(out / DUMP)
    models = db_path.parent / "models"
    shutil.rmtree(out / "models", ignore_errors=True)
    if models.is_dir():
        shutil.copytree(models, out / "models")
    counts["models"] = sum(1 for p in (out / "models").iterdir() if p.is_dir()) if (out / "models").is_dir() else 0
    return counts


def saved_at(src: Path = SNAPSHOT_DIR) -> int | None:
    conn = sqlite3.connect(":memory:")
    conn.executescript((src / DUMP).read_text())
    row = conn.execute("SELECT value FROM state WHERE key = ?", (SAVED_AT,)).fetchone()
    conn.close()
    return int(row[0]) if row else None


def _not_running(db_path: Path) -> None:
    lock = db_path.with_name(db_path.name + ".lock")
    if not lock.exists():
        return
    with open(lock, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Crypto Bot is running. Stop it (Ctrl+C) before restoring.") from None


def restore(db_path: Path = config.DB_PATH, src: Path = SNAPSHOT_DIR) -> Path | None:
    """Bring the snapshot back. An existing database and models are moved into a dated folder next to them first;
    returns that folder (None if there was nothing to move)."""
    if not (src / DUMP).exists():
        raise SystemExit("There's no saved snapshot to restore (run ./crypto-bot save first).")
    _not_running(db_path)
    data, models = db_path.parent, db_path.parent / "models"
    data.mkdir(parents=True, exist_ok=True)
    moved = None
    old = [p for p in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm"),
                       models) if p.exists()]
    if old:
        moved = data / time.strftime("before-restore-%Y%m%d-%H%M%S")
        moved.mkdir()
        for p in old:
            shutil.move(p, moved / p.name)
    tmp = db_path.with_name(db_path.name + ".restoring")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript((src / DUMP).read_text())
    conn.close()
    shutil.rmtree(models, ignore_errors=True)
    if (src / "models").is_dir():
        shutil.copytree(src / "models", models)
    tmp.replace(db_path)
    return moved


def _when(ts: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "an unknown time"


def main(cmd: str) -> None:
    if cmd == "save":
        n = save()
        print(f"Saved {n['bots']} bots, {n['trades']} trades and {n['models']} ML models to snapshot/.\n"
              "Put it in the repo with:  git add snapshot && git commit -m 'Save bots' && git push")
    elif cmd == "restore":
        moved = restore()
        print(f"Restored the bots saved {_when(saved_at())}."
              + (f" The ones that were here are in {moved.relative_to(config.ROOT)}." if moved else ""))
    elif cmd == "restore-if-new":  # the launcher's first-run step: only when there's no database yet
        if not config.DB_PATH.exists() and (SNAPSHOT_DIR / DUMP).exists():
            restore()
            print(f"Restored the bots saved {_when(saved_at())}.")
    else:
        raise SystemExit(f"Unknown command {cmd!r}: use save or restore.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
