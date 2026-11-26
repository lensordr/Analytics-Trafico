"""Loading orders: journey stops, timing, conflicts, stop confirmations and ETAs."""

import json
from datetime import datetime, timedelta

import routing
from common import identity, item, text
from masters import selected_trailer
from materials import material_label
from tariffs import order_price
from planner import WORK_KINDS, Clock, UTC, ZONE, closed_label, number, parse_time, route_plan, stamp, week_key, working_start
from storage import begin_write, reserve_id, rows

STOP_KINDS = ("Loading", "Unloading", "Trailer swap", "Tractor change")
QUICK_STOPS = ("Trailer swap", "Tractor change")  # 0.5 h by default: hooking or unhooking a trailer
DEFAULT_HANDLING_HOURS = 2  # loading/unloading time when the form does not say otherwise
MAX_EXTRA_STOPS = 6


TIMINGS = ("manual", "rules")


def order_timing(data):
    """`manual`: the dispatcher enters each arrival time (as told by the driver), no driving clock.
    `rules`: times are calculated from driving hours with the EU driving-rules clock."""
    timing = data.get("timing") or "rules"
    if timing not in TIMINGS:
        raise ValueError("Unknown timing mode.")
    return timing


def uses_rules(plan):
    """Orders planned with the driving-rules clock; later rules orders of the driver depend on them."""
    if isinstance(plan, str):
        plan = json.loads(plan or "{}")
    return bool((plan or {}).get("end_state"))


def manual_leg(source, km_key, hours_key, label, manual_times=False):
    km = number(source, km_key, 0, 0, 15000)
    hours = number(source, hours_key, 0, 0, 240)
    if km > 0 and hours <= 0 and not manual_times:
        raise ValueError(f"{label}: enter driving hours as well as kilometres. Use driving time, without rest breaks.")
    return km, hours


def manual_plan(pickup, stops):
    """Plan from arrival times entered by the dispatcher; no rests are added or checked."""
    times, timeline, previous_end = [], [], None
    for index, stop in enumerate(stops):
        arrival = pickup if index == 0 else stop["arrive_at"]
        if previous_end and arrival < previous_end and stop.get("estimated"):
            arrival = previous_end
        if previous_end and arrival < previous_end:
            raise ValueError(f"Stop {index + 1} ({stop['kind'].lower()}, {stop['location']['city']}): the arrival "
                             f"cannot be before the previous stop finishes ({stamp(previous_end)[11:16]} on {stamp(previous_end)[:10]}).")
        if previous_end and arrival > previous_end:
            timeline.append({"kind": "On the road (estimated, with breaks and rests)" if stop.get("estimated") else "On the road (time from driver)",
                             "start": stamp(previous_end), "end": stamp(arrival),
                             "minutes": round((arrival - previous_end).total_seconds() / 60)})
        # Arriving after 18:30 (or on Sunday): the loading/unloading is the next working morning.
        start = working_start(arrival) if index and stop["kind"] in WORK_KINDS else arrival
        if start > arrival:
            timeline.append({"kind": closed_label(start), "start": stamp(arrival), "end": stamp(start),
                             "minutes": round((start - arrival).total_seconds() / 60)})
        end = start + timedelta(hours=stop["handling_hours"])
        if end > start:
            timeline.append({"kind": stop["kind"], "start": stamp(start), "end": stamp(end),
                             "minutes": round(stop["handling_hours"] * 60)})
        times.append({"arrival_at": stamp(arrival), "start_at": stamp(start), "end_at": stamp(end),
                      "next_day": start > arrival, "estimated": bool(stop.get("estimated"))})
        previous_end = end
    return {"timing": "manual", "departure_at": times[0]["end_at"], "arrival_at": times[-1]["arrival_at"],
            "unloading_at": times[-1]["start_at"], "available_at": times[-1]["end_at"],
            "elapsed_hours": round((previous_end - pickup).total_seconds() / 3600, 2), "rest_hours": 0,
            "timeline": timeline, "end_state": None, "stops": times}


def weight(data, key, label):
    """Optional weight in kg (empty = not entered)."""
    raw = data.get(key)
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = -1
    if not 0 < value <= 60000:
        raise ValueError(f"{label}: weight must be between 1 and 60000 kg.")
    return value


def chosen_material(db, value):
    """Optional material loaded at a loading stop (empty = not specified)."""
    return item(db, "materials", value) if value not in (None, "", 0, "0") else None


def journey_stops(db, data, create=False):
    """Ordered stops: the loading point, optional extra stops (`via`), then the unloading point."""
    manual_times = order_timing(data) == "manual"
    via = data.get("via") or []
    if not isinstance(via, list) or len(via) > MAX_EXTRA_STOPS:
        raise ValueError(f"Add at most {MAX_EXTRA_STOPS} extra stops.")
    stops = [{"kind": "Loading", "location": item(db, "locations", data.get("loading_id")),
              "handling_hours": number(data, "loading_hours", DEFAULT_HANDLING_HOURS, 0, 6),
              "material": chosen_material(db, data.get("material_id")),
              "weight_kg": weight(data, "loading_weight_kg", "Loading")}]
    for index, extra in enumerate(via, start=2):
        if not isinstance(extra, dict):
            raise ValueError("Invalid extra stop.")
        kind = extra.get("kind")
        if kind not in STOP_KINDS:
            raise ValueError(f"Stop {index}: choose loading, unloading or trailer swap.")
        stop = {"kind": kind, "location": item(db, "locations", extra.get("location_id")),
                "handling_hours": number(extra, "handling_hours", 0.5 if kind in QUICK_STOPS else DEFAULT_HANDLING_HOURS, 0, 6),
                "leg": manual_leg(extra, "leg_km", "leg_hours", f"Stop {index}", manual_times)}
        if manual_times:
            stop["arrive_at"] = optional_arrival(extra, "arrive_at")
        if kind == "Loading":
            stop["material"] = chosen_material(db, extra.get("material_id"))
        if kind in ("Loading", "Unloading"):
            stop["weight_kg"] = weight(extra, "weight_kg", f"Stop {index}")
        if kind == "Trailer swap":
            picked = selected_trailer(db, {"trailer_id": extra.get("pick_trailer_id") or "",
                                           "new_trailer": extra.get("new_pick_trailer") or ""}, create)
            if picked is None:
                raise ValueError(f"Stop {index}: choose the trailer that is picked up at the swap.")
            stop["pick"] = picked
        if kind == "Tractor change":
            # The same trailer (and its load) goes on with another tractor; it can be chosen later.
            truck = item(db, "trucks", extra["truck_id"]) if extra.get("truck_id") else None
            driver_id = extra.get("driver_id") or (truck["driver_id"] if truck else None)
            stop["truck"], stop["driver"] = truck, item(db, "drivers", driver_id) if driver_id else None
        stops.append(stop)
    stops.append({"kind": "Unloading", "location": item(db, "locations", data.get("unloading_id")),
                  "handling_hours": number(data, "unloading_hours", DEFAULT_HANDLING_HOURS, 0, 6),
                  "weight_kg": weight(data, "unloading_weight_kg", "Final unloading"),
                  "leg": manual_leg(data, "distance_km", "driving_hours", "Unloading leg", manual_times)})
    if manual_times:
        stops[-1]["arrive_at"] = optional_arrival(data, "unloading_arrive_at")
    return stops


def optional_arrival(source, key):
    """Manual times: an empty arrival is estimated from the driving time (see estimate_arrivals)."""
    return parse_time(source[key]) if source.get(key) else None


def estimate_arrivals(pickup, stops):
    """Fill the empty arrival times: a rested single driver, the driving time of each leg with breaks and rests,
    and the closing time of loading/unloading places. Times the dispatcher entered are kept."""
    clock = Clock(pickup)
    clock.run(stops[0]["kind"], round(stops[0]["handling_hours"] * 60))
    for index, stop in enumerate(stops[1:], start=2):
        if stop.get("arrive_at"):
            if stop["arrive_at"] >= clock.t:
                clock.advance_idle(stop["arrive_at"])
            else:
                clock = Clock(stop["arrive_at"])
        else:
            if stop["leg_hours"] <= 0:
                raise ValueError(f"Stop {index} ({stop['kind'].lower()}, {stop['location']['city']}): enter the planned "
                                 "arrival, or the driving time from the previous stop, so the arrival can be estimated.")
            clock.run("Driving", round(stop["leg_hours"] * 60))
            stop["arrive_at"], stop["estimated"] = clock.t, True
        if stop["kind"] in WORK_KINDS:
            opens = working_start(clock.t)
            if opens > clock.t:
                clock.advance_idle(opens)
        clock.run(stop["kind"], round(stop["handling_hours"] * 60))
        if stop["kind"] == "Tractor change":
            clock = Clock(clock.t)  # the next tractor has its own driver


def build_plan(db, data, create=False):
    if not data.get("truck_id"):
        raise ValueError("Choose the tractor for this order.")
    truck = item(db, "trucks", data.get("truck_id"))
    if not (data.get("driver_id") or truck["driver_id"]):
        raise ValueError(f"Choose the driver for this order ({truck['plate']} has no default driver).")
    driver = item(db, "drivers", data.get("driver_id") or truck["driver_id"])
    for key, label in (("loading_id", "loading point"), ("unloading_id", "final unloading point")):
        if not data.get(key):
            raise ValueError(f"Choose the {label}.")
    if not truck["active"] or not driver["active"]:
        raise ValueError("The selected truck and driver must be active.")
    pickup = parse_time(data.get("pickup_at"))
    trailer = selected_trailer(db, data, create)
    stops = journey_stops(db, data, create)
    changes = [stop for stop in stops if stop["kind"] == "Tractor change"]
    if changes and order_timing(data) != "manual":
        raise ValueError("An order with a tractor change uses arrival times you enter (Arrival times: I enter them), "
                         "because each tractor has its own driver and driving clock.")
    previous = truck
    for stop in changes:
        if stop["truck"] and stop["truck"]["id"] == previous["id"]:
            raise ValueError(f"Tractor change in {stop['location']['city']}: choose a different tractor.")
        previous = stop["truck"] or {"id": None}
    # Follow the trailer through the journey: a swap drops the current one and picks another.
    current = trailer
    for stop in stops:
        if stop["kind"] == "Trailer swap":
            if current and stop["pick"]["id"] and current["id"] == stop["pick"]["id"]:
                raise ValueError("A trailer swap must pick up a different trailer.")
            stop["drop"], current = current, stop["pick"]
        stop["trailer"] = current
    mode = data.get("route_mode", "manual")
    legs, notices = [], []
    if mode == "here":
        moment = pickup + timedelta(hours=stops[0]["handling_hours"])
        for previous, stop in zip(stops, stops[1:]):
            route_truck = {**truck, "trailer": previous["trailer"]["plate"] if previous["trailer"] else ""}
            leg = routing.truck_route(previous["location"], stop["location"], route_truck, stamp(moment))
            legs.append((leg["distance_km"], leg["driving_hours"]))
            notices += [f'{previous["location"]["city"]} → {stop["location"]["city"]}: {text}' if leg.get("critical") else text
                        for text in leg["notices"]]
            moment += timedelta(hours=leg["driving_hours"] + stop["handling_hours"])
        source = "HERE truck"
    elif mode == "manual":
        legs = [stop["leg"] for stop in stops[1:]]
        source = "Manual"
    else:
        raise ValueError("Unknown route mode.")
    for stop, (km, hours) in zip(stops[1:], legs):
        stop["leg_km"], stop["leg_hours"] = km, hours
    stops[0]["leg_km"] = stops[0]["leg_hours"] = 0
    route = {"distance_km": round(sum(km for km, _ in legs), 1), "driving_hours": round(sum(h for _, h in legs), 4),
             "source": source, "notices": notices,
             "legs": [{"from": a["location"]["city"], "to": b["location"]["city"], "distance_km": b["leg_km"],
                       "driving_hours": b["leg_hours"]} for a, b in zip(stops, stops[1:])]}
    if mode == "here":
        route["profile"] = {key: truck[key] for key in ("height", "width", "length", "weight")}
    route["empty"] = empty_leg(db, truck, pickup, stops[0]["location"], int(data.get("id") or 0))
    if order_timing(data) == "manual":
        estimate_arrivals(pickup, stops)
        plan = manual_plan(pickup, stops)
        route["notices"] += day_notices(stops, plan)
        return truck, driver, trailer, stops, pickup, {}, route, plan
    clock_data = data.get("clock", {})
    if not isinstance(clock_data, dict):
        raise ValueError("Invalid driving clock.")
    plan = route_plan(pickup, [{"kind": s["kind"], "handling_hours": s["handling_hours"], "leg_hours": s["leg_hours"]}
                               for s in stops], clock_data)
    # Do not silently reset a driver's clock between neighbouring jobs.
    previous_orders = db.execute("SELECT * FROM orders WHERE driver_id=? AND status!='Cancelled' AND id!=?", (driver["id"], data.get("id",0))).fetchall()
    preceding = [dict(order) for order in previous_orders if parse_time(order["available_at"]) <= pickup]
    prior = max(preceding, key=lambda order: parse_time(order["available_at"])) if preceding else None
    # A previous order with manual times has no clock to carry forward.
    if prior and uses_rules(prior["plan"]):
        expected = Clock(parse_time(prior["available_at"]), state=json.loads(prior["plan"])["end_state"])
        expected.advance_idle(pickup)
        provided = Clock(pickup, clock_data)
        if provided.day < expected.day or provided.block < expected.block or provided.duty < expected.duty:
            raise ValueError("This driver's earlier order leaves a higher driving/duty clock. Use Carry forward in the driving clock panel, or enter the remaining hours accurately.")
        for key in [week_key(pickup), week_key(pickup-timedelta(days=7))]:
            if provided.weeks.get(key,0) < expected.weeks.get(key,0):
                raise ValueError("Weekly driving hours cannot be lower than this driver's previous planned order. Use Carry forward.")
        if provided.weekly_due > expected.weekly_due + timedelta(minutes=1):
            raise ValueError("Weekly rest is due earlier based on the previous order. Use Carry forward.")
    route["notices"] += day_notices(stops, plan)
    return truck, driver, trailer, stops, pickup, clock_data, route, plan


def day_notices(stops, plan):
    """Plain warnings for the dispatcher: a stop moved to the next day (closed after 18:30 / Sunday), Saturday."""
    notes = []
    for stop, times in zip(stops, plan["stops"]):
        start = parse_time(times["start_at"]).astimezone(ZONE)
        what = f"{stop['kind'].lower()} in {stop['location']['city']}"
        if times.get("next_day"):
            notes.append(f"{what[0].upper() + what[1:]}: arrives {parse_time(times['arrival_at']).astimezone(ZONE):%a %d.%m %H:%M}, "
                         f"after closing (18:30) or on Sunday → planned {start:%a %d.%m} at {start:%H:%M}.")
        if stop["kind"] in WORK_KINDS and start.weekday() == 5:
            notes.append(f"{what[0].upper() + what[1:]} is on Saturday {start:%d.%m}: few places work on Saturday, check with the company.")
    return notes


ROAD_FACTOR, EMPTY_SPEED = 1.25, 65  # road km ≈ air km × 1.25, at ~65 km/h, when HERE is not available


def empty_leg(db, truck, pickup, destination, exclude=0):
    """Km without load: from where the tractor is before this order (end of its previous part, else its reported
    location) to the first stop. HERE truck route when configured, otherwise an estimate from the straight line."""
    best = None
    for order_id, (number, parts) in all_segments(db, exclude).items():
        for index, (truck_id, _, _, end, _, last) in enumerate(parts):
            if truck_id == truck["id"] and end <= pickup + timedelta(minutes=1) and (best is None or end > best[0]):
                # all_segments knows only the tractor-change stops: the last part ends at the order's final stop.
                best = (end, order_id, last if index < len(parts) - 1 else None, number)
    if best:
        row = db.execute("SELECT location_id FROM order_stops WHERE order_id=? AND (seq=? OR ? IS NULL) "
                         "ORDER BY seq DESC LIMIT 1", (best[1], best[2], best[2])).fetchone()
        origin, after = (item(db, "locations", row["location_id"]) if row else None), best[3]
    else:
        origin, after = (item(db, "locations", truck["location_id"]) if truck.get("location_id") else None), ""
    if not origin:
        return None
    result = {"from_id": origin["id"], "from": origin["city"], "from_company": origin["company"], "after": after}
    if origin["id"] == destination["id"]:
        return {**result, "km": 0, "hours": 0, "source": "same place"}
    air = routing.air_distance(origin, destination)
    if air is None:
        return {**result, "km": None, "hours": None, "source": "no coordinates"}
    if routing.configured():
        try:
            leg = routing.truck_route(origin, destination, truck, "any")
            return {**result, "km": leg["distance_km"], "hours": leg["driving_hours"], "source": "HERE truck"}
        except ValueError:
            pass
    km = round(air * ROAD_FACTOR, 1)
    return {**result, "km": km, "hours": round(km / EMPTY_SPEED, 2), "source": "estimate"}


def trailer_spans(db):
    """The trailers each order keeps busy, per order: [(trailer_id, plate, start, end)].
    A trailer dropped at a swap is not kept busy by that order at all (no time limit): a loaded trailer left at the
    base can go out with another tractor whenever the dispatcher plans it. The trailer picked up at the swap is busy
    from the swap until the order ends."""
    swaps = {}
    for row in db.execute("SELECT order_id, seq, planned_at, done_at, drop_trailer_id, drop_trailer, pick_trailer_id, pick_trailer "
                          "FROM order_stops WHERE kind='Trailer swap' ORDER BY order_id, seq"):
        swaps.setdefault(row["order_id"], []).append(row)
    spans = {}
    for order in db.execute("SELECT id, trailer_id, trailer, pickup_at, available_at FROM orders WHERE status!='Cancelled'"):
        current, since, result = (order["trailer_id"], order["trailer"] or ""), parse_time(order["pickup_at"]), []
        # The starting trailer is free of this order once it is dropped at a swap.
        for swap in swaps.get(order["id"], []):
            at = parse_time(swap["done_at"] or swap["planned_at"])
            current, since = (swap["pick_trailer_id"], swap["pick_trailer"] or ""), at
        result.append((*current, since, parse_time(order["available_at"])))
        spans[order["id"]] = [span for span in result if span[0] or span[1]]
    return spans


def tractor_segments(order, stops):
    """Which tractor and driver have the order when: [(truck_id, driver_id, start, end, first_seq, last_seq)].
    A tractor change ends the previous tractor's part when the trailer is left (confirmed, else planned end of the
    stop) and starts the next tractor's part when it takes the trailer. Without a change: one part, the whole order."""
    truck, driver, start, first, parts = order["truck_id"], order["driver_id"], parse_time(order["pickup_at"]), 1, []
    for stop in stops:
        if stop["kind"] == "Tractor change":
            parts.append((truck, driver, start, parse_time(stop["done_at"] or stop["planned_end_at"]), first, stop["seq"]))
            truck, driver = stop.get("truck_id"), stop.get("driver_id")
            start, first = parse_time(stop["done_at"] or stop["planned_at"]), stop["seq"]
    last = max((stop["seq"] for stop in stops), default=1)
    parts.append((truck, driver, start, parse_time(order["available_at"]), first, last))
    return parts


def all_segments(db, exclude=0):
    """Tractor parts of every active order (except `exclude`): {order_id: (number, parts)}."""
    changes = {}
    for row in db.execute("SELECT * FROM order_stops WHERE kind='Tractor change' ORDER BY order_id, seq"):
        changes.setdefault(row["order_id"], []).append({key.lower(): row[key] for key in row.keys()})
    result = {}
    for row in db.execute("SELECT id, number, truck_id, driver_id, pickup_at, available_at FROM orders "
                          "WHERE status!='Cancelled' AND id!=?", (exclude,)):
        result[row["id"]] = (row["number"], tractor_segments(dict(row), changes.get(row["id"], [])))
    return result


def conflicts(db, truck, driver, trailer, start, end, exclude=0, trailer_id=None, trailer_ids=(), segments=None):
    """Orders that already use the tractor, driver or trailer at that time. `segments` are this order's tractor parts
    [(truck_id, driver_id, start, end)] when it has a tractor change; the trailer is checked over the whole order."""
    found = []
    mine = segments or [(truck, driver, start, end)]
    wanted = {value for value in (trailer_id, *trailer_ids) if value}
    plate = (trailer or "").casefold()
    spans = trailer_spans(db)
    for order_id, (number, parts) in all_segments(db, exclude).items():
        same = any(((t and t == other[0]) or (d and d == other[1])) and a < other[3] and b > other[2]
                   for t, d, a, b in mine for other in parts)
        same = same or any((tid in wanted or (plate and text_plate.casefold() == plate)) and start < b and end > a
                           for tid, text_plate, a, b in spans.get(order_id, []))
        if same:
            found.append(number)
    return found


def plan_segments(truck, driver, stops, plan):
    """Tractor parts of an order being saved (stops with their planned times)."""
    planned = [{"kind": stop["kind"], "seq": seq, "planned_at": times["start_at"], "planned_end_at": times["end_at"],
                "done_at": None, "truck_id": (stop.get("truck") or {}).get("id"),
                "driver_id": (stop.get("driver") or {}).get("id")}
               for seq, (stop, times) in enumerate(zip(stops, plan["stops"]), start=1)]
    order = {"truck_id": truck["id"], "driver_id": driver["id"], "pickup_at": plan["stops"][0]["start_at"],
             "available_at": plan["available_at"]}
    return [part[:4] for part in tractor_segments(order, planned)]


def stored_segments(order, stops):
    return [part[:4] for part in tractor_segments(order, stops)]


def save_stops(db, order_id, stops, plan, tokens=None):
    """`tokens`: the next drivers' links by stop number, kept when an order is edited."""
    db.execute("DELETE FROM order_stops WHERE order_id=?", (order_id,))
    for seq, (stop, times) in enumerate(zip(stops, plan["stops"]), start=1):
        drop, pick, material = stop.get("drop"), stop.get("pick"), stop.get("material")
        db.execute("""INSERT INTO order_stops (order_id, seq, kind, location_id, planned_at, planned_end_at,
                handling_hours, leg_km, leg_hours, drop_trailer_id, drop_trailer, pick_trailer_id, pick_trailer,
                material_id, material, weight_kg, truck_id, driver_id, driver_token)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id, seq, stop["kind"], stop["location"]["id"], times["start_at"], times["end_at"],
             stop["handling_hours"], stop["leg_km"], stop["leg_hours"],
             drop["id"] if drop else None, drop["plate"] if drop else "",
             pick["id"] if pick else None, pick["plate"] if pick else "",
             material["id"] if material else None, material_label(material) if material else "",
             stop.get("weight_kg"), (stop.get("truck") or {}).get("id"), (stop.get("driver") or {}).get("id"),
             (tokens or {}).get(seq) if stop["kind"] == "Tractor change" else None))


def create_order(db, data):
    # All reads and checks first, then every write at the end: on Turso the writes go out as one batch.
    begin_write(db)
    truck, driver, trailer_item, stops, pickup, initial, route, plan = build_plan(db, data)
    # Trailer plates typed as text are created with this order; give them ids now.
    new_trailers = {}
    for candidate in [trailer_item, *(stop.get("pick") for stop in stops)]:
        if candidate and candidate["id"] is None:
            candidate["id"] = new_trailers.setdefault(candidate["plate"], reserve_id(db, "trailers"))
    if uses_rules(plan) and not data.get("clock_confirmed"):
        raise ValueError("Confirm the driving clock and repositioning before saving the order.")
    trailer = trailer_item["plate"] if trailer_item else ""
    trailer_id = trailer_item["id"] if trailer_item else None
    picks = [stop["pick"]["id"] for stop in stops if stop.get("pick")]
    collision = conflicts(db, truck["id"], driver["id"], trailer, pickup, parse_time(plan["available_at"]), data.get("id",0),
                          trailer_id=trailer_id, trailer_ids=picks, segments=plan_segments(truck, driver, stops, plan))
    # Manual times (freestyle planning by day): the next load may be planned before the previous one is unloaded;
    # the office sees a warning. Driving-rules orders stay strictly one after the other.
    if collision and uses_rules(plan):
        busy = [row for row in db.execute("SELECT number, pickup_at, available_at FROM orders WHERE status!='Cancelled'")
                if row["number"] in collision]
        detail = "; ".join(f"{row['number']} until {parse_time(row['available_at']).astimezone(ZONE):%d.%m %H:%M}"
                           for row in busy)
        raise ValueError(f"Truck, driver or trailer is already assigned: {detail}. "
                         "Plan the new loading after that time (or change the ETA of that order first).")
    price = order_price(db, data, stops)
    lh, uh = stops[0]["handling_hours"], stops[-1]["handling_hours"]
    loading, unloading = stops[0]["location"], stops[-1]["location"]
    later_driver_orders = db.execute("SELECT * FROM orders WHERE driver_id=? AND status!='Cancelled' AND id!=?", (driver["id"],data.get("id",0))).fetchall()
    if any(parse_time(row["pickup_at"]) >= parse_time(plan["available_at"]) and uses_rules(row["plan"]) for row in later_driver_orders):
        raise ValueError("This driver already has a later order. Add jobs chronologically, or cancel and recreate later orders so their driving clocks stay consistent.")
    values = {"truck_id":truck["id"],"driver_id":driver["id"],"plate":truck["plate"],
              "trailer":trailer,"trailer_id":trailer_id,"driver_name":driver["name"],"carrier":driver["carrier"],
              "operation":truck["operation"],"loading_id":loading["id"],"unloading_id":unloading["id"],
              "pickup_at":stamp(pickup),"arrival_at":plan["arrival_at"],"unloading_at":plan["unloading_at"],
              "available_at":plan["available_at"],"distance_km":route["distance_km"],"driving_hours":route["driving_hours"],
              "loading_hours":lh,"unloading_hours":uh,"route_source":route["source"],
              "route_details":json.dumps(route),"notes":text(data,"notes",False,5000),
              "clock_input":json.dumps(initial),"plan":json.dumps(plan),
              "trailer_pickup":1 if data.get("trailer_pickup") in (True,1,"1","true","on") else 0,**price}
    if data.get("id"):
        existing = item(db,"orders",data["id"])
        if existing["status"] != "Planned":
            raise ValueError("Only planned orders can be edited. Once a stop is confirmed, undo the confirmation first.")
        later = db.execute("SELECT * FROM orders WHERE driver_id=? AND id!=? AND status!='Cancelled'", (existing["driver_id"],existing["id"])).fetchall()
        if any(parse_time(row["pickup_at"]) >= parse_time(existing["available_at"]) and uses_rules(row["plan"]) for row in later):
            raise ValueError("Later orders depend on this driver's clock. Cancel those orders before editing this one.")
        order_id = existing["id"]
    else:
        order_id = reserve_id(db, "orders")
    # The next drivers' links survive an edit.
    tokens = {row["seq"]: row["driver_token"] for row in db.execute(
        "SELECT seq, driver_token FROM order_stops WHERE order_id=? AND driver_token IS NOT NULL", (order_id,))} if data.get("id") else {}
    # Writes only from here on.
    for plate, trailer_new_id in new_trailers.items():
        db.execute("INSERT INTO trailers (id, plate) VALUES (?, ?)", (trailer_new_id, plate))
    if data.get("id"):
        db.execute("UPDATE orders SET " + ",".join(f"{key}=?" for key in values)+" WHERE id=?",(*values.values(),order_id))
    else:
        values = {"id": order_id, "number": f"MT-{pickup.astimezone(ZONE).year}-{order_id:06d}", **values}
        db.execute("INSERT INTO orders ("+",".join(values)+") VALUES ("+",".join("?" for _ in values)+")",tuple(values.values()))
    save_stops(db, order_id, stops, plan, tokens)
    return order_id


def order_stop_rows(db, order_id):
    return [dict(row) for row in db.execute("SELECT * FROM order_stops WHERE order_id=? ORDER BY seq", (order_id,))]


def park_trailer(db, trailer_id, location_id, at):
    if trailer_id:
        db.execute("UPDATE trailers SET parked_location_id=?, parked_at=? WHERE id=?", (location_id, at, trailer_id))


def attach_trailer(db, truck_id, trailer_id, plates):
    """The trailer is now physically on this tractor: not parked, and the tractor's current trailer."""
    if trailer_id:
        plate = plates[trailer_id]
        db.execute("UPDATE trailers SET parked_location_id=NULL, parked_at=NULL WHERE id=?", (trailer_id,))
        # A trailer is on one tractor at a time: another tractor that had it as current trailer loses it.
        db.execute("UPDATE trucks SET trailer_id=NULL, trailer='' WHERE trailer_id=? AND id!=?", (trailer_id, truck_id))
        db.execute("UPDATE trucks SET trailer_id=?, trailer=? WHERE id=?", (trailer_id, plate, truck_id))


def segment_truck(order, stops, seq):
    """The tractor that has the order at stop `seq` (after any tractor change before or at it)."""
    truck = order["truck_id"]
    for stop in stops:
        if stop["seq"] <= seq and stop["kind"] == "Tractor change":
            truck = stop.get("truck_id")
    return truck


def trailer_at(order, stops, seq):
    """The trailer of the order when it reaches stop `seq` (after swaps before it)."""
    trailer = order["trailer_id"]
    for stop in stops:
        if stop["seq"] < seq and stop["kind"] == "Trailer swap":
            trailer = stop["pick_trailer_id"]
    return trailer


def assign_tractor(db, data):
    """Choose (or change) the tractor that takes the trailer at a tractor change, also once the order is on its way."""
    order = item(db, "orders", data.get("order_id"))
    if order["status"] in ("Cancelled", "Completed"):
        raise ValueError(f"This order is {order['status'].lower()}.")
    stops = order_stop_rows(db, order["id"])
    try:
        stop = next(s for s in stops if s["seq"] == int(data.get("seq")) and s["kind"] == "Tractor change")
    except (TypeError, ValueError, StopIteration):
        raise ValueError("Choose a valid tractor change.") from None
    truck = item(db, "trucks", data["truck_id"]) if data.get("truck_id") else None
    driver_id = data.get("driver_id") or (truck["driver_id"] if truck else None)
    driver = item(db, "drivers", driver_id) if driver_id else None
    if truck and truck["id"] == segment_truck(order, stops, stop["seq"] - 1):
        raise ValueError("Choose a different tractor than the one that brings the trailer.")
    if stop["status"] == "Done" and stop.get("truck_id"):
        raise ValueError("The trailer was already taken at this stop. Undo the confirmation to change the tractor.")
    begin_write(db)
    same_driver = stop.get("driver_id") == (driver["id"] if driver else None)
    stop.update(truck_id=truck["id"] if truck else None, driver_id=driver["id"] if driver else None)
    if truck or driver:
        part = [p[:4] for p in tractor_segments(order, stops) if p[4] == stop["seq"]]
        clash = conflicts(db, None, None, "", parse_time(order["pickup_at"]), parse_time(order["available_at"]),
                          order["id"], segments=part)
        if clash:
            raise ValueError(f"{truck['plate'] if truck else driver['name']} is already on {', '.join(clash)} at that time.")
    # Another driver gets a new link; the old one stops working.
    db.execute("UPDATE order_stops SET truck_id=?, driver_id=?, driver_token=? WHERE id=?",
               (stop["truck_id"], stop["driver_id"], stop.get("driver_token") if same_driver else None, stop["id"]))
    if stop["status"] == "Done" and truck:
        # Already left there: the new tractor takes it now.
        plates = {row["id"]: row["plate"] for row in db.execute("SELECT id, plate FROM trailers")}
        attach_trailer(db, truck["id"], trailer_at(order, stops, stop["seq"]), plates)
    place = item(db, "locations", stop["location_id"])["city"]
    return {"message": f"{truck['plate'] if truck else 'No tractor'} takes the trailer in {place}."}


def set_stop_status(db, data):
    """Confirm (or undo) one stop. Stops are confirmed in order; the last unloading completes the order."""
    order = item(db, "orders", data.get("order_id"))
    if order["status"] == "Cancelled":
        raise ValueError("This order is cancelled.")
    stops = order_stop_rows(db, order["id"])
    try:
        seq = int(data.get("seq"))
        stop = next(s for s in stops if s["seq"] == seq)
    except (TypeError, ValueError, StopIteration):
        raise ValueError("Choose a valid stop.") from None
    place = item(db, "locations", stop["location_id"])["city"]
    plates = {row["id"]: row["plate"] for row in db.execute("SELECT id, plate FROM trailers")}
    begin_write(db)
    if data.get("done", True):
        if stop["status"] == "Done":
            raise ValueError("This stop is already confirmed.")
        pending = [s for s in stops if s["seq"] < seq and s["status"] != "Done"]
        if pending:
            first = pending[0]
            raise ValueError(f"Confirm stop {first['seq']} ({first['kind'].lower()}, "
                             f"{item(db, 'locations', first['location_id'])['city']}) first.")
        at = parse_time(data["done_at"]) if data.get("done_at") else datetime.now(UTC).replace(second=0, microsecond=0)
        earlier = [parse_time(s["done_at"]) for s in stops if s["seq"] < seq and s["done_at"]]
        if earlier and at < max(earlier):
            raise ValueError("The confirmation time cannot be before the previous stop.")
        db.execute("UPDATE order_stops SET status='Done', done_at=?, driver_done_at=? WHERE id=?", (stamp(at), stamp(at), stop["id"]))
        stop.update(status="Done", done_at=stamp(at))
        if seq == 1:
            attach_trailer(db, order["truck_id"], order["trailer_id"], plates)
        if stop["kind"] == "Trailer swap":
            park_trailer(db, stop["drop_trailer_id"], stop["location_id"], stamp(at))
            attach_trailer(db, segment_truck(order, stops, seq - 1), stop["pick_trailer_id"], plates)
        if stop["kind"] == "Tractor change":
            trailer_now = trailer_at(order, stops, seq)
            if stop["truck_id"]:
                attach_trailer(db, stop["truck_id"], trailer_now, plates)
            else:
                # Nobody takes it yet: it waits, loaded, where it was left.
                park_trailer(db, trailer_now, stop["location_id"], stamp(at))
                db.execute("UPDATE trucks SET trailer_id=NULL, trailer='' WHERE trailer_id=?", (trailer_now,))
        message = f"{stop['kind']} in {place} confirmed."
    else:
        later = [s for s in stops if s["seq"] > seq and s["status"] == "Done"]
        if stop["status"] != "Done" or later:
            raise ValueError("Only the last confirmed stop can be undone.")
        db.execute("UPDATE order_stops SET status='Planned', done_at=NULL, driver_done_at=NULL WHERE id=?", (stop["id"],))
        undone_at = stop["done_at"]
        stop.update(status="Planned", done_at=None)
        if stop["kind"] == "Trailer swap":
            # Back to the situation before the swap: the picked trailer waits at the swap point.
            park_trailer(db, stop["pick_trailer_id"], stop["location_id"], undone_at)
            here = segment_truck(order, stops, seq - 1)
            if stop["drop_trailer_id"]:
                attach_trailer(db, here, stop["drop_trailer_id"], plates)
            else:
                # The tractor arrived at the swap without a trailer: it no longer has the picked one.
                db.execute("UPDATE trucks SET trailer_id=NULL, trailer='' WHERE id=? AND trailer_id=?",
                           (here, stop["pick_trailer_id"]))
        if stop["kind"] == "Tractor change":
            # Back on the tractor that brought it.
            attach_trailer(db, segment_truck(order, stops, seq - 1), trailer_at(order, stops, seq), plates)
        message = f"{stop['kind']} in {place} set back to planned."
    done = [s for s in stops if s["status"] == "Done"]
    status = "Completed" if len(done) == len(stops) else "In transit" if done else "Planned"
    delivered = stops[-1]["done_at"] if status == "Completed" else None
    # The truck is free from the confirmed delivery, not the planned one (undo restores the plan).
    available = stops[-1]["planned_end_at"]
    if delivered and (not uses_rules(order["plan"]) or parse_time(delivered) < parse_time(available)):
        # A rules-clock order is only moved earlier: later clocks were carried from its planned end.
        available = delivered
    db.execute("UPDATE orders SET status=?, delivered_at=?, available_at=? WHERE id=?",
               (status, delivered, available, order["id"]))
    warning = ""
    if delivered:
        ended = {**order, "available_at": available}
        clash = conflicts(db, order["truck_id"], order["driver_id"], order["trailer"], parse_time(order["pickup_at"]),
                          parse_time(available), order["id"], trailer_id=order["trailer_id"],
                          segments=stored_segments(ended, stops))
        if clash:
            warning = f"The confirmed delivery overlaps with {', '.join(clash)}. Check the next order."
    return {"status": status, "message": message, "warning": warning}


def order_overlaps(db, order_id):
    """Numbers of the other orders that use the same tractor, driver or trailer at the same time as this one."""
    order = item(db, "orders", order_id)
    stops = order_stop_rows(db, order["id"])
    return conflicts(db, order["truck_id"], order["driver_id"], order["trailer"], parse_time(order["pickup_at"]),
                     parse_time(order["available_at"]), order["id"], trailer_id=order["trailer_id"],
                     trailer_ids=[s["pick_trailer_id"] for s in stops if s["pick_trailer_id"]],
                     segments=stored_segments(order, stops))


def move_order(db, data):
    """Drag on Daily planning: the stops not yet confirmed move by `minutes` (earlier or later, other days too).
    A completed trip moves (or stretches) as a whole, its confirmed times with it."""
    order = item(db, "orders", data.get("order_id"))
    try:
        minutes = int(data.get("minutes"))
    except (TypeError, ValueError):
        raise ValueError("Choose how far to move the order.") from None
    if not minutes or abs(minutes) > 14 * 24 * 60:
        raise ValueError("Move the order by up to two weeks.")
    stops = order_stop_rows(db, order["id"])
    # `end`: the card is made longer or shorter (its right edge); otherwise the whole trip moves.
    stretch = data.get("end") in (True, 1, "1", "true")
    if order["status"] == "Completed":
        return retime_completed(db, order, stops, minutes, stretch)
    first = next((s for s in stops if s["status"] != "Done"), None)
    if first is None or order["status"] == "Cancelled":
        raise ValueError(f"{order['number']}: the unloading is already confirmed; it can no longer change."
                         if order["status"] not in ("Cancelled", "Completed") else
                         f"{order['number']} is {order['status'].lower()}; its times can no longer change.")
    if stretch:
        return stretch_order(db, order, stops, minutes)
    when = parse_time(first["planned_at"]) + timedelta(minutes=minutes)
    result = update_stop_eta(db, {"order_id": order["id"], "seq": first["seq"], "planned_at": stamp(when)})
    what = "loading" if first["kind"] == "Loading" and first["seq"] == 1 else first["kind"].lower()
    return {"message": f"{order['number']}: {what} on {when.astimezone(ZONE):%d.%m}.", "warning": result["warning"]}


def retime_completed(db, order, stops, minutes, stretch):
    """A completed trip (green card) moved or made longer/shorter: every time of it (planned and confirmed by the
    driver) moves, or stretches in proportion from its start. The client keeps seeing the real confirmation times."""
    begin_write(db)
    start, end = parse_time(order["pickup_at"]), parse_time(order["available_at"])
    shift = timedelta(minutes=minutes)
    if stretch:
        if (end - start).total_seconds() <= 0 or (end + shift - start).total_seconds() < 15 * 60:
            raise ValueError(f"{order['number']}: a trip lasts at least 15 minutes.")
        factor = (end + shift - start) / (end - start)
        at = lambda value: stamp(start + (parse_time(value) - start) * factor) if value else value
    else:
        at = lambda value: stamp(parse_time(value) + shift) if value else value
    for s in stops:
        s["planned_at"], s["planned_end_at"], s["done_at"] = at(s["planned_at"]), at(s["planned_end_at"]), at(s["done_at"])
        db.execute("UPDATE order_stops SET planned_at=?, planned_end_at=?, done_at=? WHERE id=?",
                   (s["planned_at"], s["planned_end_at"], s["done_at"], s["id"]))
    first, last = stops[0], stops[-1]
    plan = json.loads(order["plan"] or "{}")
    plan.update(arrival_at=last["planned_at"], unloading_at=last["planned_at"], available_at=at(order["available_at"]),
                eta_updated_at=stamp(datetime.now(UTC)))
    plan["stops"] = [{"arrival_at": s["planned_at"], "start_at": s["planned_at"], "end_at": s["planned_end_at"]} for s in stops]
    values = (at(order["pickup_at"]), last["planned_at"], last["planned_at"], at(order["available_at"]),
              at(order["delivered_at"]), json.dumps(plan), order["id"])
    db.execute("UPDATE orders SET pickup_at=?, arrival_at=?, unloading_at=?, available_at=?, delivered_at=?, plan=? WHERE id=?", values)
    moved = {**order, "pickup_at": values[0], "available_at": values[3]}
    clash = conflicts(db, order["truck_id"], order["driver_id"], order["trailer"], parse_time(values[0]), parse_time(values[3]),
                      order["id"], trailer_id=order["trailer_id"], trailer_ids=[s["pick_trailer_id"] for s in stops if s["pick_trailer_id"]],
                      segments=stored_segments(moved, stops))
    day = parse_time(values[4] or last["planned_at"]).astimezone(ZONE)
    what = ("longer" if minutes > 0 else "shorter") if stretch else "moved"
    return {"message": f"{order['number']}: {what}, delivered on {day:%d.%m} (the driver's confirmed times changed too).",
            "warning": ("Now overlaps with " + ", ".join(clash) + ". Check the next order.") if clash else ""}


def stretch_order(db, order, stops, minutes):
    """The card made longer or shorter: the trip ends `minutes` later (or earlier) and every stop not yet confirmed
    (loading, road, unloading) stretches with it, in proportion. Confirmed stops stay as they were."""
    begin_write(db)
    done = [s for s in stops if s["status"] == "Done"]
    anchor = parse_time(done[-1]["done_at"] or done[-1]["planned_end_at"]) if done else parse_time(stops[0]["planned_at"])
    end = parse_time(stops[-1]["planned_end_at"])
    new_end = end + timedelta(minutes=minutes)
    if (end - anchor).total_seconds() <= 0 or (new_end - anchor).total_seconds() < 15 * 60:
        raise ValueError(f"{order['number']}: a trip lasts at least 15 minutes.")
    factor = (new_end - anchor) / (end - anchor)
    at = lambda value: stamp(anchor + (parse_time(value) - anchor) * factor)
    for s in stops:
        if s["status"] != "Done":
            s["planned_at"], s["planned_end_at"] = at(s["planned_at"]), at(s["planned_end_at"])
            s["handling_hours"] = round((parse_time(s["planned_end_at"]) - parse_time(s["planned_at"])).total_seconds() / 3600, 2)
            db.execute("UPDATE order_stops SET planned_at=?, planned_end_at=?, handling_hours=? WHERE id=?",
                       (s["planned_at"], s["planned_end_at"], s["handling_hours"], s["id"]))
    first, last = stops[0], stops[-1]
    plan = json.loads(order["plan"] or "{}")
    plan.update(arrival_at=last["planned_at"], unloading_at=last["planned_at"], available_at=last["planned_end_at"],
                eta_updated_at=stamp(datetime.now(UTC)))
    plan["stops"] = [{"arrival_at": s["planned_at"], "start_at": s["planned_at"], "end_at": s["planned_end_at"]} for s in stops]
    db.execute("UPDATE orders SET pickup_at=?, arrival_at=?, unloading_at=?, available_at=?, loading_hours=?, unloading_hours=?, "
               "plan=? WHERE id=?", (first["planned_at"], last["planned_at"], last["planned_at"], last["planned_end_at"],
                                     first["handling_hours"], last["handling_hours"], json.dumps(plan), order["id"]))
    moved = {**order, "pickup_at": first["planned_at"], "available_at": last["planned_end_at"]}
    clash = conflicts(db, order["truck_id"], order["driver_id"], order["trailer"], parse_time(first["planned_at"]),
                      parse_time(last["planned_end_at"]), order["id"], trailer_id=order["trailer_id"],
                      trailer_ids=[s["pick_trailer_id"] for s in stops if s["pick_trailer_id"]],
                      segments=stored_segments(moved, stops))
    return {"message": f"{order['number']}: {'longer' if minutes > 0 else 'shorter'}, delivery on {parse_time(last['planned_at']).astimezone(ZONE):%d.%m}.",
            "warning": ("Now overlaps with " + ", ".join(clash) + ". Check the next order.") if clash else ""}


def update_stop_eta(db, data):
    """New planned arrival for a stop not yet confirmed (e.g. the driver calls with a new ETA).
    Later unconfirmed stops move by the same amount, so their handling times stay the same."""
    order = item(db, "orders", data.get("order_id"))
    if order["status"] in ("Cancelled", "Completed"):
        raise ValueError(f"This order is {order['status'].lower()}; its times can no longer change.")
    stops = order_stop_rows(db, order["id"])
    try:
        seq = int(data.get("seq"))
        stop = next(s for s in stops if s["seq"] == seq)
    except (TypeError, ValueError, StopIteration):
        raise ValueError("Choose a valid stop.") from None
    if stop["status"] == "Done":
        raise ValueError("This stop is already confirmed; undo the confirmation to change its time.")
    begin_write(db)
    new = parse_time(data.get("planned_at"))
    done_times = [parse_time(s["done_at"]) for s in stops if s["seq"] < seq and s["done_at"]]
    if done_times and new < max(done_times):
        raise ValueError("The new arrival cannot be before the previous confirmed stop.")
    delta = new - parse_time(stop["planned_at"])
    for s in stops:
        if s["seq"] >= seq and s["status"] != "Done":
            s["planned_at"] = stamp(parse_time(s["planned_at"]) + delta)
            s["planned_end_at"] = stamp(parse_time(s["planned_end_at"]) + delta)
            db.execute("UPDATE order_stops SET planned_at=?, planned_end_at=? WHERE id=?",
                       (s["planned_at"], s["planned_end_at"], s["id"]))
    first, last = stops[0], stops[-1]
    plan = json.loads(order["plan"] or "{}")
    plan.update(arrival_at=last["planned_at"], unloading_at=last["planned_at"], available_at=last["planned_end_at"],
                eta_updated_at=stamp(datetime.now(UTC)))
    plan["stops"] = [{"arrival_at": s["planned_at"], "start_at": s["planned_at"], "end_at": s["planned_end_at"]} for s in stops]
    db.execute("UPDATE orders SET pickup_at=?, arrival_at=?, unloading_at=?, available_at=?, plan=? WHERE id=?",
               (first["planned_at"], last["planned_at"], last["planned_at"], last["planned_end_at"], json.dumps(plan), order["id"]))
    place = item(db, "locations", stop["location_id"])["city"]
    message = f"New ETA in {place}: {stop['planned_at'][11:16]}"
    if stop is not last:
        message += f"; delivery now {last['planned_at'][8:10]}.{last['planned_at'][5:7]} {last['planned_at'][11:16]}"
    moved = {**order, "pickup_at": first["planned_at"], "available_at": last["planned_end_at"]}
    clash = conflicts(db, order["truck_id"], order["driver_id"], order["trailer"], parse_time(first["planned_at"]),
                      parse_time(last["planned_end_at"]), order["id"], trailer_id=order["trailer_id"],
                      trailer_ids=[s["pick_trailer_id"] for s in stops if s["pick_trailer_id"]],
                      segments=stored_segments(moved, stops))
    return {"message": message + ".", "warning": ("Now overlaps with " + ", ".join(clash) + ". Check the next order.") if clash else ""}


def clock_fields(clock):
    return {"drive_today":clock.day/60,"drive_since_break":clock.block/60,"duty_today":clock.duty/60,
            "drive_week":clock.weeks.get(week_key(clock.t),0)/60,
            "drive_previous_week":clock.weeks.get(week_key(clock.t-timedelta(days=7)),0)/60,
            "weekly_rest_due_in":max(0,(clock.weekly_due-clock.t).total_seconds()/3600)}


def carry_clock(db,data):
    driver = identity(data.get("driver_id"))
    at = parse_time(data.get("pickup_at"))
    all_orders = rows(db,"orders")
    previous = [o for o in all_orders if o["driver_id"]==driver and o["status"]!="Cancelled" and o["id"]!=data.get("id") and parse_time(o["available_at"])<=at]
    if not previous:
        return {"clock":clock_fields(Clock(at)),"message":"No prior order. Enter actual hours, or confirm a fresh-rest assumption."}
    order = max(previous,key=lambda o:parse_time(o["available_at"]))
    if not uses_rules(order["plan"]):
        return {"clock":clock_fields(Clock(at)),"message":f"{order['number']} uses manual times, so there is no planned clock to carry. Enter the driver's actual hours."}
    clock = Clock(parse_time(order["available_at"]),state=order["plan"]["end_state"])
    clock.advance_idle(at)
    return {"clock":clock_fields(clock),"message":f"Carried from {order['number']}. Unassigned gaps are assumed to be rest/breaks; verify against the tachograph."}
