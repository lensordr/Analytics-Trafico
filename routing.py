"""Optional HERE truck routing. Keys stay server-side, in the environment."""
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


KEY_FILE = Path(__file__).resolve().parent / "data" / "here_api_key.txt"


def api_key():
    value = os.environ.get("HERE_API_KEY", "").strip()
    if value:
        return value
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def configured():
    return bool(api_key())


def request(endpoint, query):
    key = api_key()
    if not key:
        raise ValueError("HERE is not configured. Set HERE_API_KEY and restart, or use manual kilometres and driving hours.")
    query = {**query, "apiKey": key}
    try:
        with urlopen(endpoint + "?" + urlencode(query), timeout=25) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read(16000))
            detail = str(payload.get("cause") or payload.get("title") or "").strip()
        except (ValueError, TypeError, AttributeError, OSError):
            pass
        detail = detail.replace(key, "[redacted]")
        detail = re.sub(r"https?://\S+", "[request URL omitted]", detail)
        detail = " ".join(detail.split())[:700]
        if exc.code in (401, 403):
            message = "HERE authentication or service access failed. Check the API key and service permissions."
        elif exc.code == 429:
            message = "HERE request limit reached. Check usage limits or try again later."
        elif exc.code == 400:
            message = "HERE rejected a route/address parameter (HTTP 400)."
        else:
            message = f"HERE service request failed (HTTP {exc.code})."
        raise ValueError(message + (" " + detail if detail else "")) from None
    except (URLError, TimeoutError, json.JSONDecodeError, OSError):
        raise ValueError("HERE could not be reached. Try again or use manual routing.") from None


def geocode(query):
    data = request("https://geocode.search.hereapi.com/v1/geocode", {"q": query, "limit": 5})
    return [{"label": item.get("address", {}).get("label", item.get("title", "")),
             "lat": item["position"]["lat"], "lon": item["position"]["lng"],
             "result_type": item.get("resultType", "unknown")}
            for item in data.get("items", []) if "position" in item]


def routing_departure(value):
    """HERE requires RFC 3339 seconds, even when the planning UI uses minutes."""
    if value == "any":
        return value
    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError()
        return moment.isoformat(timespec="seconds")
    except (ValueError, TypeError):
        raise ValueError("The route departure time must include a valid date and timezone.") from None


def truck_route(origin, destination, truck, departure):
    if any(point.get("lat") is None or point.get("lon") is None for point in (origin, destination)):
        raise ValueError("Both locations need confirmed coordinates. Use Find address or enter coordinates in Locations.")
    data = request("https://router.hereapi.com/v8/routes", {
        "transportMode": "truck", "routingMode": "fast", "return": "summary",
        "origin": f"{origin['lat']},{origin['lon']}",
        "destination": f"{destination['lat']},{destination['lon']}",
        "departureTime": routing_departure(departure),
        "vehicle[height]": round(truck["height"]*100),
        "vehicle[width]": round(truck["width"]*100),
        "vehicle[length]": round(truck["length"]*100),
        "vehicle[grossWeight]": round(truck["weight"]),
        "vehicle[currentWeight]": round(truck["weight"]),
        "vehicle[trailerCount]": 1 if truck.get("trailer") else 0,
    })
    routes = data.get("routes", [])
    if not routes or not routes[0].get("sections"):
        raise ValueError("HERE did not return a truck route.")
    route = routes[0]
    notices = route.get("notices", []) + [n for section in route["sections"] for n in section.get("notices", [])]
    if any(n.get("severity") == "critical" for n in notices):
        raise ValueError("HERE reported a critical route restriction. Check the locations and vehicle profile.")
    distance = sum(section["summary"]["length"] for section in route["sections"])/1000
    hours = sum(section["summary"]["duration"] for section in route["sections"])/3600
    return {"distance_km": round(distance, 1), "driving_hours": round(hours, 4),
            "source": "HERE truck", "notices": [n.get("title", n.get("code", "Route notice")) for n in notices],
            "profile": {key: truck[key] for key in ("height", "width", "length", "weight")}}


def air_distance(a, b):
    if any(point.get("lat") is None or point.get("lon") is None for point in (a, b)):
        return None
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat, dlon = lat2-lat1, math.radians(b["lon"]-a["lon"])
    h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return round(6371*2*math.asin(min(1, math.sqrt(h))), 1)
