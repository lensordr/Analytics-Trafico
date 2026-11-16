"""HTTP server: routing, login, driver page, JSON API and static files."""

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
from urllib.parse import parse_qsl, urlsplit

import autobackup
import deca
import driver
import routing
from auth import Auth
from common import item, text
from driverlinks import driver_link, driver_order
from itv import delete_itv, import_itv, save_itv
from masters import archive_master, restore_master, save_master
from materials import delete_material, save_material
from tariffs import delete_tariff, save_tariff
from nearby import log_contact, nearby_companies
from orders import build_plan, carry_clock, create_order, set_stop_status, update_stop_eta, uses_rules
from planner import UTC, parse_time, stamp
from storage import TursoReplica, backup_to, connect, rows
from upcoming import weekly_availability

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)

class Handler(BaseHTTPRequestHandler):
    def version_string(self):
        return "MalcomTrans"  # no Python version in the Server header

    def log_message(self, format, *args):
        # Driver links are secrets: never write them to the server log.
        message = format % args
        if "/s/" in message:
            message = re.sub(r"/s/[^ ?\"]+", "/s/…", message)
        sys.stderr.write(f"{self.address_string()} - - [{self.log_date_time_string()}] {message}\n")

    def send_body(self,status,body,content_type="application/json; charset=utf-8"):
        if not isinstance(body,bytes):
            body = json.dumps(body,ensure_ascii=False).encode() if content_type.startswith("application/json") else body.encode()
        self.send_response(status)
        self.send_header("Content-Type",content_type)
        self.send_header("Content-Length",str(len(body)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","no-referrer")
        if self.https():
            self.send_header("Strict-Transport-Security","max-age=31536000")
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
        with connect(self.server.database, fresh=True) as db:
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
                state["tariffs"] = rows(db,"tariffs")
                state["settings"] = deca.settings(db)
            folder = getattr(self.server,"backup_folder",None)
            newest = autobackup.latest(folder) if folder else None
            state["auto_backup"] = {"enabled": bool(folder), "latest": stamp(datetime.fromtimestamp(newest.stat().st_mtime,UTC)) if newest else None,
                                    "count": len(autobackup.files(folder)) if folder else 0}
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
        files = {"/":("index.html","text/html; charset=utf-8"),"/analytics.js":("analytics.js","text/javascript; charset=utf-8"),"/search-select.js":("search-select.js","text/javascript; charset=utf-8"),"/style.css":("style.css","text/css; charset=utf-8"),"/driver.css":("driver.css","text/css; charset=utf-8")}
        # Interface modules: static/js/<name>.js (names checked, so no other file can be read).
        if re.fullmatch(r"/js/[a-z][a-z-]*\.js", path) and (ROOT/"static"/path.lstrip("/")).is_file():
            return self.send_body(200,(ROOT/"static"/path.lstrip("/")).read_bytes(),"text/javascript; charset=utf-8")
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
                elif path == "/api/tariffs":
                    result = {"id":save_tariff(db,data)}
                elif path == "/api/tariff-delete":
                    result = delete_tariff(db,data)
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
