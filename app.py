"""Daily Traking MalcomTrans — localhost dispatch planner."""
import argparse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import time
from urllib.parse import parse_qsl, urlsplit

from auth import Auth
import base64
import config
import deca
import driver
from planner import Clock, UTC, ZONE, number, parse_time, route_plan, stamp, week_key
import routing
from storage import TursoReplica, backup_to, begin_write, connect, initialize, reserve_id, rows
from upcoming import weekly_availability
import xlsx

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)


def text(data, key, required=True, limit=200):
    value = str(data.get(key, "")).strip()
    if (required and not value) or len(value) > limit:
        raise ValueError(f"{key.replace('_', ' ').title()} is required (maximum {limit} characters).")
    return value


def identity(value):
    try:
        result = int(value)
        if result <= 0:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise ValueError("Choose a valid item from the list.")


def item(db, table, value):
    if table not in {"drivers", "trucks", "trailers", "locations", "orders", "itv_vehicles", "materials"}:
        raise ValueError("Invalid item type.")
    row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (identity(value),)).fetchone()
    if row is None:
        raise ValueError("The selected item no longer exists. Refresh and try again.")
    # Lowercase keys: a database restored from a Turso backup can name the column PLAN.
    return {key.lower(): row[key] for key in row.keys()}


LOCATION_KINDS = ("Client", "Unloading only", "Warehouse", "Parking", "Other")
# Companies that can give the truck its next load (Parking / unloading-only sites cannot).
LOADING_KINDS = ("Client", "Warehouse", "Other")
CONTACT_RESULTS = ("Had a load", "No load now", "No answer", "Call back later")

ARCHIVE_REASONS = ("Sold", "Scrapped / end of life", "Lease or rental ended",
                   "Transferred to another company", "Added by mistake", "Other")


def find_by_plate(db, table, plate):
    row = db.execute(f"SELECT * FROM {table} WHERE plate=? COLLATE NOCASE", (plate,)).fetchone()
    return dict(row) if row else None


def archived_message(record, label):
    reason = f" ({record['archive_reason']})" if record.get("archive_reason") else ""
    return f"{label} {record['plate']} is archived{reason}. Restore it from the archive instead of adding it again."


def selected_trailer(db, data, create=False):
    """Resolve the trailer of a form. A typed new plate is created only when saving (create=True)."""
    new_plate = text(data, "new_trailer", False, 30).upper()
    if "trailer_id" in data and not data["trailer_id"] and new_plate:
        trailer = find_by_plate(db, "trailers", new_plate)
        if trailer is None:
            if not create:
                return {"id": None, "plate": new_plate, "active": 1, "archived_at": None}
            new_id = reserve_id(db, "trailers")
            db.execute("INSERT INTO trailers (id, plate) VALUES (?, ?)", (new_id, new_plate))
            trailer = {"id": new_id, "plate": new_plate, "notes": "", "active": 1, "archived_at": None}
    elif "trailer_id" in data:
        trailer = item(db, "trailers", data["trailer_id"]) if data["trailer_id"] else None
    else:
        # Compatibility with previously-open forms: only known registrations work.
        plate = text(data, "trailer", False, 30).upper()
        row = db.execute("SELECT * FROM trailers WHERE plate=? COLLATE NOCASE", (plate,)).fetchone() if plate else None
        if plate and row is None:
            raise ValueError("Select a registered trailer. Add it under Trailers first.")
        trailer = dict(row) if row else None
    if trailer and trailer.get("archived_at"):
        raise ValueError(archived_message(trailer, "Trailer"))
    if trailer and not trailer["active"]:
        raise ValueError("This trailer is inactive. Choose an active trailer or leave it unassigned.")
    return trailer


def save_master(db, kind, data):
    if kind == "drivers":
        document = text(data, "dni_nie", False, 30).upper()
        if "dni_nie" not in data and data.get("id"):
            document = item(db, "drivers", data["id"])["dni_nie"]
        values = {"name": text(data, "name"), "phone": text(data, "phone", False, 60),
                  "dni_nie": document,
                  "carrier": text(data, "carrier", False) or "MalcomTrans",
                  "active": 1 if data.get("active", True) else 0}
    elif kind == "trailers":
        values = {"plate":text(data,"plate",True,30).upper(),
                  "notes":text(data,"notes",False,2000), "active":1 if data.get("active",True) else 0}
        if "parked_location_id" in data:
            # Manual position: where the trailer is parked now (empty = on a tractor / unknown).
            parked = item(db, "locations", data["parked_location_id"]) if data["parked_location_id"] else None
            values["parked_location_id"] = parked["id"] if parked else None
            values["parked_at"] = (stamp(parse_time(data["parked_at"])) if data.get("parked_at")
                                   else stamp(datetime.now(UTC).replace(second=0, microsecond=0))) if parked else None
    elif kind == "locations":
        values = {key: text(data, key, key != "address") for key in ("company", "city", "postal_code", "country", "address")}
        values.update(lat=None, lon=None, geo_label=text(data, "geo_label", False, 400))
        # Profile fields: an older open form may not send them, so keep what is saved.
        saved = item(db, "locations", data["id"]) if data.get("id") else {}
        for key, limit in (("kind", 40), ("contact_name", 200), ("phone", 60), ("email", 200), ("notes", 2000), ("nif", 30)):
            values[key] = text(data, key, False, limit) if key in data else saved.get(key, "")
        values["nif"] = values["nif"].upper().replace(" ", "")
        values["kind"] = values["kind"] or "Client"
        if values["kind"] not in LOCATION_KINDS:
            raise ValueError("Choose a company type from the list.")
        if values["email"] and ("@" not in values["email"] or " " in values["email"]):
            raise ValueError("Enter a valid email address, or leave it empty.")
        lat, lon = data.get("lat"), data.get("lon")
        if lat not in (None, "") or lon not in (None, ""):
            values["lat"] = number(data, "lat", None, -90, 90)
            values["lon"] = number(data, "lon", None, -180, 180)
    elif kind == "trucks":
        trailer = selected_trailer(db, data, create=True)
        driver = item(db, "drivers", data["driver_id"]) if data.get("driver_id") else None
        location = item(db, "locations", data["location_id"]) if data.get("location_id") else None
        operation = text(data, "operation")
        if operation not in {"International", "National", "Local"}:
            raise ValueError("Choose International, National or Local.")
        values = {"plate": text(data, "plate", True, 30).upper(),
                  "trailer": trailer["plate"] if trailer else "",
                  "trailer_id": trailer["id"] if trailer else None,
                  "driver_id": driver["id"] if driver else None,
                  "location_id": location["id"] if location else None,
                  "available_from": stamp(parse_time(data["available_from"])) if data.get("available_from") else None,
                  "operation": operation, "active": 1 if data.get("active", True) else 0}
        for key, default, low, high in [("height",4,.5,6),("width",2.55,.5,4),("length",16.5,2,30),("weight",40000,1000,100000)]:
            values[key] = number(data, key, default, low, high)
    else:
        raise ValueError("Unknown resource.")
    if kind in ("trucks", "trailers"):
        label = "Tractor" if kind == "trucks" else "Trailer"
        same = find_by_plate(db, kind, values["plate"])
        if same and str(same["id"]) != str(data.get("id") or ""):
            if same.get("archived_at"):
                raise ValueError(archived_message(same, label))
            raise ValueError(f"{label} {same['plate']} already exists.")
    if data.get("id"):
        row = item(db, kind, data["id"])
        if row.get("archived_at"):
            raise ValueError("This item is archived. Restore it before editing.")
        if kind == "locations":
            booked = db.execute("SELECT 1 FROM orders WHERE loading_id=? OR unloading_id=? LIMIT 1", (row["id"],row["id"])).fetchone()
            if booked and any(values[key] != row[key] for key in ("company","city","postal_code","country","address")):
                raise ValueError("This location is used by an order. Add a new location to preserve order history. Coordinates can still be updated.")
        db.execute(f"UPDATE {kind} SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?", (*values.values(), row["id"]))
        if kind == "trailers":
            db.execute("UPDATE trucks SET trailer=? WHERE trailer_id=?", (values["plate"],row["id"]))
        return row["id"]
    cursor = db.execute(f"INSERT INTO {kind} (" + ",".join(values) + ") VALUES (" + ",".join("?" for _ in values) + ")", tuple(values.values()))
    return cursor.lastrowid


def archive_master(db, kind, data):
    """'Delete' a tractor or trailer by archiving it with a reason; records and history are kept."""
    if kind not in {"trucks", "trailers"}:
        raise ValueError("Only tractors and trailers can be deleted.")
    row = item(db, kind, data.get("id"))
    if row.get("archived_at"):
        raise ValueError(f"{row['plate']} is already archived.")
    reason = text(data, "reason", True, 60)
    if reason not in ARCHIVE_REASONS:
        raise ValueError("Choose a reason from the list.")
    note = text(data, "note", reason == "Other", 500)
    if kind == "trucks":
        sql = "SELECT number FROM orders WHERE truck_id=? AND status IN ('Planned','In transit') ORDER BY pickup_at"
    else:
        sql = """SELECT number FROM orders WHERE status IN ('Planned','In transit') AND (trailer_id=?1
                 OR id IN (SELECT order_id FROM order_stops WHERE pick_trailer_id=?1)) ORDER BY pickup_at"""
    planned = [r["number"] for r in db.execute(sql, (row["id"],))]
    if planned:
        raise ValueError(f"{row['plate']} still has planned orders: {', '.join(planned)}. "
                         "Complete, cancel or move them to another vehicle first.")
    db.execute(f"UPDATE {kind} SET active=0, archived_at=?, archive_reason=?, archive_note=? WHERE id=?",
               (stamp(datetime.now(UTC)), reason, note, row["id"]))
    if kind == "trailers":
        # A sold trailer can no longer be the default trailer of a tractor.
        db.execute("UPDATE trucks SET trailer='', trailer_id=NULL WHERE trailer_id=?", (row["id"],))
    return {"archived": row["plate"]}


def restore_master(db, kind, data):
    if kind not in {"trucks", "trailers"}:
        raise ValueError("Only tractors and trailers can be restored.")
    row = item(db, kind, data.get("id"))
    if not row.get("archived_at"):
        raise ValueError(f"{row['plate']} is not archived.")
    db.execute(f"UPDATE {kind} SET active=1, archived_at=NULL, archive_reason='', archive_note='' WHERE id=?", (row["id"],))
    return {"restored": row["plate"]}


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


def material_label(material):
    return f"{material['code']} · {material['name']}" if material["code"] else material["name"]


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


def picked_trailers(db):
    """Trailer ids picked up at swap stops, per order."""
    result = {}
    for row in db.execute("SELECT order_id, pick_trailer_id FROM order_stops WHERE pick_trailer_id IS NOT NULL"):
        result.setdefault(row["order_id"], set()).add(row["pick_trailer_id"])
    return result


def conflicts(db, truck, driver, trailer, start, end, exclude=0, trailer_id=None, trailer_ids=()):
    found = []
    wanted = {value for value in (trailer_id, *trailer_ids) if value}
    picks = picked_trailers(db)
    for row in db.execute("SELECT * FROM orders WHERE status!='Cancelled' AND id!=?", (exclude,)):
        used = ({row["trailer_id"]} | picks.get(row["id"], set())) - {None}
        same = row["truck_id"] == truck or row["driver_id"] == driver or bool(wanted & used) or (trailer and row["trailer"].casefold() == trailer.casefold())
        if same and start < parse_time(row["available_at"]) and end > parse_time(row["pickup_at"]):
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
              "clock_input":json.dumps(initial),"plan":json.dumps(plan)}
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


# ---------------------------------------------------------------- ITV register
ITV_KINDS = ("Tractora", "Remolque")


def itv_value(raw):
    """(expires_on 'YYYY-MM-DD' or None, expiry_text) from a document cell.
    Non-date text such as 'CADUCADO' or '---------' is kept exactly as written."""
    if raw is None:
        return None, ""
    if hasattr(raw, "isoformat") and not isinstance(raw, str):
        return (raw.date() if isinstance(raw, datetime) else raw).isoformat(), ""
    value = str(raw).strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat(), ""
        except ValueError:
            pass
    return None, value


def parse_itv_workbook(data):
    """Read the ITV Excel: one sheet per vehicle type (sheet name with REMOLQUE / TRACTORA),
    a 'Matricula' column and an expiry ('Fecha', 'Caducidad') column."""
    vehicles, seen = [], set()
    for name, sheet in xlsx.read_workbook(data).items():
        upper = name.upper()
        kind = "Remolque" if ("REMOLQ" in upper or "TRAIL" in upper) else "Tractora" if ("TRACT" in upper or "CAMI" in upper) else None
        rows_ = [[("" if v is None else v) for v in row] for row in sheet]
        if not any(any(str(v).strip() for v in row) for row in rows_):
            continue
        if kind is None:
            raise ValueError(f"Sheet '{name}': name it REMOLQUE or TRACTORA so the vehicle type is clear.")
        header = next((i for i, row in enumerate(rows_) if any("MATRIC" in str(v).upper() for v in row)), None)
        if header is None:
            raise ValueError(f"Sheet '{name}': no 'Matricula' column found.")
        titles = [str(v).upper() for v in rows_[header]]
        plate_col = next(i for i, v in enumerate(titles) if "MATRIC" in v)
        date_col = next((i for i, v in enumerate(titles) if i != plate_col and any(k in v for k in ("FECHA", "CADUC", "ITV", "EXPIR"))),
                        plate_col + 1)
        for row in rows_[header + 1:]:
            plate = str(row[plate_col]).strip() if plate_col < len(row) else ""
            if not plate:
                continue
            key = (kind, plate.upper())
            if key in seen:
                raise ValueError(f"Sheet '{name}': {plate} appears twice.")
            seen.add(key)
            expires_on, expiry_text = itv_value(row[date_col] if date_col < len(row) and row[date_col] != "" else None)
            vehicles.append({"kind": kind, "plate": plate, "expires_on": expires_on, "expiry_text": expiry_text})
    if not vehicles:
        raise ValueError("No vehicles found in the Excel file.")
    return vehicles


def import_itv(db, data):
    """Preview (default) or replace the whole ITV register with the Excel document."""
    try:
        content = base64.b64decode(str(data.get("file", "")), validate=True)
    except ValueError:
        raise ValueError("The file could not be read. Choose the Excel file again.") from None
    vehicles = parse_itv_workbook(content)
    current = db.execute("SELECT count(*) AS n FROM itv_vehicles").fetchone()["n"]
    if not data.get("confirm"):
        return {"vehicles": vehicles, "current": current}
    begin_write(db)
    db.execute("DELETE FROM itv_vehicles")
    for v in vehicles:
        db.execute("INSERT INTO itv_vehicles (kind, plate, expires_on, expiry_text) VALUES (?, ?, ?, ?)",
                   (v["kind"], v["plate"], v["expires_on"], v["expiry_text"]))
    return {"message": f"ITV list replaced: {len(vehicles)} vehicles imported."}


def save_itv(db, data):
    kind = text(data, "kind", True, 20)
    if kind not in ITV_KINDS:
        raise ValueError("Choose Tractora or Remolque.")
    plate = text(data, "plate", True, 30).upper()
    expires_on, expiry_text = itv_value(data.get("expires_on") or None)
    if expiry_text:
        raise ValueError("Enter a valid expiry date.")
    if not expires_on:
        expiry_text = text(data, "expiry_text", False, 40)
    same = db.execute("SELECT id FROM itv_vehicles WHERE kind=? AND upper(plate)=?", (kind, plate)).fetchone()
    if same and str(same["id"]) != str(data.get("id") or ""):
        raise ValueError(f"{kind} {plate} is already in the ITV list.")
    values = (kind, plate, expires_on, expiry_text, text(data, "notes", False, 500))
    if data.get("id"):
        row = item(db, "itv_vehicles", data["id"])
        db.execute("UPDATE itv_vehicles SET kind=?, plate=?, expires_on=?, expiry_text=?, notes=? WHERE id=?", (*values, row["id"]))
        return row["id"]
    return db.execute("INSERT INTO itv_vehicles (kind, plate, expires_on, expiry_text, notes) VALUES (?, ?, ?, ?, ?)", values).lastrowid


def delete_itv(db, data):
    row = item(db, "itv_vehicles", data.get("id"))
    db.execute("DELETE FROM itv_vehicles WHERE id=?", (row["id"],))
    return {"message": f"{row['plate']} removed from the ITV list."}


def save_material(db, data):
    """Materials loaded at clients. The code is your own (it may repeat in the source list)."""
    values = {"code": text(data, "code", False, 20), "name": text(data, "name", True, 120),
              "ler_code": text(data, "ler_code", False, 20), "notes": text(data, "notes", False, 1000),
              "active": 1 if data.get("active", True) else 0}
    same = db.execute("SELECT id FROM materials WHERE code=? AND lower(name)=lower(?)", (values["code"], values["name"])).fetchone()
    if same and str(same["id"]) != str(data.get("id") or ""):
        raise ValueError(f"{values['code']} · {values['name']} is already in the list.")
    if data.get("id"):
        material = item(db, "materials", data["id"])
        db.execute("UPDATE materials SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                   (*values.values(), material["id"]))
        # Planned and future stops show the corrected name; confirmed history keeps what was loaded.
        db.execute("UPDATE order_stops SET material=? WHERE material_id=? AND status!='Done'",
                   (material_label(values), material["id"]))
        return material["id"]
    return db.execute("INSERT INTO materials (" + ",".join(values) + ") VALUES (" + ",".join("?" for _ in values) + ")",
                      tuple(values.values())).lastrowid


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
    stops = order_stop_rows(db, order["id"])
    places = {stop["location_id"]: item(db, "locations", stop["location_id"]) for stop in stops}
    return order, stops, places


def delete_material(db, data):
    material = item(db, "materials", data.get("id"))
    used = db.execute("SELECT count(*) FROM order_stops WHERE material_id=?", (material["id"],)).fetchone()[0]
    # Orders keep the material text they were created with.
    db.execute("UPDATE order_stops SET material_id=NULL WHERE material_id=?", (material["id"],))
    db.execute("DELETE FROM materials WHERE id=?", (material["id"],))
    return {"message": f"{material_label(material)} removed" + (f" (kept as text on {used} stops)." if used else ".")}


class Handler(BaseHTTPRequestHandler):
    def send_body(self,status,body,content_type="application/json; charset=utf-8"):
        if not isinstance(body,bytes):
            body = json.dumps(body,ensure_ascii=False).encode() if content_type.startswith("application/json") else body.encode()
        self.send_response(status)
        self.send_header("Content-Type",content_type)
        self.send_header("Content-Length",str(len(body)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def valid_host(self):
        local = {f"127.0.0.1:{self.server.server_port}",f"localhost:{self.server.server_port}"}
        return self.headers.get("Host") in local | getattr(self.server,"public_hosts",set())

    # ---- login (only when APP_PASSWORD is set, i.e. the online version)
    @property
    def auth(self):
        return getattr(self.server,"auth",None) or Auth()

    def client_address_text(self):
        # Behind Render's proxy the visitor's address is the first X-Forwarded-For entry.
        forwarded = self.headers.get("X-Forwarded-For","")
        return forwarded.split(",")[0].strip() or self.client_address[0]

    def https(self):
        return self.headers.get("X-Forwarded-Proto","") == "https"

    def redirect(self,location,cookie=None):
        self.send_response(303)
        self.send_header("Location",location)
        if cookie:
            self.send_header("Set-Cookie",cookie)
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length","0")
        self.end_headers()

    def login_page(self,message="",status=200):
        page = (ROOT/"static"/"login.html").read_text(encoding="utf-8")
        note = f'<div class="notice warning">{message}</div>' if message else ""
        self.send_body(status,page.replace("<!--MESSAGE-->",note),"text/html; charset=utf-8")

    def handle_login(self):
        address = self.client_address_text()
        if self.auth.blocked(address):
            return self.login_page("Too many wrong passwords. Wait 15 minutes and try again.",429)
        length = int(self.headers.get("Content-Length","0") or 0)
        if not 0 < length < 2000:
            return self.login_page("Enter the password.",400)
        password = dict(parse_qsl(self.rfile.read(length).decode("utf-8","replace"))).get("password","")
        if self.auth.check(address,password):
            return self.redirect("/",self.auth.new_cookie(self.https()))
        time.sleep(1)
        return self.login_page("Wrong password.",401)

    # ---- driver page: the secret link is the only key; one order, next stop only
    def driver_get(self,token,message="",error="",lang=None):
        query = dict(parse_qsl(urlsplit(self.path).query))
        lang = driver.lang_of(lang or query.get("l"))
        with connect(self.server.database) as db:
            order, stops, places = driver_order(db, token)
        if order is None:
            return self.send_body(404, driver.missing_page(lang), "text/html; charset=utf-8")
        if query.get("ok") and not message and not error:
            message = driver.TEXT[lang]["saved"]
        confirm = int(query["c"]) if query.get("c", "").isdigit() else None
        self.send_body(200, driver.render(token, order, stops, places, lang, confirm, message, error), "text/html; charset=utf-8")

    def driver_post(self,token):
        length = int(self.headers.get("Content-Length","0") or 0)
        form = dict(parse_qsl(self.rfile.read(length).decode("utf-8","replace"))) if 0 < length < 2000 else {}
        lang = driver.lang_of(form.get("l"))
        with connect(self.server.database) as db:
            order, stops, _ = driver_order(db, token)
            if order is None:
                return self.send_body(404, driver.missing_page(lang), "text/html; charset=utf-8")
            next_stop = next((s for s in stops if s["status"] != "Done"), None)
            if order["status"] == "Cancelled" or next_stop is None or str(next_stop["seq"]) != form.get("seq"):
                # Double tap or an old page: nothing to confirm, just show the current state.
                return self.redirect(f"/s/{token}?l={lang}")
            try:
                set_stop_status(db, {"order_id": order["id"], "seq": next_stop["seq"]})
            except ValueError as error:
                return self.driver_get(token, error=str(error), lang=lang)
        return self.redirect(f"/s/{token}?l={lang}&ok=1")

    def signed_in(self):
        return self.auth.valid(self.headers.get("Cookie"))

    def do_HEAD(self):
        # Uptime monitors (UptimeRobot) check with HEAD: same answer as GET, without the body.
        path = urlsplit(self.path).path
        if path == "/healthz" and self.valid_host():
            return self.send_body(200,{"ok":True})
        return self.send_body(405,{"error":"Use GET"})

    def do_GET(self):
        if not self.valid_host():
            return self.send_body(403,{"error":"Invalid host"})
        path = urlsplit(self.path).path
        if path == "/healthz":
            return self.send_body(200,{"ok":True})
        if path == "/login":
            return self.redirect("/") if self.auth.enabled and self.signed_in() else self.login_page() if self.auth.enabled else self.redirect("/")
        if path == "/logout":
            return self.redirect("/login",self.auth.clear_cookie())
        if path.startswith("/s/"):
            return self.driver_get(path[3:])
        if path not in ("/style.css","/driver.css") and not self.signed_in():
            return self.send_body(401,{"error":"Please sign in again."}) if path.startswith("/api/") else self.redirect("/login")
        if path == "/api/state":
            with connect(self.server.database) as db:
                state = {key:rows(db,key) for key in ("drivers","trucks","trailers","locations","orders")}
                state["itv"] = rows(db,"itv_vehicles")
                state["materials"] = rows(db,"materials")
                state["settings"] = deca.settings(db)
            state.update(token=TOKEN,auth=self.auth.enabled,here_configured=routing.configured(),now=stamp(datetime.now(UTC)),timezone="Europe/Madrid",
                         storage="turso" if isinstance(self.server.database,TursoReplica) else "sqlite")
            return self.send_body(200,state)
        if path == "/api/deca.pdf":
            query = dict(parse_qsl(urlsplit(self.path).query))
            try:
                with connect(self.server.database) as db:
                    filename, body = deca.pdf(db, query.get("order"))
            except ValueError as error:
                return self.send_body(400, {"error": str(error)})
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(body)
        if path == "/api/backup":
            # SQLite online backup produces a transactionally consistent snapshot.
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)/"backup.sqlite3"
                backup_to(self.server.database, target)
                body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type","application/vnd.sqlite3")
            self.send_header("Content-Disposition",'attachment; filename="MalcomTrans-backup.sqlite3"')
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store")
            self.end_headers()
            return self.wfile.write(body)
        files = {"/":("index.html","text/html; charset=utf-8"),"/app.js":("app.js","text/javascript; charset=utf-8"),"/analytics.js":("analytics.js","text/javascript; charset=utf-8"),"/search-select.js":("search-select.js","text/javascript; charset=utf-8"),"/style.css":("style.css","text/css; charset=utf-8"),"/driver.css":("driver.css","text/css; charset=utf-8")}
        if path not in files:
            return self.send_body(404,{"error":"Page not found"})
        filename,mime = files[path]
        self.send_body(200,(ROOT/"static"/filename).read_bytes(),mime)

    def do_POST(self):
        if self.valid_host() and urlsplit(self.path).path == "/login":
            return self.handle_login()
        if self.valid_host() and urlsplit(self.path).path.startswith("/s/"):
            return self.driver_post(urlsplit(self.path).path[3:])
        if self.valid_host() and not self.signed_in():
            return self.send_body(401,{"error":"Please sign in again."})
        if not self.valid_host() or not secrets.compare_digest(self.headers.get("X-CSRF-Token",""),TOKEN):
            return self.send_body(403,{"error":"Please reload the app and try again."})
        try:
            length = int(self.headers.get("Content-Length","0"))
            limit = 3_000_000 if urlsplit(self.path).path == "/api/itv-import" else 100000
            if not 0<length<limit:
                raise ValueError("Invalid request size.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data,dict):
                raise ValueError("Expected a form object.")
            path = urlsplit(self.path).path
            with connect(self.server.database) as db:
                if path in {"/api/drivers","/api/trucks","/api/trailers","/api/locations"}:
                    result = {"id":save_master(db,path.rsplit("/",1)[1],data)}
                elif path == "/api/archive":
                    result = archive_master(db,data.get("kind"),data)
                elif path == "/api/restore":
                    result = restore_master(db,data.get("kind"),data)
                elif path == "/api/preview":
                    *_,route,plan = build_plan(db,data)
                    result = {"route":route,"plan":plan}
                elif path == "/api/orders":
                    result = {"id":create_order(db,data)}
                elif path == "/api/order-status":
                    order = item(db,"orders",data.get("id"))
                    status = data.get("status")
                    if status == "Completed":
                        raise ValueError("Confirm the stops instead: confirming the last unloading completes the order.")
                    if status != "Cancelled":
                        raise ValueError("Invalid status.")
                    if order["status"] == "Completed":
                        raise ValueError("Completed orders cannot be cancelled.")
                    if order["status"] == "Cancelled" and status!="Cancelled":
                        raise ValueError("Cancelled orders cannot be reopened. Create a new order.")
                    if status == "Cancelled":
                        later = [o for o in rows(db,"orders") if o["status"]!="Cancelled" and o["id"]!=order["id"] and o["driver_id"]==order["driver_id"] and parse_time(o["pickup_at"])>=parse_time(order["available_at"]) and uses_rules(o["plan"])]
                        if later:
                            raise ValueError("Cancel this driver's later orders first; their clocks depend on this order.")
                    db.execute("UPDATE orders SET status=? WHERE id=?",(status,order["id"]))
                    result = {"ok":True}
                elif path == "/api/stop-status":
                    result = set_stop_status(db,data)
                elif path == "/api/stop-eta":
                    result = update_stop_eta(db,data)
                elif path == "/api/geocode":
                    result = {"matches":routing.geocode(text(data,"query",True,600))}
                elif path == "/api/upcoming":
                    result = weekly_availability(db,data)
                elif path == "/api/itv":
                    result = {"id":save_itv(db,data)}
                elif path == "/api/itv-delete":
                    result = delete_itv(db,data)
                elif path == "/api/settings":
                    result = deca.save_settings(db,data)
                elif path == "/api/deca":
                    result = deca.prepare(db,data)
                elif path == "/api/driver-link":
                    result = driver_link(db,data)
                elif path == "/api/materials":
                    result = {"id":save_material(db,data)}
                elif path == "/api/material-delete":
                    result = delete_material(db,data)
                elif path == "/api/itv-import":
                    result = import_itv(db,data)
                elif path == "/api/nearby":
                    result = nearby_companies(db,data)
                elif path == "/api/location-contact":
                    result = log_contact(db,data)
                elif path == "/api/clock":
                    result = carry_clock(db,data)
                else:
                    return self.send_body(404,{"error":"Unknown action"})
            self.send_body(200,result)
        except (ValueError,KeyError,TypeError) as exc:
            self.send_body(400,{"error":str(exc)})
        except sqlite3.IntegrityError:
            self.send_body(400,{"error":"This registration already exists, or a linked item is invalid."})
        except Exception:
            import traceback
            traceback.print_exc()
            self.send_body(500,{"error":"The request could not be saved. Check the server terminal."})


class Server(ThreadingHTTPServer):
    """On Windows, SO_REUSEADDR lets a second copy share the port silently; refuse instead."""
    if os.name == "nt":
        allow_reuse_address = False

        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port",type=int,default=5000)
    parser.add_argument("--database",type=Path,default=None,
                        help="Local SQLite file. Ignored when Turso is configured in .env.")
    args = parser.parse_args()
    config.load_env()
    turso = config.turso_settings()
    if turso and args.database is None:
        database = TursoReplica(ROOT/"data"/"turso-replica.db", *turso)
    else:
        database = args.database or ROOT/"data"/"malcomtrans.sqlite3"
    initialize(database)
    # Online (Render sets PORT): listen on all interfaces, only with a dispatcher password.
    online = bool(os.environ.get("PORT"))
    password = os.environ.get("APP_PASSWORD","").strip() or None
    if online and (not password or len(password) < 10):
        print("APP_PASSWORD (at least 10 characters) is required when the app runs online.",flush=True)
        sys.exit(1)
    port = int(os.environ.get("PORT") or args.port)
    try:
        server = Server(("0.0.0.0" if online else "127.0.0.1",port),Handler)
    except OSError:
        print(f"Port {args.port} is already in use: MalcomTrans (or another program) is already running.",flush=True)
        print(f"Open http://127.0.0.1:{args.port} or close the other MalcomTrans window first.",flush=True)
        sys.exit(1)
    with server:
        server.database = database
        server.auth = Auth(password, os.environ.get("SESSION_SECRET","").strip() or None)
        server.public_hosts = {host.strip() for host in (os.environ.get("PUBLIC_HOST",""), os.environ.get("RENDER_EXTERNAL_HOSTNAME","")) if host.strip()}
        # Online: remember the public address, so driver links work from any copy of the app.
        public = os.environ.get("PUBLIC_HOST","").strip() or os.environ.get("RENDER_EXTERNAL_HOSTNAME","").strip()
        if online and public:
            with connect(database) as db:
                if deca.settings(db)["public_url"] != f"https://{public}":
                    deca.save_settings(db, {"public_url": f"https://{public}"})
        print(f"Daily Traking MalcomTrans: http://127.0.0.1:{server.server_port}",flush=True)
        print(f"Database: {database}",flush=True)
        print("Dispatch clock: Europe/Madrid. Ctrl+C to stop.",flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
