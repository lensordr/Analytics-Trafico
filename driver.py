"""Driver page: opened from the WhatsApp link (/s/<token>), no login, big buttons.

Server-rendered HTML without JavaScript so it works on any phone. The driver sees only
their own order and can confirm only the next stop, in two taps (button, then "Yes").
"""
from html import escape
from urllib.parse import quote

from planner import ZONE, parse_time

TEXT = {
    "ro": {
        "title": "Cursa ta", "truck": "Camion", "trailer": "Remorcă",
        "Loading": "Încărcare", "Unloading": "Descărcare", "Trailer swap": "Schimb remorcă",
        "extra_loading": "Încărcare suplimentară", "extra_unloading": "Descărcare parțială",
        "do_Loading": "✅ AM ÎNCĂRCAT", "do_Unloading": "✅ AM DESCĂRCAT", "do_Trailer swap": "✅ AM SCHIMBAT REMORCA",
        "sure": "Confirmi?", "yes": "DA, CONFIRM", "back": "Înapoi",
        "planned": "Planificat", "done": "Confirmat", "map": "📍 Deschide în Google Maps",
        "route": "🗺️ Traseu complet", "material": "Marfă", "leave": "Lași", "take": "iei",
        "next": "Pasul următor", "finished": "✅ Cursa e finalizată. Mulțumim!",
        "cancelled": "Această cursă a fost anulată. Sună la birou.",
        "saved": "Mulțumim! Am primit confirmarea.", "missing": "Link invalid. Cere un link nou de la birou.",
        "help": "Dacă ai apăsat greșit, sună la birou.", "other": "Español",
    },
    "es": {
        "title": "Tu viaje", "truck": "Camión", "trailer": "Remolque",
        "Loading": "Carga", "Unloading": "Descarga", "Trailer swap": "Cambio de remolque",
        "extra_loading": "Carga adicional", "extra_unloading": "Descarga parcial",
        "do_Loading": "✅ HE CARGADO", "do_Unloading": "✅ HE DESCARGADO", "do_Trailer swap": "✅ HE CAMBIADO EL REMOLQUE",
        "sure": "¿Confirmas?", "yes": "SÍ, CONFIRMO", "back": "Volver",
        "planned": "Previsto", "done": "Confirmado", "map": "📍 Abrir en Google Maps",
        "route": "🗺️ Ruta completa", "material": "Mercancía", "leave": "Dejas", "take": "coges",
        "next": "Siguiente paso", "finished": "✅ Viaje terminado. ¡Gracias!",
        "cancelled": "Este viaje está cancelado. Llama a la oficina.",
        "saved": "¡Gracias! Hemos recibido la confirmación.", "missing": "Enlace no válido. Pide un enlace nuevo a la oficina.",
        "help": "Si te has equivocado, llama a la oficina.", "other": "Română",
    },
}
DAYS = {"ro": ["lun", "mar", "mie", "joi", "vin", "sâm", "dum"], "es": ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]}


def lang_of(value):
    return value if value in TEXT else "ro"


def _when(value, lang):
    moment = parse_time(value).astimezone(ZONE)
    return f"{DAYS[lang][moment.weekday()]} {moment:%d.%m %H:%M}"


def _point(place):
    if place.get("lat") is not None and place.get("lon") is not None:
        return f"{float(place['lat']):.6f},{float(place['lon']):.6f}"
    return ", ".join(str(place.get(key) or "") for key in ("company", "address", "postal_code", "city", "country") if place.get(key))


def maps_url(place):
    return "https://www.google.com/maps/search/?api=1&query=" + quote(_point(place))


def route_url(places):
    points = [_point(place) for place in places]
    if len(points) < 2:
        return ""
    url = f"https://www.google.com/maps/dir/?api=1&origin={quote(points[0])}&destination={quote(points[-1])}"
    if len(points) > 2:
        url += "&waypoints=" + quote("|".join(points[1:-1][:9]))
    return url + "&travelmode=driving"


def _label(stop, stops, t):
    if stop["kind"] == "Loading" and stop["seq"] > 1:
        return t["extra_loading"]
    if stop["kind"] == "Unloading" and stop["seq"] < len(stops):
        return t["extra_unloading"]
    return t[stop["kind"]]


def _kg(value):
    return f"{value:,.0f} kg".replace(",", ".")


def page(body, lang, title):
    return f"""<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>{escape(title)}</title><link rel="icon" href="data:,"><link rel="stylesheet" href="/driver.css"></head>
<body class="driver">{body}</body></html>"""


def missing_page(lang):
    t = TEXT[lang]
    return page(f'<main><div class="message warn">{t["missing"]}</div></main>', lang, t["title"])


def render(token, order, stops, places, lang, confirm_seq=None, message="", error=""):
    t = TEXT[lang]
    base = f"/s/{escape(token)}"
    next_stop = next((s for s in stops if s["status"] != "Done"), None)
    head = (f'<header><div class="order">{escape(order["number"])}</div>'
            f'<div class="vehicle">{t["truck"]} <b>{escape(order["plate"])}</b>'
            + (f' · {t["trailer"]} <b>{escape(order["trailer"])}</b>' if order["trailer"] else "")
            + f'</div><a class="lang" href="{base}?l={"es" if lang == "ro" else "ro"}">{t["other"]}</a></header>')
    parts = [head]
    if message:
        parts.append(f'<div class="message ok">{escape(message)}</div>')
    if error:
        parts.append(f'<div class="message warn">{escape(error)}</div>')
    if order["status"] == "Cancelled":
        parts.append(f'<div class="message warn">{t["cancelled"]}</div>')
    elif next_stop is None:
        parts.append(f'<div class="message ok big">{t["finished"]}</div>')
    else:
        place = places[next_stop["location_id"]]
        action = t["do_" + next_stop["kind"]]
        if confirm_seq == next_stop["seq"]:
            control = (f'<form method="post" action="{base}"><input type="hidden" name="seq" value="{next_stop["seq"]}">'
                       f'<input type="hidden" name="l" value="{lang}"><p class="sure">{t["sure"]} {action[2:]}</p>'
                       f'<button class="action yes" type="submit">{t["yes"]}</button></form>'
                       f'<a class="back" href="{base}?l={lang}">{t["back"]}</a>')
        else:
            control = f'<a class="action" href="{base}?l={lang}&c={next_stop["seq"]}">{action}</a>'
        parts.append(f'<section class="next"><div class="next-label">{t["next"]} · {_label(next_stop, stops, t)}</div>'
                     f'<div class="place">{escape(place["company"])}</div><div class="city">{escape(place["city"])}</div>'
                     f'{control}<a class="map" href="{maps_url(place)}">{t["map"]}</a></section>')
    items = []
    for stop in stops:
        place = places[stop["location_id"]]
        done = stop["status"] == "Done"
        lines = [f'<div class="stop-title">{"✓" if done else str(stop["seq"]) + "."} {_label(stop, stops, t)}</div>',
                 f'<div class="stop-place">{escape(place["company"])}</div>']
        address = place.get("address") or f'{place.get("postal_code", "")} {place.get("city", "")}'
        lines.append(f'<div class="stop-address">{escape(address)}</div>')
        if stop["kind"] == "Trailer swap":
            lines.append(f'<div>{t["leave"]} <b>{escape(stop["drop_trailer"] or "—")}</b>, {t["take"]} <b>{escape(stop["pick_trailer"])}</b></div>')
        goods = [stop.get("material", "").split(" · ")[-1] if stop.get("material") else "", _kg(stop["weight_kg"]) if stop.get("weight_kg") else ""]
        if any(goods):
            lines.append(f'<div>{t["material"]}: {escape(" · ".join(g for g in goods if g))}</div>')
        lines.append(f'<div class="stop-time">{t["done"] + " " + _when(stop["done_at"], lang) if done and stop["done_at"] else t["planned"] + " " + _when(stop["planned_at"], lang)}</div>')
        lines.append(f'<a class="map small" href="{maps_url(place)}">{t["map"]}</a>')
        items.append(f'<li class="{"done" if done else ""}">{"".join(lines)}</li>')
    parts.append(f'<ol class="stops">{"".join(items)}</ol>')
    route = route_url([places[s["location_id"]] for s in stops])
    if route:
        parts.append(f'<a class="map route" href="{route}">{t["route"]}</a>')
    parts.append(f'<p class="help">{t["help"]}</p>')
    return page("<main>" + "".join(parts) + "</main>", lang, f'{order["number"]} · {t["title"]}')
