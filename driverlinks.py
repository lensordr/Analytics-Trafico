"""Secret per-order links for the driver page (/s/<token>)."""

from datetime import datetime, time, timedelta
import re
import secrets

import deca
from common import item
from orders import order_stop_rows, tractor_segments, update_stop_eta
from planner import UTC, WORK_KINDS, ZONE, parse_time, stamp, working_start
from storage import begin_write

LINK_DAYS_AFTER_END = 7

def driver_link(db, data):
    """Secret link for the driver page of an order (created once, reused in every message).
    With `seq` (a tractor change): the link of the driver who takes the trailer there, for their part only."""
    order = item(db, "orders", data.get("order_id"))
    if order["status"] == "Cancelled":
        raise ValueError("This order is cancelled.")
    if data.get("seq"):
        stop = db.execute("SELECT id, driver_token FROM order_stops WHERE order_id=? AND seq=? AND kind='Tractor change'",
                          (order["id"], int(data["seq"]))).fetchone()
        if stop is None:
            raise ValueError("Choose a valid tractor change.")
        token = stop["driver_token"]
        if not token:
            token = secrets.token_urlsafe(16)
            db.execute("UPDATE order_stops SET driver_token=? WHERE id=?", (token, stop["id"]))
        base = deca.settings(db)["public_url"]
        return {"token": token, "url": f"{base}/s/{token}" if base else ""}
    token = order["driver_token"]
    if not token:
        token = secrets.token_urlsafe(16)
        db.execute("UPDATE orders SET driver_token=? WHERE id=?", (token, order["id"]))
    base = deca.settings(db)["public_url"]
    return {"token": token, "url": f"{base}/s/{token}" if base else ""}


def driver_deca(db, order, stops, form):
    """Material (free text), weight and the driver's DNI/NIE sent from the driver page after a loading.

    Everything is optional. Returns the DeCA identifier when the document could be generated, else None
    (e.g. the shipper has no NIF yet: the office completes it)."""
    try:
        seq = int(form.get("seq", ""))
    except ValueError:
        raise ValueError("Invalid stop.") from None
    stop = next((s for s in stops if s["seq"] == seq and s["kind"] == "Loading" and s["status"] == "Done"), None)
    if stop is None or order["status"] == "Cancelled":
        raise ValueError("Invalid stop.")
    material = " ".join(form.get("material", "").split())[:120]
    weight = form.get("weight_kg", "").strip().replace(",", ".")
    if weight:
        try:
            weight = float(weight)
        except ValueError:
            raise ValueError("weight") from None
        if not 1 <= weight <= 60000:
            raise ValueError("weight")
    current = (stop.get("material") or "").split(" · ")[-1]
    if material and material.casefold() != current.casefold():
        # Typed by the driver: free text, not one of the office's materials.
        db.execute("UPDATE order_stops SET material_id=NULL, material=? WHERE order_id=? AND seq=?", (material, order["id"], seq))
    if weight:
        db.execute("UPDATE order_stops SET weight_kg=? WHERE order_id=? AND seq=?", (weight, order["id"], seq))
    dni = re.sub(r"\s", "", form.get("dni", "")).upper()[:30]
    if dni:
        db.execute("UPDATE drivers SET dni_nie=? WHERE id=?", (dni, order["driver_id"]))
    try:
        result = deca.prepare(db, {"order_id": order["id"], "shipper_id": order["deca_shipper_id"] or order["loading_id"],
                                   "notes": order["deca_notes"] or ""})
    except ValueError:
        return None  # something only the office can add (shipper NIF, company details, another loading)
    return result["id"]


def driver_order(db, token):
    """The order of a driver link, with only that driver's part of the journey when the order changes tractor.
    The order's tractor, driver and trailer shown are the ones of that part."""
    row = db.execute("SELECT * FROM orders WHERE driver_token=?", (token,)).fetchone() if token else None
    start_seq = 1
    if row is None and token:
        stop = db.execute("SELECT order_id, seq FROM order_stops WHERE driver_token=? AND kind='Tractor change'",
                          (token,)).fetchone()
        if stop:
            row = db.execute("SELECT * FROM orders WHERE id=?", (stop["order_id"],)).fetchone()
            start_seq = stop["seq"]
    if row is None:
        return None, [], {}
    order = {key.lower(): row[key] for key in row.keys()}
    ended = order["delivered_at"] if order["status"] == "Completed" else order["available_at"] if order["status"] == "Cancelled" else None
    if ended and parse_time(ended) < datetime.now(UTC) - timedelta(days=LINK_DAYS_AFTER_END):
        return None, [], {}  # expired: the page no longer shows addresses or the trip
    stops = order_stop_rows(db, order["id"])
    part = next((p for p in tractor_segments(order, stops) if p[4] == start_seq), None)
    if part is None:
        return None, [], {}
    truck_id, driver_id, _, _, first, last = part
    if (first, last) != (1, stops[-1]["seq"] if stops else 1):
        stops = [stop for stop in stops if first <= stop["seq"] <= last]
        truck = db.execute("SELECT plate, trailer FROM trucks WHERE id=?", (truck_id,)).fetchone() if truck_id else None
        driver = db.execute("SELECT name FROM drivers WHERE id=?", (driver_id,)).fetchone() if driver_id else None
        order.update(plate=truck["plate"] if truck else "—", driver_id=driver_id, driver_name=driver["name"] if driver else "",
                     part_first=first, part_last=last)
    places = {stop["location_id"]: item(db, "locations", stop["location_id"]) for stop in stops}
    return order, stops, places


OPEN_RECORD_MINUTES = 10  # repeated views within this time are not written again


def mark_opened(db, token):
    """Record that the driver opened the link (first time, and the last time at most every 10 minutes)."""
    now = datetime.now(UTC)
    row = db.execute("SELECT last_at FROM link_opens WHERE token=?", (token,)).fetchone()
    if row and parse_time(row["last_at"]) > now - timedelta(minutes=OPEN_RECORD_MINUTES):
        return
    begin_write(db)
    if row:
        db.execute("UPDATE link_opens SET last_at=?, views=views+1 WHERE token=?", (stamp(now), token))
    else:
        db.execute("INSERT INTO link_opens (token, first_at, last_at) VALUES (?,?,?)", (token, stamp(now), stamp(now)))


def link_opens(db):
    return {row["token"]: {"first_at": row["first_at"], "last_at": row["last_at"], "views": row["views"]}
            for row in db.execute("SELECT * FROM link_opens")}


DELAY_HOURS = {"1": 1, "2": 2, "4": 4}
REPORT_DAYS = 3


def report_delay(db, order, stop, delay, now=None):
    """The driver is running late for their next stop: move its ETA (and the later stops) and tell the office.
    "tomorrow": the next morning at 08:00. A loading/unloading after 18:30 or on Sunday moves to the next working morning."""
    now = now or datetime.now(UTC)
    base = max(parse_time(stop["planned_at"]), now)
    if delay == "tomorrow":
        new = datetime.combine(base.astimezone(ZONE).date() + timedelta(days=1), time(8), ZONE).astimezone(UTC)
    elif delay in DELAY_HOURS:
        new = base + timedelta(hours=DELAY_HOURS[delay])
    else:
        raise ValueError("Choose how late you are.")
    if stop["kind"] in WORK_KINDS:
        new = working_start(new)
    update_stop_eta(db, {"order_id": order["id"], "seq": stop["seq"], "planned_at": stamp(new)})
    db.execute("INSERT OR REPLACE INTO eta_reports (order_id, seq, reported_at, delay, new_at) VALUES (?,?,?,?,?)",
               (order["id"], stop["seq"], stamp(now), delay, stamp(new)))
    return new


def eta_reports(db, now=None):
    since = stamp((now or datetime.now(UTC)) - timedelta(days=REPORT_DAYS))
    return [dict(row) for row in db.execute("SELECT * FROM eta_reports WHERE reported_at>=? ORDER BY reported_at DESC", (since,))]
