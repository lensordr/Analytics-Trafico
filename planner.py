"""Conservative single-driver EU planning; not a tachograph/compliance system."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
try:
    ZONE = ZoneInfo("Europe/Madrid")
except ZoneInfoNotFoundError:
    raise RuntimeError("Timezone data missing. Run: python -m pip install tzdata")


def parse_time(value):
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            aware = dt.replace(tzinfo=ZONE)
            if aware.astimezone(UTC).astimezone(ZONE).replace(tzinfo=None) != dt:
                raise ValueError("This time does not exist due to daylight saving time.")
            if aware.utcoffset() != dt.replace(tzinfo=ZONE, fold=1).utcoffset():
                raise ValueError("This hour is ambiguous due to daylight saving. Choose another hour.")
            dt = aware
        return dt.astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid date/time: {value}. Use the dispatch clock (Europe/Madrid). {exc}")


def stamp(dt):
    return dt.astimezone(ZONE).isoformat(timespec="minutes")


def week_key(dt):
    day = dt.astimezone(ZONE).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def number(data, key, default, low, high):
    try:
        raw = data.get(key)
        # An empty form field means "not entered": use the default (None = required).
        value = float(default if raw in (None, "") else raw)
        if not low <= value <= high:
            raise ValueError()
        return value
    except (TypeError, ValueError):
        raise ValueError(f"{key.replace('_', ' ')} must be between {low} and {high}.")


class Clock:
    def __init__(self, start, initial=None, state=None):
        self.t = start
        self.events = []
        if state:
            self.day = state["day"]
            self.block = state["block"]
            self.duty = state["duty"]
            self.weeks = dict(state["weeks"])
            self.weekly_due = parse_time(state["weekly_due"])
        else:
            data = initial or {}
            self.day = round(number(data, "drive_today", 0, 0, 9) * 60)
            self.block = round(number(data, "drive_since_break", 0, 0, 4.5) * 60)
            self.duty = round(number(data, "duty_today", 0, 0, 13) * 60)
            current = round(number(data, "drive_week", 0, 0, 56) * 60)
            previous = round(number(data, "drive_previous_week", 0, 0, 56) * 60)
            if self.block > self.day or self.day > self.duty or self.day > current:
                raise ValueError("Driving clock must satisfy: since break ≤ today ≤ duty today, and today ≤ this week.")
            if current + previous > 5400:
                raise ValueError("This week + previous week cannot exceed 90 hours.")
            self.weeks = {week_key(start): current, week_key(start - timedelta(days=7)): previous}
            self.weekly_due = start + timedelta(hours=number(data, "weekly_rest_due_in", 144, 0, 144))

    def state(self):
        return {"day": self.day, "block": self.block, "duty": self.duty,
                "weeks": self.weeks, "weekly_due": stamp(self.weekly_due), "at": stamp(self.t)}

    def event(self, kind, minutes):
        if minutes <= 0:
            return
        start = self.t
        self.t += timedelta(minutes=minutes)
        if self.events and self.events[-1]["kind"] == kind and self.events[-1]["end"] == stamp(start):
            self.events[-1]["end"] = stamp(self.t)
            self.events[-1]["minutes"] += minutes
        else:
            self.events.append({"kind": kind, "start": stamp(start), "end": stamp(self.t), "minutes": minutes})

    def rest(self, hours=11, kind="Daily rest"):
        if hours < 45 and self.t + timedelta(hours=hours) > self.weekly_due:
            hours, kind = 45, "Weekly rest"
        self.event(kind, round(hours * 60))
        self.day = self.block = self.duty = 0
        if hours >= 45:
            self.weekly_due = self.t + timedelta(hours=144)

    def advance_idle(self, until):
        """Unassigned time is only credited as rest by explicit planning assumption."""
        minutes = max(0, round((until - self.t).total_seconds() / 60))
        if minutes >= 2700:
            self.event("Assumed weekly rest while unassigned", minutes)
            self.day = self.block = self.duty = 0
            self.weekly_due = self.t + timedelta(hours=144)
        elif minutes >= 660:
            self.event("Assumed daily rest while unassigned", minutes)
            self.day = self.block = self.duty = 0
        elif minutes >= 45:
            self.event("Unassigned break (assumed)", minutes)
            self.block = 0
            self.duty += minutes
        else:
            self.event("Unassigned time", minutes)
            self.duty += minutes

    def run(self, kind, minutes):
        """Minute-resolution simulation includes ISO-week rollover and weekly rest."""
        minutes = round(minutes)
        for _ in range(minutes):
            for attempt in range(10):
                current = week_key(self.t)
                previous = week_key(self.t - timedelta(days=7))
                weekly = self.weeks.get(current, 0)
                fortnight = weekly + self.weeks.get(previous, 0)
                if self.t >= self.weekly_due:
                    self.rest(45, "Weekly rest")
                    continue
                if kind == "Driving" and (weekly >= 3360 or fortnight >= 5400):
                    local = self.t.astimezone(ZONE)
                    monday = (local.date() + timedelta(days=7-local.weekday()))
                    next_week = datetime.combine(monday, datetime.min.time(), ZONE).astimezone(UTC)
                    hours = max(45, (next_week-self.t).total_seconds()/3600)
                    self.rest(hours, "Weekly-limit rest")
                    continue
                if self.duty >= 780 or (kind == "Driving" and self.day >= 540):
                    self.rest()
                    continue
                if kind == "Driving" and self.block >= 270:
                    # Prefer daily rest when a 45-minute break would exhaust the duty window.
                    if self.t + timedelta(minutes=45) > self.weekly_due:
                        self.rest(45, "Weekly rest")
                    elif self.duty + 45 >= 780:
                        self.rest()
                    else:
                        self.event("Driving break", 45)
                        self.block = 0
                        self.duty += 45
                    continue
                break
            else:
                raise ValueError("Could not resolve driving clock; check initial hours.")
            self.event(kind, 1)
            self.duty += 1
            if kind == "Driving":
                self.day += 1
                self.block += 1
                self.weeks[current] = self.weeks.get(current, 0) + 1


def route_plan(start, stops, initial=None, state=None):
    """Plan a journey of ordered stops: [{kind, handling_hours, leg_hours}], first = loading.

    `leg_hours` is the pure driving time from the previous stop; rests and breaks are added.
    Each stop gets its arrival, handling start (after any rest) and handling end."""
    clock = Clock(start, initial, state)
    loading = round(stops[0]["handling_hours"] * 60)
    if loading and (clock.duty + loading > 780 or start + timedelta(minutes=loading) > clock.weekly_due):
        raise ValueError("Rest is due before loading can finish. Move loading later and update the driving clock.")
    times, departure = [], start
    for index, stop in enumerate(stops):
        if index:
            clock.run("Driving", round(stop.get("leg_hours", 0) * 60))
        arrival = clock.t
        before = len(clock.events)
        clock.run(stop["kind"], round(stop["handling_hours"] * 60))
        handling = [event for event in clock.events[before:] if event["kind"] == stop["kind"]]
        begin = parse_time(handling[0]["start"]) if handling else arrival
        times.append({"arrival_at": stamp(arrival), "start_at": stamp(begin), "end_at": stamp(clock.t)})
        if index == 0:
            departure = clock.t
    ready = clock.t
    return {"departure_at": stamp(departure), "arrival_at": times[-1]["arrival_at"],
            "unloading_at": times[-1]["start_at"], "available_at": stamp(ready),
            "elapsed_hours": round((ready-start).total_seconds()/3600, 2),
            "rest_hours": round(sum(e["minutes"] for e in clock.events if "rest" in e["kind"].lower() or "break" in e["kind"].lower())/60, 2),
            "timeline": clock.events, "end_state": clock.state(), "stops": times}


def trip_plan(start, driving_hours, loading_hours=1, unloading_hours=1, initial=None, state=None):
    """Classic two-stop trip: loading, driving, unloading."""
    return route_plan(start, [{"kind": "Loading", "handling_hours": loading_hours},
                              {"kind": "Unloading", "handling_hours": unloading_hours, "leg_hours": driving_hours}],
                      initial, state)
