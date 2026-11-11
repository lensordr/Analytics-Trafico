"""Calendar-week availability derived from saved dispatch assignments."""
from datetime import datetime, time, timedelta
from planner import UTC, ZONE, parse_time, stamp
from storage import rows


def monday(day):
    return day - timedelta(days=day.weekday())


def midnight(day):
    return datetime.combine(day, time.min, ZONE).astimezone(UTC)


def subtract_bookings(start, end, bookings):
    cursor = start
    for busy_start, busy_end in sorted(bookings):
        if busy_end <= cursor or busy_start >= end:
            continue
        if busy_start > cursor:
            yield cursor, min(busy_start, end)
        cursor = max(cursor, busy_end)
        if cursor >= end:
            return
    if cursor < end:
        yield cursor, end


def weekly_availability(db, data, now=None):
    now = (now or datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
    try:
        offset, count = int(data.get("week_offset", 0)), int(data.get("week_count", 1))
        if offset not in (0, 1, 2) or count not in (1, 2) or offset + count > 3:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("Choose one or two weeks, from this week up to two weeks ahead.")
    start_day = monday(now.astimezone(ZONE).date()) + timedelta(weeks=offset)
    horizon_start, horizon_end = midnight(start_day), midnight(start_day+timedelta(weeks=count))
    orders = [o for o in rows(db, "orders") if o["status"] != "Cancelled"]
    drivers = {d["id"]: d for d in rows(db, "drivers")}
    locations = {loc["id"]: loc for loc in rows(db, "locations")}
    trucks = [t for t in rows(db, "trucks") if t["active"]]
    all_windows, unknown = [], []
    for truck in trucks:
        own_orders = [o for o in orders if o["truck_id"] == truck["id"]]
        latest_order = max(own_orders, key=lambda o:parse_time(o["pickup_at"]), default=None)
        driver_id = truck["driver_id"] or (latest_order["driver_id"] if latest_order else None)
        driver = drivers.get(driver_id)
        trailer = truck["trailer"] or (latest_order["trailer"] if latest_order else "")
        # Upcoming planning follows the tractor, independently of driver assignment.
        related = own_orders
        bookings = [(parse_time(o["pickup_at"]), parse_time(o["available_at"])) for o in related]
        anchors = [{"at":parse_time(o["available_at"]), "location_id":o["unloading_id"],
                    "source_order_id":o["id"], "source":o["number"]} for o in own_orders]
        if truck["location_id"] and truck["available_from"]:
            anchors.append({"at":parse_time(truck["available_from"]), "location_id":truck["location_id"],
                            "source_order_id":None, "source":"Reported fleet location"})
        anchors = sorted((a for a in anchors if a["location_id"] in locations), key=lambda a:a["at"])
        if not anchors:
            unknown.append({"truck_id":truck["id"], "plate":truck["plate"],
                            "reason":"Add a known location and availability time, or a loading order."})
            continue
        for index, anchor in enumerate(anchors):
            start = max(anchor["at"], now, horizon_start)
            end = min(anchors[index+1]["at"] if index+1 < len(anchors) else horizon_end, horizon_end)
            if start >= end:
                continue
            for free_start, free_end in subtract_bookings(start, end, bookings):
                if (free_end-free_start).total_seconds() < 7200:
                    continue
                next_jobs = [o for o in related if parse_time(o["pickup_at"]) == free_end]
                next_job = next_jobs[0] if next_jobs else None
                all_windows.append({"truck_id":truck["id"], "plate":truck["plate"], "trailer":trailer,
                                    "driver_id":driver["id"] if driver else None,
                                    "driver_name":driver["name"] if driver else "Driver not assigned",
                                    "carrier":driver["carrier"] if driver else "",
                                    "operation":truck["operation"], "location":locations[anchor["location_id"]],
                                    "source":anchor["source"], "source_order_id":anchor["source_order_id"],
                                    "known_since":stamp(anchor["at"]), "from":stamp(free_start), "until":stamp(free_end),
                                    "next_order":next_job["number"] if next_job else None})
    weeks = []
    for index in range(count):
        week_day = start_day + timedelta(weeks=index)
        week_start, week_end = midnight(week_day), midnight(week_day+timedelta(weeks=1))
        windows = []
        for window in all_windows:
            start = max(parse_time(window["from"]), week_start)
            end = min(parse_time(window["until"]), week_end)
            if (end-start).total_seconds() < 7200:
                continue
            windows.append({**window, "from":stamp(start), "until":stamp(end),
                            "hours":round((end-start).total_seconds()/3600, 1),
                            "week_end_boundary":end == week_end})
        windows.sort(key=lambda w:(parse_time(w["from"]),w["plate"]))
        available_ids = {w["truck_id"] for w in windows}
        unknown_ids = {t["truck_id"] for t in unknown}
        scheduled = [{"truck_id":t["id"], "plate":t["plate"]} for t in trucks
                     if t["id"] not in available_ids and t["id"] not in unknown_ids]
        weeks.append({"start":week_day.isoformat(), "end":(week_day+timedelta(days=6)).isoformat(),
                      "week_number":week_day.isocalendar().week, "year":week_day.isocalendar().year,
                      "windows":windows, "scheduled":scheduled})
    return {"weeks":weeks, "unknown":unknown, "active_trucks":len(trucks), "now":stamp(now),
            "week_offset":offset, "week_count":count}
