"""Secret per-order links for the driver page (/s/<token>)."""

from datetime import datetime, timedelta
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
