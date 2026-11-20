"""Directory: tractors, trailers, drivers and saved locations (save, archive, restore)."""

from datetime import datetime

from common import item, text
from names import proper_name
from planner import UTC, number, parse_time, stamp
from storage import reserve_id

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


SERVICE_EVERY = 60000
MAX_KM = 5_000_000


def _today():
    from planner import ZONE
    return datetime.now(UTC).astimezone(ZONE).date().isoformat()


def _ro(data):
    """The workshop account sees the maintenance page in Romanian (it sends lang=ro)."""
    return data.get("lang") == "ro"


def _km(data, key, label):
    raw = data.get(key)
    if raw in (None, ""):
        return None
    try:
        value = round(float(str(raw).replace(" ", "")))
    except ValueError:
        raise ValueError(f"{label}: scrie km ca număr." if _ro(data) else f"{label}: write the km as a number.")
    if not 0 <= value <= MAX_KM:
        raise ValueError((f"{label}: între 0 și {MAX_KM:,} km." if _ro(data) else f"{label}: between 0 and {MAX_KM:,} km.").replace(",", "."))
    return value


def _day(data, key="day"):
    day = text(data, key, False, 10) or _today()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise ValueError("Alege data citirii." if _ro(data) else "Choose the day of the km reading.")
    return day


def km_values(saved, last, odometer, day, every=None, ro=False):
    """Maintenance of one tractor: km of the last oil change and the km now (read at the end of the month, on `day`).
    The next change is the last one + `service_every_km`. A new reading keeps the previous one, so the km per
    month (and the day of the next change) can be estimated from the two."""
    plate = saved.get("plate", "")
    if last is not None and odometer is not None and odometer < last:
        raise ValueError((f"{plate}: km actuali ({odometer:,}) sunt mai mici decât km la ultimul schimb ({last:,})." if ro else
                          f"{plate}: the km now ({odometer:,}) are lower than the km of the last change ({last:,}).").replace(",", "."))
    values = {"last_service_km": last, "odometer_km": odometer,
              "service_every_km": round(every or saved.get("service_every_km") or SERVICE_EVERY)}
    old, old_on = saved.get("odometer_km"), saved.get("odometer_on")
    if odometer is None:
        values.update(odometer_on=None, prev_odometer_km=None, prev_odometer_on=None)
    elif odometer == old and old_on:
        values.update(odometer_on=old_on, prev_odometer_km=saved.get("prev_odometer_km"), prev_odometer_on=saved.get("prev_odometer_on"))
    elif old is not None and old_on and old_on < day and old <= odometer:
        values.update(odometer_on=day, prev_odometer_km=old, prev_odometer_on=old_on)  # a new month: keep the last one
    else:  # first reading, or a correction of the same day
        values.update(odometer_on=day, prev_odometer_km=saved.get("prev_odometer_km") if old_on == day else None,
                      prev_odometer_on=saved.get("prev_odometer_on") if old_on == day else None)
    values["service_due_km"] = last + values["service_every_km"] if last is not None else None
    return values


def service_values(db, data):
    """Maintenance fields of the tractor form. An older open form may not send them: keep what is saved."""
    saved = item(db, "trucks", data["id"]) if data.get("id") else {}
    if "last_service_km" not in data and "odometer_km" not in data:
        return {}
    every = _km(data, "service_every_km", "Change every") or SERVICE_EVERY
    if every < 1000:
        raise ValueError("Change every: at least 1.000 km.")
    return km_values(saved, _km(data, "last_service_km", "Last change at"), _km(data, "odometer_km", "Km now"),
                     _day(data, "odometer_on"), every)


def _update_km(db, truck_id, values):
    db.execute("UPDATE trucks SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?", (*values.values(), truck_id))


def save_km_list(db, data):
    """The month-end list: for each tractor the km of the last change and the km now, all read on the same day."""
    day = _day(data)
    rows, ro = data.get("items"), _ro(data)
    if not isinstance(rows, list) or not rows:
        raise ValueError("Nimic de salvat." if ro else "Nothing to save.")
    changed = 0
    for row in rows:
        truck = item(db, "trucks", row.get("id"))
        row = {**row, "lang": data.get("lang")}
        last = _km(row, "last_service_km", f"{truck['plate']} " + ("ultimul schimb" if ro else "last change"))
        odometer = _km(row, "odometer_km", f"{truck['plate']} " + ("km actuali" if ro else "km now"))
        if last == truck.get("last_service_km") and odometer == truck.get("odometer_km"):
            continue
        _update_km(db, truck["id"], km_values(truck, last, odometer, day, ro=ro))
        changed += 1
    if ro:
        return {"message": f"Km salvați pentru {changed} {'tractor' if changed == 1 else 'tractoare'}." if changed else "Nimic schimbat."}
    return {"message": f"Km saved for {changed} tractor{'s' if changed != 1 else ''}." if changed else "Nothing changed."}


def service_done(db, data):
    """The oil change was done at `km`: it becomes the last change (and the km now, if higher)."""
    truck = item(db, "trucks", data.get("id"))
    ro = _ro(data)
    km = _km(data, "km", "Km la schimb" if ro else "Km at the change")
    if km is None:
        raise ValueError("Scrie km la schimb." if ro else "Write the km at the change.")
    day = _day(data)
    odometer = truck.get("odometer_km")
    values = km_values(truck, km, km if odometer is None or km >= odometer else odometer,
                       day if odometer is None or km >= odometer else truck.get("odometer_on") or day, ro=ro)
    _update_km(db, truck["id"], values)
    if ro:
        return {"message": f"Schimbul la {truck['plate']} salvat la {km:,} km. Următorul la {values['service_due_km']:,} km.".replace(",", ".")}
    return {"message": f"Change of {truck['plate']} saved at {km:,} km. Next one at {values['service_due_km']:,} km.".replace(",", ".")}


def save_master(db, kind, data):
    if kind == "drivers":
        document = text(data, "dni_nie", False, 30).upper()
        if "dni_nie" not in data and data.get("id"):
            document = item(db, "drivers", data["id"])["dni_nie"]
        values = {"name": text(data, "name"), "phone": text(data, "phone", False, 60),
                  "dni_nie": document,
                  "carrier": text(data, "carrier", False) or "MalcomTrans",
                  "weekend_away": 1 if data.get("weekend_away") else 0,
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
        if not data.get("id"):  # a new place: "FERIMET S.L" is saved as "Ferimet S.L."
            values["company"], values["city"] = proper_name(values["company"], True), proper_name(values["city"], False)
        values.update(lat=None, lon=None, geo_label=text(data, "geo_label", False, 400))
        # Profile fields: an older open form may not send them, so keep what is saved.
        saved = item(db, "locations", data["id"]) if data.get("id") else {}
        for key, limit in (("kind", 40), ("contact_name", 200), ("phone", 60), ("email", 200), ("notes", 2000), ("nif", 30)):
            values[key] = text(data, key, False, limit) if key in data else saved.get(key, "")
        values["nif"] = values["nif"].upper().replace(" ", "")
        values["transport_company"] = (1 if data.get("transport_company") else 0) if "transport_company" in data \
            else saved.get("transport_company", 0)
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
        values.update(service_values(db, data))
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
