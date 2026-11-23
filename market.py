"""Witruck history: every trip of the last weeks (own trucks and partners), imported from the two Witruck exports
(Rutas → Exportar: places and carrier; Rutas → Facturación: client, material and price), joined by route number.

Used for the key clients (trips per week, trend, own share) and, around a free truck, the lanes those clients load
often ("Next loads"). Places are matched to saved locations by postal code and name, clients by NIF or name.
Prices stay inside the dispatcher's app: never on client pages.
"""
import csv
import io
import re
import unicodedata
from datetime import date, datetime, timedelta

from names import proper_name
from planner import UTC, ZONE, stamp
from routing import air_distance
from storage import begin_write

ROAD_FACTOR = 1.25
KEEP_DAYS = 180
WEEKS = 4
COUNTRIES = {"espana": "Spain", "spain": "Spain", "francia": "France", "france": "France", "belgia": "Belgium",
             "belgica": "Belgium", "belgium": "Belgium", "paises bajos": "Netherlands", "holanda": "Netherlands",
             "netherlands": "Netherlands", "italia": "Italy", "italy": "Italy", "alemania": "Germany", "germany": "Germany",
             "austria": "Austria", "portugal": "Portugal", "suiza": "Switzerland", "polonia": "Poland", "eslovenia": "Slovenia"}
LEGAL_WORDS = {"sl", "slu", "sa", "sau", "bv", "nv", "sll", "srl", "spa", "gmbh", "sas", "sarl", "ltd", "scoop", "coop", "s", "l", "u", "a"}
COMMON_WORDS = {"grupo", "group", "compania", "servicios", "inversiones", "hierros", "recycling", "reciclajes", "metals", "metales",
                "transportes", "gestio", "gestion", "ambiental", "espanola", "internacional", "systems", "plus"}
ROUTE_COLUMNS = ("day", "status", "plate", "driver", "own", "carrier", "km", "from_company", "from_address", "from_pc", "from_city",
                 "from_country", "to_company", "to_address", "to_pc", "to_city", "to_country")
BILL_COLUMNS = ("day", "status", "plate", "trailer", "driver", "own", "client", "client_nif", "material", "weight_t",
                "price_unit", "price_basis", "revenue", "cost")
ALL_COLUMNS = tuple(dict.fromkeys(ROUTE_COLUMNS + BILL_COLUMNS))


def _plain(value):
    text = unicodedata.normalize("NFD", str(value or "")).replace("�", "")
    return re.sub(r"[^a-z0-9 ]", " ", "".join(c for c in text if unicodedata.category(c) != "Mn").lower())


def name_key(value):
    """'FERIMET S.L.' → 'ferimet'; 'TEGA GESTIÓ AMBIENTAL SL' → 'tega gestio ambiental' (legal forms dropped)."""
    words = [w for w in _plain(re.sub(r"\b([A-Za-z])\.", r"\1", str(value or ""))).split() if w not in LEGAL_WORDS]
    return " ".join(words)


def first_word(value):
    """The distinctive word of a name: 'Ferimet Granollers' → 'ferimet', 'Compania Española de Laminacion' → 'laminacion'."""
    words = [w for w in name_key(value).split() if len(w) >= 4 and w not in COMMON_WORDS]
    return words[0] if words else (name_key(value).split() or [""])[0]


def postal(value):
    """'2.08755' → '08755', '08210.' → '08210', '2408 ZC' → '2408ZC'."""
    text = re.sub(r"\s", "", str(value or "")).strip(".").upper()
    match = re.fullmatch(r"\d\.(\d{5})", text)
    return match.group(1) if match else text


def country(value):
    return COUNTRIES.get(_plain(value).strip(), str(value or "").strip().title())


def number(value):
    """Spanish numbers: '5,78' → 5.78, '2.200' → 2200, '140,107' → 140.107; '' or 0 → None."""
    text = str(value or "").strip()
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        text = text.replace(".", "")
    try:
        result = float(text)
    except ValueError:
        return None
    return result or None


def _route_row(row):
    own = (row.get("tipo transportista") or "").strip().upper() == "I"
    return {"day": row.get("fecha", "")[:10], "status": row.get("estado", "").strip(), "plate": row.get("tractora", "").strip(),
            "driver": row.get("conductor", "").strip(), "own": int(own),
            "carrier": "" if own else (row.get("nombre fiscal") or "").strip(), "km": number(row.get("distancia")),
            **{f"{end}_{key}": value for end, label in (("from", "origen"), ("to", "destino")) for key, value in (
                ("company", (row.get(f"nombre del {label}") or "").strip()), ("address", (row.get(f"direccion {label}") or "").strip()),
                ("pc", postal(row.get(f"cp {label}"))), ("city", (row.get(f"poblacion {label}") or "").strip()),
                ("country", country(row.get(f"pais {label}"))))}}


def _bill_row(row):
    own = (row.get("tipo transportista") or "").strip().upper() == "I"
    basis = (row.get("tipo de precio oc") or "").replace("�", "€").strip()
    unit, total = number(row.get("precio unitario oc")), number(row.get("precio total oc"))
    weight = number(row.get("peso origen")) or number(row.get("peso destino"))
    if not total and unit:  # not invoiced yet: estimated from the unit price (per trip, or per tonne × weight or 24 t)
        total = unit if "viaje" in basis.lower() else round(unit * (weight or 24), 2)
    return {"day": row.get("fecha oc", "")[:10], "status": row.get("estado oc", "").strip(), "plate": row.get("tractora", "").strip(),
            "trailer": row.get("remolque", "").strip(), "driver": row.get("conductor", "").strip(), "own": int(own),
            "client": " ".join((row.get("nombre cliente") or "").split()), "client_nif": (row.get("cif cliente") or "").strip().upper(),
            "material": (row.get("material") or "").strip(), "weight_t": weight, "price_unit": unit, "price_basis": basis,
            "revenue": total, "cost": number(row.get("precio total or"))}


def parse_export(text):
    """One Witruck CSV export → ('routes' | 'billing', {route_no: values})."""
    text = str(text or "").lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    if "nombre cliente" in header and "orden de ruta" in header:
        kind, key, parse = "billing", "orden de ruta", _bill_row
    elif "nombre del origen" in header and "numero" in header:
        kind, key, parse = "routes", "numero", _route_row
    else:
        raise ValueError("This is not a Witruck export. Use Rutas → Exportar or Rutas → Facturación (data.csv).")
    found = {}
    for row in reader:
        try:
            number_ = int(str(row.get(key) or "").strip())
        except ValueError:
            continue
        values = parse(row)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", values["day"]):
            found[number_] = values
    return kind, found


def import_witruck(db, data):
    """Save the trips of one or both exports (the same route number is updated, never duplicated)."""
    files = data.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 4:
        raise ValueError("Choose the Witruck export files (data.csv).")
    parsed, kinds = {}, []
    for text in files:
        kind, found = parse_export(text)
        kinds.append(kind)
        for route_no, values in found.items():
            parsed.setdefault(route_no, {}).update(values)
    if not parsed:
        raise ValueError("The file has no routes.")
    now = stamp(datetime.now(UTC))
    before = {row[0] for row in db.execute("SELECT route_no FROM market_trips")}
    begin_write(db)
    for route_no, values in parsed.items():
        columns = [c for c in ALL_COLUMNS if c in values]
        db.execute(f"INSERT INTO market_trips (route_no, imported_at, {', '.join(columns)}) VALUES (?, ?, {', '.join('?' for _ in columns)}) "
                   f"ON CONFLICT(route_no) DO UPDATE SET imported_at=excluded.imported_at, "
                   + ", ".join(f"{c}=excluded.{c}" for c in columns),
                   (route_no, now, *(values[c] for c in columns)))
    oldest = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    db.execute("DELETE FROM market_trips WHERE day < ?", (oldest,))
    days = sorted(v["day"] for v in parsed.values())
    new = len(set(parsed) - before)
    missing = [k for k in ("routes", "billing") if k not in kinds]
    hint = {"routes": " Add the Exportar file too: it has the loading and unloading companies.",
            "billing": " Add the Facturación file too: it has the client and the price."}
    return {"message": f"{len(parsed)} Witruck {'route' if len(parsed) == 1 else 'routes'} from {days[0]} to {days[-1]} imported ({new} new)." + "".join(hint[k] for k in missing),
            "routes": len(parsed), "new": new}


# ---------------------------------------------------------------- matching to saved places
class Places:
    def __init__(self, db):
        self.locations = [dict(row) for row in db.execute("SELECT id, company, city, postal_code, country, lat, lon, kind, phone, nif FROM locations")]
        self.by_pc, self.by_city = {}, {}
        for loc in self.locations:
            self.by_pc.setdefault((postal(loc["postal_code"]), loc["country"]), []).append(loc)
            self.by_city.setdefault((_plain(loc["city"]).strip(), loc["country"]), []).append(loc)
        with_routes = {row[0] for row in db.execute("SELECT DISTINCT client_id FROM tariffs WHERE active=1 AND client_id IS NOT NULL")}
        self.clients = {}
        for loc in self.locations:
            self.clients.setdefault(("nif", (loc.get("nif") or "").upper().replace("-", "")), []).append(loc)
            self.clients.setdefault(("name", name_key(loc["company"])), []).append(loc)
            if loc["id"] in with_routes:
                self.clients.setdefault(("word", first_word(loc["company"])), []).append(loc)
        self.with_routes, self._cache = with_routes, {}

    def place(self, company, pc, city, country_):
        """The saved location of a trip end (or None) and its coordinates (or the town's, roughly)."""
        key = (company, pc, city, country_)
        if key in self._cache:
            return self._cache[key]
        same_pc = self.by_pc.get((pc, country_), [])
        same_city = self.by_city.get((_plain(city).strip(), country_), [])
        word, full = first_word(company), name_key(company)
        match = (next((l for l in same_pc if name_key(l["company"]) == full), None)
                 or next((l for l in same_pc if word and first_word(l["company"]) == word), None)
                 or next((l for l in same_city if name_key(l["company"]) == full), None))
        nearby = [l for l in (([match] if match else []) + same_pc + same_city) if l.get("lat") is not None]
        point = {"lat": nearby[0]["lat"], "lon": nearby[0]["lon"]} if nearby else None
        self._cache[key] = (match, point)
        return match, point

    def client(self, name, nif):
        nif = (nif or "").upper().replace("-", "")
        for key in (("nif", nif) if nif else None, ("name", name_key(name)), ("word", first_word(name))):
            found = self.clients.get(key) if key else None
            if found:
                return next((l for l in found if l["id"] in self.with_routes), found[0])
        return None


def _town(value):
    return proper_name(" ".join(str(value or "").split()), False) or "?"


def _week_start(day):
    return day - timedelta(days=day.weekday())


def _trips(db, since):
    return [dict(row) for row in db.execute("SELECT * FROM market_trips WHERE day >= ? AND status NOT IN ('ANULADA', 'CANCELADA')", (since,))]


def _client_name(trip, client):
    name = trip.get("client") or ""
    if client and ("�" in name or not name):
        return client["company"]
    return proper_name(name) if name else "Client unknown"


def key_clients(db, now=None):
    """The clients of the last WEEKS weeks by Witruck trips: per week, own share, revenue and their main lanes."""
    today = (now or datetime.now(UTC)).astimezone(ZONE).date()
    weeks = [_week_start(today) - timedelta(weeks=i) for i in range(WEEKS - 1, -1, -1)]
    trips = _trips(db, weeks[0].isoformat())
    places = Places(db)
    groups = {}
    for trip in trips:
        client = places.client(trip.get("client"), trip.get("client_nif"))
        key = ("id", client["id"]) if client else ("name", name_key(trip.get("client")) or "?")
        group = groups.setdefault(key, {"name": _client_name(trip, client), "client_id": client["id"] if client else None,
                                        "phone": bool(client and client.get("phone")), "has_routes": bool(client and client["id"] in places.with_routes),
                                        "trips": 0, "own": 0, "weeks": [0] * WEEKS, "revenue": 0.0, "priced": 0, "lanes": {}, "last": "",
                                        "materials": {}})
        group["trips"] += 1
        group["own"] += trip["own"] or 0
        week = (_week_start(date.fromisoformat(trip["day"])) - weeks[0]).days // 7
        if 0 <= week < WEEKS:
            group["weeks"][week] += 1
        if trip.get("revenue"):
            group["revenue"] += trip["revenue"]
            group["priced"] += 1
        lane = f"{_town(trip.get('from_city'))} → {_town(trip.get('to_city'))}"
        group["lanes"][lane] = group["lanes"].get(lane, 0) + 1
        if trip.get("material"):
            group["materials"][trip["material"]] = group["materials"].get(trip["material"], 0) + 1
        group["last"] = max(group["last"], trip["day"])
    result = []
    for group in groups.values():
        lanes = sorted(group.pop("lanes").items(), key=lambda kv: -kv[1])
        materials = sorted(group.pop("materials").items(), key=lambda kv: -kv[1])
        done = group["weeks"][:-1]  # the current week is not over
        trend = None
        if len(done) >= 2 and max(done[-1], done[-2]) >= 3:  # too few trips: no trend
            trend = round((done[-1] - done[-2]) / max(done[-2], 1) * 100)
        result.append({**group, "revenue": round(group["revenue"], 2), "lanes": [{"lane": k, "trips": v} for k, v in lanes[:4]],
                       "material": materials[0][0] if materials else "", "trend": trend})
    result.sort(key=lambda g: (-g["trips"], g["name"]))
    total = len(trips)
    latest = db.execute("SELECT max(imported_at), max(day) FROM market_trips").fetchone()
    return {"clients": result, "total": total, "own": sum(t["own"] or 0 for t in trips), "weeks": [w.isoformat() for w in weeks],
            "imported_at": latest[0], "last_day": latest[1]}


def lanes_near(db, origin, radius_km=None, now=None, limit=10, days=WEEKS * 7):
    """Lanes of the key clients that load near a place (the last `days`): who, from where to where, how often, who does it.

    Ranked by trips and closeness. "onward" = trips loading near the lane's destination (what the truck can do next)."""
    if origin.get("lat") is None:
        return []
    today = (now or datetime.now(UTC)).astimezone(ZONE).date()
    trips = _trips(db, (today - timedelta(days=days)).isoformat())
    places = Places(db)
    lanes, starts = {}, []
    for trip in trips:
        start, start_at = places.place(trip.get("from_company"), trip.get("from_pc"), trip.get("from_city"), trip.get("from_country"))
        if start_at:
            starts.append(start_at)
        end, end_at = places.place(trip.get("to_company"), trip.get("to_pc"), trip.get("to_city"), trip.get("to_country"))
        client = places.client(trip.get("client"), trip.get("client_nif"))
        key = (client["id"] if client else name_key(trip.get("client")), trip.get("from_pc"), first_word(trip.get("from_company")),
               trip.get("to_pc"), first_word(trip.get("to_company")))
        lane = lanes.setdefault(key, {"client": _client_name(trip, client), "client_id": client["id"] if client else None,
                                      "from": {"company": proper_name(trip.get("from_company") or ""), "city": _town(trip.get("from_city")), "country": trip.get("from_country") or "",
                                               "id": start["id"] if start else None},
                                      "to": {"company": proper_name(trip.get("to_company") or ""), "city": _town(trip.get("to_city")), "country": trip.get("to_country") or "",
                                             "id": end["id"] if end else None},
                                      "_from": start_at, "_to": end_at, "trips": 0, "own": 0, "carriers": {}, "revenue": 0.0, "priced": 0,
                                      "last": "", "km": None, "material": ""})
        lane["trips"] += 1
        lane["own"] += trip["own"] or 0
        if not trip["own"] and trip.get("carrier"):
            lane["carriers"][trip["carrier"]] = lane["carriers"].get(trip["carrier"], 0) + 1
        if trip.get("revenue"):
            lane["revenue"] += trip["revenue"]
            lane["priced"] += 1
        lane["last"] = max(lane["last"], trip["day"])
        lane["km"] = lane["km"] or trip.get("km")
        lane["material"] = lane["material"] or trip.get("material") or ""
    reach = (radius_km or 150) * ROAD_FACTOR
    found = []
    for lane in lanes.values():
        if not lane["_from"] or not lane["client"]:
            continue
        empty = round(air_distance(origin, lane["_from"]) * ROAD_FACTOR, 1)
        if empty > reach:
            continue
        onward = sum(1 for s in starts if lane["_to"] and air_distance(lane["_to"], s) <= 60) if lane["_to"] else None
        carriers = sorted(lane.pop("carriers").items(), key=lambda kv: -kv[1])
        lane.pop("_from"), lane.pop("_to")
        found.append({**lane, "empty_km": empty, "onward": onward, "carriers": [c for c, _ in carriers[:2]],
                      "per_trip": round(lane["revenue"] / lane["priced"], 2) if lane["priced"] else None,
                      "revenue": round(lane["revenue"], 2), "score": lane["trips"] / (1 + empty / 40)})
    found.sort(key=lambda l: -l["score"])
    return found[:limit]


# ---------------------------------------------------------------- who usually does a route (dispatcher only)
def _same_place(places, trip, end, loc):
    """A trip end is this saved location: matched to it, or the same postal code and the same distinctive name."""
    if not loc:
        return False
    match, _ = places.place(trip.get(f"{end}_company"), trip.get(f"{end}_pc"), trip.get(f"{end}_city"), trip.get(f"{end}_country"))
    if match:
        return match["id"] == loc["id"]
    pc = postal(trip.get(f"{end}_pc")).lstrip("0")
    return bool(pc) and pc == postal(loc.get("postal_code")).lstrip("0") and first_word(trip.get(f"{end}_company")) == first_word(loc.get("company"))


def usual_trucks(db, data, now=None, days=120, limit=5):
    """The tractors (and drivers) that did this route in the Witruck history, with their share: the exact route of the
    client first; with too few trips, the same loading place of the client, then any client, then the client's trips."""
    client = db.execute("SELECT id, company, nif FROM locations WHERE id=?", (data.get("client_id") or 0,)).fetchone()
    start = db.execute("SELECT id, company, postal_code FROM locations WHERE id=?", (data.get("from_id") or 0,)).fetchone()
    end = db.execute("SELECT id, company, postal_code FROM locations WHERE id=?", (data.get("to_id") or 0,)).fetchone()
    client, start, end = (dict(x) if x else None for x in (client, start, end))
    if not (start or end or client):
        return {"basis": "", "trips": 0, "trucks": []}
    today = (now or datetime.now(UTC)).astimezone(ZONE).date()
    trips = _trips(db, (today - timedelta(days=days)).isoformat())
    places = Places(db)
    of_client = (lambda t: (places.client(t.get("client"), t.get("client_nif")) or {}).get("id") == client["id"]) if client else (lambda t: True)
    at_start = [t for t in trips if _same_place(places, t, "from", start)] if start else []
    levels = []
    if start and end:
        levels.append(("route", [t for t in at_start if _same_place(places, t, "to", end) and of_client(t)]))
    if start:
        levels.append(("loading", [t for t in at_start if of_client(t)]))
    if end:
        levels.append(("unloading", [t for t in trips if _same_place(places, t, "to", end) and of_client(t)]))
    if start:
        levels.append(("loading_any", at_start))
    if client:
        levels.append(("client", [t for t in trips if of_client(t)]))
    basis, chosen = next(((b, ts) for b, ts in levels if len(ts) >= 3), next(((b, ts) for b, ts in levels if ts), ("", [])))
    if not chosen:
        return {"basis": "", "trips": 0, "trucks": []}
    trucks = [dict(r) for r in db.execute("SELECT id, plate, driver_id, own_fleet FROM trucks WHERE archived_at IS NULL AND active=1")]
    drivers = [dict(r) for r in db.execute("SELECT id, name, active FROM drivers")]
    key = lambda p: re.sub(r"[^A-Z0-9]", "", re.sub(r"\(.*?\)", "", str(p or "").upper()))
    norm = lambda s: re.sub(r"[^a-z]", "", _plain(s))
    def truck_of(plate):
        p = key(re.sub(r"^\d\.", "", plate or ""))
        hits = [t for t in trucks if key(t["plate"]) and p.startswith(key(t["plate"]))]
        return max(hits, key=lambda t: len(key(t["plate"])), default=None)
    found = {}
    for trip in chosen:
        truck = truck_of(trip.get("plate"))
        k = truck["id"] if truck else key(trip.get("plate"))
        row = found.setdefault(k, {"truck_id": truck["id"] if truck else None, "plate": truck["plate"] if truck else (trip.get("plate") or "?"),
                                   "own": bool(truck["own_fleet"]) if truck else bool(trip.get("own")), "trips": 0, "last": "", "drivers": {}})
        row["trips"] += 1
        row["last"] = max(row["last"], trip.get("day") or "")
        if trip.get("driver"):
            row["drivers"][trip["driver"]] = row["drivers"].get(trip["driver"], 0) + 1
    total = len(chosen)
    result = []
    for row in found.values():
        names = row.pop("drivers")
        name = max(names.items(), key=lambda kv: kv[1])[0] if names else ""
        driver = next((d for d in drivers if d["active"] and norm(name) and norm(d["name"]).startswith(norm(name)[:6])), None)
        default = next((t["driver_id"] for t in trucks if t["id"] == row["truck_id"]), None)
        result.append({**row, "share": round(row["trips"] * 100 / total), "driver": name, "driver_id": (driver or {}).get("id") or default})
    result.sort(key=lambda r: (-r["trips"], not r["own"], r["plate"]))
    return {"basis": basis, "trips": total, "trucks": result[:limit]}
