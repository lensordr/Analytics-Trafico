"""Client order form: a secret link per client (/c/<token>) where the client asks for transport.

The client sees only its own saved routes (tariffs whose client is this company), with the company and town of each
loading and unloading place, never prices, tariff notes or other clients' routes. A route that is not saved can be
typed ("other place"): the request then arrives marked as a new route without tariff. Requests land in the
dispatcher's "Client requests"; an order created from a request is linked to it.
"""
import json
import re
import secrets
from datetime import date, datetime, timedelta

import deca
from common import item
from planner import UTC, ZONE, parse_time, stamp

MAX_REQUESTS_PER_DAY = 40
REQUEST_DAYS = 90
STATUSES = ("New", "Planned", "Rejected")
DEFAULT_WEIGHT = "24 t"


def _clean(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _note(value, limit):
    lines = [" ".join(line.split()) for line in str(value or "").splitlines()]
    return "\n".join(line for line in lines if line)[:limit]


def client_link(db, data):
    """The client's form link (created once; `renew` makes a new one and the old link stops working)."""
    client = item(db, "locations", data.get("client_id"))
    token = client.get("client_token")
    if not token or data.get("renew"):
        token = secrets.token_urlsafe(18)
        db.execute("UPDATE locations SET client_token=? WHERE id=?", (token, client["id"]))
    base = deca.settings(db)["public_url"]
    routes = len(client_routes(db, client["id"]))
    return {"token": token, "url": f"{base}/c/{token}" if base else "", "routes": routes,
            "related": [] if routes else related_clients(db, client)}


def related_clients(db, client):
    """A place without routes of its own (e.g. a branch "Ferimet Sabadell"): the companies with a similar name that
    do have routes as client (e.g. "FERIMET S.L."), so the dispatcher sends the right link."""
    words = [w for w in re.findall(r"[a-z0-9]+", client["company"].casefold()) if len(w) >= 4]
    if not words:
        return []
    found = []
    for row in db.execute("SELECT l.id, l.company, l.city, count(t.id) AS routes FROM locations l JOIN tariffs t "
                          "ON t.client_id=l.id AND t.active=1 WHERE l.id!=? GROUP BY l.id", (client["id"],)):
        if words[0] in row["company"].casefold():
            found.append({"id": row["id"], "company": row["company"], "city": row["city"], "routes": row["routes"]})
    return sorted(found, key=lambda r: -r["routes"])[:5]


def revoke_client_link(db, data):
    client = item(db, "locations", data.get("client_id"))
    db.execute("UPDATE locations SET client_token=NULL WHERE id=?", (client["id"],))
    return {"message": f"The form link of {client['company']} no longer works."}


def client_by_token(db, token):
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{16,40}", token):
        return None
    row = db.execute("SELECT * FROM locations WHERE client_token=?", (token,)).fetchone()
    return {key.lower(): row[key] for key in row.keys()} if row else None


def _place(row, prefix):
    return {"id": row[f"{prefix}_id"], "company": row[f"{prefix}_company"], "city": row[f"{prefix}_city"],
            "country": row[f"{prefix}_country"], "postal_code": row[f"{prefix}_postal"] or ""}


def client_routes(db, client_id):
    """The client's own active routes: [{tariff_id, from, to, trips}] (no price, no notes). `trips`: orders already
    done on the route (to suggest the usual ones first)."""
    result, seen = [], set()
    trips = {}
    for row in db.execute("SELECT loading_id, unloading_id, count(*) AS n FROM orders WHERE status!='Cancelled' "
                          "AND (client_id=? OR client_id IS NULL) GROUP BY loading_id, unloading_id", (client_id,)):
        trips[(row["loading_id"], row["unloading_id"])] = row["n"]
    for row in db.execute(
            "SELECT t.id, f.id AS from_id, f.company AS from_company, f.city AS from_city, f.country AS from_country, "
            "f.postal_code AS from_postal, d.id AS to_id, d.company AS to_company, d.city AS to_city, "
            "d.country AS to_country, d.postal_code AS to_postal "
            "FROM tariffs t JOIN locations f ON f.id=t.from_id JOIN locations d ON d.id=t.to_id "
            "WHERE t.client_id=? AND t.active=1 ORDER BY f.company, d.company, t.id", (client_id,)):
        key = (row["from_id"], row["to_id"])
        if key in seen:
            continue  # several tariffs on the same route: one choice for the client
        seen.add(key)
        result.append({"tariff_id": row["id"], "from": _place(row, "from"), "to": _place(row, "to"), "trips": trips.get(key, 0)})
    return result


def editable_request(db, client, request_id):
    """The client's own request, while the office has not confirmed it yet (the client may still change it)."""
    try:
        row = db.execute("SELECT * FROM load_requests WHERE id=? AND client_id=?", (int(request_id), client["id"])).fetchone()
    except (TypeError, ValueError):
        row = None
    if row is None or row["status"] != "New" or json.loads(row["order_ids"] or "[]"):
        raise ValueError("not_editable")
    return _row(row)


def request_form(db, client, request):
    """A saved request as the form's fields (to edit it). A place the office saved that is not on the client's
    list (a new route) comes back as typed fields."""
    form = {"request_id": request["id"], "load_date": request["load_date"], "load_time": request["load_time"], "notes": request["notes"]}
    routes = client_routes(db, client["id"])
    listed = {r["from"]["id"] for r in routes} | {r["to"]["id"] for r in routes}
    for side in ("from", "to"):
        place_id = request[f"{side}_id"]
        if place_id and place_id not in listed:
            typed = request.get(f"{side}_place") or item(db, "locations", place_id)  # what the client typed, else the saved place
            form[f"{side}_id"] = "other"
            form.update({f"{side}_{key}": typed.get(key) or "" for key, _ in PLACE_FIELDS})
        elif place_id:
            form[f"{side}_id"] = str(place_id)
        else:
            form[f"{side}_id"] = "other"
            place = request.get(f"{side}_place") or {}
            form.update({f"{side}_{key}": place.get(key, "") for key, _ in PLACE_FIELDS} if place else {f"{side}_other": request[f"{side}_text"]})
    return form


def submit_request(db, client, form, now=None):
    """Save a client's transport request from the form (a new one, or `request_id`: a change to one not confirmed
    yet). Raises ValueError with a text key for the page."""
    now = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    editing = editable_request(db, client, form["request_id"]) if form.get("request_id") else None
    since = stamp(now - timedelta(days=1))
    sent = db.execute("SELECT count(*) AS n FROM load_requests WHERE client_id=? AND created_at>=?", (client["id"], since)).fetchone()
    if not editing and sent["n"] >= MAX_REQUESTS_PER_DAY:
        raise ValueError("too_many")
    routes = client_routes(db, client["id"])
    places = {r["from"]["id"] for r in routes} | {r["to"]["id"] for r in routes}

    def choice(key):
        value = str(form.get(key, "")).strip()
        if value == "other":
            return None
        if value.isdigit() and int(value) in places:
            return int(value)
        raise ValueError("choose_place")

    from_id, to_id = choice("from_id"), choice("to_id")
    from_place, from_text = new_place(form, "from") if from_id is None else ({}, "")
    to_place, to_text = new_place(form, "to") if to_id is None else ({}, "")
    route = next((r for r in routes if r["from"]["id"] == from_id and r["to"]["id"] == to_id), None) if from_id and to_id else None
    try:
        day = date.fromisoformat(str(form.get("load_date", "")))
    except ValueError:
        raise ValueError("load_date") from None
    today = now.astimezone(ZONE).date()
    if not today - timedelta(days=1) <= day <= today + timedelta(days=120):
        raise ValueError("load_date")
    unload_by = str(form.get("unload_by", "")).strip()
    if unload_by:
        try:
            if date.fromisoformat(unload_by) < day:
                raise ValueError("unload_by")
        except ValueError:
            raise ValueError("unload_by") from None
    try:
        trucks = int(form.get("trucks") or 1)
    except ValueError:
        raise ValueError("trucks") from None
    if not 1 <= trucks <= 30:
        raise ValueError("trucks")
    lang = form.get("l") if form.get("l") in ("en", "fr") else "es"
    if editing:
        db.execute("UPDATE load_requests SET lang=?, tariff_id=?, from_id=?, to_id=?, from_text=?, to_text=?, from_place=?, to_place=?, "
                   "load_date=?, load_time=?, notes=? WHERE id=?",
                   (lang, route["tariff_id"] if route else None, from_id, to_id, from_text, to_text,
                    json.dumps(from_place, ensure_ascii=False) if from_place else "", json.dumps(to_place, ensure_ascii=False) if to_place else "",
                    day.isoformat(), _clean(form.get("load_time"), 40), _note(form.get("notes"), 1000), editing["id"]))
        return editing["id"]
    db.execute("INSERT INTO load_requests (client_id, created_at, lang, tariff_id, from_id, to_id, from_text, to_text, "
               "from_place, to_place, load_date, load_time, unload_by, trucks, weight, material, reference, contact, notes) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (client["id"], stamp(now), lang, route["tariff_id"] if route else None, from_id, to_id, from_text, to_text,
                json.dumps(from_place, ensure_ascii=False) if from_place else "", json.dumps(to_place, ensure_ascii=False) if to_place else "",
                day.isoformat(), _clean(form.get("load_time"), 40),
                unload_by, trucks, _clean(form.get("weight"), 60) or DEFAULT_WEIGHT, _clean(form.get("material"), 120),
                _clean(form.get("reference"), 80), _clean(form.get("contact"), 120), _note(form.get("notes"), 1000)))


PLACE_FIELDS = (("company", 200), ("address", 200), ("postal_code", 12), ("city", 120), ("country", 60))


def new_place(form, side):
    """A loading or unloading place that is not on the client's list: company, address, postal code, town, country.
    Returns ({fields}, "one line for the dispatcher"). An old page with the single address box still works."""
    place = {key: _clean(form.get(f"{side}_{key}"), limit) for key, limit in PLACE_FIELDS}
    if not any(place.values()):
        text = _note(form.get(f"{side}_other"), 400)
        if len(text) < 5:
            raise ValueError("other_place")
        return {}, text
    if not all(place[key] for key in ("company", "address", "postal_code", "city")):
        raise ValueError("other_place")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]{2,11}", place["postal_code"]):
        raise ValueError("postal_code")
    place["country"] = place["country"] or "España"
    text = f'{place["company"]} · {place["address"]} · {place["postal_code"]} {place["city"]} · {place["country"]}'
    return place, text


def _row(row):
    data = {key.lower(): row[key] for key in row.keys()}
    data["order_ids"] = json.loads(data.get("order_ids") or "[]")
    for key in ("from_place", "to_place"):
        data[key] = json.loads(data.get(key) or "{}")
    return data


def client_history(db, client_id, days=365):
    """The client's transports with us (no prices): day, route, tractor + trailer, status. Newest first."""
    since = stamp(datetime.now(UTC) - timedelta(days=days))
    return [{key.lower(): row[key] for key in row.keys()} for row in db.execute(
        "SELECT o.id, o.pickup_at, o.plate, o.trailer, o.status, f.company AS from_company, f.city AS from_city, "
        "d.company AS to_company, d.city AS to_city FROM orders o JOIN locations f ON f.id=o.loading_id "
        "JOIN locations d ON d.id=o.unloading_id WHERE o.client_id=? AND o.status!='Cancelled' AND o.pickup_at>=? "
        "ORDER BY o.pickup_at DESC LIMIT 300", (client_id, since))]


def client_requests(db, client_id, limit=12):
    return [_row(row) for row in db.execute("SELECT * FROM load_requests WHERE client_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                                            (client_id, limit))]


def load_requests(db, now=None):
    """Requests of the last 90 days and every request still waiting, for the dispatcher."""
    since = stamp((now or datetime.now(UTC)) - timedelta(days=REQUEST_DAYS))
    return [_row(row) for row in db.execute("SELECT * FROM load_requests WHERE created_at>=? OR status='New' "
                                            "ORDER BY created_at DESC, id DESC", (since,))]


def _request(db, request_id):
    try:
        row = db.execute("SELECT * FROM load_requests WHERE id=?", (int(request_id),)).fetchone()
    except (TypeError, ValueError):
        row = None
    if row is None:
        raise ValueError("This client request no longer exists. Refresh and try again.")
    return _row(row)


def set_request_status(db, data, user=""):
    """Dispatcher: reject (with a reason the client sees), mark planned, or put back to new."""
    request = _request(db, data.get("id"))
    status = data.get("status")
    if status not in STATUSES:
        raise ValueError("Choose a valid status.")
    reason = _clean(data.get("reason"), 300) if status == "Rejected" else ""
    db.execute("UPDATE load_requests SET status=?, reason=?, handled_at=?, handled_by=? WHERE id=?",
               (status, reason, stamp(datetime.now(UTC)) if status != "New" else None, user or "", request["id"]))
    return {"message": {"Rejected": "Request rejected.", "Planned": "Request marked as planned.", "New": "Request open again."}[status]}


def request_place(db, data):
    """Dispatcher: the new place of a request was saved under Saved locations: the request now uses it (and the
    client's tariff for the route, if there is one)."""
    request = _request(db, data.get("id"))
    side = data.get("side")
    if side not in ("from", "to"):
        raise ValueError("Choose the loading or the unloading.")
    place = item(db, "locations", data.get("location_id"))
    ids = {"from": request["from_id"], "to": request["to_id"], side: place["id"]}
    tariff = db.execute("SELECT id FROM tariffs WHERE client_id=? AND from_id=? AND to_id=? AND active=1 ORDER BY id LIMIT 1",
                        (request["client_id"], ids["from"], ids["to"])).fetchone() if ids["from"] and ids["to"] else None
    db.execute(f"UPDATE load_requests SET {side}_id=?, tariff_id=? WHERE id=?",
               (place["id"], tariff["id"] if tariff else request["tariff_id"], request["id"]))
    return {"message": f'{place["company"]} saved and set on the request.'}


def match_request(db, order_id, user=""):
    """An order made by hand (not from the request card) for a client who asked for it: same client, loading,
    unloading and day as an open request still missing trucks. It is linked, so the client sees the truck."""
    order = item(db, "orders", order_id)
    if not order.get("client_id"):
        return None
    day = parse_time(order["pickup_at"]).astimezone(ZONE).date().isoformat()
    for row in db.execute("SELECT * FROM load_requests WHERE client_id=? AND from_id=? AND to_id=? AND load_date=? "
                          "AND status IN ('New','Planned') ORDER BY created_at", (order["client_id"], order["loading_id"], order["unloading_id"], day)):
        request = _row(row)
        if order_id in request["order_ids"]:
            return request["id"]
        if len(_active_orders(db, request)) < request["trucks"]:
            link_order(db, request["id"], order_id, user)
            return request["id"]
    return None


def _active_orders(db, request):
    ids = request["order_ids"]
    return [row["id"] for row in db.execute(f"SELECT id FROM orders WHERE status!='Cancelled' AND id IN ({','.join('?' * len(ids))})", ids)] if ids else []


def order_cancelled(db, order_id):
    """A cancelled order no longer serves its request: it is taken off, and a confirmed request now missing a
    truck goes back to new (the client sees "received", the office sees it to plan again)."""
    for row in db.execute("SELECT * FROM load_requests WHERE order_ids LIKE ?", (f"%{int(order_id)}%",)).fetchall():
        request = _row(row)
        if order_id not in request["order_ids"]:
            continue
        orders = [i for i in request["order_ids"] if i != order_id]
        status = "New" if request["status"] == "Planned" and len(_active_orders(db, {**request, "order_ids": orders})) < request["trucks"] else request["status"]
        db.execute("UPDATE load_requests SET order_ids=?, status=? WHERE id=?", (json.dumps(orders), status, request["id"]))


def link_order(db, request_id, order_id, user=""):
    """An order created from a request: linked to it; the request is planned once it has an order per truck."""
    request = _request(db, request_id)
    orders = [*request["order_ids"], order_id]
    status = "Planned" if len(_active_orders(db, {**request, "order_ids": orders})) >= request["trucks"] else request["status"]
    db.execute("UPDATE load_requests SET order_ids=?, status=?, handled_at=?, handled_by=? WHERE id=?",
               (json.dumps(orders), status, stamp(datetime.now(UTC)), user or "", request["id"]))
