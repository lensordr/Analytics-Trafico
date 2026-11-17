"""Loading orders: journey stops, timing, conflicts, stop confirmations and ETAs."""

import json
from datetime import datetime, timedelta

import routing
from common import identity, item, text
from masters import selected_trailer
from materials import material_label
from tariffs import order_price
from planner import Clock, UTC, ZONE, number, parse_time, route_plan, stamp, week_key
from storage import begin_write, reserve_id, rows

STOP_KINDS = ("Loading", "Unloading", "Trailer swap")
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


def arrival_input(source, key, label):
    if not source.get(key):
        raise ValueError(f"{label}: enter the planned arrival time.")
    return parse_time(source[key])


def manual_plan(pickup, stops):
    """Plan from arrival times entered by the dispatcher; no rests are added or checked."""
    times, timeline, previous_end = [], [], None
    for index, stop in enumerate(stops):
        arrival = pickup if index == 0 else stop["arrive_at"]
        if previous_end and arrival < previous_end:
            raise ValueError(f"Stop {index + 1} ({stop['kind'].lower()}, {stop['location']['city']}): the arrival "
                             f"cannot be before the previous stop finishes ({stamp(previous_end)[11:16]} on {stamp(previous_end)[:10]}).")
        if previous_end and arrival > previous_end:
            timeline.append({"kind": "On the road (time from driver)", "start": stamp(previous_end), "end": stamp(arrival),
                             "minutes": round((arrival - previous_end).total_seconds() / 60)})
        end = arrival + timedelta(hours=stop["handling_hours"])
        if end > arrival:
            timeline.append({"kind": stop["kind"], "start": stamp(arrival), "end": stamp(end),
                             "minutes": round(stop["handling_hours"] * 60)})
        times.append({"arrival_at": stamp(arrival), "start_at": stamp(arrival), "end_at": stamp(end)})
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
                "handling_hours": number(extra, "handling_hours", 0.5 if kind == "Trailer swap" else DEFAULT_HANDLING_HOURS, 0, 6),
                "leg": manual_leg(extra, "leg_km", "leg_hours", f"Stop {index}", manual_times)}
        if manual_times:
            stop["arrive_at"] = arrival_input(extra, "arrive_at", f"Stop {index}")
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
        stops.append(stop)
    stops.append({"kind": "Unloading", "location": item(db, "locations", data.get("unloading_id")),
                  "handling_hours": number(data, "unloading_hours", DEFAULT_HANDLING_HOURS, 0, 6),
                  "weight_kg": weight(data, "unloading_weight_kg", "Final unloading"),
                  "leg": manual_leg(data, "distance_km", "driving_hours", "Unloading leg", manual_times)})
    if manual_times:
        stops[-1]["arrive_at"] = arrival_input(data, "unloading_arrive_at", "Final unloading")
    return stops


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
            notices += leg["notices"]
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
    if order_timing(data) == "manual":
        return truck, driver, trailer, stops, pickup, {}, route, manual_plan(pickup, stops)
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
    return truck, driver, trailer, stops, pickup, clock_data, route, plan


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


def conflicts(db, truck, driver, trailer, start, end, exclude=0, trailer_id=None, trailer_ids=()):
    found = []
    wanted = {value for value in (trailer_id, *trailer_ids) if value}
    plate = (trailer or "").casefold()
    spans = trailer_spans(db)
    for row in db.execute("SELECT * FROM orders WHERE status!='Cancelled' AND id!=?", (exclude,)):
        overlaps = lambda a, b: start < b and end > a
        same = (row["truck_id"] == truck or row["driver_id"] == driver) and overlaps(parse_time(row["pickup_at"]), parse_time(row["available_at"]))
        same = same or any((tid in wanted or (plate and text_plate.casefold() == plate)) and overlaps(a, b)
                           for tid, text_plate, a, b in spans.get(row["id"], []))
        if same:
            found.append(row["number"])
    return found


def save_stops(db, order_id, stops, plan):
    db.execute("DELETE FROM order_stops WHERE order_id=?", (order_id,))
    for seq, (stop, times) in enumerate(zip(stops, plan["stops"]), start=1):
        drop, pick, material = stop.get("drop"), stop.get("pick"), stop.get("material")
        db.execute("""INSERT INTO order_stops (order_id, seq, kind, location_id, planned_at, planned_end_at,
                handling_hours, leg_km, leg_hours, drop_trailer_id, drop_trailer, pick_trailer_id, pick_trailer,
                material_id, material, weight_kg)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id, seq, stop["kind"], stop["location"]["id"], times["start_at"], times["end_at"],
             stop["handling_hours"], stop["leg_km"], stop["leg_hours"],
             drop["id"] if drop else None, drop["plate"] if drop else "",
             pick["id"] if pick else None, pick["plate"] if pick else "",
             material["id"] if material else None, material_label(material) if material else "",
             stop.get("weight_kg")))


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
                          trailer_id=trailer_id, trailer_ids=picks)
    if collision:
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
    # Writes only from here on.
    for plate, trailer_new_id in new_trailers.items():
        db.execute("INSERT INTO trailers (id, plate) VALUES (?, ?)", (trailer_new_id, plate))
    if data.get("id"):
        db.execute("UPDATE orders SET " + ",".join(f"{key}=?" for key in values)+" WHERE id=?",(*values.values(),order_id))
    else:
        values = {"id": order_id, "number": f"MT-{pickup.astimezone(ZONE).year}-{order_id:06d}", **values}
        db.execute("INSERT INTO orders ("+",".join(values)+") VALUES ("+",".join("?" for _ in values)+")",tuple(values.values()))
    save_stops(db, order_id, stops, plan)
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
        db.execute("UPDATE order_stops SET status='Done', done_at=? WHERE id=?", (stamp(at), stop["id"]))
        stop.update(status="Done", done_at=stamp(at))
        if seq == 1:
            attach_trailer(db, order["truck_id"], order["trailer_id"], plates)
        if stop["kind"] == "Trailer swap":
            park_trailer(db, stop["drop_trailer_id"], stop["location_id"], stamp(at))
            attach_trailer(db, order["truck_id"], stop["pick_trailer_id"], plates)
        message = f"{stop['kind']} in {place} confirmed."
    else:
        later = [s for s in stops if s["seq"] > seq and s["status"] == "Done"]
        if stop["status"] != "Done" or later:
            raise ValueError("Only the last confirmed stop can be undone.")
        db.execute("UPDATE order_stops SET status='Planned', done_at=NULL WHERE id=?", (stop["id"],))
        undone_at = stop["done_at"]
        stop.update(status="Planned", done_at=None)
        if stop["kind"] == "Trailer swap":
            # Back to the situation before the swap: the picked trailer waits at the swap point.
            park_trailer(db, stop["pick_trailer_id"], stop["location_id"], undone_at)
            if stop["drop_trailer_id"]:
                attach_trailer(db, order["truck_id"], stop["drop_trailer_id"], plates)
            else:
                # The tractor arrived at the swap without a trailer: it no longer has the picked one.
                db.execute("UPDATE trucks SET trailer_id=NULL, trailer='' WHERE id=? AND trailer_id=?",
                           (order["truck_id"], stop["pick_trailer_id"]))
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
        clash = conflicts(db, order["truck_id"], order["driver_id"], order["trailer"], parse_time(order["pickup_at"]),
                          parse_time(available), order["id"], trailer_id=order["trailer_id"])
        if clash:
            warning = f"The confirmed delivery overlaps with {', '.join(clash)}. Check the next order."
    return {"status": status, "message": message, "warning": warning}


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
    clash = conflicts(db, order["truck_id"], order["driver_id"], order["trailer"], parse_time(first["planned_at"]),
                      parse_time(last["planned_end_at"]), order["id"], trailer_id=order["trailer_id"],
                      trailer_ids=[s["pick_trailer_id"] for s in stops if s["pick_trailer_id"]])
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
