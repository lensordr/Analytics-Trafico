"""Daily Traking MalcomTrans — localhost dispatch planner."""
import argparse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import sys
from urllib.parse import urlsplit

import config
from planner import Clock, UTC, ZONE, number, parse_time, stamp, trip_plan, week_key
import routing
from storage import TursoReplica, backup_to, connect, initialize, rows
from upcoming import weekly_availability

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
    if table not in {"drivers", "trucks", "trailers", "locations", "orders"}:
        raise ValueError("Invalid item type.")
    row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (identity(value),)).fetchone()
    if row is None:
        raise ValueError("The selected item no longer exists. Refresh and try again.")
    return dict(row)


def selected_trailer(db, data):
    if "trailer_id" in data:
        trailer = item(db, "trailers", data["trailer_id"]) if data["trailer_id"] else None
    else:
        # Compatibility with previously-open forms: only known registrations work.
        plate = text(data, "trailer", False, 30).upper()
        row = db.execute("SELECT * FROM trailers WHERE plate=? COLLATE NOCASE", (plate,)).fetchone() if plate else None
        if plate and row is None:
            raise ValueError("Select a registered trailer. Add it under Trailers first.")
        trailer = dict(row) if row else None
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
    elif kind == "locations":
        values = {key: text(data, key, key != "address") for key in ("company", "city", "postal_code", "country", "address")}
        values.update(lat=None, lon=None, geo_label=text(data, "geo_label", False, 400))
        lat, lon = data.get("lat"), data.get("lon")
        if lat not in (None, "") or lon not in (None, ""):
            values["lat"] = number(data, "lat", None, -90, 90)
            values["lon"] = number(data, "lon", None, -180, 180)
    elif kind == "trucks":
        trailer = selected_trailer(db, data)
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
    if data.get("id"):
        row = item(db, kind, data["id"])
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


def build_plan(db, data):
    truck = item(db, "trucks", data.get("truck_id"))
    driver = item(db, "drivers", data.get("driver_id") or truck["driver_id"])
    if not truck["active"] or not driver["active"]:
        raise ValueError("The selected truck and driver must be active.")
    loading = item(db, "locations", data.get("loading_id"))
    unloading = item(db, "locations", data.get("unloading_id"))
    pickup = parse_time(data.get("pickup_at"))
    load_hours = number(data, "loading_hours", 1, 0, 6)
    unload_hours = number(data, "unloading_hours", 1, 0, 6)
    trailer = selected_trailer(db, data)
    mode = data.get("route_mode", "manual")
    if mode == "here":
        route_truck = {**truck, "trailer": trailer["plate"] if trailer else ""}
        route = routing.truck_route(loading, unloading, route_truck, stamp(pickup+timedelta(hours=load_hours)))
    elif mode == "manual":
        km = number(data, "distance_km", 0, 0, 15000)
        hours = number(data, "driving_hours", 0, 0, 240)
        if km > 0 and hours <= 0:
            raise ValueError("Enter driving hours as well as kilometres. Use driving time, without rest breaks.")
        route = {"distance_km": km, "driving_hours": hours, "source": "Manual", "notices": []}
    else:
        raise ValueError("Unknown route mode.")
    clock_data = data.get("clock", {})
    if not isinstance(clock_data, dict):
        raise ValueError("Invalid driving clock.")
    plan = trip_plan(pickup, route["driving_hours"], load_hours, unload_hours, clock_data)
    # Do not silently reset a driver's clock between neighbouring jobs.
    previous_orders = db.execute("SELECT * FROM orders WHERE driver_id=? AND status!='Cancelled' AND id!=?", (driver["id"], data.get("id",0))).fetchall()
    preceding = [dict(order) for order in previous_orders if parse_time(order["available_at"]) <= pickup]
    if preceding:
        prior = max(preceding, key=lambda order: parse_time(order["available_at"]))
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
    return truck, driver, loading, unloading, pickup, load_hours, unload_hours, clock_data, route, plan


def conflicts(db, truck, driver, trailer, start, end, exclude=0, trailer_id=None):
    found = []
    for row in db.execute("SELECT * FROM orders WHERE status!='Cancelled' AND id!=?", (exclude,)):
        same = row["truck_id"] == truck or row["driver_id"] == driver or (trailer_id and row["trailer_id"] == trailer_id) or (trailer and row["trailer"].casefold() == trailer.casefold())
        if same and start < parse_time(row["available_at"]) and end > parse_time(row["pickup_at"]):
            found.append(row["number"])
    return found


def create_order(db, data):
    db.execute("BEGIN IMMEDIATE")
    truck, driver, loading, unloading, pickup, lh, uh, initial, route, plan = build_plan(db, data)
    if not data.get("clock_confirmed"):
        raise ValueError("Confirm the driving clock and repositioning before saving the order.")
    trailer_item = selected_trailer(db, data)
    trailer = trailer_item["plate"] if trailer_item else ""
    trailer_id = trailer_item["id"] if trailer_item else None
    collision = conflicts(db, truck["id"], driver["id"], trailer, pickup, parse_time(plan["available_at"]), data.get("id",0), trailer_id=trailer_id)
    if collision:
        raise ValueError("Truck, driver or trailer is already assigned: " + ", ".join(collision))
    later_driver_orders = db.execute("SELECT * FROM orders WHERE driver_id=? AND status!='Cancelled' AND id!=?", (driver["id"],data.get("id",0))).fetchall()
    if any(parse_time(row["pickup_at"]) >= parse_time(plan["available_at"]) for row in later_driver_orders):
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
            raise ValueError("Only planned orders can be edited.")
        later = db.execute("SELECT * FROM orders WHERE driver_id=? AND id!=? AND status!='Cancelled'", (existing["driver_id"],existing["id"])).fetchall()
        if any(parse_time(row["pickup_at"]) >= parse_time(existing["available_at"]) for row in later):
            raise ValueError("Later orders depend on this driver's clock. Cancel those orders before editing this one.")
        db.execute("UPDATE orders SET " + ",".join(f"{key}=?" for key in values)+" WHERE id=?",(*values.values(),existing["id"]))
        return existing["id"]
    cursor = db.execute("INSERT INTO orders ("+",".join(values)+") VALUES ("+",".join("?" for _ in values)+")",tuple(values.values()))
    order_id = cursor.lastrowid
    number_value = f"MT-{pickup.astimezone(ZONE).year}-{order_id:06d}"
    db.execute("UPDATE orders SET number=? WHERE id=?",(number_value,order_id))
    return order_id


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
    clock = Clock(parse_time(order["available_at"]),state=order["plan"]["end_state"])
    clock.advance_idle(at)
    return {"clock":clock_fields(clock),"message":f"Carried from {order['number']}. Unassigned gaps are assumed to be rest/breaks; verify against the tachograph."}


def recommend(db,data):
    destination = item(db,"locations",data.get("location_id"))
    pickup = parse_time(data.get("pickup_at"))
    now = datetime.now(UTC).replace(second=0,microsecond=0)
    if pickup < now:
        raise ValueError("Choose a future loading time for recommendations.")
    all_orders = [o for o in rows(db,"orders") if o["status"]!="Cancelled"]
    locations = {o["id"]:o for o in rows(db,"locations")}
    drivers = {o["id"]:o for o in rows(db,"drivers")}
    results, excluded = [], []
    for truck in rows(db,"trucks"):
        if not truck["active"] or not truck["driver_id"] or not drivers[truck["driver_id"]]["active"]:
            continue
        related = [o for o in all_orders if o["truck_id"]==truck["id"] or o["driver_id"]==truck["driver_id"] or (truck["trailer_id"] and o["trailer_id"]==truck["trailer_id"]) or (truck["trailer"] and o["trailer"].casefold()==truck["trailer"].casefold())]
        if any(parse_time(o["pickup_at"])<=pickup<parse_time(o["available_at"]) for o in related):
            excluded.append({"plate":truck["plate"],"reason":"Already assigned at the new loading time."})
            continue
        prior = [o for o in all_orders if o["truck_id"]==truck["id"] and parse_time(o["available_at"])<=pickup]
        previous = max(prior,key=lambda o:parse_time(o["available_at"])) if prior else None
        origin = locations.get(previous["unloading_id"] if previous else truck["location_id"])
        ready = parse_time(previous["available_at"]) if previous else (parse_time(truck["available_from"]) if truck["available_from"] else None)
        if origin is None or ready is None or ready>pickup:
            excluded.append({"plate":truck["plate"],"reason":"Add a known location and availability time, or a preceding order."})
            continue
        departure = max(ready,now)
        air = routing.air_distance(origin,destination)
        same_area = all(origin[k].casefold()==destination[k].casefold() for k in ("country","city","postal_code"))
        result = {"truck_id":truck["id"],"plate":truck["plate"],"driver_id":truck["driver_id"],
                  "driver_name":drivers[truck["driver_id"]]["name"],"carrier":drivers[truck["driver_id"]]["carrier"],
                  "trailer":truck["trailer"],"operation":truck["operation"],"origin":origin,
                  "available_at":stamp(ready),"origin_source":previous["number"] if previous else "Fleet location",
                  "air_km":air,"same_area":same_area,"road_km":None,"arrival_at":None,
                  "feasible":None,"clock":None,"warning":"Geographic shortlist only. Road access, repositioning time and driving clock are not verified."}
        if data.get("use_here"):
            try:
                route = routing.truck_route(origin,destination,truck,stamp(departure))
                # A different driver may have driven the truck's last trip; use the driver's own history.
                driver_prior = [o for o in all_orders if o["driver_id"]==truck["driver_id"] and parse_time(o["available_at"])<=departure]
                last_driver_order = max(driver_prior,key=lambda o:parse_time(o["available_at"])) if driver_prior else None
                if last_driver_order:
                    clock = Clock(parse_time(last_driver_order["available_at"]),state=last_driver_order["plan"]["end_state"])
                    clock.advance_idle(departure)
                else:
                    clock = Clock(departure)
                clock.run("Driving",round(route["driving_hours"]*60))
                arrival = clock.t
                collision = conflicts(db,truck["id"],truck["driver_id"],truck["trailer"],departure,arrival,trailer_id=truck["trailer_id"])
                feasible = arrival<=pickup and not collision
                result.update(road_km=route["distance_km"],arrival_at=stamp(arrival),feasible=feasible)
                if feasible:
                    clock.advance_idle(pickup)
                    result["clock"] = clock_fields(clock)
                result["warning"] = "Planning estimate; verify actual clocks and truck position. Repositioning is not a saved order."
                if not last_driver_order:
                    result["warning"] += " Fresh-rest assumption: no previous driver order."
                if collision:
                    result["warning"] += " Repositioning conflicts with " + ", ".join(collision)
            except ValueError as exc:
                result["warning"] = str(exc)
        if air is None and not same_area and result["road_km"] is None:
            excluded.append({"plate":truck["plate"],"reason":"Coordinates missing; distance cannot be ranked."})
            continue
        results.append(result)
    results.sort(key=lambda r:(r["feasible"] is False, r["road_km"] if r["road_km"] is not None else (r["air_km"] if r["air_km"] is not None else -1)))
    return {"candidates":results,"excluded":excluded,"location":destination}


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
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}",f"localhost:{self.server.server_port}"}

    def do_GET(self):
        if not self.valid_host():
            return self.send_body(403,{"error":"Invalid host"})
        path = urlsplit(self.path).path
        if path == "/api/state":
            with connect(self.server.database) as db:
                state = {key:rows(db,key) for key in ("drivers","trucks","trailers","locations","orders")}
            state.update(token=TOKEN,here_configured=routing.configured(),now=stamp(datetime.now(UTC)),timezone="Europe/Madrid")
            return self.send_body(200,state)
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
        files = {"/":("index.html","text/html; charset=utf-8"),"/app.js":("app.js","text/javascript; charset=utf-8"),"/analytics.js":("analytics.js","text/javascript; charset=utf-8"),"/search-select.js":("search-select.js","text/javascript; charset=utf-8"),"/style.css":("style.css","text/css; charset=utf-8")}
        if path not in files:
            return self.send_body(404,{"error":"Page not found"})
        filename,mime = files[path]
        self.send_body(200,(ROOT/"static"/filename).read_bytes(),mime)

    def do_POST(self):
        if not self.valid_host() or not secrets.compare_digest(self.headers.get("X-CSRF-Token",""),TOKEN):
            return self.send_body(403,{"error":"Please reload the app and try again."})
        try:
            length = int(self.headers.get("Content-Length","0"))
            if not 0<length<100000:
                raise ValueError("Invalid request size.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data,dict):
                raise ValueError("Expected a form object.")
            path = urlsplit(self.path).path
            with connect(self.server.database) as db:
                if path in {"/api/drivers","/api/trucks","/api/trailers","/api/locations"}:
                    result = {"id":save_master(db,path.rsplit("/",1)[1],data)}
                elif path == "/api/preview":
                    *_,route,plan = build_plan(db,data)
                    result = {"route":route,"plan":plan}
                elif path == "/api/orders":
                    result = {"id":create_order(db,data)}
                elif path == "/api/order-status":
                    order = item(db,"orders",data.get("id"))
                    status = data.get("status")
                    if status not in {"Planned","Completed","Cancelled"}:
                        raise ValueError("Invalid status.")
                    if order["status"] == "Cancelled" and status!="Cancelled":
                        raise ValueError("Cancelled orders cannot be reopened. Create a new order.")
                    if status == "Cancelled":
                        later = [o for o in rows(db,"orders") if o["status"]!="Cancelled" and o["id"]!=order["id"] and o["driver_id"]==order["driver_id"] and parse_time(o["pickup_at"])>=parse_time(order["available_at"])]
                        if later:
                            raise ValueError("Cancel this driver's later orders first; their clocks depend on this order.")
                    db.execute("UPDATE orders SET status=? WHERE id=?",(status,order["id"]))
                    result = {"ok":True}
                elif path == "/api/geocode":
                    result = {"matches":routing.geocode(text(data,"query",True,600))}
                elif path == "/api/recommend":
                    result = recommend(db,data)
                elif path == "/api/upcoming":
                    result = weekly_availability(db,data)
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
    with ThreadingHTTPServer(("127.0.0.1",args.port),Handler) as server:
        server.database = database
        print(f"Daily Traking MalcomTrans: http://127.0.0.1:{server.server_port}",flush=True)
        print(f"Database: {database}",flush=True)
        print("Dispatch clock: Europe/Madrid. Ctrl+C to stop.",flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
