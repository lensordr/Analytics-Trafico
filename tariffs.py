"""Client tariffs (price per route) and the price of an order.

A tariff says what a client pays for a route from one saved location to another:
  loading_t   € per tonne of the weight loaded   (billed on at least MIN_BILLED_KG)
  unloading_t € per tonne of the weight unloaded (billed on at least MIN_BILLED_KG)
  trip        fixed € per trip
  hour        € per hour (the total is entered on the order)
Orders keep a copy of the basis and rate, so later tariff changes never alter past orders.
"""
from datetime import date

from common import identity, item, text
from planner import number

BASES = ("loading_t", "unloading_t", "trip", "hour")
MIN_BILLED_KG = 24000  # per-tonne prices are billed on 24 t even when less is loaded


def chosen(db, table, value):
    return item(db, table, value) if value not in (None, "", 0, "0") else None


def price_date(data):
    value = str(data.get("price_date") or "").strip()[:10]
    if not value:
        return date.today().isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("Price date: use the format YYYY-MM-DD.")


def save_tariff(db, data):
    client = item(db, "locations", data.get("client_id"))
    origin = item(db, "locations", data.get("from_id"))
    destination = item(db, "locations", data.get("to_id"))
    basis = data.get("basis")
    if basis not in BASES:
        raise ValueError("Choose how the price is calculated.")
    values = {"client_id": client["id"], "from_id": origin["id"], "to_id": destination["id"], "basis": basis,
              "rate": number(data, "rate", None, 0, 100000), "km": number(data, "km", 0, 0, 15000),
              "price_date": price_date(data), "notes": text(data, "notes", False, 1000),
              "reference": text(data, "reference", False, 80),
              "active": 1 if data.get("active", True) else 0}
    if values["active"]:
        same = db.execute("SELECT id FROM tariffs WHERE client_id=? AND from_id=? AND to_id=? AND basis=? AND active=1 AND id!=?",
                          (client["id"], origin["id"], destination["id"], basis, identity(data["id"]) if data.get("id") else 0)).fetchone()
        if same:
            raise ValueError(f"{client['company']} already has an active tariff for this route. Edit that one, "
                             "or untick Active on one of them.")
    if data.get("id"):
        tariff = item(db, "tariffs", data["id"])
        db.execute("UPDATE tariffs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?", (*values.values(), tariff["id"]))
        return tariff["id"]
    return db.execute("INSERT INTO tariffs (" + ",".join(values) + ") VALUES (" + ",".join("?" for _ in values) + ")",
                      tuple(values.values())).lastrowid


def delete_tariff(db, data):
    tariff = item(db, "tariffs", data.get("id"))
    used = db.execute("SELECT count(*) FROM orders WHERE tariff_id=?", (tariff["id"],)).fetchone()[0]
    # Orders keep their own copy of the basis and rate.
    db.execute("UPDATE orders SET tariff_id=NULL WHERE tariff_id=?", (tariff["id"],))
    db.execute("DELETE FROM tariffs WHERE id=?", (tariff["id"],))
    return {"message": "Tariff removed" + (f" (the {used} orders that used it keep their price)." if used else ".")}


def order_price(db, data, stops):
    """Client and price fields of an order (all optional): client, tariff used, basis, rate, manual total."""
    client = chosen(db, "locations", data.get("client_id"))
    tariff = chosen(db, "tariffs", data.get("tariff_id"))
    basis = data.get("rate_basis") or ""
    if basis and basis not in BASES:
        raise ValueError("Choose how the price is calculated.")
    rate = number(data, "rate", None, 0, 100000) if basis and data.get("rate") not in (None, "") else None
    total = number(data, "price_eur", None, 0, 1000000) if data.get("price_eur") not in (None, "") else None
    if tariff:
        if client and tariff["client_id"] != client["id"]:
            raise ValueError("The chosen tariff belongs to another client.")
        client = client or item(db, "locations", tariff["client_id"])
        if (tariff["from_id"], tariff["to_id"]) != (stops[0]["location"]["id"], stops[-1]["location"]["id"]):
            tariff = None  # the route changed after the tariff was chosen: keep the rate, drop the link
    # The reference code: as typed on the order; an order saved from a tariff without the field takes the tariff's.
    reference = text(data, "reference", False, 80) if "reference" in data else (tariff or {}).get("reference", "")
    return {"client_id": client["id"] if client else None, "tariff_id": tariff["id"] if tariff else None,
            "rate_basis": basis if rate is not None else "", "rate": rate, "price_eur": total, "reference": reference or ""}


def order_amount(order, stops):
    """Price of an order in €, or None when it cannot be calculated (no rate, or a price per hour)."""
    if order.get("price_eur") is not None:
        return order["price_eur"]
    basis, rate = order.get("rate_basis"), order.get("rate")
    if rate is None or not basis:
        return None
    if basis == "trip":
        return round(rate, 2)
    loaded = sum(stop["weight_kg"] or 0 for stop in stops if stop["kind"] == "Loading")
    if basis == "loading_t":
        return round(max(loaded, MIN_BILLED_KG) / 1000 * rate, 2)
    if basis == "unloading_t":
        unloads = [stop for stop in stops if stop["kind"] == "Unloading"]
        unloaded = sum(stop["weight_kg"] or 0 for stop in unloads[:-1])
        # Final unloading without a weight = everything still on the truck.
        unloaded += unloads[-1]["weight_kg"] if unloads and unloads[-1]["weight_kg"] else max(loaded - unloaded, 0)
        return round(max(unloaded, MIN_BILLED_KG) / 1000 * rate, 2)
    return None
