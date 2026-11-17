"""Next loads for a free truck: the saved routes (active tariffs) that load near it, and whether the
driver is home at the base for the weekend.

Distances: the tariff's km for the loaded leg; empty and return legs are straight-line × ROAD_FACTOR.
Times: AVERAGE_KMH, loading and unloading as on a new order (2 h each; longer ones are changed on the order), 45-min break after 4.5 h driving, 11-h rest after
9 h driving or 13 h on duty. The driver is assumed rested when the truck becomes free. An estimate, not a plan.
"""
from datetime import datetime, time, timedelta

import deca
import routing
from common import item
from orders import DEFAULT_HANDLING_HOURS
from tariffs import MIN_BILLED_KG
from planner import UTC, ZONE, number, parse_time, stamp
from storage import rows

ROAD_FACTOR = 1.25
AVERAGE_KMH = 65
HANDLING_MINUTES = round(DEFAULT_HANDLING_HOURS * 60)


def home_base(db):
    """The location trucks return to: Settings → home base, else the Parking location named "… base …"."""
    chosen = deca.settings(db).get("home_base_id")
    if chosen:
        row = db.execute("SELECT * FROM locations WHERE id=?", (chosen,)).fetchone()
        if row:
            return dict(row)
    row = db.execute("SELECT * FROM locations WHERE kind='Parking' AND lower(company) LIKE '%base%' ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else None


def road_km(a, b):
    if a["id"] == b["id"]:
        return 0.0
    air = routing.air_distance(a, b)
    return round(air * ROAD_FACTOR, 1) if air is not None else None


class QuickClock:
    """Fast driving-time estimate with breaks and daily rests (minutes, chunked rather than minute by minute)."""

    def __init__(self, start):
        self.t, self.day, self.block, self.duty = start, 0, 0, 0

    def _rest(self):
        self.t += timedelta(hours=11)
        self.day = self.block = self.duty = 0

    def drive(self, km):
        left = round(km / AVERAGE_KMH * 60)
        while left > 0:
            if self.day >= 540 or self.duty >= 780:
                self._rest()
                continue
            if self.block >= 270:
                if self.duty + 45 >= 780:
                    self._rest()
                else:
                    self.t += timedelta(minutes=45)
                    self.duty += 45
                    self.block = 0
                continue
            chunk = min(left, 270 - self.block, 540 - self.day, 780 - self.duty)
            self.t += timedelta(minutes=chunk)
            self.day, self.block, self.duty, left = self.day + chunk, self.block + chunk, self.duty + chunk, left - chunk

    def work(self, minutes):
        if self.duty + minutes > 780:
            self._rest()
        self.t += timedelta(minutes=minutes)
        self.duty += minutes


def weekend_deadline(start):
    """End of the Saturday of the week the truck is free in (a truck free on Sunday: next Saturday)."""
    local = start.astimezone(ZONE).date()
    saturday = local + timedelta(days=(5 - local.weekday()) % 7)
    return datetime.combine(saturday + timedelta(days=1), time.min, ZONE).astimezone(UTC)


def next_loads(db, data, now=None):
    origin = item(db, "locations", data.get("location_id"))
    radius = number(data, "radius_km", 0, 0, 5000) or None  # 0 or missing: no distance limit, closest first
    load_t = max(number(data, "load_t", MIN_BILLED_KG / 1000, 1, 45), MIN_BILLED_KG / 1000)  # never below the billed minimum
    now = (now or datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
    start = max(parse_time(data["from"]), now) if data.get("from") else now
    driver = item(db, "drivers", data["driver_id"]) if data.get("driver_id") else None
    if "weekend_away" in data and data["weekend_away"] is not None:
        away = bool(data["weekend_away"])
    else:
        away = bool(driver and driver.get("weekend_away"))
    base = home_base(db)
    locations = {loc["id"]: loc for loc in rows(db, "locations")}
    history = {}
    for order in db.execute("SELECT loading_id, unloading_id, pickup_at FROM orders WHERE status!='Cancelled'"):
        key = (order["loading_id"], order["unloading_id"])
        count, last = history.get(key, (0, ""))
        history[key] = (count + 1, max(last, order["pickup_at"]))
    deadline = weekend_deadline(start)
    results, missing = [], set()
    for tariff in db.execute("SELECT * FROM tariffs WHERE active=1"):
        tariff = dict(tariff)
        loading, unloading = locations.get(tariff["from_id"]), locations.get(tariff["to_id"])
        if not loading or not unloading:
            continue
        empty = road_km(origin, loading)
        if empty is None:
            same_city = all(origin[k].strip().casefold() == loading[k].strip().casefold() for k in ("city", "country"))
            if not same_city:
                missing.add(tariff["id"])
                continue
            empty = 0.0
        if radius and empty > radius * ROAD_FACTOR:
            continue
        loaded = tariff["km"] or road_km(loading, unloading)
        back = road_km(unloading, base) if base else None
        if tariff["basis"] == "trip":
            revenue = tariff["rate"]
        elif tariff["basis"] in ("loading_t", "unloading_t") and tariff["rate"] is not None:
            revenue = round(tariff["rate"] * load_t, 2)
        else:
            revenue = None
        per_km = round(revenue / (empty + loaded), 2) if revenue and loaded and empty + loaded > 0 else None
        # € per hour of work (driving empty and loaded + loading and unloading): fair between short and long trips.
        per_hour = round(revenue / ((empty + loaded) / AVERAGE_KMH + 2 * HANDLING_MINUTES / 60), 2) if revenue and loaded else None
        load_at = unload_at = home_at = None
        if loaded:
            clock = QuickClock(start)
            clock.drive(empty)
            load_at = clock.t
            clock.work(HANDLING_MINUTES)
            clock.drive(loaded)
            unload_at = clock.t
            clock.work(HANDLING_MINUTES)
            if back is not None:
                clock.drive(back)
                home_at = clock.t
        done, last = history.get((loading["id"], unloading["id"]), (0, ""))
        client = locations.get(tariff["client_id"]) or {}
        results.append({"tariff": tariff, "empty_km": empty, "loaded_km": loaded, "loaded_km_estimated": not tariff["km"],
                        "back_km": back, "revenue": revenue, "per_km": per_km, "per_hour": per_hour,
                        "load_at": stamp(load_at) if load_at else None, "unload_at": stamp(unload_at) if unload_at else None, "home_at": stamp(home_at) if home_at else None,
                        "home_for_weekend": None if home_at is None or away else home_at <= deadline,
                        "transport_client": bool(client.get("transport_company")),
                        "orders": done, "last_order_at": last or None})
    # Thursday on (when the driver goes home at weekends): routes that get the driver home come first.
    # Then direct clients before transport companies (they pay less), then by € per hour.
    towards_home = not away and start.astimezone(ZONE).weekday() >= 3
    results.sort(key=lambda r: ((r["home_for_weekend"] is False) if towards_home else False, r["transport_client"],
                                -(r["per_hour"] or -1), r["empty_km"]))
    return {"origin": origin, "base": base, "radius_km": radius, "load_t": load_t, "from": stamp(start),
            "weekend_deadline": stamp(deadline), "weekend_away": away, "towards_home": towards_home,
            "driver": {"id": driver["id"], "name": driver["name"]} if driver else None,
            "total": len(results), "routes": results, "missing_coordinates": len(missing),
            "note": None if origin.get("lat") is not None else
            f"{origin['company']} ({origin['city']}) has no coordinates: only routes loading in the same city are shown."}
