"""Photos of the loading and unloading documents (CMR, albarán), needed for invoicing.

After confirming a loading or an unloading, the driver must send the photo(s) of the document before the next step
(or say "later": the office then sees the document as missing). The phone shrinks each photo to a readable JPEG of a
few hundred KB before sending it. The photos stay in the database only until the office PC copies them into OneDrive
(sync_photos.py); KEEP_DAYS after that copy the image itself is emptied, the record stays (who sent what and when).
"""
import base64
import binascii
import re
from datetime import datetime, timedelta

from planner import UTC, stamp

DOC_KINDS = ("Loading", "Unloading")
MAX_BYTES = 1_500_000
MAX_PER_STOP = 6
KEEP_DAYS = 30


def needs_docs(stop):
    """A confirmed loading or unloading whose document photo the driver has not sent (nor put off)."""
    return stop["kind"] in DOC_KINDS and stop["status"] == "Done" and not stop.get("docs")


def counts(db, order_id):
    """{stop seq: photos sent} of one order."""
    return {row["seq"]: row["n"] for row in db.execute(
        "SELECT seq, count(*) AS n FROM stop_photos WHERE order_id=? GROUP BY seq", (order_id,))}


def _stop(stops, seq):
    try:
        seq = int(seq)
    except (TypeError, ValueError):
        seq = None
    stop = next((s for s in stops if s["seq"] == seq), None)
    if stop is None or stop["kind"] not in DOC_KINDS or stop["status"] != "Done":
        raise ValueError("stop")
    return stop


def save_photo(db, order, stops, seq, image, now=None):
    """One photo from the driver page (a JPEG as a data URL). Returns the photos now sent for that stop."""
    stop = _stop(stops, seq)
    found = re.fullmatch(r"data:image/jpeg;base64,([A-Za-z0-9+/=\s]+)", str(image or ""))
    try:
        data = base64.b64decode(found.group(1), validate=False) if found else b""
    except (binascii.Error, ValueError):
        data = b""
    if not data.startswith(b"\xff\xd8\xff") or len(data) > MAX_BYTES:
        raise ValueError("photo")
    sent = counts(db, order["id"]).get(stop["seq"], 0)
    if sent >= MAX_PER_STOP:
        raise ValueError("too_many")
    db.execute("INSERT INTO stop_photos (order_id, seq, kind, n, taken_at, bytes, data) VALUES (?,?,?,?,?,?,?)",
               (order["id"], stop["seq"], stop["kind"], sent + 1, stamp(now or datetime.now(UTC)), len(data), data))
    return sent + 1


def finish(db, order, stops, seq, mode):
    """The driver is done with the photos of a stop ("sent", at least one photo) or will send them later."""
    stop = _stop(stops, seq)
    if mode == "sent" and not counts(db, order["id"]).get(stop["seq"]):
        raise ValueError("no_photo")
    if mode not in ("sent", "later"):
        raise ValueError("stop")
    db.execute("UPDATE order_stops SET docs=? WHERE order_id=? AND seq=?", (mode, order["id"], stop["seq"]))
    return stop


def photo_list(db, days=120):
    """The office's list (no images): which stop has how many photos, and whether they are already on the PC."""
    since = stamp(datetime.now(UTC) - timedelta(days=days))
    return [{key.lower(): row[key] for key in row.keys()} for row in db.execute(
        "SELECT id, order_id, seq, kind, n, taken_at, bytes, downloaded_at, data IS NOT NULL AS stored "
        "FROM stop_photos WHERE taken_at>=? ORDER BY order_id, seq, n", (since,))]


def photo_bytes(db, photo_id):
    try:
        row = db.execute("SELECT data FROM stop_photos WHERE id=?", (int(photo_id),)).fetchone()
    except (TypeError, ValueError):
        row = None
    if row is None or row["data"] is None:
        raise ValueError("This photo is no longer online: it is in the OneDrive folder on the office PC.")
    return bytes(row["data"])


def empty_old(db, now=None):
    """Empty the images copied to the PC more than KEEP_DAYS ago (the record stays). Returns how many."""
    before = stamp((now or datetime.now(UTC)) - timedelta(days=KEEP_DAYS))
    old = db.execute("SELECT count(*) AS n FROM stop_photos WHERE data IS NOT NULL AND downloaded_at IS NOT NULL "
                     "AND downloaded_at<?", (before,)).fetchone()["n"]
    if old:
        db.execute("UPDATE stop_photos SET data=NULL WHERE data IS NOT NULL AND downloaded_at IS NOT NULL AND downloaded_at<?", (before,))
    return old
