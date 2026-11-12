"""Documento de Control (DeCA) for a loading order: data checks, a QR code with the document data,
and a one-page PDF built with the standard library (Helvetica, WinAnsi).

Mandatory content (Orden FOM/2861/2012): shipper and effective carrier with NIF, origin and destination,
nature and weight of the goods, vehicle registration and date. The QR code holds the same data as text,
so it can be read offline by any phone (the app is local: a link to it would not open elsewhere).
"""
from datetime import datetime
import secrets

from planner import UTC, ZONE, parse_time, stamp
import qr

SETTINGS = {"carrier_name": "", "carrier_nif": ""}
COUNTRY_ES = {"spain": "España", "france": "Francia", "italy": "Italia", "germany": "Alemania",
              "portugal": "Portugal", "netherlands": "Países Bajos", "belgium": "Bélgica",
              "luxembourg": "Luxemburgo", "poland": "Polonia", "slovakia": "Eslovaquia", "switzerland": "Suiza"}


def settings(db):
    result = dict(SETTINGS)
    for row in db.execute("SELECT key, value FROM settings"):
        if row["key"] in result:
            result[row["key"]] = row["value"]
    return result


def save_settings(db, data):
    values = {}
    for key in SETTINGS:
        value = str(data.get(key, "")).strip()
        if len(value) > 200:
            raise ValueError("Company name and NIF: maximum 200 characters.")
        values[key] = value.upper().replace(" ", "") if key == "carrier_nif" else value
    for key, value in values.items():
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))
    return {"message": "Company details saved.", "settings": values}


def _row(db, sql, *args):
    row = db.execute(sql, args).fetchone()
    return {key.lower(): row[key] for key in row.keys()} if row else None


def _order(db, order_id):
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        raise ValueError("Choose an order.")
    order = _row(db, "SELECT * FROM orders WHERE id=?", order_id)
    if not order:
        raise ValueError("The order no longer exists. Refresh and try again.")
    return order


def _kg(value):
    text = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def _place(location):
    return f"{location['company']} ({location['city']})" if location["city"] else location["company"]


def _domicile(location):
    country = COUNTRY_ES.get((location["country"] or "").strip().lower(), location["country"])
    address = (location["address"] or "").strip()
    if address and location["postal_code"] and location["postal_code"] in address:
        return f"{address}, {country}"
    parts = [address, f"{location['postal_code']} {location['city']}".strip(), country]
    return ", ".join(part for part in parts if part)


def _material_name(db, stop):
    if stop.get("material_id"):
        row = _row(db, "SELECT name FROM materials WHERE id=?", stop["material_id"])
        if row:
            return row["name"]
    return (stop.get("material") or "").split(" · ")[-1]


def document(db, order_id, shipper_id=None, notes=None):
    """All DeCA fields for an order, or ValueError listing what is missing."""
    order = _order(db, order_id)
    if order["status"] == "Cancelled":
        raise ValueError("This order is cancelled.")
    stops = [{key.lower(): row[key] for key in row.keys()}
             for row in db.execute("SELECT * FROM order_stops WHERE order_id=? ORDER BY seq", (order["id"],))]
    places = {}
    for stop in stops:
        places[stop["location_id"]] = _row(db, "SELECT * FROM locations WHERE id=?", stop["location_id"])
    shipper_id = shipper_id or order["deca_shipper_id"] or order["loading_id"]
    shipper = _row(db, "SELECT * FROM locations WHERE id=?", int(shipper_id))
    if not shipper:
        raise ValueError("Choose the shipper (cargador) from Saved locations.")
    driver = _row(db, "SELECT * FROM drivers WHERE id=?", order["driver_id"]) or {"name": order["driver_name"], "dni_nie": ""}
    company = settings(db)
    missing = []
    if not company["carrier_name"] or not company["carrier_nif"]:
        missing.append("Add your company name and NIF in Settings & backup.")
    if not shipper["nif"]:
        missing.append(f"Add the NIF of {shipper['company']} in Saved locations.")
    lines, loaded, carried = [], [], 0.0
    for stop in stops:
        if stop["kind"] == "Trailer swap":
            continue
        place = places[stop["location_id"]]
        if stop["kind"] == "Loading":
            name = _material_name(db, stop)
            if not name:
                missing.append(f"Stop {stop['seq']} ({place['company']}): choose the material loaded.")
            if not stop["weight_kg"]:
                missing.append(f"Stop {stop['seq']} ({place['company']}): enter the weight loaded (kg).")
            if name and stop["weight_kg"]:
                loaded.append(name)
                carried += stop["weight_kg"]
                lines.append(("Carga", f"{_place(place)} — {name}, {_kg(stop['weight_kg'])} kg"))
        else:
            names = " + ".join(dict.fromkeys(loaded)) or "—"
            unloaded = stop["weight_kg"] if stop["weight_kg"] else carried
            carried = max(0.0, carried - unloaded)
            lines.append(("Descarga", f"{_place(place)} — {names}, {_kg(unloaded)} kg" if unloaded else f"{_place(place)} — {names}"))
    if missing:
        raise ValueError(" ".join(dict.fromkeys(missing)))
    trailers = [order["trailer"]] if order["trailer"] else []
    trailers += [stop["pick_trailer"] for stop in stops if stop["kind"] == "Trailer swap" and stop["pick_trailer"]]
    created = parse_time(order["deca_created_at"]) if order["deca_created_at"] else datetime.now(UTC)
    return {
        "id": order["deca_id"] or "",
        "order": order,
        "shipper_id": shipper["id"],
        "rows": [
            ("Identificador", order["deca_id"] or "(se asigna al generar)"),
            ("Tipo de transporte", "Transporte de mercancías"),
            ("Cargador", f"{shipper['company']} (NIF {shipper['nif']})"),
            ("Domicilio cargador", _domicile(shipper)),
            ("Transportista Efectivo", f"{company['carrier_name']} (NIF {company['carrier_nif']})"),
            ("Conductor", f"{driver['name']} (NIF {driver['dni_nie']})" if driver.get("dni_nie") else driver["name"]),
            ("Fecha de realización", parse_time(order["pickup_at"]).astimezone(ZONE).strftime("%d/%m/%Y")),
            *[(f"{number}. {kind}", value) for number, (kind, value) in enumerate(lines, start=1)],
            ("Matrícula tractora", order["plate"]),
            ("Remolque", " → ".join(trailers) or "—"),
        ],
        "notes": (order["deca_notes"] if notes is None else notes) or "—",
        "created": created.astimezone(ZONE).strftime("%d/%m/%Y %H:%M"),
    }


def prepare(db, data):
    """Save the shipper and observations, assign the identifier once, and return the PDF address."""
    order = _order(db, data.get("order_id"))
    notes = str(data.get("notes", "")).strip()
    if len(notes) > 500:
        raise ValueError("Observaciones: maximum 500 characters.")
    shipper_id = data.get("shipper_id") or order["loading_id"]
    document(db, order["id"], shipper_id, notes)  # raises with everything that is missing
    deca_id = order["deca_id"] or secrets.token_hex(16)
    created = order["deca_created_at"] or stamp(datetime.now(UTC).replace(second=0, microsecond=0))
    db.execute("UPDATE orders SET deca_id=?, deca_shipper_id=?, deca_notes=?, deca_created_at=? WHERE id=?",
               (deca_id, int(shipper_id), notes, created, order["id"]))
    return {"id": deca_id, "url": f"/api/deca.pdf?order={order['id']}"}


def qr_text(doc):
    lines = ["Documento de Control (DeCA)"] + [f"{label}: {value}" for label, value in doc["rows"]]
    if doc["notes"] != "—":
        lines.append(f"Observaciones: {doc['notes']}")
    lines.append(f"Fecha de creación: {doc['created']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ PDF
def _pdf_text(value):
    raw = str(value).replace("→", "->").encode("cp1252", "replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _wrap(value, width):
    words, lines, line = str(value).split(), [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    return lines + [line] if line else [""]


def render(doc):
    ops = []
    text = lambda font, size, x, y, value: ops.append(b"BT /%s %d Tf %.2f %.2f Td (%s) Tj ET" % (font, size, x, y, _pdf_text(value)))
    text(b"F2", 16, 50, 800, "Documento de Control (DeCA)")
    y = 774
    for label, value in doc["rows"]:
        text(b"F2", 10, 50, y, label + ":")
        for index, part in enumerate(_wrap(value, 68)):
            text(b"F1", 10, 190, y - index * 13, part)
        y -= 18 + 13 * (len(_wrap(value, 68)) - 1)
    ops.append(b"0.5 w 50 %.2f m 545 %.2f l S" % (y + 11, y + 11))
    text(b"F2", 10, 50, y - 4, "Observaciones:")
    note_lines = _wrap(doc["notes"], 68)
    for index, part in enumerate(note_lines):
        text(b"F1", 10, 190, y - 4 - index * 13, part)
    y -= 4 + 13 * len(note_lines) + 14
    text(b"F1", 8, 50, y, f"Fecha de creación: {doc['created']}")
    modules = qr.matrix(qr_text(doc))
    size = len(modules)
    side = float(max(170, min(240, size * 3)))  # about 1 mm per module: easy to scan from a phone
    cell = side / size
    left, top = (595.28 - side) / 2, y - 24
    ops.append(b"0 g")
    for row_index, row in enumerate(modules):
        x = 0
        while x < size:
            if row[x]:
                start = x
                while x < size and row[x]:
                    x += 1
                ops.append(b"%.3f %.3f %.3f %.3f re" % (left + start * cell, top - (row_index + 1) * cell, (x - start) * cell, cell))
            else:
                x += 1
    ops.append(b"f")
    content = b"\n".join(ops)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Title (%s) /Producer (MalcomTrans) >>" % _pdf_text("DeCA " + doc["id"]),
    ]
    out, offsets = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 7 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def pdf(db, order_id):
    order = _order(db, order_id)
    if not order["deca_id"]:
        raise ValueError("Generate the DeCA from the order first.")
    doc = document(db, order["id"])
    return f"DeCA-{order['number']}.pdf", render(doc)
