"""Daily Traking MalcomTrans: start the server (python app.py).

The code lives in modules: common, masters, materials, tariffs, orders, nearby, nextloads, itv, driverlinks,
server, plus planner, storage, deca, driver, qr, xlsx, routing, upcoming, auth, config.
"""
import argparse
import os
import sys
from pathlib import Path

import autobackup
import config
import routing  # noqa: F401  (tests patch app.routing)
import time  # noqa: F401  (tests patch app.time)
from http.server import ThreadingHTTPServer  # noqa: F401
import deca
from auth import Auth
from planner import UTC, ZONE, parse_time  # noqa: F401
from storage import TursoReplica, connect, initialize
from common import *  # noqa: F401,F403
from masters import *  # noqa: F401,F403
from materials import *  # noqa: F401,F403
from tariffs import *  # noqa: F401,F403
from orders import *  # noqa: F401,F403
from nearby import *  # noqa: F401,F403
from nextloads import *  # noqa: F401,F403
from itv import *  # noqa: F401,F403
from driverlinks import *  # noqa: F401,F403
from server import *  # noqa: F401,F403
from server import Handler, ROOT, Server, TOKEN  # noqa: F401


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
        # On your computer only: the online server loses its files at every restart.
        server.backup_folder = None if online else ROOT/"backups"/"auto"
        if server.backup_folder:
            autobackup.start(database, server.backup_folder)
        print(f"Daily Traking MalcomTrans: http://127.0.0.1:{server.server_port}",flush=True)
        print(f"Database: {database}",flush=True)
        print("Dispatch clock: Europe/Madrid. Ctrl+C to stop.",flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
