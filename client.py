"""Client order page (/c/<token>): Spanish / English, the client's own routes only, no prices."""
from html import escape

from clientlinks import DEFAULT_WEIGHT

TEXT = {
    "es": {
        "title": "Solicitud de transporte", "hello": "Hola", "intro": "Indica qué necesitas cargar. Te confirmamos el camión lo antes posible.",
        "from": "Punto de carga", "to": "Punto de descarga", "choose": "Elige…", "other": "Otra dirección (escribir)",
        "other_help": "Empresa, dirección, población y código postal", "only_from": "Solo los destinos de esta carga",
        "load_date": "Día de carga", "load_time": "Hora o franja (opcional)", "load_time_ph": "p. ej. 08:00–12:00",
        "unload_by": "Descargar como tarde (opcional)", "trucks": "Número de camiones", "weight": "Peso aproximado",
        "weight_help": "24 t por defecto. Si es más o distinto, escríbelo (p. ej. 26 t, 2 × 24 t).",
        "material": "Material (opcional)", "reference": "Tu referencia / pedido (opcional)",
        "contact": "Persona y teléfono de contacto (opcional)", "notes": "Observaciones (opcional)", "send": "Enviar solicitud",
        "sent": "¡Solicitud enviada! La oficina la ha recibido y te confirmará el camión.",
        "yours": "Tus últimas solicitudes", "none": "Todavía no has enviado ninguna solicitud.",
        "status": {"New": "Recibida", "Planned": "Confirmada", "Rejected": "No podemos"},
        "trucks_short": "camión(es)", "new_route": "Ruta nueva",
        "errors": {"choose_place": "Elige el punto de carga y el de descarga de la lista.",
                   "other_place": "Escribe la dirección completa de la otra carga o descarga.",
                   "load_date": "Elige un día de carga válido (desde hoy hasta 4 meses).",
                   "unload_by": "El día de descarga no puede ser anterior al de carga.",
                   "trucks": "Número de camiones: de 1 a 30.",
                   "too_many": "Demasiadas solicitudes hoy. Llámanos o escríbenos por WhatsApp."},
        "missing": "Este enlace ya no funciona. Pide uno nuevo a la oficina.",
        "no_routes": "Aún no tienes rutas guardadas: escribe la carga y la descarga en «Otra dirección».",
    },
    "en": {
        "title": "Transport request", "hello": "Hello", "intro": "Tell us what you need to load. We confirm the truck as soon as possible.",
        "from": "Loading point", "to": "Unloading point", "choose": "Choose…", "other": "Other address (type it)",
        "other_help": "Company, address, town and postal code", "only_from": "Only the destinations of this loading",
        "load_date": "Loading day", "load_time": "Time or time window (optional)", "load_time_ph": "e.g. 08:00–12:00",
        "unload_by": "Unload at the latest (optional)", "trucks": "Number of trucks", "weight": "Approximate weight",
        "weight_help": "24 t by default. If it is more or different, type it (e.g. 26 t, 2 × 24 t).",
        "material": "Material (optional)", "reference": "Your reference / order (optional)",
        "contact": "Contact person and phone (optional)", "notes": "Notes (optional)", "send": "Send request",
        "sent": "Request sent! The office has it and will confirm the truck.",
        "yours": "Your latest requests", "none": "You have not sent any request yet.",
        "status": {"New": "Received", "Planned": "Confirmed", "Rejected": "We cannot"},
        "trucks_short": "truck(s)", "new_route": "New route",
        "errors": {"choose_place": "Choose the loading and the unloading point from the list.",
                   "other_place": "Type the full address of the other loading or unloading.",
                   "load_date": "Choose a valid loading day (from today up to 4 months).",
                   "unload_by": "The unloading day cannot be before the loading day.",
                   "trucks": "Number of trucks: from 1 to 30.",
                   "too_many": "Too many requests today. Call us or write on WhatsApp."},
        "missing": "This link no longer works. Ask the office for a new one.",
        "no_routes": "You have no saved routes yet: type the loading and unloading under “Other address”.",
    },
}


def lang_of(value, accept=""):
    if value in TEXT:
        return value
    return "en" if accept.lower().startswith("en") else "es"


def _page(lang, title, body):
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">'
            f'<title>{escape(title)} · MalcomTrans</title><link rel="icon" href="data:,"><link rel="stylesheet" href="/client.css">'
            f'<script src="/client.js" defer></script></head><body class="client"><main>{body}</main></body></html>')


def missing_page(lang):
    return _page(lang, TEXT[lang]["title"], f'<p class="message warn">{TEXT[lang]["missing"]}</p>')


def _day(value):
    parts = str(value).split("-")
    return f"{parts[2]}/{parts[1]}/{parts[0]}" if len(parts) == 3 else str(value)


def _label(place):
    return f'{place["company"]} · {place["city"]}' + ("" if place["country"] in ("Spain", "España") else f' ({place["country"]})')


def render(token, client, routes, requests, lang, today, message="", error="", form=None):
    t, form = TEXT[lang], form or {}
    base = f"/c/{token}"
    value = lambda key, default="": escape(str(form.get(key, default)))
    froms = {r["from"]["id"]: r["from"] for r in routes}
    tos = {}
    for r in routes:
        tos.setdefault(r["to"]["id"], {"place": r["to"], "from": set()})["from"].add(r["from"]["id"])

    def options(items, chosen):
        return "".join(f'<option value="{pid}"{" selected" if str(pid) == chosen else ""}{extra}>{escape(_label(place))}</option>'
                       for pid, place, extra in items)

    from_items = [(pid, place, "") for pid, place in sorted(froms.items(), key=lambda kv: _label(kv[1]).casefold())]
    to_items = [(pid, entry["place"], f' data-from="{" ".join(str(f) for f in sorted(entry["from"]))}"')
                for pid, entry in sorted(tos.items(), key=lambda kv: _label(kv[1]["place"]).casefold())]
    chosen_from, chosen_to = str(form.get("from_id", "")), str(form.get("to_id", ""))

    def place_field(key, label, items, chosen):
        other = chosen == "other" or not items
        return (f'<label class="field"><span>{label}</span><select name="{key}" id="{key}" required>'
                f'<option value="">{t["choose"]}</option>{options(items, chosen)}'
                f'<option value="other"{" selected" if other else ""}>{t["other"]}</option></select></label>'
                f'<label class="field other" data-other="{key}"{"" if other else " hidden"}><span>{t["other_help"]}</span>'
                f'<textarea name="{key.replace("_id", "_other")}" rows="2">{value(key.replace("_id", "_other"))}</textarea></label>')

    history = "".join(
        f'<li><span class="badge {r["status"].lower()}">{t["status"].get(r["status"], r["status"])}</span>'
        f'<strong>{escape(_day(r["load_date"]))}</strong> · {r["trucks"]} {t["trucks_short"]}<br><small>{escape(r["route"])}</small>'
        + (f'<br><small class="reason">{escape(r["reason"])}</small>' if r["status"] == "Rejected" and r.get("reason") else "")
        + "</li>" for r in requests) or f'<li class="muted">{t["none"]}</li>'
    langs = " ".join(f'<a class="lang{" on" if code == lang else ""}" href="{base}?l={code}">{code.upper()}</a>' for code in TEXT)
    body = (
        f'<header><div><div class="brand">MalcomTrans</div><h1>{t["title"]}</h1></div><nav class="langs">{langs}</nav></header>'
        f'<p class="hello">{t["hello"]}, <strong>{escape(client["company"])}</strong>{"" if client["company"].rstrip().endswith(".") else "."} {t["intro"]}</p>'
        + (f'<p class="message ok">{escape(message)}</p>' if message else "")
        + (f'<p class="message warn">{escape(error)}</p>' if error else "")
        + ("" if routes else f'<p class="message info">{t["no_routes"]}</p>')
        + f'<form method="post" action="{base}" class="card"><input type="hidden" name="l" value="{lang}">'
        + place_field("from_id", t["from"], from_items, chosen_from)
        + place_field("to_id", t["to"], to_items, chosen_to)
        + f'<p class="hint" id="to-hint" hidden>{t["only_from"]}</p>'
        + '<div class="row">'
        + f'<label class="field"><span>{t["load_date"]}</span><input type="date" name="load_date" required min="{today}" value="{value("load_date", today)}"></label>'
        + f'<label class="field"><span>{t["load_time"]}</span><input name="load_time" maxlength="40" placeholder="{t["load_time_ph"]}" value="{value("load_time")}"></label>'
        + '</div><div class="row">'
        + f'<label class="field"><span>{t["trucks"]}</span><input type="number" name="trucks" min="1" max="30" required value="{value("trucks", 1)}"></label>'
        + f'<label class="field"><span>{t["weight"]}</span><input name="weight" maxlength="60" value="{value("weight", DEFAULT_WEIGHT)}"></label>'
        + f'</div><p class="hint">{t["weight_help"]}</p>'
        + f'<label class="field"><span>{t["unload_by"]}</span><input type="date" name="unload_by" min="{today}" value="{value("unload_by")}"></label>'
        + f'<label class="field"><span>{t["material"]}</span><input name="material" maxlength="120" value="{value("material")}"></label>'
        + f'<label class="field"><span>{t["reference"]}</span><input name="reference" maxlength="80" value="{value("reference")}"></label>'
        + f'<label class="field"><span>{t["contact"]}</span><input name="contact" maxlength="120" value="{value("contact")}"></label>'
        + f'<label class="field"><span>{t["notes"]}</span><textarea name="notes" rows="3" maxlength="1000">{value("notes")}</textarea></label>'
        + f'<button class="send" type="submit">{t["send"]}</button></form>'
        + f'<section class="card"><h2>{t["yours"]}</h2><ul class="history">{history}</ul></section>')
    return _page(lang, t["title"], body)
