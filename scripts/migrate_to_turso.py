"""Copy the local SQLite database (data/malcomtrans.sqlite3) into the empty Turso database from .env.

Run once, with the app stopped:
    .venv\\Scripts\\python.exe scripts\\migrate_to_turso.py
It refuses to run if the Turso database already contains dispatch data.
"""
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from storage import TursoReplica, connect, initialize  # noqa: E402

TABLES = ("drivers", "locations", "trailers", "trucks", "orders")  # parents before children


def main():
    source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "malcomtrans.sqlite3"
    if not source_path.exists():
        sys.exit(f"Local database not found: {source_path}")
    config.load_env()
    settings = config.turso_settings()
    if not settings:
        sys.exit("Turso is not configured. Fill TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in .env first.")

    # Bring the local file up to the current schema before copying.
    initialize(source_path)
    target = TursoReplica(ROOT / "data" / "turso-replica.db", *settings)
    initialize(target)

    with connect(target) as db:
        existing = {table: db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] for table in TABLES}
    if any(existing.values()):
        sys.exit(f"Turso already contains data {existing}. Nothing was copied.")

    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    copied = {}
    with connect(target) as db:
        db.execute("PRAGMA foreign_keys = OFF")
        for table in TABLES:
            records = source.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for record in records:
                columns = record.keys()
                db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                           tuple(record))
            copied[table] = len(records)
    source.close()

    with connect(target) as db:
        verified = {table: db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] for table in TABLES}
    print("Copied:  ", copied)
    print("In Turso:", verified)
    if verified != copied:
        sys.exit("Counts differ. Check the output above before using the app.")
    print("Migration complete. The local file was not changed.")


if __name__ == "__main__":
    main()
