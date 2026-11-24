"""Driver page: opened from the WhatsApp link (/s/<token>), no login, big buttons.

Server-rendered HTML without JavaScript so it works on any phone. The driver sees only
their own order and can confirm only the next stop, in two taps (button, then "Yes").
"""
from html import escape
from urllib.parse import quote

from planner import ZONE, parse_time

TEXT = {
    "ro": {
        "title": "Cursa ta", "truck": "Camion", "trailer": "Remorcă", "ref": "Referință",
        "Loading": "Încărcare", "Unloading": "Descărcare", "Trailer swap": "Schimb remorcă",
        "extra_loading": "Încărcare suplimentară", "extra_unloading": "Descărcare parțială",
        "pickup": "Iei remorca încărcată", "do_pickup": "✅ AM LUAT REMORCA",
        "Tractor change": "Lași remorca (o ia alt camion)", "do_Tractor change": "✅ AM LĂSAT REMORCA",
        "do_Loading": "✅ AM ÎNCĂRCAT", "do_Unloading": "✅ AM DESCĂRCAT", "do_Trailer swap": "✅ AM SCHIMBAT REMORCA",
        "sure": "Confirmi?", "yes": "DA, CONFIRM", "back": "Înapoi",
        "planned": "Planificat", "done": "Confirmat", "map": "📍 Deschide în Google Maps",
        "route": "🗺️ Traseu complet", "material": "Marfă", "leave": "Lași", "take": "iei",
        "next": "Pasul următor", "finished": "✅ Cursa e finalizată. Mulțumim!",
        "cancelled": "Această cursă a fost anulată. Sună la birou.",
        "saved": "Mulțumim! Am primit confirmarea.", "missing": "Link invalid. Cere un link nou de la birou.",
        "help": "Dacă ai apăsat greșit, sună la birou.",
        "deca_title": "📄 DeCA (opțional)",
        "deca_intro": "Completează marfa, greutatea și DNI/NIE-ul tău pentru documentul de control. Poți sări peste: îl completează biroul.",
        "f_material": "Marfă (ex. chatarra, inox)", "f_weight": "Greutate încărcată (kg)", "f_dni": "DNI / NIE-ul tău",
        "deca_save": "SALVEAZĂ ȘI FĂ DeCA", "deca_skip": "Mai târziu", "deca_ready": "DeCA e gata. Arată-l de aici la control.",
        "deca_open": "📄 Deschide DeCA", "deca_fill": "📄 Completează DeCA",
        "deca_office": "Datele au fost salvate. DeCA îl termină biroul.",
        "bad_weight": "Greutatea trebuie să fie între 1 și 60000 kg.",
        "late": "⏱ Întârzii?",
        "late_help": "Spune-ne cu cât, biroul vede imediat:",
        "d1": "~1 oră",
        "d2": "~2 ore",
        "d4": "~4 ore",
        "tomorrow": "🌙 Ajung mâine dimineață",
        "eta_saved": "Mulțumim! Biroul a fost anunțat. Noua oră estimată: ",
    },
    "es": {
        "title": "Tu viaje", "truck": "Camión", "trailer": "Remolque", "ref": "Referencia",
        "Loading": "Carga", "Unloading": "Descarga", "Trailer swap": "Cambio de remolque",
        "extra_loading": "Carga adicional", "extra_unloading": "Descarga parcial",
        "pickup": "Recoges el remolque cargado", "do_pickup": "✅ HE COGIDO EL REMOLQUE",
        "Tractor change": "Dejas el remolque (lo lleva otro camión)", "do_Tractor change": "✅ HE DEJADO EL REMOLQUE",
        "do_Loading": "✅ HE CARGADO", "do_Unloading": "✅ HE DESCARGADO", "do_Trailer swap": "✅ HE CAMBIADO EL REMOLQUE",
        "sure": "¿Confirmas?", "yes": "SÍ, CONFIRMO", "back": "Volver",
        "planned": "Previsto", "done": "Confirmado", "map": "📍 Abrir en Google Maps",
        "route": "🗺️ Ruta completa", "material": "Mercancía", "leave": "Dejas", "take": "coges",
        "next": "Siguiente paso", "finished": "✅ Viaje terminado. ¡Gracias!",
        "cancelled": "Este viaje está cancelado. Llama a la oficina.",
        "saved": "¡Gracias! Hemos recibido la confirmación.", "missing": "Enlace no válido. Pide un enlace nuevo a la oficina.",
        "help": "Si te has equivocado, llama a la oficina.",
        "deca_title": "📄 DeCA (opcional)",
        "deca_intro": "Rellena la mercancía, el peso y tu DNI/NIE para el documento de control. Puedes saltarlo: lo completa la oficina.",
        "f_material": "Mercancía (p. ej. chatarra, inox)", "f_weight": "Peso cargado (kg)", "f_dni": "Tu DNI / NIE",
        "deca_save": "GUARDAR Y HACER DeCA", "deca_skip": "Más tarde", "deca_ready": "El DeCA está listo. Enséñalo desde aquí en un control.",
        "deca_open": "📄 Abrir DeCA", "deca_fill": "📄 Rellenar DeCA",
        "deca_office": "Datos guardados. La oficina completará el DeCA.",
        "bad_weight": "El peso debe estar entre 1 y 60000 kg.",
        "late": "⏱ ¿Llegas tarde?",
        "late_help": "Dinos cuánto, la oficina lo ve al momento:",
        "d1": "~1 hora",
        "d2": "~2 horas",
        "d4": "~4 horas",
        "tomorrow": "🌙 Llego mañana por la mañana",
        "eta_saved": "¡Gracias! La oficina ya lo sabe. Nueva hora estimada: ",
    },
    "ru": {
        "title": "Твой рейс", "truck": "Машина", "trailer": "Прицеп", "ref": "Референс",
        "Loading": "Загрузка", "Unloading": "Выгрузка", "Trailer swap": "Смена прицепа",
        "extra_loading": "Дополнительная загрузка", "extra_unloading": "Частичная выгрузка",
        "pickup": "Забираешь загруженный прицеп", "do_pickup": "✅ ПРИЦЕП ЗАБРАН",
        "Tractor change": "Оставляешь прицеп (его забирает другая машина)", "do_Tractor change": "✅ ПРИЦЕП ОСТАВЛЕН",
        "do_Loading": "✅ ЗАГРУЖЕНО", "do_Unloading": "✅ ВЫГРУЖЕНО", "do_Trailer swap": "✅ ПРИЦЕП ЗАМЕНЁН",
        "sure": "Подтверждаешь?", "yes": "ДА, ПОДТВЕРЖДАЮ", "back": "Назад",
        "planned": "По плану", "done": "Подтверждено", "map": "📍 Открыть в Google Maps",
        "route": "🗺️ Полный маршрут", "material": "Груз", "leave": "Оставляешь", "take": "берёшь",
        "next": "Следующий шаг", "finished": "✅ Рейс завершён. Спасибо!",
        "cancelled": "Этот рейс отменён. Позвони в офис.",
        "saved": "Спасибо! Подтверждение получено.", "missing": "Ссылка недействительна. Попроси новую ссылку в офисе.",
        "help": "Если ошибся, позвони в офис.",
        "deca_title": "📄 DeCA (необязательно)",
        "deca_intro": "Укажи груз, вес и свой DNI/NIE для контрольного документа. Можно пропустить: его заполнит офис.",
        "f_material": "Груз (напр. chatarra, inox)", "f_weight": "Загруженный вес (кг)", "f_dni": "Твой DNI / NIE",
        "deca_save": "СОХРАНИТЬ И СДЕЛАТЬ DeCA", "deca_skip": "Позже", "deca_ready": "DeCA готов. Покажи его отсюда при проверке.",
        "deca_open": "📄 Открыть DeCA", "deca_fill": "📄 Заполнить DeCA",
        "deca_office": "Данные сохранены. DeCA заполнит офис.",
        "bad_weight": "Вес должен быть от 1 до 60000 кг.",
        "late": "⏱ Опаздываешь?",
        "late_help": "Скажи, на сколько — офис сразу увидит:",
        "d1": "~1 час",
        "d2": "~2 часа",
        "d4": "~4 часа",
        "tomorrow": "🌙 Приеду завтра утром",
        "eta_saved": "Спасибо! Офис предупреждён. Новое ожидаемое время: ",
    },
    "uk": {
        "title": "Твій рейс", "truck": "Машина", "trailer": "Причіп", "ref": "Референс",
        "Loading": "Завантаження", "Unloading": "Розвантаження", "Trailer swap": "Заміна причепа",
        "extra_loading": "Додаткове завантаження", "extra_unloading": "Часткове розвантаження",
        "pickup": "Забираєш завантажений причіп", "do_pickup": "✅ ПРИЧІП ЗАБРАНО",
        "Tractor change": "Залишаєш причіп (його забирає інша машина)", "do_Tractor change": "✅ ПРИЧІП ЗАЛИШЕНО",
        "do_Loading": "✅ ЗАВАНТАЖЕНО", "do_Unloading": "✅ РОЗВАНТАЖЕНО", "do_Trailer swap": "✅ ПРИЧІП ЗАМІНЕНО",
        "sure": "Підтверджуєш?", "yes": "ТАК, ПІДТВЕРДЖУЮ", "back": "Назад",
        "planned": "За планом", "done": "Підтверджено", "map": "📍 Відкрити в Google Maps",
        "route": "🗺️ Повний маршрут", "material": "Вантаж", "leave": "Залишаєш", "take": "береш",
        "next": "Наступний крок", "finished": "✅ Рейс завершено. Дякуємо!",
        "cancelled": "Цей рейс скасовано. Зателефонуй в офіс.",
        "saved": "Дякуємо! Підтвердження отримано.", "missing": "Посилання недійсне. Попроси нове посилання в офісі.",
        "help": "Якщо помилився, зателефонуй в офіс.",
        "deca_title": "📄 DeCA (необов’язково)",
        "deca_intro": "Вкажи вантаж, вагу і свій DNI/NIE для контрольного документа. Можна пропустити: його заповнить офіс.",
        "f_material": "Вантаж (напр. chatarra, inox)", "f_weight": "Завантажена вага (кг)", "f_dni": "Твій DNI / NIE",
        "deca_save": "ЗБЕРЕГТИ І ЗРОБИТИ DeCA", "deca_skip": "Пізніше", "deca_ready": "DeCA готовий. Покажи його звідси під час перевірки.",
        "deca_open": "📄 Відкрити DeCA", "deca_fill": "📄 Заповнити DeCA",
        "deca_office": "Дані збережено. DeCA заповнить офіс.",
        "bad_weight": "Вага має бути від 1 до 60000 кг.",
        "late": "⏱ Запізнюєшся?",
        "late_help": "Скажи, на скільки — офіс одразу побачить:",
        "d1": "~1 година",
        "d2": "~2 години",
        "d4": "~4 години",
        "tomorrow": "🌙 Приїду завтра вранці",
        "eta_saved": "Дякуємо! Офіс попереджено. Новий очікуваний час: ",
    },
}
DAYS = {"ro": ["lun", "mar", "mie", "joi", "vin", "sâm", "dum"], "es": ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"],
        "ru": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"], "uk": ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]}
# Short names for the language switch at the top of the driver page.
LANG_NAMES = {"ro": "RO", "es": "ES", "ru": "RU", "uk": "UA"}


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


def _pickup(stop, order):
    """The driver picks up a loaded trailer here: first stop of a pick-up order, or the tractor change where
    this driver's part starts."""
    if stop["kind"] == "Tractor change":
        return bool(order) and order.get("part_first") == stop["seq"]
    return bool(order and order.get("trailer_pickup")) and stop["seq"] == 1 and stop["kind"] == "Loading"


def _label(stop, stops, t, order=None):
    if _pickup(stop, order):
        return t["pickup"]
    if stop["kind"] == "Loading" and stop["seq"] > 1:
        return t["extra_loading"]
    if stop["kind"] == "Unloading" and stop["seq"] < stops[-1]["seq"]:
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


def deca_form(base, stop, place, lang, dni):
    """Optional form shown after a loading is confirmed (no JavaScript: a plain form, then the page again)."""
    t = TEXT[lang]
    material = (stop.get("material") or "").split(" · ")[-1]
    weight = f'{stop["weight_kg"]:.0f}' if stop.get("weight_kg") else ""
    return (f'<section class="deca"><h2>{t["deca_title"]}</h2><p>{t["deca_intro"]}</p>'
            f'<p class="deca-place">{escape(place["company"])}</p>'
            f'<form method="post" action="{base}"><input type="hidden" name="action" value="deca">'
            f'<input type="hidden" name="seq" value="{stop["seq"]}"><input type="hidden" name="l" value="{lang}">'
            f'<label>{t["f_material"]}<input name="material" maxlength="120" value="{escape(material)}" autocomplete="off"></label>'
            f'<label>{t["f_weight"]}<input name="weight_kg" type="number" inputmode="numeric" min="1" max="60000" step="1" value="{weight}"></label>'
            f'<label>{t["f_dni"]}<input name="dni" maxlength="30" value="{escape(dni)}" autocomplete="off" autocapitalize="characters"></label>'
            f'<button class="action" type="submit">{t["deca_save"]}</button></form>'
            f'<a class="back" href="{base}?l={lang}">{t["deca_skip"]}</a></section>')


def render(token, order, stops, places, lang, confirm_seq=None, message="", error="", deca_seq=None, dni=""):
    t = TEXT[lang]
    base = f"/s/{escape(token)}"
    next_stop = next((s for s in stops if s["status"] != "Done"), None)
    head = (f'<header><div class="order">{escape(order["number"])}</div>'
            f'<div class="vehicle">{t["truck"]} <b>{escape(order["plate"])}</b>'
            + (f' · {t["trailer"]} <b>{escape(order["trailer"])}</b>' if order["trailer"] else "")
            + '</div><div class="langs">' + " ".join(f'<a class="lang" href="{base}?l={code}">{name}</a>'
                                                     for code, name in LANG_NAMES.items() if code != lang)
            + '</div></header>')
    parts = [head]
    if order.get("reference"):
        parts.append(f'<div class="reference">{t["ref"]}: <b>{escape(order["reference"])}</b></div>')
    if message:
        parts.append(f'<div class="message ok">{escape(message)}</div>')
    if error:
        parts.append(f'<div class="message warn">{escape(error)}</div>')
    deca_stop = next((s for s in stops if s["seq"] == deca_seq and s["kind"] == "Loading" and s["status"] == "Done"), None)
    if deca_stop and order["status"] != "Cancelled":
        parts.append(deca_form(base, deca_stop, places[deca_stop["location_id"]], lang, dni))
    if order["status"] == "Cancelled":
        parts.append(f'<div class="message warn">{t["cancelled"]}</div>')
    elif next_stop is None:
        parts.append(f'<div class="message ok big">{t["finished"]}</div>')
    else:
        place = places[next_stop["location_id"]]
        action = t["do_pickup"] if _pickup(next_stop, order) else t["do_" + next_stop["kind"]]
        if confirm_seq == next_stop["seq"]:
            control = (f'<form method="post" action="{base}"><input type="hidden" name="seq" value="{next_stop["seq"]}">'
                       f'<input type="hidden" name="l" value="{lang}"><p class="sure">{t["sure"]} {action[2:]}</p>'
                       f'<button class="action yes" type="submit">{t["yes"]}</button></form>'
                       f'<a class="back" href="{base}?l={lang}">{t["back"]}</a>')
        else:
            control = f'<a class="action" href="{base}?l={lang}&c={next_stop["seq"]}">{action}</a>'
        # Running late: the driver says by how much; the planning and the office are updated at once.
        late = "" if confirm_seq == next_stop["seq"] else (
            f'<details class="late"><summary>{t["late"]}</summary><p>{t["late_help"]}</p>'
            f'<form method="post" action="{base}"><input type="hidden" name="action" value="eta">'
            f'<input type="hidden" name="seq" value="{next_stop["seq"]}"><input type="hidden" name="l" value="{lang}">'
            + "".join(f'<button class="late-option" type="submit" name="delay" value="{key}">{t[label]}</button>'
                      for key, label in (("1", "d1"), ("2", "d2"), ("4", "d4"), ("tomorrow", "tomorrow")))
            + '</form></details>')
        parts.append(f'<section class="next"><div class="next-label">{t["next"]} · {_label(next_stop, stops, t, order)}</div>'
                     f'<div class="place">{escape(place["company"])}</div><div class="city">{escape(place["city"])}</div>'
                     f'{control}<a class="map" href="{maps_url(place)}">{t["map"]}</a>{late}</section>')
    # Once something is loaded: open the DeCA (to show at a control), or fill it in.
    loaded = [s for s in stops if s["kind"] == "Loading" and s["status"] == "Done"]
    # The driver who takes the trailer at a tractor change carries the load too: they can open the DeCA.
    carries = loaded or (order.get("part_first") or 1) > 1
    if carries and order["status"] != "Cancelled" and not deca_stop:
        if order.get("deca_id"):
            parts.append(f'<a class="map deca-link" href="/d/{escape(order["deca_id"])}">{t["deca_open"]}</a>')
        elif loaded:
            parts.append(f'<a class="map deca-link" href="{base}?l={lang}&deca={loaded[-1]["seq"]}">{t["deca_fill"]}</a>')
    items = []
    for stop in stops:
        place = places[stop["location_id"]]
        done = stop["status"] == "Done"
        lines = [f'<div class="stop-title">{"✓" if done else str(stop["seq"]) + "."} {_label(stop, stops, t, order)}</div>',
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
