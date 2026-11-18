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
from planner import UTC, ZONE, stamp

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


def submit_request(db, client, form, now=None):
    """Save a client's transport request from the form. Raises ValueError with a text key for the page."""
    now = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    since = stamp(now - timedelta(days=1))
    sent = db.execute("SELECT count(*) AS n FROM load_requests WHERE client_id=? AND created_at>=?", (client["id"], since)).fetchone()
    if sent["n"] >= MAX_REQUESTS_PER_DAY:
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
    from_text, to_text = _note(form.get("from_other"), 400), _note(form.get("to_other"), 400)
    if (from_id is None and len(from_text) < 5) or (to_id is None and len(to_text) < 5):
        raise ValueError("other_place")
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
    lang = "en" if form.get("l") == "en" else "es"
    db.execute("INSERT INTO load_requests (client_id, created_at, lang, tariff_id, from_id, to_id, from_text, to_text, "
               "load_date, load_time, unload_by, trucks, weight, material, reference, contact, notes) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (client["id"], stamp(now), lang, route["tariff_id"] if route else None, from_id, to_id,
                "" if from_id else from_text, "" if to_id else to_text, day.isoformat(), _clean(form.get("load_time"), 40),
                unload_by, trucks, _clean(form.get("weight"), 60) or DEFAULT_WEIGHT, _clean(form.get("material"), 120),
                _clean(form.get("reference"), 80), _clean(form.get("contact"), 120), _note(form.get("notes"), 1000)))


def _row(row):
    data = {key.lower(): row[key] for key in row.keys()}
    data["order_ids"] = json.loads(data.get("order_ids") or "[]")
    return data


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


def link_order(db, request_id, order_id, user=""):
    """An order created from a request: linked to it; the request is planned once it has an order per truck."""
    request = _request(db, request_id)
    orders = [*request["order_ids"], order_id]
    status = "Planned" if len(orders) >= request["trucks"] else request["status"]
    db.execute("UPDATE load_requests SET order_ids=?, status=?, handled_at=?, handled_by=? WHERE id=?",
               (json.dumps(orders), status, stamp(datetime.now(UTC)), user or "", request["id"]))
