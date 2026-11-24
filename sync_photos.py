"""Copy the drivers' document photos (CMR / albarán) from MalcomTrans to a folder on the office PC (OneDrive).

    <folder>\\Poze\\2026-11 Noiembrie\\8798\\2026-11-24 Incarcare · MT-2026-000507 · Enerco · 1.jpg

One folder per month, inside it one per tractor; the date in the name is the day of the loading / unloading.
Each photo is copied once (then marked as copied); KEEP_DAYS after the copy the image is emptied online, so the
database stays small. Windows Task Scheduler runs it every few minutes (pythonw, no window):

    .venv\\Scripts\\pythonw.exe sync_photos.py "C:\\Users\\...\\MalcomTransApp"

Option --database <file.sqlite3> works on a local copy instead of Turso (for testing).
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from photos import empty_old  # noqa: E402
from planner import UTC, ZONE, parse_time, stamp  # noqa: E402
from storage import TursoReplica, connect, initialize  # noqa: E402

MONTHS = ("Ianuarie", "Februarie", "Martie", "Aprilie", "Mai", "Iunie", "Iulie", "August", "Septembrie",
          "Octombrie", "Noiembrie", "Decembrie")
KIND = {"Loading": "Incarcare", "Unloading": "Descarcare"}


def safe(text, limit=60):
    """A piece of a Windows file name: no \\ / : * ? " < > |, no trailing dot or space."""
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(text or "")).strip(" .")[:limit].strip(" .") or "-"


def target(folder, photo):
    day = parse_time(photo["done_at"] or photo["taken_at"]).astimezone(ZONE)
    month = folder / "Poze" / f"{day:%Y-%m} {MONTHS[day.month - 1]}" / safe(photo["plate"] or "fara camion", 20)
    name = f'{day:%Y-%m-%d} {KIND.get(photo["kind"], photo["kind"])} · {safe(photo["number"], 20)} · {safe(photo["place"])} · {photo["n"]}'
    path, extra = month / f"{name}.jpg", 2
    while path.exists():  # never overwrite: a second copy gets (2), (3)…
        path, extra = month / f"{name} ({extra}).jpg", extra + 1
    return path


def sync(database, folder, log=print):
    folder = Path(folder)
    with connect(database, fresh=True) as db:
        waiting = [dict(row) for row in db.execute(
            "SELECT p.id, p.kind, p.n, p.taken_at, s.done_at, o.number, COALESCE(t.plate, o.plate) AS plate, l.company AS place "
            "FROM stop_photos p JOIN orders o ON o.id=p.order_id "
            "JOIN order_stops s ON s.order_id=p.order_id AND s.seq=p.seq "
            "LEFT JOIN trucks t ON t.id=s.truck_id JOIN locations l ON l.id=s.location_id "
            "WHERE p.downloaded_at IS NULL AND p.data IS NOT NULL ORDER BY p.id")]
    copied = 0
    for photo in waiting:
        with connect(database) as db:
            row = db.execute("SELECT data FROM stop_photos WHERE id=?", (photo["id"],)).fetchone()
        if not row or row["data"] is None:
            continue
        path = target(folder, photo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(row["data"]))
        with connect(database) as db:
            db.execute("UPDATE stop_photos SET downloaded_at=? WHERE id=?", (stamp(datetime.now(UTC)), photo["id"]))
        copied += 1
        log(f"copied {path.relative_to(folder)}")
    with connect(database) as db:
        emptied = empty_old(db)
    return copied, emptied


def main():
    parser = argparse.ArgumentParser(description="Copy the drivers' document photos to the office PC.")
    parser.add_argument("folder", help="destination folder, e.g. the OneDrive MalcomTransApp folder")
    parser.add_argument("--database", help="a local .sqlite3 copy instead of Turso (testing)")
    args = parser.parse_args()
    folder = Path(args.folder)
    if not folder.is_dir():
        raise SystemExit(f"Folder not found: {folder}")
    log_file = folder / "Poze" / "sync.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    def log(text):
        with log_file.open("a", encoding="utf-8") as out:
            out.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")

    try:
        if args.database:
            database = Path(args.database)
            initialize(database)
        else:
            config.load_env()
            settings = config.turso_settings()
            if not settings:
                raise SystemExit("Turso is not configured in .env")
            database = TursoReplica(ROOT / "data" / "photos-replica.db", *settings)
        copied, emptied = sync(database, folder, log)
        if copied or emptied:
            log(f"done: {copied} copied, {emptied} emptied online")
    except Exception as error:  # the scheduled run has no window: the reason goes to the log
        log(f"ERROR {type(error).__name__}: {error}")
        raise


if __name__ == "__main__":
    main()
