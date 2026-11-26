"""HTTP server: routing, login, driver page, JSON API and static files."""

import gzip
import hashlib
import json
import os
import re
import secrets
import socket
import sys
import sqlite3
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from html import escape
from urllib.parse import parse_qsl, urlencode, urlsplit

import assistant
import autobackup
import client
import deca
import driver
import oauth
import routing
from auth import Auth
from clientlinks import client_by_token, client_history, client_photo, client_photo_zip, client_steps, client_link, client_requests, editable_request, request_form, client_routes, link_order, load_requests, match_request, order_cancelled, request_place, revoke_client_link, set_request_status, submit_request
from common import item, text
from names import apply_name_fixes, name_fixes
from market import import_witruck, key_clients, usual_trucks
from driverlinks import driver_deca, driver_link, driver_order, eta_reports, link_opens, mark_opened, report_delay
from itv import delete_itv, import_itv, save_itv
from masters import archive_master, restore_master, save_km_list, save_master, service_done, set_truck_order
from materials import delete_material, save_material
from tariffs import delete_tariff, save_tariff
import photos
from nearby import log_contact, nearby_companies
from nextloads import next_loads
from orders import assign_tractor, build_plan, carry_clock, create_order, set_stop_status, update_stop_eta, uses_rules, move_order, order_overlaps
from planner import UTC, ZONE, parse_time, stamp
from storage import TursoReplica, backup_to, connect, rows
from upcoming import weekly_availability

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
# What the workshop account may save: the km of the oil changes and the month-end km, nothing else.
# Installable app (PWA): the manifest, its icons and the service worker load before signing in.
PWA_FILES = ("/manifest.webmanifest", "/sw.js", "/icon-180.png", "/icon-192.png", "/icon-512.png")
SERVICE_POSTS = {"/api/truck-km", "/api/truck-service"}
# What the workshop account (SERVICE_USERS) sees of each tractor: the plate and its maintenance, nothing else.
SERVICE_TRUCK_FIELDS = ("id", "plate", "operation", "active", "archived_at", "driver_id", "own_fleet", "last_service_km", "service_every_km",
                        "service_due_km", "odometer_km", "odometer_on", "prev_odometer_km", "prev_odometer_on")
COMPRESSIBLE = {"application/json", "text/html", "text/javascript", "text/css"}

CONNECTOR_GET = {"/.well-known/oauth-protected-resource","/.well-known/oauth-protected-resource/mcp",
                 "/.well-known/oauth-authorization-server","/.well-known/oauth-authorization-server/mcp",
                 "/.well-known/openid-configuration","/oauth/authorize","/mcp"}
CONNECTOR_POST = {"/oauth/register","/oauth/token","/oauth/authorize","/mcp"}

class Handler(BaseHTTPRequestHandler):
    def version_string(self):
        return "MalcomTrans"  # no Python version in the Server header

    def log_message(self, format, *args):
        # Driver links are secrets: never write them to the server log.
        message = format % args
        if "/s/" in message or "/d/" in message or "/c/" in message:
            message = re.sub(r"/([sdc])/[^ ?\"]+", r"/\1/…", message)
        sys.stderr.write(f"{self.address_string()} - - [{self.log_date_time_string()}] {message}\n")

    def send_body(self,status,body,content_type="application/json; charset=utf-8",headers=None):
        if not isinstance(body,bytes):
            body = json.dumps(body,ensure_ascii=False).encode() if content_type.startswith("application/json") else body.encode()
        # Text compresses about 5x (the app state: ~750 KB -> ~140 KB): much faster online.
        compressed = len(body) > 1400 and "gzip" in self.headers.get("Accept-Encoding","") and content_type.split(";")[0] in COMPRESSIBLE
        if compressed:
            body = gzip.compress(body, 6)
        self.send_response(status)
        self.send_header("Content-Type",content_type)
        self.send_header("Content-Length",str(len(body)))
        if compressed:
            self.send_header("Content-Encoding","gzip")
        if content_type.split(";")[0] in COMPRESSIBLE:
            self.send_header("Vary","Accept-Encoding")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if "Cache-Control" not in (headers or {}):
            self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","no-referrer")
        if self.https():
            self.send_header("Strict-Transport-Security","max-age=31536000")
        if "Content-Security-Policy" not in (headers or {}):
            self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_static(self, path, mime):
        """App files (JS, CSS, pages): the browser keeps them and asks "changed?"; unchanged → 304, nothing sent."""
        body = path.read_bytes()
        tag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
        if self.headers.get("If-None-Match") == tag:
            self.send_response(304)
            self.send_header("ETag", tag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self.send_body(200, body, mime, {"ETag": tag, "Cache-Control": "no-cache"})

    def valid_host(self):
        local = {f"127.0.0.1:{self.server.server_port}",f"localhost:{self.server.server_port}"}
        return self.headers.get("Host") in local | getattr(self.server,"public_hosts",set())

    # ---- login (only when APP_PASSWORD is set, i.e. the online version)
    @property
    def auth(self):
        return getattr(self.server,"auth",None) or Auth()

    def client_address_text(self):
        # Online, traffic reaches the app through Cloudflare, which sets CF-Connecting-IP itself
        # (a visitor cannot fake it). X-Forwarded-For can be typed by the visitor, so it is only a fallback.
        if not getattr(self.server,"public_hosts",None):
            return self.client_address[0]
        for header in ("CF-Connecting-IP","True-Client-IP"):
            if self.headers.get(header,"").strip():
                return self.headers[header].strip()
        forwarded = [part.strip() for part in self.headers.get("X-Forwarded-For","").split(",") if part.strip()]
        return forwarded[-1] if forwarded else self.client_address[0]

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
        # With dispatcher users (APP_USERS) each one signs in with their name.
        name = ('<div class="field"><label for="user">User</label><input id="user" name="user" autocomplete="username" '
                'autocapitalize="none" required autofocus></div>') if self.auth.named else ""
        if self.auth.named:  # the cursor starts in the user field
            page = page.replace('type="password" autocomplete="current-password" required autofocus>','type="password" autocomplete="current-password" required>')
        page = page.replace("<!--MESSAGE-->",note).replace("<!--USER-->",name)
        self.send_body(status,page,"text/html; charset=utf-8")

    def handle_login(self):
        address = self.client_address_text()
        if self.auth.blocked(address):
            return self.login_page("Too many wrong passwords. Wait 15 minutes and try again.",429)
        length = int(self.headers.get("Content-Length","0") or 0)
        if not 0 < length < 2000:
            return self.login_page("Enter the password.",400)
        form = dict(parse_qsl(self.rfile.read(length).decode("utf-8","replace")))
        user = self.auth.check(address,form.get("password",""),form.get("user",""))
        if user is not None:
            return self.redirect("/",self.auth.new_cookie(self.https(),user))
        time.sleep(1)
        return self.login_page("Wrong user or password." if self.auth.named else "Wrong password.",401)

    # ---- driver page: the secret link is the only key; one order, next stop only
    def driver_get(self,token,message="",error="",lang=None):
        query = dict(parse_qsl(urlsplit(self.path).query))
        lang = driver.lang_of(lang or query.get("l"))
        with connect(self.server.database) as db:
            order, stops, places = driver_order(db, token)
        if order is None:
            return self.send_body(404, driver.missing_page(lang), "text/html; charset=utf-8")
        try:
            with connect(self.server.database) as db:
                mark_opened(db, token)
        except Exception:
            pass  # the page must open even if the view cannot be recorded
        if query.get("ok") and not message and not error:
            message = driver.TEXT[lang]["saved"]
        with connect(self.server.database) as db:
            sent = photos.counts(db, order["id"])
        photo_seq = int(query["photo"]) if query.get("photo", "").isdigit() else None
        if query.get("eta") and not message and not error:
            upcoming = next((s for s in stops if s["status"] != "Done"), None)
            message = driver.TEXT[lang]["eta_saved"] + (driver._when(upcoming["planned_at"], lang) if upcoming else "")
        confirm = int(query["c"]) if query.get("c", "").isdigit() else None
        deca_seq = int(query["deca"]) if query.get("deca", "").isdigit() else None
        if query.get("dc") == "1" and not message:
            message = driver.TEXT[lang]["deca_ready"]
        elif query.get("dc") == "0" and not message:
            message = driver.TEXT[lang]["deca_office"]
        dni = ""
        if deca_seq:
            with connect(self.server.database) as db:
                row = db.execute("SELECT dni_nie FROM drivers WHERE id=?", (order["driver_id"],)).fetchone()
                dni = row["dni_nie"] if row else ""
        self.send_body(200, driver.render(token, order, stops, places, lang, confirm, message, error, deca_seq, dni, sent, photo_seq),
                       "text/html; charset=utf-8")

    def driver_photo(self, token):
        """One document photo from the driver page (JSON {seq, image}); the phone has already made it small."""
        length = int(self.headers.get("Content-Length","0") or 0)
        if not 0 < length < 2_200_000:
            return self.send_body(413, {"error": "photo"})
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self.send_body(400, {"error": "photo"})
        with connect(self.server.database, fresh=True) as db:
            order, stops, _ = driver_order(db, token)
            if order is None or order["status"] == "Cancelled":
                return self.send_body(404, {"error": "link"})
            try:
                sent = photos.save_photo(db, order, stops, data.get("seq"), data.get("image"))
            except ValueError as error:
                return self.send_body(400, {"error": str(error)})
        return self.send_body(200, {"sent": sent})

    def driver_post(self,token):
        length = int(self.headers.get("Content-Length","0") or 0)
        form = dict(parse_qsl(self.rfile.read(length).decode("utf-8","replace"))) if 0 < length < 2000 else {}
        lang = driver.lang_of(form.get("l"))
        with connect(self.server.database, fresh=True) as db:
            order, stops, _ = driver_order(db, token)
            if order is None:
                return self.send_body(404, driver.missing_page(lang), "text/html; charset=utf-8")
            if form.get("action") == "docs":
                try:
                    stop = photos.finish(db, order, stops, form.get("seq"), form.get("mode"))
                except ValueError:
                    return self.redirect(f"/s/{token}?l={lang}#docs")
                # After the loading document: the optional DeCA form (material, weight, DNI/NIE).
                return self.redirect(f"/s/{token}?l={lang}" + (f"&deca={stop['seq']}" if stop["kind"] == "Loading" and form.get("mode") == "sent" else ""))
            if form.get("action") == "deca":
                try:
                    ready = driver_deca(db, order, stops, form)
                except ValueError as error:
                    text = driver.TEXT[lang]["bad_weight"] if str(error) == "weight" else str(error)
                    return self.driver_get(token, error=text, lang=lang)
                return self.redirect(f"/s/{token}?l={lang}&dc={1 if ready else 0}")
            next_stop = next((s for s in stops if s["status"] != "Done"), None)
            if form.get("action") == "eta":
                if order["status"] == "Cancelled" or next_stop is None or str(next_stop["seq"]) != form.get("seq"):
                    return self.redirect(f"/s/{token}?l={lang}")
                try:
                    report_delay(db, order, next_stop, form.get("delay"))
                except ValueError as error:
                    return self.driver_get(token, error=str(error), lang=lang)
                return self.redirect(f"/s/{token}?l={lang}&eta=1")
            if any(photos.needs_docs(s) for s in stops):
                # The document photo of the last loading/unloading comes first (the page shows only that step).
                return self.redirect(f"/s/{token}?l={lang}#docs")
            if order["status"] == "Cancelled" or next_stop is None or str(next_stop["seq"]) != form.get("seq"):
                # Double tap or an old page: nothing to confirm, just show the current state.
                return self.redirect(f"/s/{token}?l={lang}")
            try:
                set_stop_status(db, {"order_id": order["id"], "seq": next_stop["seq"]})
            except ValueError as error:
                return self.driver_get(token, error=str(error), lang=lang)
        # After a loading or unloading: the document photo first (then, after a loading, the optional DeCA form).
        extra = "#docs" if next_stop["kind"] in photos.DOC_KINDS else ""
        return self.redirect(f"/s/{token}?l={lang}&ok=1{extra}")

    def signed_in(self):
        return self.auth.valid(self.headers.get("Cookie"))

    def service_user(self):
        """The workshop account: sees only ITV and the tractors' maintenance, and cannot change anything."""
        return self.auth.role(self.auth.user(self.headers.get("Cookie"))) == "service"

    def dispatcher_state(self):
        with connect(self.server.database) as db:
            state = {key:rows(db,key) for key in ("drivers","trucks","trailers","locations","orders")}
            state["itv"] = rows(db,"itv_vehicles")
            state["materials"] = rows(db,"materials")
            state["tariffs"] = rows(db,"tariffs")
            state["settings"] = deca.settings(db)
            state["link_opens"] = link_opens(db)
            state["eta_reports"] = eta_reports(db)
            state["load_requests"] = load_requests(db)
            state["stop_photos"] = photos.photo_list(db)
        folder = getattr(self.server,"backup_folder",None)
        newest = autobackup.latest(folder) if folder else None
        state["auto_backup"] = {"enabled": bool(folder), "latest": stamp(datetime.fromtimestamp(newest.stat().st_mtime,UTC)) if newest else None,
                                "count": len(autobackup.files(folder)) if folder else 0}
        state["role"] = "dispatcher"
        return state

    def service_state(self):
        with connect(self.server.database) as db:
            drivers = {row["id"]: row["name"] for row in db.execute("SELECT id, name FROM drivers")}
            trucks = [{key: t[key] for key in SERVICE_TRUCK_FIELDS} | {"driver_name": drivers.get(t["driver_id"], "")}
                      for t in rows(db, "trucks") if not t["archived_at"] and t["own_fleet"]]
            itv = rows(db, "itv_vehicles")
        return {"role": "service", "trucks": trucks, "itv": itv, "drivers": [], "trailers": [], "locations": [], "orders": [],
                "materials": [], "tariffs": [], "settings": {}, "link_opens": {}, "eta_reports": [], "load_requests": [], "stop_photos": [],
                "auto_backup": {"enabled": False, "latest": None, "count": 0}}

    # ---- client order page: the secret link identifies the client; only its own routes, no prices
    def client_get(self, token, message="", error="", form=None, lang=None):
        query = dict(parse_qsl(urlsplit(self.path).query))
        lang = client.lang_of(lang or query.get("l"), self.headers.get("Accept-Language",""))
        with connect(self.server.database) as db:
            company = client_by_token(db, token)
            if company is None:
                return self.send_body(404, client.missing_page(lang), "text/html; charset=utf-8")
            routes = client_routes(db, company["id"])
            places = {row["id"]: row for row in db.execute("SELECT id, company, city FROM locations")}
            history = client_requests(db, company["id"])
            transports = client_history(db, company["id"])
            steps = client_steps(db, [row["id"] for row in transports])
            if query.get("edit") and form is None and not error:
                try:
                    form = request_form(db, company, editable_request(db, company, query["edit"]))
                except ValueError as fault:
                    error = client.TEXT[lang]["errors"][str(fault)]
        label = lambda pid, typed: f'{places[pid]["company"]} ({places[pid]["city"]})' if pid in places else " ".join(typed.split())[:60]
        ids = [order_id for request in history for order_id in request["order_ids"]]
        with connect(self.server.database) as db:
            orders = {row["id"]: row for row in db.execute(
                f"SELECT o.id, o.plate, o.trailer, o.status, COALESCE((SELECT COALESCE(s.driver_done_at, s.done_at) FROM order_stops s "
                f"WHERE s.order_id=o.id AND s.seq=1 AND s.status='Done'), o.pickup_at) AS pickup_at FROM orders o "
                f"WHERE o.id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
        for request in history:
            typed = lambda side: f'{request[side + "_place"]["company"]} ({request[side + "_place"]["city"]})' if request.get(side + "_place") else request[side + "_text"]
            request["route"] = f'{label(request["from_id"], typed("from"))} → {label(request["to_id"], typed("to"))}'
            # The assembly that loads (tractor + trailer) and the day: shown to the client once the order is planned.
            request["vehicles"] = [{"plate": o["plate"], "trailer": o["trailer"] or "", "day": parse_time(o["pickup_at"]).astimezone(ZONE).date().isoformat()}
                                   for o in (orders.get(i) for i in request["order_ids"]) if o and o["status"] != "Cancelled"]
        if query.get("ok") and not message and not error:
            message = client.TEXT[lang]["saved" if query.get("ok") == "2" else "sent"]
        local = lambda value: parse_time(value).astimezone(ZONE).strftime("%d/%m %H:%M") if value else ""
        for row in transports:
            row["day"] = parse_time(row["pickup_at"]).astimezone(ZONE).date().isoformat()
            row["steps"] = [{**s, "done": local(s["done_at"]) if s["status"] == "Done" else ""} for s in steps.get(row["id"], [])]
            loaded = next((s["done_at"] for s in steps.get(row["id"], []) if s["seq"] == 1 and s["status"] == "Done" and s["done_at"]), None)
            if loaded:
                row["day"] = parse_time(loaded).astimezone(ZONE).date().isoformat()
        tab = "history" if query.get("tab") == "history" else "new"
        today = datetime.now(UTC).astimezone(ZONE).date().isoformat()
        self.send_body(200 if not error else 400, client.render(token, company, routes, history, lang, today, message, error, form, tab, transports),
                       "text/html; charset=utf-8", {"X-Robots-Tag": "noindex"})

    def client_file(self, token, kind, key):
        """A document photo (/c/<token>/foto/<id>, ?dl=1 to save it) or all photos of an order (/c/<token>/fotos/<order id>)."""
        with connect(self.server.database) as db:
            company = client_by_token(db, token)
            found = None if company is None else client_photo(db, company["id"], key) if kind == "foto" else client_photo_zip(db, company["id"], key)
        if found is None:
            return self.send_body(404, "<!doctype html><meta charset=utf-8><title>MalcomTrans</title><p>No disponible.</p>", "text/html; charset=utf-8")
        filename, body = found
        save = kind == "fotos" or dict(parse_qsl(urlsplit(self.path).query)).get("dl") == "1"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip" if kind == "fotos" else "image/jpeg")
        self.send_header("Content-Disposition", f'{"attachment" if save else "inline"}; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()
        return self.wfile.write(body)

    def client_post(self, token):
        length = int(self.headers.get("Content-Length","0") or 0)
        form = dict(parse_qsl(self.rfile.read(length).decode("utf-8","replace"))) if 0 < length < 8000 else {}
        lang = client.lang_of(form.get("l"))
        with connect(self.server.database, fresh=True) as db:
            company = client_by_token(db, token)
            if company is None:
                return self.send_body(404, client.missing_page(lang), "text/html; charset=utf-8")
            try:
                submit_request(db, company, form)
            except ValueError as error:
                key = str(error)
                return self.client_get(token, error=client.TEXT[lang]["errors"].get(key, key), form=form, lang=lang)
        return self.redirect(f"/c/{token}?l={lang}&ok={2 if form.get('request_id') else 1}")

    # ---- Claude connector (MCP): OAuth sign-in with the dispatcher's password, then read-only tools at /mcp
    def base_url(self):
        return f"{'https' if self.https() else 'http'}://{self.headers.get('Host')}"

    def local_only(self):
        return not getattr(self.server,"public_hosts",None)

    def read_form(self, limit=8000):
        length = int(self.headers.get("Content-Length","0") or 0)
        if not 0 < length < limit:
            return {}
        raw = self.rfile.read(length).decode("utf-8","replace")
        if self.headers.get("Content-Type","").startswith("application/json"):
            try:
                data = json.loads(raw)
            except ValueError:
                return {}
            return data if isinstance(data,dict) else {}
        return dict(parse_qsl(raw))

    def connector_page(self, params, message="", status=200):
        fields = "".join(f'<input type="hidden" name="{key}" value="{escape(str(params.get(key,"")))}">'
                         for key in ("client_id","redirect_uri","state","code_challenge","code_challenge_method","response_type"))
        user = ('<div class="field"><label for="user">User</label><input id="user" name="user" autocomplete="username" '
                'autocapitalize="none" required autofocus></div>') if self.auth.named else ""
        password = ('<div class="field"><label for="password">Password</label><input id="password" name="password" type="password" '
                    f'autocomplete="current-password" required{"" if self.auth.named else " autofocus"}></div>') if self.auth.enabled else ""
        note = f'<div class="notice warning">{escape(message)}</div>' if message else ""
        page = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>Connect Claude · MalcomTrans</title><link rel="icon" href="data:,"><link rel="stylesheet" href="/style.css"></head>'
                '<body class="login-page"><div class="login-box panel padded">'
                '<div class="brand login-brand"><span class="brand-mark">M<span>↗</span></span><span>MalcomTrans<small>DAILY TRAKING</small></span></div>'
                '<h1>Connect Claude</h1><p class="subtitle">Claude will be able to <strong>read</strong> the planning (tractors, orders, places, '
                'saved routes) to recommend tractors and loads. It cannot create, change or delete anything. '
                'Phones, driver links and documents are not shared.</p>'
                f'{note}<form method="post" action="/oauth/authorize" class="login-form">{fields}{user}{password}'
                '<button class="btn primary" type="submit">Allow read access</button></form></div></body></html>')
        targets = " ".join(sorted({f"{urlsplit(uri).scheme}://{urlsplit(uri).netloc}" for uri in oauth.CLAUDE_CALLBACKS}))
        if self.local_only():
            targets += " http://127.0.0.1:* http://localhost:*"
        csp = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
               f"frame-ancestors 'none'; base-uri 'none'; form-action 'self' {targets}")
        self.send_body(status,page,"text/html; charset=utf-8",{"Content-Security-Policy":csp})

    def connector_request_error(self, params):
        if params.get("response_type") != "code":
            return "Unsupported response type."
        if not oauth.redirect_allowed(params.get("redirect_uri",""), self.local_only()):
            return "This address is not allowed to connect."
        if not params.get("code_challenge") or params.get("code_challenge_method") != "S256":
            return "PKCE (S256) is required."
        return None

    def connector_get(self, path):
        base = self.base_url()
        if path in ("/.well-known/oauth-protected-resource","/.well-known/oauth-protected-resource/mcp"):
            return self.send_body(200, oauth.resource_metadata(base))
        if path in ("/.well-known/oauth-authorization-server","/.well-known/oauth-authorization-server/mcp","/.well-known/openid-configuration"):
            return self.send_body(200, oauth.metadata(base))
        if path == "/oauth/authorize":
            params = dict(parse_qsl(urlsplit(self.path).query))
            problem = self.connector_request_error(params)
            if problem:
                return self.send_body(400, f"<!doctype html><meta charset=utf-8><title>MalcomTrans</title><p>{escape(problem)}</p>", "text/html; charset=utf-8")
            return self.connector_page(params)
        return self.send_body(405, {"error": "Use POST"}, headers={"Allow": "POST"})

    def connector_post(self, path):
        if path == "/oauth/register":
            status, body = oauth.register(self.read_form(), self.local_only())
            return self.send_body(status, body)
        if path == "/oauth/token":
            status, body = oauth.exchange(self.auth, self.read_form())
            return self.send_body(status, body, headers={"Pragma": "no-cache"})
        if path == "/oauth/authorize":
            params = self.read_form()
            problem = self.connector_request_error(params)
            if problem:
                return self.send_body(400, {"error": problem})
            user = ""
            if self.auth.enabled:
                address = self.client_address_text()
                if self.auth.blocked(address):
                    return self.connector_page(params, "Too many wrong passwords. Wait 15 minutes and try again.", 429)
                user = self.auth.check(address, params.get("password",""), params.get("user",""))
                if user is None:
                    time.sleep(1)
                    return self.connector_page(params, "Wrong user or password." if self.auth.named else "Wrong password.", 401)
                if self.auth.role(user) == "service":  # the workshop account sees only ITV and maintenance
                    return self.connector_page(params, "This account cannot connect Claude.", 403)
            code = oauth.new_code(self.auth, user, params.get("client_id",""), params["redirect_uri"], params["code_challenge"])
            query = urlencode({"code": code, **({"state": params["state"]} if params.get("state") else {})})
            return self.redirect(params["redirect_uri"] + ("&" if "?" in params["redirect_uri"] else "?") + query)
        # /mcp: JSON-RPC with the bearer token
        header = self.headers.get("Authorization","")
        token = oauth.verify(self.auth, "access", header[7:].strip()) if header[:7].lower() == "bearer " else None
        if token is None:
            return self.send_body(401, {"error": "invalid_token"}, headers={
                "WWW-Authenticate": f'Bearer resource_metadata="{self.base_url()}/.well-known/oauth-protected-resource"'})
        length = int(self.headers.get("Content-Length","0") or 0)
        if not 0 < length < 100000:
            return self.send_body(400, {"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"Invalid request size"}})
        try:
            message = json.loads(self.rfile.read(length))
        except ValueError:
            return self.send_body(400, {"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"Parse error"}})
        open_db = lambda: connect(self.server.database)
        if isinstance(message, list):
            answers = [a for a in (assistant.handle(m, open_db) for m in message) if a is not None]
            return self.send_body(200, answers) if answers else self.send_body(202, b"", "application/json")
        answer = assistant.handle(message, open_db)
        if answer is None:
            return self.send_body(202, b"", "application/json")
        return self.send_body(200, answer)

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
        if path.startswith("/c/"):
            token, _, rest = path[3:].partition("/")
            kind, _, key = rest.partition("/")
            if kind in ("foto", "fotos") and key:
                return self.client_file(token, kind, key)
            return self.client_get(path[3:])
        if path in CONNECTOR_GET:
            return self.connector_get(path)
        if path.startswith("/d/"):
            # DeCA opened from its QR code (inspection, client): the secret identifier is the key.
            try:
                with connect(self.server.database) as db:
                    filename, body = deca.pdf_by_link(db, path[3:])
            except ValueError:
                return self.send_body(404, "<!doctype html><meta charset=utf-8><title>DeCA</title><p>Documento no encontrado.</p>",
                                      "text/html; charset=utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex")
            self.end_headers()
            return self.wfile.write(body)
        if path not in ("/style.css","/driver.css","/driver.js","/client.css","/client.js")+PWA_FILES and not self.signed_in():
            return self.send_body(401,{"error":"Please sign in again."}) if path.startswith("/api/") else self.redirect("/login")
        if self.service_user() and path.startswith(("/api/", "/tiles/")) and path != "/api/state":
            return self.send_body(403, {"error": "This account can only see ITV and maintenance."})
        tile = re.fullmatch(r"/tiles/(\d{1,2})/(\d{1,7})/(\d{1,7})\.png", path)
        if tile:
            try:
                body = routing.map_tile(*(int(value) for value in tile.groups()))
            except ValueError:
                return self.send_body(404, {"error": "No map tile"})
            return self.send_body(200, body, "image/png", {"Cache-Control": "private, max-age=604800"})
        if path == "/api/state":
            state = self.service_state() if self.service_user() else self.dispatcher_state()
            state.update(token=TOKEN,auth=self.auth.enabled,user=self.auth.user(self.headers.get("Cookie")) or "",here_configured=routing.configured(),timezone="Europe/Madrid",
                         storage="turso" if isinstance(self.server.database,TursoReplica) else "sqlite")
            # Unchanged data (the page asks every minute): answer "304 Not Modified" without sending it again.
            tag = '"' + hashlib.sha1(json.dumps(state,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()[:24] + '"'
            if self.headers.get("If-None-Match") == tag:
                self.send_response(304)
                self.send_header("ETag",tag)
                self.send_header("Cache-Control","no-store")
                self.end_headers()
                return
            state["now"] = stamp(datetime.now(UTC))
            return self.send_body(200,state,headers={"ETag":tag})
        if path == "/api/photo":
            try:
                with connect(self.server.database) as db:
                    body = photos.photo_bytes(db, dict(parse_qsl(urlsplit(self.path).query)).get("id"))
            except ValueError as error:
                return self.send_body(404, {"error": str(error)})
            return self.send_body(200, body, "image/jpeg", {"Cache-Control": "private, max-age=86400"})
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
        files = {"/manifest.webmanifest":("manifest.webmanifest","application/manifest+json"),"/sw.js":("sw.js","text/javascript; charset=utf-8"),
                 "/icon-180.png":("icon-180.png","image/png"),"/icon-192.png":("icon-192.png","image/png"),"/icon-512.png":("icon-512.png","image/png"),
                 "/":("index.html","text/html; charset=utf-8"),"/analytics.js":("analytics.js","text/javascript; charset=utf-8"),"/search-select.js":("search-select.js","text/javascript; charset=utf-8"),"/style.css":("style.css","text/css; charset=utf-8"),"/driver.css":("driver.css","text/css; charset=utf-8"),"/client.css":("client.css","text/css; charset=utf-8"),"/client.js":("client.js","text/javascript; charset=utf-8"),"/driver.js":("driver.js","text/javascript; charset=utf-8")}
        # Interface modules: static/js/<name>.js (names checked, so no other file can be read).
        if re.fullmatch(r"/js/[a-z][a-z-]*\.js", path) and (ROOT/"static"/path.lstrip("/")).is_file():
            return self.send_static(ROOT/"static"/path.lstrip("/"),"text/javascript; charset=utf-8")
        if path not in files:
            return self.send_body(404,{"error":"Page not found"})
        filename,mime = files[path]
        if path == "/":
            return self.send_body(200,(ROOT/"static"/filename).read_bytes(),mime)
        self.send_static(ROOT/"static"/filename,mime)

    def do_POST(self):
        if self.valid_host() and urlsplit(self.path).path == "/login":
            return self.handle_login()
        if self.valid_host() and re.fullmatch(r"/s/[A-Za-z0-9_-]{8,64}/photo", urlsplit(self.path).path):
            return self.driver_photo(urlsplit(self.path).path[3:-6])
        if self.valid_host() and urlsplit(self.path).path.startswith("/s/"):
            return self.driver_post(urlsplit(self.path).path[3:])
        if self.valid_host() and urlsplit(self.path).path.startswith("/c/"):
            return self.client_post(urlsplit(self.path).path[3:])
        if self.valid_host() and urlsplit(self.path).path in CONNECTOR_POST:
            return self.connector_post(urlsplit(self.path).path)
        if self.valid_host() and not self.signed_in():
            return self.send_body(401,{"error":"Please sign in again."})
        if not self.valid_host() or not secrets.compare_digest(self.headers.get("X-CSRF-Token",""),TOKEN):
            return self.send_body(403,{"error":"Please reload the app and try again."})
        if self.service_user() and urlsplit(self.path).path not in SERVICE_POSTS:
            return self.send_body(403,{"error":"This account can only see ITV and enter the maintenance km."})
        try:
            length = int(self.headers.get("Content-Length","0"))
            limit = 3_000_000 if urlsplit(self.path).path in ("/api/itv-import", "/api/witruck-import") else 100000
            if not 0<length<limit:
                raise ValueError("Invalid request size.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data,dict):
                raise ValueError("Expected a form object.")
            path = urlsplit(self.path).path
            with connect(self.server.database, fresh=True) as db:
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
                    clash = order_overlaps(db,result["id"])
                    if clash:
                        result["warning"] = f"Same tractor, driver or trailer at the same time as {', '.join(clash)}." 
                    user = self.auth.user(self.headers.get("Cookie")) or ""
                    if data.get("request_id"):  # created from a client request: link them
                        link_order(db,data["request_id"],result["id"],user)
                    else:  # made by hand for a client who asked for it: linked too, so the client sees the truck
                        match_request(db,result["id"],user)
                elif path == "/api/witruck-import":
                    result = import_witruck(db,data)
                elif path == "/api/key-clients":
                    result = key_clients(db)
                elif path == "/api/name-fixes":
                    result = {"fixes": name_fixes(db)}
                elif path == "/api/name-fixes-apply":
                    result = apply_name_fixes(db,data)
                elif path == "/api/client-link":
                    result = client_link(db,data)
                elif path == "/api/client-link-revoke":
                    result = revoke_client_link(db,data)
                elif path == "/api/load-request":
                    result = set_request_status(db,data,self.auth.user(self.headers.get("Cookie")) or "")
                elif path == "/api/request-place":
                    result = request_place(db,data)
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
                    order_cancelled(db,order["id"])
                    result = {"ok":True}
                elif path == "/api/stop-status":
                    result = set_stop_status(db,data)
                elif path == "/api/truck-order":
                    result = set_truck_order(db,data)
                elif path == "/api/order-move":
                    result = move_order(db,data)
                elif path == "/api/stop-eta":
                    result = update_stop_eta(db,data)
                elif path == "/api/assign-tractor":
                    result = assign_tractor(db,data)
                elif path == "/api/road-km":
                    point = lambda prefix: {"lat": float(data[prefix + "_lat"]), "lon": float(data[prefix + "_lon"])}
                    truck = item(db, "trucks", data.get("truck_id"))
                    result = routing.road_km(point("from"), point("to"), truck)
                elif path == "/api/geocode":
                    result = {"matches":routing.geocode(text(data,"query",True,600))}
                elif path == "/api/upcoming":
                    result = weekly_availability(db,data)
                elif path == "/api/truck-service":
                    result = service_done(db,data)
                elif path == "/api/usual-trucks":
                    result = usual_trucks(db,data)
                elif path == "/api/truck-km":
                    result = save_km_list(db,data)
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
                elif path == "/api/tariffs":
                    result = {"id":save_tariff(db,data)}
                elif path == "/api/tariff-delete":
                    result = delete_tariff(db,data)
                elif path == "/api/material-delete":
                    result = delete_material(db,data)
                elif path == "/api/itv-import":
                    result = import_itv(db,data)
                elif path == "/api/next-loads":
                    result = next_loads(db,data)
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
