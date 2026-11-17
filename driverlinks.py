"""Secret per-order links for the driver page (/s/<token>)."""

from datetime import datetime, timedelta
import re
import secrets

import deca
from common import item
from orders import order_stop_rows
from planner import UTC, parse_time

LINK_DAYS_AFTER_END = 7

def driver_link(db, data):
    """Secret link for the driver page of an order (created once, reused in every message)."""
    order = item(db, "orders", data.get("order_id"))
    if order["status"] == "Cancelled":
        raise ValueError("This order is cancelled.")
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
    row = db.execute("SELECT * FROM orders WHERE driver_token=?", (token,)).fetchone() if token else None
    if row is None:
        return None, [], {}
    order = {key.lower(): row[key] for key in row.keys()}
    ended = order["delivered_at"] if order["status"] == "Completed" else order["available_at"] if order["status"] == "Cancelled" else None
    if ended and parse_time(ended) < datetime.now(UTC) - timedelta(days=LINK_DAYS_AFTER_END):
        return None, [], {}  # expired: the page no longer shows addresses or the trip
    stops = order_stop_rows(db, order["id"])
    places = {stop["location_id"]: item(db, "locations", stop["location_id"]) for stop in stops}
    return order, stops, places
