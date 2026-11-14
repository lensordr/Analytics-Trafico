"""Database access: plain local SQLite, or a Turso embedded replica (local copy synced with the cloud)."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from planner import parse_time


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
        # Once per connection: every statement sent to Turso costs a network round trip.
        self._conn.execute("PRAGMA foreign_keys = ON")
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
            db = _LibsqlConnection(self._conn, self.sync)
            try:
                yield db
                db.flush()  # a batch commits itself and syncs
                if db.direct:
                    self._conn.commit()
            except Exception:
                db.pending = []
                try:
                    self._conn.rollback()
                except ValueError:
                    pass  # nothing open (e.g. a batch already rolled itself back)
                raise
            if db.direct:
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


def _literal(value):
    """SQL literal for a parameter, used only to batch already-validated writes to Turso."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("Invalid number.")
        return repr(value)
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    text = str(value)
    if "\x00" in text:
        raise ValueError("Text cannot contain NUL characters.")
    return "'" + text.replace("'", "''") + "'"


def _render(sql, parameters):
    """Replace ? placeholders (outside quoted text) with literals."""
    values, out, quote = iter(parameters), [], None
    for char in sql:
        if quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "?":
            try:
                char = _literal(next(values))
            except StopIteration:
                raise ValueError("Too few SQL parameters.") from None
            except TypeError:
                raise ValueError("Invalid SQL parameter.") from None
        out.append(char)
    if next(values, _render) is not _render:
        raise ValueError("Too many SQL parameters.")
    return "".join(out)


class _QueuedCursor:
    lastrowid = None
    rowcount = -1

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())


class _LibsqlConnection:
    """Exposes the subset of sqlite3.Connection the app uses, with Row results."""

    _READ_ONLY = ("SELECT", "PRAGMA", "WITH", "EXPLAIN")

    def __init__(self, conn, after_flush=lambda: None):
        self._conn = conn
        self.after_flush = after_flush
        self.wrote = False
        self.direct = False  # writes sent one by one (outside a batch); they need commit + sync
        self.deferred = False
        self.pending = []
        self.reserved = {}

    def execute(self, sql, parameters=()):
        if not sql.lstrip().upper().startswith(self._READ_ONLY):
            self.wrote = True
            if self.deferred:
                self.pending.append(_render(sql, tuple(parameters)))
                return _QueuedCursor()
        elif self.pending:
            # Reading back queued writes: send them first (flush also refreshes the replica).
            self.flush()
        if not self.deferred and not sql.lstrip().upper().startswith(self._READ_ONLY):
            self.direct = True
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

    def flush(self):
        """Send queued writes to Turso as one atomic batch: one round trip instead of one per statement.

        Turso does not report errors from a script, so the batch ends by writing a unique receipt;
        after syncing, a missing receipt means the batch was rolled back."""
        if not self.pending:
            return
        token = secrets.token_hex(12)
        script = ("PRAGMA foreign_keys = ON;\nBEGIN IMMEDIATE;\n" + ";\n".join(self.pending) +
                  ";\nDELETE FROM write_receipts;\n"
                  f"INSERT INTO write_receipts (token, at) VALUES ('{token}', datetime('now'));\nCOMMIT;")
        self.pending, self.deferred = [], False
        try:
            self._conn.executescript(script)
        except ValueError as exc:
            try:
                self._conn.execute("ROLLBACK")
            except ValueError:
                pass
            raise _translate(exc) from None
        self.after_flush()
        if self._conn.execute("SELECT 1 FROM write_receipts WHERE token=?", (token,)).fetchone() is None:
            raise sqlite3.OperationalError("The change was not saved in the cloud database (nothing was changed). "
                                           "Refresh and try again; the data may have changed meanwhile.")


def begin_write(db):
    """Start the writing part of a request.

    SQLite: take the write lock now. Turso: reads keep using the fast local replica, and the
    writes are queued and sent at the end of the request as one atomic batch. Code after this
    call must not read back its own writes, and new rows need ids from `reserve_id`."""
    if isinstance(db, _LibsqlConnection):
        if db.direct and not db.deferred:
            # Earlier one-by-one writes hold an open transaction; the batch needs its own.
            db._conn.commit()
            db.after_flush()
            db.direct = False
        db.deferred = True
    elif not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")


def reserve_id(db, table):
    """Next row id for an INSERT with an explicit id (works while Turso writes are queued)."""
    if table not in {"orders", "trailers"}:
        raise ValueError("Invalid table.")
    row = db.execute(f"""SELECT max(coalesce((SELECT seq FROM sqlite_sequence WHERE name=?), 0),
                                   coalesce((SELECT max(id) FROM {table}), 0)) + 1""", (table,)).fetchone()
    value = row[0]
    if isinstance(db, _LibsqlConnection):
        value = max(value, db.reserved.get(table, 0) + 1)
        db.reserved[table] = value
    return value


@contextmanager
def connect(target):
    if isinstance(target, TursoReplica):
        with target.session() as db:
            yield db
        return
    db = sqlite3.connect(target, timeout=15)
    # Same rows as on Turso: lower-case keys, even in a copy of a Turso backup (column `PLAN`).
    db.row_factory = lambda cursor, values: Row(zip((column[0].lower() for column in cursor.description), values))
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
        # Archive instead of delete: sold/scrapped equipment keeps its trip history.
        for table in ("trucks", "trailers"):
            columns = {row["name"].lower() for row in db.execute(f"PRAGMA table_info({table})")}
            if "archived_at" not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN archived_at TEXT")
            if "archive_reason" not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN archive_reason TEXT NOT NULL DEFAULT ''")
            if "archive_note" not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN archive_note TEXT NOT NULL DEFAULT ''")
        # Import existing registrations once without rewriting historical snapshots.
        db.execute("""INSERT OR IGNORE INTO trailers (plate)
            SELECT DISTINCT upper(trim(trailer)) FROM trucks WHERE trailer_id IS NULL AND trim(trailer)!=''
            UNION SELECT DISTINCT upper(trim(trailer)) FROM orders WHERE trailer_id IS NULL AND trim(trailer)!=''""")
        for table in ("trucks", "orders"):
            db.execute(f"""UPDATE {table} SET trailer_id=(
                SELECT id FROM trailers WHERE plate=trim({table}.trailer) COLLATE NOCASE)
                WHERE trailer_id IS NULL AND trim(trailer)!=''""")
        db.execute("CREATE INDEX IF NOT EXISTS orders_trailer ON orders(trailer_id, pickup_at)")
        # Proof that a batch of writes reached Turso (see _LibsqlConnection.flush); keeps one row.
        db.execute("CREATE TABLE IF NOT EXISTS write_receipts (token TEXT PRIMARY KEY, at TEXT NOT NULL)")
        # Journeys are ordered stops (loading, extra loading/unloading, trailer swap, unloading),
        # each confirmed on its own. The order row keeps the first/last stop for summaries.
        db.executescript("""
        CREATE TABLE IF NOT EXISTS order_stops (
            id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
            seq INTEGER NOT NULL, kind TEXT NOT NULL,
            location_id INTEGER NOT NULL REFERENCES locations(id),
            planned_at TEXT NOT NULL, planned_end_at TEXT NOT NULL,
            handling_hours REAL NOT NULL DEFAULT 1,
            leg_km REAL NOT NULL DEFAULT 0, leg_hours REAL NOT NULL DEFAULT 0,
            drop_trailer_id INTEGER REFERENCES trailers(id), drop_trailer TEXT NOT NULL DEFAULT '',
            pick_trailer_id INTEGER REFERENCES trailers(id), pick_trailer TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Planned', done_at TEXT,
            UNIQUE(order_id, seq));
        CREATE INDEX IF NOT EXISTS order_stops_planned ON order_stops(planned_at);
        """)
        columns = {row["name"].lower() for row in db.execute("PRAGMA table_info(orders)")}
        if "delivered_at" not in columns:
            db.execute("ALTER TABLE orders ADD COLUMN delivered_at TEXT")
        columns = {row["name"].lower() for row in db.execute("PRAGMA table_info(trailers)")}
        if "parked_location_id" not in columns:
            db.execute("ALTER TABLE trailers ADD COLUMN parked_location_id INTEGER REFERENCES locations(id)")
        if "parked_at" not in columns:
            db.execute("ALTER TABLE trailers ADD COLUMN parked_at TEXT")
        # Company profile and contact details: used to find who to call near a free truck.
        columns = {row["name"].lower() for row in db.execute("PRAGMA table_info(locations)")}
        for column, default in (("kind", "'Client'"), ("contact_name", "''"), ("phone", "''"),
                                ("email", "''"), ("notes", "''")):
            if column not in columns:
                db.execute(f"ALTER TABLE locations ADD COLUMN {column} TEXT NOT NULL DEFAULT {default}")
        # Log of calls/emails to a company: "had a load", "no answer"... newest first in the UI.
        db.execute("""CREATE TABLE IF NOT EXISTS location_contacts (
            id INTEGER PRIMARY KEY, location_id INTEGER NOT NULL REFERENCES locations(id),
            at TEXT NOT NULL, result TEXT NOT NULL, note TEXT NOT NULL DEFAULT '')""")
        db.execute("CREATE INDEX IF NOT EXISTS location_contacts_location ON location_contacts(location_id, at)")
        # ITV (vehicle inspection) register, kept exactly as the official document: its plates are
        # the full registrations and do not need to match the tractors/trailers used for dispatch.
        db.execute("""CREATE TABLE IF NOT EXISTS itv_vehicles (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, plate TEXT NOT NULL,
            expires_on TEXT, expiry_text TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '')""")
        # Materials loaded at clients (own codes), chosen on loading stops; stops keep a text snapshot.
        db.execute("""CREATE TABLE IF NOT EXISTS materials (
            id INTEGER PRIMARY KEY, code TEXT NOT NULL DEFAULT '', name TEXT NOT NULL,
            ler_code TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1)""")
        columns = {row["name"].lower() for row in db.execute("PRAGMA table_info(order_stops)")}
        if "material_id" not in columns:
            db.execute("ALTER TABLE order_stops ADD COLUMN material_id INTEGER REFERENCES materials(id)")
        if "material" not in columns:
            db.execute("ALTER TABLE order_stops ADD COLUMN material TEXT NOT NULL DEFAULT ''")
        if "weight_kg" not in columns:
            db.execute("ALTER TABLE order_stops ADD COLUMN weight_kg REAL")
        # Documento de Control (DeCA): shipper's NIF, the document id and its own fields per order.
        columns = {row["name"].lower() for row in db.execute("PRAGMA table_info(locations)")}
        if "nif" not in columns:
            db.execute("ALTER TABLE locations ADD COLUMN nif TEXT NOT NULL DEFAULT ''")
            # Imported companies carry "CIF X" in their notes: copy it into the new field once.
            for row in db.execute("SELECT id, notes FROM locations").fetchall():
                found = re.search(r"\bCIF\s+([A-Z0-9][A-Z0-9 .\-]{5,20}?)(?:\.\s|\.?$)", row["notes"] or "")
                if found:
                    nif = re.sub(r"[^A-Z0-9]", "", found.group(1).upper())
                    db.execute("UPDATE locations SET nif=? WHERE id=?", (nif, row["id"]))
        columns = {row["name"].lower() for row in db.execute("PRAGMA table_info(orders)")}
        for column, kind in (("deca_id", "TEXT"), ("deca_shipper_id", "INTEGER REFERENCES locations(id)"),
                             ("deca_notes", "TEXT NOT NULL DEFAULT ''"), ("deca_created_at", "TEXT")):
            if column not in columns:
                db.execute(f"ALTER TABLE orders ADD COLUMN {column} {kind}")
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')")
        # Secret link for the driver page (/s/<token>), created when the WhatsApp message is prepared.
        columns = {row["name"].lower() for row in db.execute("PRAGMA table_info(orders)")}
        if "driver_token" not in columns:
            db.execute("ALTER TABLE orders ADD COLUMN driver_token TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS orders_driver_token ON orders(driver_token)")
        # Existing two-stop orders become a loading stop and an unloading stop (once).
        db.execute("""INSERT INTO order_stops (order_id, seq, kind, location_id, planned_at, planned_end_at,
                handling_hours, status, done_at)
            SELECT id, 1, 'Loading', loading_id, pickup_at, pickup_at, loading_hours,
                CASE WHEN status='Completed' THEN 'Done' ELSE 'Planned' END,
                CASE WHEN status='Completed' THEN pickup_at END
            FROM orders WHERE NOT EXISTS (SELECT 1 FROM order_stops s WHERE s.order_id=orders.id)""")
        db.execute("""INSERT INTO order_stops (order_id, seq, kind, location_id, planned_at, planned_end_at,
                handling_hours, leg_km, leg_hours, status, done_at)
            SELECT id, 2, 'Unloading', unloading_id, unloading_at, available_at, unloading_hours,
                distance_km, driving_hours,
                CASE WHEN status='Completed' THEN 'Done' ELSE 'Planned' END,
                CASE WHEN status='Completed' THEN unloading_at END
            FROM orders WHERE (SELECT count(*) FROM order_stops s WHERE s.order_id=orders.id)=1""")
        db.execute("UPDATE orders SET delivered_at=unloading_at WHERE status='Completed' AND delivered_at IS NULL")
        # Delivered orders free the truck from the confirmed delivery (orders confirmed before this rule).
        # Driving-rules orders are only moved earlier: later clocks were carried from their planned end.
        for order in db.execute("SELECT id, available_at, delivered_at, plan FROM orders "
                                "WHERE status='Completed' AND delivered_at IS NOT NULL").fetchall():
            delivered, available = parse_time(order["delivered_at"]), parse_time(order["available_at"])
            rules = bool(json.loads(order["plan"] or "{}").get("end_state"))
            if delivered != available and (not rules or delivered < available):
                db.execute("UPDATE orders SET available_at=? WHERE id=?", (order["delivered_at"], order["id"]))


def rows(db, table):
    if table not in {"drivers", "trucks", "trailers", "locations", "orders", "itv_vehicles", "materials"}:
        raise ValueError("Unknown table")
    # Lower-case keys also cover backups restored from Turso, whose schema may say `PLAN`.
    result = [{key.lower(): row[key] for key in row.keys()}
              for row in db.execute(f"SELECT * FROM {table} ORDER BY id DESC")]
    if table == "orders":
        stops = {}
        for stop in db.execute("SELECT * FROM order_stops ORDER BY order_id, seq"):
            stops.setdefault(stop["order_id"], []).append({key.lower(): stop[key] for key in stop.keys()})
        for row in result:
            for field in ("plan", "clock_input", "route_details"):
                row[field] = json.loads(row[field])
            row["stops"] = stops.get(row["id"], [])
    if table == "locations":
        contacts = {}
        for contact in db.execute("SELECT * FROM location_contacts ORDER BY at DESC, id DESC"):
            contacts.setdefault(contact["location_id"], []).append({key.lower(): contact[key] for key in contact.keys()})
        for row in result:
            row["contacts"] = contacts.get(row["id"], [])
    return result
