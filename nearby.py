"""Companies near a free truck, and the contact log."""

from datetime import datetime, timedelta

import routing
from common import item, text
from masters import CONTACT_RESULTS, LOADING_KINDS
from planner import UTC, number, parse_time, stamp
from storage import rows

def nearby_companies(db, data, now=None):
    """Saved companies around a truck's free position, closest first, with load and contact history."""
    origin = item(db, "locations", data.get("location_id"))
    radius = number(data, "radius_km", 150, 5, 2000)
    include_all = bool(data.get("include_all"))
    now = (now or datetime.now(UTC)).astimezone(UTC)
    locations = rows(db, "locations")
    by_id = {loc["id"]: loc for loc in locations}
    orders = {o["id"]: o for o in rows(db, "orders") if o["status"] != "Cancelled"}
    loads = {}
    for order in orders.values():
        for stop in order["stops"]:
            if stop["kind"] == "Loading":
                loads.setdefault(stop["location_id"], []).append((stop["planned_at"], order))
    same_city = lambda a, b: all(a[k].strip().casefold() == b[k].strip().casefold() for k in ("city", "country"))
    results, missing = [], []
    for loc in locations:
        if not include_all and loc["kind"] not in LOADING_KINDS:
            continue
        distance = routing.air_distance(origin, loc)
        if distance is None:
            if not same_city(origin, loc):
                missing.append(loc["company"])
                continue
        elif distance > radius:
            continue
        history = sorted(loads.get(loc["id"], []), key=lambda entry: entry[0], reverse=True)
        destinations = {}
        for _, order in history:
            target = by_id.get(order["unloading_id"])
            if target:
                label = f"{target['city']} ({target['country']})"
                destinations[label] = destinations.get(label, 0) + 1
        last_contact = loc["contacts"][0] if loc["contacts"] else None
        recent = bool(last_contact and now - parse_time(last_contact["at"]) < timedelta(days=3))
        results.append({"location": {k: v for k, v in loc.items() if k != "contacts"},
                        "km": distance, "same_site": loc["id"] == origin["id"],
                        "loads": len(history), "last_load_at": history[0][0] if history else None,
                        "destinations": sorted(destinations, key=lambda d: -destinations[d])[:3],
                        "last_contact": last_contact, "contacts": loc["contacts"][:5],
                        "contacted_recently": recent})
    # Closest first; at the same distance, companies that already gave loads come first.
    results.sort(key=lambda r: (r["km"] if r["km"] is not None else 0, not r["same_site"], -r["loads"]))
    note = None
    if origin.get("lat") is None or origin.get("lon") is None:
        note = (f"{origin['company']} ({origin['city']}) has no coordinates, so only companies in the same city "
                "are shown. Add coordinates under Saved locations to rank by distance.")
    return {"origin": origin, "radius_km": radius, "companies": results,
            "missing_coordinates": len(missing), "note": note}


def log_contact(db, data):
    location = item(db, "locations", data.get("location_id"))
    result = text(data, "result", True, 40)
    if result not in CONTACT_RESULTS:
        raise ValueError("Choose the result of the contact from the list.")
    at = parse_time(data["at"]) if data.get("at") else datetime.now(UTC).replace(second=0, microsecond=0)
    db.execute("INSERT INTO location_contacts (location_id, at, result, note) VALUES (?, ?, ?, ?)",
               (location["id"], stamp(at), result, text(data, "note", False, 500)))
    return {"message": f"Contact with {location['company']} saved: {result}."}
