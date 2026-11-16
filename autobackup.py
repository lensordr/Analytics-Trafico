"""Automatic daily backup on your computer: backups/auto/MalcomTrans-<date-time>.sqlite3, last 30 kept.

Runs only in the copy on your computer: the online server (Render) loses its files at every restart.
A backup is made at start-up when the newest one is older than a day, then checked every hour.
"""
from datetime import datetime
from pathlib import Path
import threading
import time

from storage import backup_to

KEEP = 30
CHECK_EVERY = 3600
MAX_AGE = 23 * 3600


def files(folder):
    return sorted(Path(folder).glob("MalcomTrans-*.sqlite3"))


def latest(folder):
    found = files(folder)
    return found[-1] if found else None


def run_once(database, folder, now=None):
    """Make today's backup if the newest one is older than a day; keep the last KEEP. Returns the new file or None."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    newest = latest(folder)
    now = now or time.time()
    if newest and now - newest.stat().st_mtime < MAX_AGE:
        return None
    # Date and time in the name: never overwrites an older copy (which may still be open elsewhere).
    stem = f"MalcomTrans-{datetime.fromtimestamp(now):%Y-%m-%d-%H%M%S}"
    target, extra = folder / f"{stem}.sqlite3", 1
    while target.exists():
        target, extra = folder / f"{stem}-{extra}.sqlite3", extra + 1
    partial = target.with_suffix(".partial")
    backup_to(database, partial)
    partial.rename(target)  # a half-written copy never looks like a backup
    for old in files(folder)[:-KEEP]:
        old.unlink()
    return target


def start(database, folder):
    def loop():
        while True:
            try:
                made = run_once(database, folder)
                if made:
                    print(f"Automatic backup: {made}", flush=True)
            except Exception as error:  # a failed backup must never stop the app
                print(f"Automatic backup failed: {error}", flush=True)
            time.sleep(CHECK_EVERY)
    threading.Thread(target=loop, name="auto-backup", daemon=True).start()
