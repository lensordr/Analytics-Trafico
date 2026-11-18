"""Claude connector (MCP over HTTP): read-only dispatch tools for Claude (claude.ai → Settings → Connectors).

Claude asks; these tools answer from the live planning with the same rules as the app (free position of each tractor,
empty km straight line × 1.25 at 65 km/h with breaks and rests, places open 08:00–18:30 and closed on Sunday, saved
routes and nearby companies).
Nothing is written. No phone numbers, driver links, DNI or prices of other carriers are sent.
"""
import json
from datetime import datetime, timedelta

import routing
from nearby import nearby_companies
from nextloads import QuickClock, next_loads
from orders import tractor_segments
from planner import OPENS_AT, UTC, ZONE, parse_time, stamp, working_start
from storage import rows

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
ROAD_FACTOR, EMPTY_KMH = 1.25, 65
INSTRUCTIONS = (
    "MalcomTrans dispatch planner (Spanish road haulage, Europe/Madrid time). Read-only. "
    "Workflow: find_places to turn a client or town into a location id; fleet_status for where every tractor is and "
    "when it is free; suggest_tractors to choose which tractor to send to a loading; loads_near for what a free tractor "
    "(or a place) can load next. Loading/unloading places close at 18:30 and on Sunday (moves to the next morning 08:00). "
    "Km are estimates unless marked 'HERE'. Recommend; the dispatcher creates the order in the app. "
    "Answer in the user's language.")


def _fmt(dt):
    return dt.astimezone(ZONE).strftime("%a %d.%m %H:%M") if dt else None


def can_work(at):
    """When a loading can start at a place reached at `at`: closed after 18:30, on Sunday and before 08:00."""
    start = working_start(at)
    local = start.astimezone(ZONE)
    if local.hour < OPENS_AT:
        start = local.replace(hour=OPENS_AT, minute=0).astimezone(UTC)
    return start


def _when(value, now):
    if not value:
        return now
    text = str(value).strip().replace(" ", "T", 1)
    if len(text) == 10:
        text += "T08:00"
    return parse_time(text)


class Data:
    """One read of the planning, shared by the tools of a request."""

    def __init__(self, db, now=None):
        self.now = (now or datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
        self.db = db
        self.locations = {loc["id"]: loc for loc in rows(db, "locations")}
        self.drivers = {d["id"]: d for d in rows(db, "drivers")}
        self.trucks = [t for t in rows(db, "trucks") if t["active"] and not t.get("archived_at")]
        self.orders = [o for o in rows(db, "orders") if o["status"] != "Cancelled"]
        self.parts = {}
        for order in self.orders:
            for truck_id, driver_id, start, end, first, last in tractor_segments(order, order["stops"]):
                if truck_id:
                    self.parts.setdefault(truck_id, []).append(
                        {"order": order, "driver_id": driver_id, "start": start, "end": end, "first": first, "last": last})

    def place(self, location_id):
        loc = self.locations.get(location_id)
        return loc and {"id": loc["id"], "company": loc["company"], "city": loc["city"], "country": loc["country"]}

    def end_place(self, part):
        order = part["order"]
        stop = next((s for s in order["stops"] if s["seq"] == part["last"]), None)
        return self.locations.get(stop["location_id"] if stop else order["unloading_id"])

    def tractor_at(self, truck, at):
        """Where the tractor is at `at` (or ends the work it is doing then), when it is free, its next booking."""
        parts = self.parts.get(truck["id"], [])
        busy = next((p for p in parts if p["start"] <= at < p["end"]), None)
        before = max((p for p in parts if p["end"] <= at), key=lambda p: p["end"], default=None)
        nxt = min((p for p in parts if p["start"] > at), key=lambda p: p["start"], default=None)
        ref = busy or before
        place = self.end_place(ref) if ref else self.locations.get(truck.get("location_id"))
        free = ref["end"] if ref else (parse_time(truck["available_from"]) if truck.get("available_from") else None)
        driver = self.drivers.get((ref or {}).get("driver_id") or truck.get("driver_id"))
        return {"place": place, "free": free, "busy": busy, "next": nxt, "driver": driver}

    def last_free(self, truck):
        """After all its bookings: where and when the tractor becomes free."""
        parts = self.parts.get(truck["id"], [])
        last = max(parts, key=lambda p: p["end"], default=None)
        if not last or last["end"] < self.now - timedelta(days=30):
            loc = self.locations.get(truck.get("location_id")) if not last else self.end_place(last)
            return loc, self.now, last
        return self.end_place(last), max(last["end"], self.now), last

    def truck(self, plate):
        key = str(plate or "").replace(" ", "").replace("-", "").casefold()
        for truck in self.trucks:
            if truck["plate"].replace(" ", "").replace("-", "").casefold() == key:
                return truck
        raise ValueError(f"No active tractor with plate {plate}. Use fleet_status to see the plates.")

    def location(self, location_id):
        try:
            loc = self.locations.get(int(location_id))
        except (TypeError, ValueError):
            loc = None
        if not loc:
            raise ValueError("Unknown location id. Use find_places first.")
        return loc


def _driver(driver):
    if not driver:
        return None
    return {"name": driver["name"], "carrier": driver.get("carrier") or "MalcomTrans",
            "goes_home_weekends": not driver.get("weekend_away")}


# ---------------------------------------------------------------- tools
def find_places(data, args):
    query = str(args.get("query", "")).strip().casefold()
    if len(query) < 2:
        raise ValueError("Write at least 2 letters of the company, town or postal code.")
    loads, unloads = {}, {}
    for order in data.orders:
        for stop in order["stops"]:
            target = loads if stop["kind"] == "Loading" else unloads if stop["kind"] == "Unloading" else None
            if target is not None:
                target[stop["location_id"]] = target.get(stop["location_id"], 0) + 1
    words = query.split()
    found = []
    for loc in data.locations.values():
        hay = " ".join(str(loc.get(k) or "") for k in ("company", "city", "postal_code", "country", "address")).casefold()
        if all(word in hay for word in words):
            found.append({"id": loc["id"], "company": loc["company"], "city": loc["city"], "postal_code": loc["postal_code"],
                          "country": loc["country"], "kind": loc.get("kind"), "transport_company": bool(loc.get("transport_company")),
                          "has_coordinates": loc.get("lat") is not None, "loadings": loads.get(loc["id"], 0),
                          "unloadings": unloads.get(loc["id"], 0)})
    found.sort(key=lambda r: (-(r["loadings"] + r["unloadings"]), r["company"]))
    return {"count": len(found), "places": found[:20]}


def fleet_status(data, args):
    result = []
    for truck in sorted(data.trucks, key=lambda t: t["plate"]):
        now = data.tractor_at(truck, data.now)
        place, free, last = data.last_free(truck)
        upcoming = sorted((p for p in data.parts.get(truck["id"], []) if p["end"] > data.now), key=lambda p: p["start"])
        result.append({
            "plate": truck["plate"], "trailer": truck.get("trailer") or None, "driver": _driver(now["driver"]),
            "now": (f"on order {now['busy']['order']['number']}" if now["busy"] else "free"),
            "free_from": _fmt(free), "free_at": data.place(place["id"]) if place else None,
            "bookings": [{"order": p["order"]["number"], "from": _fmt(p["start"]), "to": _fmt(p["end"]),
                          "loading": (data.place(p["order"]["loading_id"]) or {}).get("city"),
                          "unloading": (data.place(p["order"]["unloading_id"]) or {}).get("city")} for p in upcoming[:4]]})
    return {"now": _fmt(data.now), "tractors": result}


def suggest_tractors(data, args):
    target = data.location(args.get("location_id"))
    if target.get("lat") is None:
        raise ValueError(f"{target['company']} ({target['city']}) has no coordinates yet: save its position in the app first.")
    at = _when(args.get("at"), data.now)
    options = []
    for truck in data.trucks:
        info = data.tractor_at(truck, at)
        origin = info["place"]
        if not origin or origin.get("lat") is None:
            continue
        km = round(routing.air_distance(origin, target) * ROAD_FACTOR, 1)
        hours = km / EMPTY_KMH
        ready = max(info["free"] or data.now, data.now)
        clock = QuickClock(ready)  # breaks and daily rests, driver rested when free
        clock.drive(km)
        arrive = clock.t
        can_load = can_work(max(arrive, at))
        clash = info["next"] and info["next"]["start"] < can_load + timedelta(hours=4)
        options.append({"truck": truck, "origin": origin, "km": km, "hours": round(hours, 1), "ready": ready,
                        "arrive": arrive, "can_load": can_load, "late": arrive > at + timedelta(minutes=30),
                        "info": info, "clash": bool(clash)})
    options.sort(key=lambda o: (o["clash"], o["late"], o["arrive"] if o["late"] else o["km"]))
    top = options[:int(args.get("limit") or 6)]
    if routing.configured():  # exact truck km for the first three
        for option in top[:3]:
            try:
                leg = routing.truck_route(option["origin"], target, option["truck"], "any")
                option["km"], option["hours"], option["here"] = leg["distance_km"], round(leg["driving_hours"], 1), True
            except Exception:
                pass
    return {"loading": data.place(target["id"]), "wanted_at": _fmt(at), "options": [{
        "plate": o["truck"]["plate"], "trailer": o["truck"].get("trailer") or None, "driver": _driver(o["info"]["driver"]),
        "free_from": _fmt(o["ready"]), "free_at": data.place(o["origin"]["id"]),
        "empty_km": o["km"], "empty_hours": o["hours"], "km_source": "HERE truck route" if o.get("here") else "estimate",
        "arrives": _fmt(o["arrive"]), "can_load": _fmt(o["can_load"]), "in_time": not o["late"],
        "next_booking": ({"order": o["info"]["next"]["order"]["number"], "starts": _fmt(o["info"]["next"]["start"])}
                         if o["info"]["next"] else None),
        "conflicts_with_next_booking": o["clash"]} for o in top],
        "note": "Ranked: no clash with the next booking, then in time, then fewest empty km."}


def loads_near(data, args):
    driver = None
    if args.get("plate"):
        truck = data.truck(args["plate"])
        if args.get("from"):
            info = data.tractor_at(truck, _when(args["from"], data.now))
            origin, start, driver = info["place"], max(info["free"] or data.now, _when(args["from"], data.now)), info["driver"]
        else:
            origin, start, last = data.last_free(truck)
            driver = data.drivers.get((last or {}).get("driver_id") or truck.get("driver_id"))
        if not origin:
            raise ValueError(f"{truck['plate']} has no known position.")
    elif args.get("location_id"):
        origin, start = data.location(args["location_id"]), _when(args.get("from"), data.now)
    else:
        raise ValueError("Give a tractor plate or a location id.")
    radius = float(args.get("radius_km") or 150)
    routes = next_loads(data.db, {"location_id": origin["id"], "from": stamp(start), "radius_km": radius,
                                  **({"driver_id": driver["id"]} if driver else {})}, now=data.now)
    companies = nearby_companies(data.db, {"location_id": origin["id"], "radius_km": radius}, now=data.now)
    return {
        "from": data.place(origin["id"]), "free_from": _fmt(start), "driver": _driver(driver), "radius_km": radius,
        "saved_routes": [{
            "client": (data.place(r["tariff"]["client_id"]) or {}).get("company"),
            "loading": data.place(r["tariff"]["from_id"]), "unloading": data.place(r["tariff"]["to_id"]),
            "empty_km": r["empty_km"], "loaded_km": r["loaded_km"], "revenue_eur": r["revenue"], "eur_per_km": r["per_km"],
            "eur_per_hour": r["per_hour"], "reaches_loading": _fmt(parse_time(r["load_at"])) if r["load_at"] else None,
            "can_load": _fmt(can_work(parse_time(r["load_at"]))) if r["load_at"] else None,
            "unloads_at": _fmt(parse_time(r["unload_at"])) if r["unload_at"] else None,
            "home_for_weekend": r["home_for_weekend"], "transport_company": r["transport_client"],
            "done_before": r["orders"]} for r in routes["routes"][:10]],
        "companies_nearby": [{
            "id": c["location"]["id"], "company": c["location"]["company"], "city": c["location"]["city"],
            "km_air": c["km"], "loads_given": c["loads"], "last_load": c["last_load_at"][:10] if c["last_load_at"] else None,
            "usual_destinations": c["destinations"],
            "last_contact": ({"at": c["last_contact"]["at"][:10], "result": c["last_contact"]["result"]}
                             if c["last_contact"] else None)} for c in companies["companies"][:15]],
        "note": " ".join(filter(None, [routes["note"], companies["note"], "Saved routes: best first (towards home from "
              "Thursday, direct clients before transport companies, then € per hour). unloads_at assumes loading on "
              "arrival; if can_load is later, the unloading moves too."]))}


TOOLS = {
    "find_places": (find_places, "Find saved companies and places (clients, loading/unloading points) by company name, "
                    "town or postal code. Returns location ids for the other tools, with how often each loaded or unloaded.",
                    {"query": {"type": "string", "description": "Part of the company name, town or postal code"}}, ["query"]),
    "fleet_status": (fleet_status, "All active tractors: driver, trailer, whether on an order now, when and where they "
                     "become free after their bookings, and their next bookings.", {}, []),
    "suggest_tractors": (suggest_tractors, "Which tractor to send to a loading point: ranks the tractors by when they "
                         "are free, empty km to the loading and whether they arrive in time (with the 18:30 / Sunday "
                         "closing rule and their next booking).",
                         {"location_id": {"type": "integer", "description": "Loading place id (from find_places)"},
                          "at": {"type": "string", "description": "Wanted loading time, Europe/Madrid, e.g. 2026-11-19 08:00 (default: now)"},
                          "limit": {"type": "integer", "description": "How many options (default 6)"}}, ["location_id"]),
    "loads_near": (loads_near, "What a tractor (by plate, from where it becomes free) or a place can load next: saved "
                   "routes with tariffs (empty km, revenue, €/km, home for the weekend) and saved companies nearby with "
                   "their load and contact history.",
                   {"plate": {"type": "string", "description": "Tractor plate"},
                    "location_id": {"type": "integer", "description": "Or a place id (from find_places)"},
                    "from": {"type": "string", "description": "From when, Europe/Madrid (default: when it becomes free)"},
                    "radius_km": {"type": "number", "description": "Search radius in km (default 150)"}}, []),
}


def tool_list():
    return [{"name": name, "description": description,
             "inputSchema": {"type": "object", "properties": props, "required": required},
             "annotations": {"readOnlyHint": True, "openWorldHint": False}}
            for name, (_, description, props, required) in TOOLS.items()]


def call(db, name, args, now=None):
    if name not in TOOLS:
        raise KeyError(name)
    return TOOLS[name][0](Data(db, now), args if isinstance(args, dict) else {})


def handle(message, open_db, now=None):
    """One JSON-RPC message → the response (None for notifications)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    method, mid, params = message["method"], message.get("id"), message.get("params") or {}
    if mid is None:
        return None  # notification (e.g. notifications/initialized)
    ok = lambda result: {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[1],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "MalcomTrans", "version": "1.0"}, "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        try:
            with open_db() as db:
                result = call(db, params.get("name"), params.get("arguments") or {}, now)
            return ok({"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
                       "isError": False})
        except KeyError:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool {params.get('name')}"}}
        except ValueError as error:
            return ok({"content": [{"type": "text", "text": str(error)}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
