"""One-off import of the routes exported from the old Witruck admin (data/witruck_rutas_*.json) as orders.

Usage:  python import_witruck_routes.py EXPORT_JSON [--database COPY.sqlite3]
Without --database it writes to the app's own database (Turso when configured). Make a backup first.
It stops if Witruck routes are already imported (orders whose notes start with "Witruck route").

Each Witruck route becomes one order with manual times: Witruck has only the day, so the loadings of each
tractor, driver and trailer are placed one after another from 06:00 (2 h loading, driving at 65 km/h from
the coordinates, 2 h unloading). Finished routes are confirmed (Completed); assigned ones keep the loading
confirmed (In transit); cancelled ones are skipped. Missing tractors, drivers, trailers, materials and
places are added. The carrier's cost is not imported.
"""
import argparse
import collections
import json
import math
import re
import unicodedata
from datetime import datetime, time, timedelta
from pathlib import Path

import config
from masters import save_master
from materials import save_material
from orders import DEFAULT_HANDLING_HOURS, create_order, set_stop_status
from planner import ZONE, parse_time, stamp
from storage import TursoReplica, connect, initialize, rows

ROOT = Path(__file__).resolve().parent
BASIS = {"FROM_WEIGHT_X_TONE_PRICE": "loading_t", "TO_WEIGHT_X_TONE_PRICE": "unloading_t", "JOURNEY": "trip", "TIME": "hour"}
COUNTRY = {"ESPANA": "Spain", "SPAIN": "Spain", "ES": "Spain", "FRANCIA": "France", "FRANCE": "France", "PORTUGAL": "Portugal",
           "ITALIA": "Italy", "ALEMANIA": "Germany", "BELGICA": "Belgium", "BELGIA": "Belgium", "AUSTRIA": "Austria",
           "ANDORRA": "Andorra", "HOLANDA": "Netherlands", "PAISES BAJOS": "Netherlands", "FRANTA": "France"}
LEGAL = r"\b(S\s?L\s?U?|S\s?A\s?U?|S\s?C\s?C\s?L|SAS|SARL|SRL|SPA|GMBH|LTD|S\s?COOP)\b"
ALIAS = {"COMPANIA ESPANOLA DE LAMINACION": "CELSA", "INVERSIONES ASOCIADAS GENERALES": "INAGEN",
         "MALCOM": "MALCOMTRANS BASE", "MALCOM TRANS": "MALCOMTRANS BASE"}  # our base
KMH, ROAD = 65, 1.25
DAY_START = time(6, 0)


def plain(value):
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", value)).strip()


def norm_name(value):
    value = re.sub(r"\s+", " ", re.sub(LEGAL, " ", plain(value))).strip()
    return ALIAS.get(value, value)


def zip5(value):
    digits = re.sub(r"\D", "", value or "")
    return digits[-5:].zfill(5) if digits else ""


def gps(text):
    found = re.findall(r"-?\d+\.\d+", text or "")
    if len(found) >= 2 and 27 < float(found[0]) < 60 and -20 < float(found[1]) < 25:
        return float(found[0]), float(found[1])
    return None


def km(a, b):
    h = (math.sin(math.radians(b[0] - a[0]) / 2) ** 2
         + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2)
    return 6371 * 2 * math.asin(min(1, math.sqrt(h)))


def truck_key(plate):
    """Our tractors are saved by their number: '7377 (CARGOMATE)' -> 7377, 'CJ57' -> CJ57."""
    return re.sub(r"[^A-Z0-9]", "", plate.upper().split("(")[0].split()[0] if plate.strip() else "")


def witruck_plate(plate):
    return re.sub(r"^\d+\.", "", re.sub(r"\s", "", plate.upper()))  # '3.3813MZW' is 3813MZW


def short_plate(plate):
    """New tractors are saved like the others: the number, or the letters and number of a foreign plate."""
    match = re.match(r"^(\d{4})", plate) or re.match(r"^([A-Z]{1,3}\d{2,4})", plate)
    return match.group(1) if match else plate


def driver_key(name):
    return re.sub(r"\s", "", plain(name))


def database(args):
    if args.database:
        return args.database
    config.load_env()
    turso = config.turso_settings()
    return TursoReplica(ROOT / "data" / "turso-import.db", *turso) if turso else ROOT / "data" / "malcomtrans.sqlite3"


class Places:
    """Witruck places -> our saved locations (same name and postal code, or the same site by GPS); new ones are added."""

    def __init__(self, locations):
        self.by_name_zip, self.by_name, self.with_gps = {}, collections.defaultdict(list), []
        for loc in locations:
            names = {norm_name(loc["company"])}
            also = re.search(r"Also written as: (.*?)\.(?: |$)", loc.get("notes") or "")
            if also:
                names |= {norm_name(n) for n in also.group(1).split(", ")}
            for name in names:
                self.by_name_zip.setdefault((name, zip5(loc["postal_code"])), loc)
                self.by_name[name].append(loc)
            if loc["lat"] is not None:
                self.with_gps.append((loc, names))

    def find(self, place):
        name, z, point = norm_name(place["name"]), zip5(place["zip"]), gps(place["gps"])
        if (name, z) in self.by_name_zip:
            return self.by_name_zip[(name, z)]
        first = name.split(" ")[0] if name else ""
        if point:
            close = sorted((km(point, (loc["lat"], loc["lon"])), loc) for loc, names in self.with_gps
                           if any(n.split(" ")[0] == first for n in names if n))
            if close and close[0][0] < 1.0:
                return close[0][1]
        same = [loc for loc in self.by_name.get(name, []) if zip5(loc["postal_code"])[:3] == z[:3]]
        if len(same) == 1:
            return same[0]
        # Witruck sometimes has the wrong postal code (e.g. our own, 08295): a name we have only once is that place.
        anywhere = {loc["id"]: loc for loc in self.by_name.get(name, [])}
        return next(iter(anywhere.values())) if len(anywhere) == 1 and not point else None

    def add(self, loc):
        names = {norm_name(loc["company"])}
        self.by_name_zip[(norm_name(loc["company"]), zip5(loc["postal_code"]))] = loc
        self.by_name[norm_name(loc["company"])].append(loc)
        if loc["lat"] is not None:
            self.with_gps.append((loc, names))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("export", type=Path)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    routes = [r for r in json.loads(args.export.read_text(encoding="utf-8")) if r["status"] != "CANCELLED"]
    routes.sort(key=lambda r: (r["date"], r["n"]))
    target = database(args)
    initialize(target)
    report = collections.Counter()
    skipped = []

    # ---- Masters: places, tractors, drivers, trailers, materials (each saved right away).
    with connect(target, fresh=True) as db:
        if db.execute("SELECT 1 FROM orders WHERE notes LIKE 'Witruck route %' LIMIT 1").fetchone():
            raise SystemExit("Witruck routes are already imported: nothing changed.")
        places = Places(rows(db, "locations"))
        by_nif = {re.sub(r"\W", "", loc["nif"]).upper(): loc["id"] for loc in rows(db, "locations") if loc["nif"]}
        place_ids = {}
        for route in routes:
            for side in ("from", "to"):
                place = route[side]
                key = (norm_name(place["name"]), zip5(place["zip"]), place["gps"])
                if key in place_ids:
                    continue
                found = places.find(place)
                if found:
                    point = gps(place["gps"])
                    if found["lat"] is None and point:  # fill in missing coordinates from Witruck
                        db.execute("UPDATE locations SET lat=?, lon=? WHERE id=?", (*point, found["id"]))
                        found.update(lat=point[0], lon=point[1])
                        report["coordinates added"] += 1
                    place_ids[key] = found["id"]
                    continue
                point = gps(place["gps"])
                country = COUNTRY.get(plain(place["country"]), "Spain" if zip5(place["zip"]) else "")
                new_id = save_master(db, "locations", {
                    "company": place["name"][:200] or place["city"], "city": place["city"].strip(" ,")[:200] or "?",
                    "postal_code": place["zip"] or "-", "country": country or place["country"] or "Spain",
                    "address": place["address"][:200], "lat": point[0] if point else None, "lon": point[1] if point else None,
                    "kind": "Client" if side == "from" else "Unloading only", "phone": place["phone"][:60],
                    "email": place["email"] if "@" in place["email"] and " " not in place["email"] else "",
                    "notes": "Added from the Witruck routes." + ("" if point else " No GPS yet.")})
                loc = dict(db.execute("SELECT * FROM locations WHERE id=?", (new_id,)).fetchone())
                places.add(loc)
                place_ids[key] = new_id
                report["locations added"] += 1

        trucks = {truck_key(t["plate"]): t for t in rows(db, "trucks") if not t["archived_at"]}
        truck_ids, truck_drivers = {}, collections.defaultdict(collections.Counter)
        for route in routes:
            plate = witruck_plate(route["truck"])
            if plate in truck_ids:
                continue
            match = [t for key, t in trucks.items() if key and plate.startswith(key)]
            if match:
                truck_ids[plate] = match[0]["id"]
            else:
                new_plate = short_plate(plate)
                truck_ids[plate] = save_master(db, "trucks", {"plate": new_plate, "operation": "International"})
                trucks[truck_key(new_plate)] = {"id": truck_ids[plate], "plate": new_plate, "driver_id": None}
                report["tractors added"] += 1

        drivers = {driver_key(d["name"]): d["id"] for d in rows(db, "drivers")}
        driver_ids = {}
        for route in routes:
            name = route["driver"].strip() or re.sub(r",.*", "", route["carrier"]).strip()[:40] + " (driver)"
            key = driver_key(name)
            if key not in drivers:
                carrier = "MalcomTrans" if "MALCOM" in plain(route["carrier"]) else route["carrier"].strip()
                drivers[key] = save_master(db, "drivers", {"name": name[:200], "carrier": carrier[:200]})
                report["drivers added"] += 1
            driver_ids[route["n"]] = drivers[key]
            truck_drivers[truck_ids[witruck_plate(route["truck"])]][drivers[key]] += 1
        for truck in rows(db, "trucks"):
            if truck["id"] in truck_drivers and not truck["driver_id"]:
                db.execute("UPDATE trucks SET driver_id=? WHERE id=?", (truck_drivers[truck["id"]].most_common(1)[0][0], truck["id"]))
                report["tractors given a default driver"] += 1

        trailers = {t["plate"].upper(): t["id"] for t in rows(db, "trailers")}
        for route in routes:
            plate = route["trailer"].upper().replace(" ", "")
            if plate and plate not in trailers:
                trailers[plate] = save_master(db, "trailers", {"plate": plate})
                report["trailers added"] += 1

        materials = {m["name"].strip().casefold(): m["id"] for m in rows(db, "materials")}
        for route in routes:
            if route["material"] and route["material"][1].strip().casefold() not in materials:
                code, name = route["material"]
                materials[name.strip().casefold()] = save_material(db, {"code": code, "name": name.strip()})
                report["materials added"] += 1
        tariffs = {(t["client_id"], t["from_id"], t["to_id"], t["basis"]): t["id"] for t in rows(db, "tariffs") if t["active"]}
        locations = {loc["id"]: loc for loc in rows(db, "locations")}
        busy = collections.defaultdict(lambda: datetime(2000, 1, 1, tzinfo=ZONE))
        existing = [dict(o) for o in db.execute("SELECT truck_id, driver_id, trailer_id, pickup_at, available_at FROM orders WHERE status!='Cancelled'")]

    # ---- Orders, one session each (on Turso each session is written as one batch).
    for route in routes:
        place = lambda side: place_ids[(norm_name(route[side]["name"]), zip5(route[side]["zip"]), route[side]["gps"])]
        loading_id, unloading_id = place("from"), place("to")
        truck_id, driver_id = truck_ids[witruck_plate(route["truck"])], driver_ids[route["n"]]
        trailer_plate = route["trailer"].upper().replace(" ", "")
        trailer_id = trailers.get(trailer_plate) if trailer_plate else None
        a, b = locations[loading_id], locations[unloading_id]
        if a["lat"] is not None and b["lat"] is not None:
            distance = round(km((a["lat"], a["lon"]), (b["lat"], b["lon"])) * ROAD, 1)
        else:
            distance = route["distance"] if 0 < route.get("distance", 0) < 3000 and route.get("distance") != 1000 else 0
        distance = distance if loading_id != unloading_id else 0
        hours = round(distance / KMH, 2)
        keys = [("truck", truck_id), ("driver", driver_id)] + ([("trailer", trailer_id)] if trailer_id else [])
        day_start = datetime.combine(datetime.fromisoformat(route["date"]).date(), DAY_START, ZONE)
        pickup = max([day_start] + [busy[k] for k in keys])
        # Do not run into an order already in the app (e.g. ones entered by hand): start after it.
        for order in existing:
            if order["truck_id"] == truck_id or order["driver_id"] == driver_id or (trailer_id and order["trailer_id"] == trailer_id):
                start, end = parse_time(order["pickup_at"]), parse_time(order["available_at"])
                finish = pickup + timedelta(hours=2 * DEFAULT_HANDLING_HOURS + hours)
                if pickup < end and finish > start:
                    pickup = max(pickup, end)
        arrive = pickup + timedelta(hours=DEFAULT_HANDLING_HOURS + hours)
        client = by_nif.get(re.sub(r"\W", "", route["cif"] or "").upper())
        price = route.get("price") or {}
        basis = BASIS.get(price.get("type"), "")
        rate = (price.get("ton") if basis in ("loading_t", "unloading_t") else price.get("trip")) or None
        # An order (OC) moved in several routes: the client price goes on the route that delivers it.
        legs = sum(1 for r in routes if r["oc"] == route["oc"])
        delivers = not route.get("ocTo") or norm_name(route["to"]["name"]) == norm_name(route["ocTo"]["name"])
        notes = [f"Witruck route {route['n']} · OC {route['oc']}" + (f" (the OC has {legs} routes)" if legs > 1 else "")]
        if "MALCOM" not in plain(route["carrier"]):
            notes.append(f"Carrier: {route['carrier']}.")
        if not client:
            notes.append(f"Client: {route['customer']} ({route['cif'] or 'no CIF'}).")
        if route.get("office"):
            notes.append(f"Invoice to: {route['office']}.")
        notes += [n for n in (route["notes"], route["ocNotes"]) if n]
        if legs > 1 and not delivers:
            notes.append("Client price on the route that delivers this OC.")
        weight = (route.get("ocFrom") or {}).get("weight") or 0
        data = {"truck_id": truck_id, "driver_id": driver_id, "trailer_id": trailer_id or "", "loading_id": loading_id,
                "unloading_id": unloading_id, "pickup_at": stamp(pickup), "timing": "manual", "route_mode": "manual",
                "distance_km": distance, "driving_hours": hours, "unloading_arrive_at": stamp(arrive),
                "loading_hours": DEFAULT_HANDLING_HOURS, "unloading_hours": DEFAULT_HANDLING_HOURS,
                "material_id": materials.get((route["material"] or ["", ""])[1].strip().casefold(), ""),
                "loading_weight_kg": round(weight * 1000) if weight and norm_name(route["from"]["name"]) == norm_name((route.get("ocFrom") or {}).get("name", "")) else "",
                "notes": " ".join(notes)[:5000]}
        if client:
            data["client_id"] = client
        if delivers and basis and rate is not None:
            data.update(rate_basis=basis, rate=rate)
            tariff = tariffs.get((client, loading_id, unloading_id, basis)) if client else None
            if tariff:
                data["tariff_id"] = tariff
        try:
            with connect(target) as db:
                order_id = create_order(db, data)
        except ValueError as error:
            skipped.append(f"{route['n']} ({route['date']}, {route['truck']}): {error}")
            continue
        report["orders added"] += 1
        with connect(target) as db:
            order = dict(db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())
            stops = [dict(s) for s in db.execute("SELECT seq, planned_end_at FROM order_stops WHERE order_id=? ORDER BY seq", (order_id,))]
        confirm = stops if route["status"] == "FINALIZED" else stops[:1]
        for stop in confirm:
            with connect(target) as db:
                set_stop_status(db, {"order_id": order_id, "seq": stop["seq"], "done_at": stop["planned_end_at"]})
        report["completed" if route["status"] == "FINALIZED" else "in transit"] += 1
        end = parse_time(order["available_at"])
        for key in keys:
            busy[key] = max(busy[key], end)
    print(json.dumps(dict(report), ensure_ascii=False))
    for line in skipped:
        print("Skipped", line)


if __name__ == "__main__":
    main()
