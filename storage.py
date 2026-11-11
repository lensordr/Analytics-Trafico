"""Database access: plain local SQLite, or a Turso embedded replica (local copy synced with the cloud)."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time


class TursoReplica:
    """One shared libsql connection to a local replica file that syncs with Turso.

    Writes are delegated to the Turso primary; reads are served from the local file.
    A lock serialises requests, which is fine for a single-dispatcher app.
    """

    SYNC_INTERVAL = 30  # seconds between background pulls when only reading

    def __init__(self, local_path, url, token):
        import libsql  # only needed when Turso is configured

        self.local_path = Path(local_path)
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self.url = url
        self._conn = libsql.connect(str(self.local_path), sync_url=url, auth_token=token)
        self._lock = threading.RLock()
        self._last_sync = 0.0
        self.sync()

    def sync(self):
        with self._lock:
            self._conn.sync()
            self._last_sync = time.monotonic()

    @contextmanager
    def session(self):
        with self._lock:
            if time.monotonic() - self._last_sync > self.SYNC_INTERVAL:
                self.sync()
            db = _LibsqlConnection(self._conn)
            try:
                db.execute("PRAGMA foreign_keys = ON")
                yield db
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            if db.wrote:
                self.sync()

    def __str__(self):
        return f"Turso ({self.url}) via local replica {self.local_path.name}"


class Row(dict):
    """sqlite3.Row-like: access by column name, by index, and dict(row)."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _translate(exc):
    message = str(exc)
    if "constraint failed" in message.lower():
        return sqlite3.IntegrityError(message)
    return sqlite3.OperationalError(message)


class _LibsqlCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount

    def _row(self, values):
        if values is None:
            return None
        # Turso may store keyword-like column names in upper case (e.g. `plan` -> `PLAN`);
        # SQL names are case-insensitive and every column in this app is lower case.
        return Row(zip((column[0].lower() for column in self._cursor.description), values))

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchall(self):
        return [self._row(values) for values in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _LibsqlConnection:
    """Exposes the subset of sqlite3.Connection the app uses, with Row results."""

    _READ_ONLY = ("SELECT", "PRAGMA", "WITH", "EXPLAIN")

    def __init__(self, conn):
        self._conn = conn
        self.wrote = False

    def execute(self, sql, parameters=()):
        if not sql.lstrip().upper().startswith(self._READ_ONLY):
            self.wrote = True
        try:
            return _LibsqlCursor(self._conn.execute(sql, tuple(parameters)))
        except ValueError as exc:
            raise _translate(exc) from None

    def executescript(self, script):
        self.wrote = True
        try:
            self._conn.executescript(script)
        except ValueError as exc:
            raise _translate(exc) from None


@contextmanager
def connect(target):
    if isinstance(target, TursoReplica):
        with target.session() as db:
            yield db
        return
    db = sqlite3.connect(target, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def backup_to(target, destination):
    """Write a consistent standalone SQLite copy of the database to `destination`."""
    source_path = target.local_path if isinstance(target, TursoReplica) else target
    if isinstance(target, TursoReplica):
        target.sync()
    source = sqlite3.connect(source_path, timeout=15)
    copy = sqlite3.connect(destination)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()


def initialize(target):
    if not isinstance(target, TursoReplica):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    with connect(target) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS drivers (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT '', carrier TEXT NOT NULL DEFAULT 'MalcomTrans',
            active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY, company TEXT NOT NULL, city TEXT NOT NULL,
            postal_code TEXT NOT NULL, country TEXT NOT NULL, address TEXT NOT NULL DEFAULT '',
            lat REAL, lon REAL, geo_label TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS trailers (
            id INTEGER PRIMARY KEY, plate TEXT NOT NULL UNIQUE COLLATE NOCASE,
            notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS trucks (
            id INTEGER PRIMARY KEY, plate TEXT NOT NULL UNIQUE COLLATE NOCASE,
            trailer TEXT NOT NULL DEFAULT '', driver_id INTEGER REFERENCES drivers(id),
            operation TEXT NOT NULL DEFAULT 'International', active INTEGER NOT NULL DEFAULT 1,
            location_id INTEGER REFERENCES locations(id), available_from TEXT,
            height REAL NOT NULL DEFAULT 4, width REAL NOT NULL DEFAULT 2.55,
            length REAL NOT NULL DEFAULT 16.5, weight REAL NOT NULL DEFAULT 40000);
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT UNIQUE,
            truck_id INTEGER NOT NULL REFERENCES trucks(id),
            driver_id INTEGER NOT NULL REFERENCES drivers(id),
            plate TEXT NOT NULL, trailer TEXT NOT NULL DEFAULT '', driver_name TEXT NOT NULL,
            carrier TEXT NOT NULL, operation TEXT NOT NULL,
            loading_id INTEGER NOT NULL REFERENCES locations(id),
            unloading_id INTEGER NOT NULL REFERENCES locations(id),
            pickup_at TEXT NOT NULL, arrival_at TEXT NOT NULL,
            unloading_at TEXT NOT NULL, available_at TEXT NOT NULL,
            distance_km REAL NOT NULL, driving_hours REAL NOT NULL,
            loading_hours REAL NOT NULL, unloading_hours REAL NOT NULL,
            route_source TEXT NOT NULL, route_details TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Planned',
            clock_input TEXT NOT NULL, plan TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS orders_truck ON orders(truck_id, pickup_at);
        CREATE INDEX IF NOT EXISTS orders_driver ON orders(driver_id, pickup_at);
        """)
        # Existing workspaces predate the driver's identity-document field.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(drivers)")}
        if "dni_nie" not in columns:
            db.execute("ALTER TABLE drivers ADD COLUMN dni_nie TEXT NOT NULL DEFAULT ''")
        for table in ("trucks", "orders"):
            columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            if "trailer_id" not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN trailer_id INTEGER REFERENCES trailers(id)")
        # Import existing registrations once without rewriting historical snapshots.
        db.execute("""INSERT OR IGNORE INTO trailers (plate)
            SELECT DISTINCT upper(trim(trailer)) FROM trucks WHERE trailer_id IS NULL AND trim(trailer)!=''
            UNION SELECT DISTINCT upper(trim(trailer)) FROM orders WHERE trailer_id IS NULL AND trim(trailer)!=''""")
        for table in ("trucks", "orders"):
            db.execute(f"""UPDATE {table} SET trailer_id=(
                SELECT id FROM trailers WHERE plate=trim({table}.trailer) COLLATE NOCASE)
                WHERE trailer_id IS NULL AND trim(trailer)!=''""")
        db.execute("CREATE INDEX IF NOT EXISTS orders_trailer ON orders(trailer_id, pickup_at)")


def rows(db, table):
    if table not in {"drivers", "trucks", "trailers", "locations", "orders"}:
        raise ValueError("Unknown table")
    # Lower-case keys also cover backups restored from Turso, whose schema may say `PLAN`.
    result = [{key.lower(): row[key] for key in row.keys()}
              for row in db.execute(f"SELECT * FROM {table} ORDER BY id DESC")]
    if table == "orders":
        for row in result:
            for field in ("plan", "clock_input", "route_details"):
                row[field] = json.loads(row[field])
    return result
