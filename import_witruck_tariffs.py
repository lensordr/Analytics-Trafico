"""One-off import of the client tariffs exported from the old Witruck admin (data/witruck_tarifas.json).

Usage:  python import_witruck_tariffs.py PLACES_JSON [--database COPY.sqlite3]
PLACES_JSON is the list of deduplicated, geocoded loading/unloading places prepared from the export.
Without --database it writes to the app's own database (Turso when configured). Run it once: it stops if
Witruck tariffs are already there. Make a backup first.
"""
import argparse
import collections
import json
import re
from pathlib import Path

import config
from storage import TursoReplica, begin_write, connect, initialize

ROOT = Path(__file__).resolve().parent
BASIS = {"FROM_WEIGHT_X_TONE_PRICE": "loading_t", "TO_WEIGHT_X_TONE_PRICE": "unloading_t", "JOURNEY": "trip", "TIME": "hour"}
NOTE = "Added from the Witruck tariffs."


def database(args):
    if args.database:
        return args.database
    config.load_env()
    turso = config.turso_settings()
    # Its own replica file: the running app keeps using data/turso-replica.db and picks up the import at its next sync.
    return TursoReplica(ROOT / "data" / "turso-import.db", *turso) if turso else ROOT / "data" / "malcomtrans.sqlite3"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("places", type=Path)
    parser.add_argument("--export", type=Path, default=ROOT / "data" / "witruck_tarifas.json")
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    export = json.loads(args.export.read_text(encoding="utf-8"))
    prepared = json.loads(args.places.read_text(encoding="utf-8"))
    places, ends = prepared["places"], prepared["ends"]
    target = database(args)
    initialize(target)
    with connect(target, fresh=True) as db:
        if db.execute("SELECT 1 FROM tariffs WHERE source_id IS NOT NULL LIMIT 1").fetchone():
            raise SystemExit("Witruck tariffs are already imported: nothing changed.")
        by_nif = {re.sub(r"\W", "", row["nif"]).upper(): row["id"] for row in db.execute("SELECT id, nif FROM locations WHERE nif!=''")}
        next_location = db.execute("SELECT coalesce(max(id), 0) + 1 FROM locations").fetchone()[0]
        next_tariff = db.execute("SELECT coalesce(max(id), 0) + 1 FROM tariffs").fetchone()[0]
        customers = {c["id"]: c for c in export["customers"]}
        origins = {pair[0] for pair in ends.values()}
        begin_write(db)
        place_location = {}
        for place in places:
            if place.get("location_id"):
                place_location[place["index"]] = place["location_id"]
                continue
            lat, lon = (place["gps"] or [None, None])[:2]
            names = [name for name in place["names"] if name.strip().upper() != place["name"].strip().upper()]
            notes = NOTE + (f" Also written as: {', '.join(names[:6])}." if names else "") + ("" if lat is not None else " No GPS yet.")
            db.execute("INSERT INTO locations (id, company, city, postal_code, country, address, lat, lon, geo_label, kind, phone, email, notes) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (next_location, place["name"][:200], (place["city"] or "?")[:200], place["zip"] or "-", place["country"],
                        place["address"][:200], lat, lon, place.get("geo_label", "")[:400],
                        "Client" if place["index"] in origins else "Unloading only", place["phone"][:60],
                        place["email"][:200] if "@" in place["email"] else "", notes))
            place_location[place["index"]] = next_location
            next_location += 1
        groups, skipped = collections.defaultdict(list), collections.Counter()
        for tariff in export["tariffs"]:
            vat = re.sub(r"\W", "", ((customers[tariff["customer"]["id"]].get("billingInfo") or {}).get("vatId") or "")).upper()
            client = by_nif.get(vat)
            start, end = ends.get(tariff["id"], [None, None])
            if client is None or start is None or end is None:
                skipped[customers[tariff["customer"]["id"]]["name"].strip()] += 1
                continue
            info = tariff["priceInfo"]
            basis = BASIS[info["priceType"]]
            rate = info["tonPrice"] if basis in ("loading_t", "unloading_t") else info["journeyPrice"]
            groups[(client, place_location[start], place_location[end], basis)].append(
                (info["updatedAt"][:10], tariff["id"], rate or 0, tariff.get("distance") or 0, (tariff.get("customerOffice") or "").strip()))
        count = inactive = 0
        for (client, start, end, basis), versions in groups.items():
            # Same client, route and basis more than once: the newest price is active, older ones are kept inactive.
            for position, (day, source, rate, km, office) in enumerate(sorted(versions, reverse=True)):
                notes = [] if rate else ["No price in Witruck: check it."]
                if office:
                    notes.append(f"Invoice to: {office}.")
                if position:
                    notes.append("Older price for the same route.")
                    inactive += 1
                db.execute("INSERT INTO tariffs (id, client_id, from_id, to_id, basis, rate, km, price_date, notes, active, source_id) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (next_tariff, client, start, end, basis, rate, km, day, " ".join(notes), 0 if position else 1, source))
                next_tariff += 1
                count += 1
    print(f"Locations added: {sum(1 for p in places if not p.get('location_id'))}; tariffs added: {count} "
          f"({inactive} older prices kept inactive); skipped (client not in your locations): {dict(skipped)}")


if __name__ == "__main__":
    main()
