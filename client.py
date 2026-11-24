"""Client order page (/c/<token>): Spanish / English / French, the client's own routes only, no prices."""
from html import escape

from clientlinks import DEFAULT_WEIGHT

TEXT = {
    "es": {
        "title": "Solicitud de transporte", "hello": "Hola", "intro": "Indica qué necesitas cargar. Te confirmamos el camión lo antes posible.",
        "from": "Punto de carga", "to": "Punto de descarga", "choose": "Elige…", "other": "Otra dirección (ruta nueva)",
        "other_help": "Empresa, dirección, población y código postal", "only_from": "Solo los destinos de esta carga",
        "search": "Buscar: empresa, población o código postal", "no_match": "Ninguna coincidencia. Usa «＋ Añadir ruta nueva».",
        "add_route": "＋ Añadir ruta nueva", "add_route_help": "¿No está en tu lista? Escribe la carga y la descarga nuevas.",
        "back_routes": "← Volver a mis rutas", "p_company": "Empresa", "p_address": "Dirección (calle y número / polígono)",
        "p_postal_code": "Código postal", "p_city": "Población", "p_country": "País", "p_country_default": "España",
        "usual": "Tus rutas habituales", "usual_help": "Toca una ruta para rellenar la carga y la descarga.",
        "load_date": "Día de carga", "load_time": "Hora o franja (opcional)", "load_time_ph": "p. ej. 08:00–12:00",
        "unload_by": "Descargar como tarde (opcional)", "trucks": "Número de camiones", "weight": "Peso aproximado",
        "weight_help": "24 t por defecto. Si es más o distinto, escríbelo (p. ej. 26 t, 2 × 24 t).",
        "material": "Material (opcional)", "reference": "Tu referencia / pedido (opcional)",
        "contact": "Persona y teléfono de contacto (opcional)", "notes": "Observaciones (opcional)", "send": "Enviar solicitud",
        "sent": "¡Solicitud enviada! La oficina la ha recibido y te confirmará el camión.",
        "yours": "Tus últimas solicitudes", "none": "Todavía no has enviado ninguna solicitud.",
        "status": {"New": "Recibida", "Planned": "Confirmada", "Rejected": "No podemos"},
        "trucks_short": "camión(es)", "new_route": "Ruta nueva", "truck": "Camión", "trailer": "remolque", "loads": "carga el",
        "errors": {"choose_place": "Elige el punto de carga y el de descarga de la lista.",
                   "other_place": "Ruta nueva: escribe empresa, dirección, código postal y población.",
                   "postal_code": "Ruta nueva: el código postal no es válido.",
                   "load_date": "Elige un día de carga válido (desde hoy hasta 4 meses).",
                   "unload_by": "El día de descarga no puede ser anterior al de carga.",
                   "trucks": "Número de camiones: de 1 a 30.",
                   "too_many": "Demasiadas solicitudes hoy. Llámanos o escríbenos por WhatsApp."},
        "missing": "Este enlace ya no funciona. Pide uno nuevo a la oficina.",
        "no_routes": "Aún no tienes rutas guardadas: escribe la carga y la descarga nuevas.",
    },
    "en": {
        "title": "Transport request", "hello": "Hello", "intro": "Tell us what you need to load. We confirm the truck as soon as possible.",
        "from": "Loading point", "to": "Unloading point", "choose": "Choose…", "other": "Other address (new route)",
        "other_help": "Company, address, town and postal code", "only_from": "Only the destinations of this loading",
        "search": "Search: company, town or postal code", "no_match": "No match. Use “＋ Add a new route”.",
        "add_route": "＋ Add a new route", "add_route_help": "Not on your list? Type the new loading and unloading.",
        "back_routes": "← Back to my routes", "p_company": "Company", "p_address": "Address (street and number / industrial estate)",
        "p_postal_code": "Postal code", "p_city": "Town", "p_country": "Country", "p_country_default": "Spain",
        "usual": "Your usual routes", "usual_help": "Tap a route to fill in the loading and unloading.",
        "load_date": "Loading day", "load_time": "Time or time window (optional)", "load_time_ph": "e.g. 08:00–12:00",
        "unload_by": "Unload at the latest (optional)", "trucks": "Number of trucks", "weight": "Approximate weight",
        "weight_help": "24 t by default. If it is more or different, type it (e.g. 26 t, 2 × 24 t).",
        "material": "Material (optional)", "reference": "Your reference / order (optional)",
        "contact": "Contact person and phone (optional)", "notes": "Notes (optional)", "send": "Send request",
        "sent": "Request sent! The office has it and will confirm the truck.",
        "yours": "Your latest requests", "none": "You have not sent any request yet.",
        "status": {"New": "Received", "Planned": "Confirmed", "Rejected": "We cannot"},
        "trucks_short": "truck(s)", "new_route": "New route", "truck": "Truck", "trailer": "trailer", "loads": "loads on",
        "errors": {"choose_place": "Choose the loading and the unloading point from the list.",
                   "other_place": "New route: type company, address, postal code and town.",
                   "postal_code": "New route: the postal code is not valid.",
                   "load_date": "Choose a valid loading day (from today up to 4 months).",
                   "unload_by": "The unloading day cannot be before the loading day.",
                   "trucks": "Number of trucks: from 1 to 30.",
                   "too_many": "Too many requests today. Call us or write on WhatsApp."},
        "missing": "This link no longer works. Ask the office for a new one.",
        "no_routes": "You have no saved routes yet: type the new loading and unloading.",
    },
    "fr": {
        "title": "Demande de transport", "hello": "Bonjour", "intro": "Indiquez ce que vous devez charger. Nous vous confirmons le camion au plus vite.",
        "from": "Lieu de chargement", "to": "Lieu de déchargement", "choose": "Choisir…", "other": "Autre adresse (nouvelle route)",
        "other_help": "Société, adresse, ville et code postal", "only_from": "Seulement les destinations de ce chargement",
        "search": "Rechercher : société, ville ou code postal", "no_match": "Aucun résultat. Utilisez « ＋ Ajouter une nouvelle route ».",
        "add_route": "＋ Ajouter une nouvelle route", "add_route_help": "Pas dans votre liste ? Saisissez le nouveau chargement et le nouveau déchargement.",
        "back_routes": "← Retour à mes routes", "p_company": "Société", "p_address": "Adresse (rue et numéro / zone industrielle)",
        "p_postal_code": "Code postal", "p_city": "Ville", "p_country": "Pays", "p_country_default": "France",
        "usual": "Vos routes habituelles", "usual_help": "Touchez une route pour remplir le chargement et le déchargement.",
        "load_date": "Jour de chargement", "load_time": "Heure ou créneau (facultatif)", "load_time_ph": "ex. 08:00–12:00",
        "unload_by": "Déchargement au plus tard (facultatif)", "trucks": "Nombre de camions", "weight": "Poids approximatif",
        "weight_help": "24 t par défaut. Si c'est plus ou différent, indiquez-le (ex. 26 t, 2 × 24 t).",
        "material": "Matière (facultatif)", "reference": "Votre référence / commande (facultatif)",
        "contact": "Personne et téléphone de contact (facultatif)", "notes": "Remarques (facultatif)", "send": "Envoyer la demande",
        "sent": "Demande envoyée ! Le bureau l'a reçue et vous confirmera le camion.",
        "yours": "Vos dernières demandes", "none": "Vous n'avez encore envoyé aucune demande.",
        "status": {"New": "Reçue", "Planned": "Confirmée", "Rejected": "Impossible"},
        "trucks_short": "camion(s)", "new_route": "Nouvelle route", "truck": "Camion", "trailer": "remorque", "loads": "charge le",
        "errors": {"choose_place": "Choisissez le lieu de chargement et celui de déchargement dans la liste.",
                   "other_place": "Nouvelle route : indiquez société, adresse, code postal et ville.",
                   "postal_code": "Nouvelle route : le code postal n'est pas valide.",
                   "load_date": "Choisissez un jour de chargement valide (d'aujourd'hui à 4 mois).",
                   "unload_by": "Le jour de déchargement ne peut pas être avant celui du chargement.",
                   "trucks": "Nombre de camions : de 1 à 30.",
                   "too_many": "Trop de demandes aujourd'hui. Appelez-nous ou écrivez-nous sur WhatsApp."},
        "missing": "Ce lien ne fonctionne plus. Demandez-en un nouveau au bureau.",
        "no_routes": "Vous n'avez pas encore de routes enregistrées : saisissez le nouveau chargement et le nouveau déchargement.",
    },
}


def lang_of(value, accept=""):
    if value in TEXT:
        return value
    first = accept.lower()[:2]
    return first if first in ("en", "fr") else "es"


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
        search = lambda p: escape(" ".join(str(p.get(k) or "") for k in ("company", "city", "postal_code", "country")))
        return "".join(f'<option value="{pid}"{" selected" if str(pid) == chosen else ""}{extra} data-search="{search(place)}">'
                       f'{escape(_label(place))}{" · " + escape(place["postal_code"]) if place.get("postal_code") else ""}</option>'
                       for pid, place, extra in items)

    from_items = [(pid, place, "") for pid, place in sorted(froms.items(), key=lambda kv: _label(kv[1]).casefold())]
    to_items = [(pid, entry["place"], f' data-from="{" ".join(str(f) for f in sorted(entry["from"]))}"')
                for pid, entry in sorted(tos.items(), key=lambda kv: _label(kv[1]["place"]).casefold())]
    chosen_from, chosen_to = str(form.get("from_id", "")), str(form.get("to_id", ""))

    def place_field(key, label, items, chosen):
        other = chosen == "other" or not items
        finder = (f'<div class="finder" data-for="{key}"><input type="search" placeholder="{t["search"]}" autocomplete="off" '
                  f'aria-label="{t["search"]}"><div class="matches" hidden></div><p class="no-match" hidden>{t["no_match"]}</p></div>') if len(items) > 6 else ""
        side = key[:-3]
        box = lambda name, extra="": (f'<label class="field"><span>{t["p_" + name]}</span><input name="{side}_{name}" '
                                      f'value="{value(side + "_" + name, t["p_country_default"] if name == "country" else "")}"{extra}></label>')
        return (f'<div class="field"><span class="label">{label}</span>{finder}<select name="{key}" id="{key}" required aria-label="{label}">'
                f'<option value="">{t["choose"]}</option>{options(items, chosen)}'
                f'<option value="other"{" selected" if other else ""}>{t["other"]}</option></select></div>'
                f'<fieldset class="new-place" data-other="{key}"{"" if other else " hidden"}><legend>{label} · {t["new_route"].lower()}</legend>'
                + box("company", ' maxlength="200" autocomplete="organization" data-need')
                + box("address", ' maxlength="200" autocomplete="street-address" data-need')
                + '<div class="row">' + box("postal_code", ' maxlength="12" autocomplete="postal-code" data-need')
                + box("city", ' maxlength="120" autocomplete="address-level2" data-need') + '</div>'
                + box("country", ' maxlength="60" autocomplete="country-name"') + '</fieldset>')

    history = "".join(
        f'<li><span class="badge {r["status"].lower()}">{t["status"].get(r["status"], r["status"])}</span>'
        f'<strong>{escape(_day(r["load_date"]))}</strong>{f" · {r['trucks']} {t['trucks_short']}" if r["trucks"] > 1 else ""}<br><small>{escape(r["route"])}</small>'
        + (f'<br><small class="reason">{escape(r["reason"])}</small>' if r["status"] == "Rejected" and r.get("reason") else "")
        + "".join(f'<div class="vehicle">🚛 {t["truck"]} <strong>{escape(v["plate"])}</strong>'
                  + (f' + {t["trailer"]} <strong>{escape(v["trailer"])}</strong>' if v["trailer"] else "")
                  + f' · {t["loads"]} {_day(v["day"])}</div>' for v in r.get("vehicles", []))
        + "</li>" for r in requests) or f'<li class="muted">{t["none"]}</li>'
    usual = sorted((r for r in routes if r["trips"]), key=lambda r: -r["trips"])[:6]
    usual_html = (f'<section class="card usual"><h2>{t["usual"]}</h2><p class="hint">{t["usual_help"]}</p>'
                  + "".join(f'<button type="button" class="route-pick" data-from="{r["from"]["id"]}" data-to="{r["to"]["id"]}">'
                            f'<strong>{escape(r["from"]["company"])}</strong> <small>{escape(r["from"]["city"])}</small>'
                            f'<span>→</span><strong>{escape(r["to"]["company"])}</strong> <small>{escape(r["to"]["city"])}</small></button>'
                            for r in usual) + '</section>') if usual else ""
    langs = " ".join(f'<a class="lang{" on" if code == lang else ""}" href="{base}?l={code}">{code.upper()}</a>' for code in TEXT)
    body = (
        f'<header><div><div class="brand">MalcomTrans</div><h1>{t["title"]}</h1></div><nav class="langs">{langs}</nav></header>'
        f'<p class="hello">{t["hello"]}, <strong>{escape(client["company"])}</strong>{"" if client["company"].rstrip().endswith(".") else "."} {t["intro"]}</p>'
        + (f'<p class="message ok">{escape(message)}</p>' if message else "")
        + (f'<p class="message warn">{escape(error)}</p>' if error else "")
        + ("" if routes else f'<p class="message info">{t["no_routes"]}</p>')
        + usual_html
        + f'<form method="post" action="{base}" class="card" id="request-form"><input type="hidden" name="l" value="{lang}">'
        + f'<div class="add-route"><button type="button" class="add-route-btn" id="add-route">{t["add_route"]}</button>'
          f'<button type="button" class="back-routes" id="back-routes" hidden>{t["back_routes"]}</button>'
          f'<p class="hint">{t["add_route_help"]}</p></div>'
        + place_field("from_id", t["from"], from_items, chosen_from)
        + place_field("to_id", t["to"], to_items, chosen_to)
        + f'<p class="hint" id="to-hint" hidden>{t["only_from"]}</p>'
        + '<div class="row">'
        + f'<label class="field"><span>{t["load_date"]}</span><input type="date" name="load_date" required min="{today}" value="{value("load_date", today)}"></label>'
        + f'<label class="field"><span>{t["load_time"]}</span><input name="load_time" maxlength="40" placeholder="{t["load_time_ph"]}" value="{value("load_time")}"></label>'
        + '</div>'
        + f'<label class="field"><span>{t["notes"]}</span><textarea name="notes" rows="3" maxlength="1000">{value("notes")}</textarea></label>'
        + f'<button class="send" type="submit">{t["send"]}</button></form>'
        + f'<section class="card"><h2>{t["yours"]}</h2><ul class="history">{history}</ul></section>')
    return _page(lang, t["title"], body)
