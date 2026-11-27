"""Billing: delivered trips waiting for an invoice (Pending) and the invoices made from them (Done).

One invoice = one client + one tractor + one month (month of the delivery). MalcomTrans issues every invoice:
for its own trucks in its own name, and for the trucks of the autónomos who work through it "en nombre y por
cuenta de" the autónomo (facturación por terceros), with the autónomo's name, NIF, series, IVA and IRPF.
Who issues is found from the carrier of the order: a carrier with its own issuer record, else MalcomTrans.

The weight of every trip is entered by hand here; the price comes from the order's rate (copied from the
client's tariff), so per-tonne prices follow the weight. Km, material, rate and the line total can be changed
before the invoice is made. An invoice keeps a copy of everything printed on it, so later changes never alter
it; a cancelled invoice keeps its number and puts its trips back to Pending.
"""
from datetime import date, datetime
import json

from common import item, text
from deca import settings as company_settings, _pdf_text, _wrap
from planner import UTC, ZONE, number, parse_time, stamp
from tariffs import MIN_BILLED_KG

BASIS_LABEL = {"loading_t": "€/t cargada", "unloading_t": "€/t descargada", "trip": "€/viaje", "hour": "€/hora"}


def _issuer_row(row):
    return {key.lower(): row[key] for key in row.keys()}


def issuers(db):
    """Who can issue invoices: MalcomTrans itself (made on first use) and the autónomos set up for it."""
    if not db.execute("SELECT 1 FROM billing_issuers WHERE own=1").fetchone():
        company = company_settings(db)
        db.execute("INSERT INTO billing_issuers (carrier, name, nif, series, own) VALUES ('MalcomTrans', ?, ?, 'MT', 1)",
                   (company["carrier_name"] or "MalcomTrans", company["carrier_nif"]))
    return [_issuer_row(row) for row in db.execute("SELECT * FROM billing_issuers ORDER BY own DESC, name COLLATE NOCASE")]


def save_issuer(db, data):
    values = {"carrier": text(data, "carrier", True, 200), "name": text(data, "name", True, 200),
              "nif": text(data, "nif", True, 30).upper().replace(" ", ""), "address": text(data, "address", False, 200),
              "postal_code": text(data, "postal_code", False, 12), "city": text(data, "city", False, 120),
              "country": text(data, "country", False, 60) or "España", "iban": text(data, "iban", False, 40).upper(),
              "series": text(data, "series", True, 12).upper().replace(" ", ""),
              "iva_pct": number(data, "iva_pct", 21, 0, 30), "irpf_pct": number(data, "irpf_pct", 0, 0, 30)}
    if data.get("id"):
        current = _issuer(db, data["id"])
        if current["own"]:
            values["carrier"] = "MalcomTrans"
        clash = db.execute("SELECT 1 FROM billing_issuers WHERE (carrier=? COLLATE NOCASE OR series=?) AND id!=?",
                           (values["carrier"], values["series"], current["id"])).fetchone()
        if clash:
            raise ValueError("Another issuer already uses this carrier or series.")
        db.execute("UPDATE billing_issuers SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                   (*values.values(), current["id"]))
        return {"id": current["id"], "message": f"{values['name']} saved."}
    if db.execute("SELECT 1 FROM billing_issuers WHERE carrier=? COLLATE NOCASE OR series=?",
                  (values["carrier"], values["series"])).fetchone():
        raise ValueError("Another issuer already uses this carrier or series.")
    cursor = db.execute("INSERT INTO billing_issuers (" + ",".join(values) + ") VALUES (" + ",".join("?" * len(values)) + ")",
                        tuple(values.values()))
    return {"id": cursor.lastrowid, "message": f"{values['name']} saved."}


def _issuer(db, issuer_id):
    try:
        row = db.execute("SELECT * FROM billing_issuers WHERE id=?", (int(issuer_id),)).fetchone()
    except (TypeError, ValueError):
        row = None
    if row is None:
        raise ValueError("This issuer no longer exists. Refresh and try again.")
    return _issuer_row(row)


def _issuer_for(carrier, all_issuers):
    own = next(i for i in all_issuers if i["own"])
    return next((i for i in all_issuers if not i["own"] and i["carrier"].casefold() == (carrier or "").casefold()), own)


def line_amount(line):
    """Total of one trip: a total typed by hand, else the rate on the weight (at least 24 t) or per trip."""
    if line["amount_manual"] is not None:
        return round(line["amount_manual"], 2)
    rate, basis = line["rate"], line["basis"]
    if rate is None or not basis:
        return None
    if basis == "trip":
        return round(rate, 2)
    if basis in ("loading_t", "unloading_t"):
        if not line["weight_kg"]:
            return None
        return round(max(line["weight_kg"], MIN_BILLED_KG) / 1000 * rate, 2)
    return line["price_eur"]  # per hour: the total entered on the order


def _lines(db, where, args=()):
    rows = db.execute(
        "SELECT o.*, f.city AS from_city, f.company AS from_company, d.city AS to_city, d.company AS to_company, "
        "c.company AS client_name FROM orders o JOIN locations f ON f.id=o.loading_id JOIN locations d ON d.id=o.unloading_id "
        f"LEFT JOIN locations c ON c.id=o.client_id WHERE {where} ORDER BY o.delivered_at, o.id", args).fetchall()
    materials = {}
    for stop in db.execute("SELECT order_id, material FROM order_stops WHERE kind='Loading' AND material!='' ORDER BY order_id, seq"):
        materials.setdefault(stop["order_id"], stop["material"].split(" · ")[-1])
    result = []
    for row in rows:
        o = {key.lower(): row[key] for key in row.keys()}
        day = parse_time(o["delivered_at"] or o["unloading_at"]).astimezone(ZONE).date()
        line = {"order_id": o["id"], "number": o["number"], "day": day.isoformat(), "month": day.isoformat()[:7],
                "client_id": o["client_id"], "client": o["client_name"] or "", "truck_id": o["truck_id"], "plate": o["plate"],
                "carrier": o["carrier"], "from": f"{o['from_company']} ({o['from_city']})", "to": f"{o['to_company']} ({o['to_city']})",
                "reference": o.get("reference") or "", "unload_reference": o.get("unload_reference") or "",
                "km": o["bill_km"] if o["bill_km"] is not None else round(o["distance_km"] or 0),
                "material": o["bill_material"] if o["bill_material"] is not None else materials.get(o["id"], ""),
                "weight_kg": o["bill_weight_kg"], "basis": o["rate_basis"] or "",
                "rate": o["bill_rate"] if o["bill_rate"] is not None else o["rate"], "price_eur": o["price_eur"],
                "amount_manual": o["bill_amount"], "invoice_id": o["invoice_id"]}
        line["amount"] = line_amount(line)
        line["missing"] = [label for label, bad in (("weight", not line["weight_kg"]), ("price", line["amount"] is None),
                                                    ("client", not line["client_id"])) if bad]
        result.append(line)
    return result


def billing_state(db, data=None):
    """Pending groups (client + tractor + month) and the invoices, for the Billing page."""
    all_issuers = issuers(db)
    groups = {}
    for line in _lines(db, "o.status='Completed' AND o.invoice_id IS NULL"):
        issuer = _issuer_for(line["carrier"], all_issuers)
        key = (line["client_id"] or 0, line["truck_id"], line["month"], issuer["id"])
        group = groups.setdefault(key, {"key": "-".join(map(str, key)), "client_id": line["client_id"], "client": line["client"],
                                        "truck_id": line["truck_id"], "plate": line["plate"], "month": line["month"],
                                        "issuer_id": issuer["id"], "lines": []})
        group["lines"].append(line)
    for group in groups.values():
        group["base"] = round(sum(l["amount"] or 0 for l in group["lines"]), 2)
        group["ready"] = bool(group["client_id"]) and not any(l["missing"] for l in group["lines"])
    invoices = [{key.lower(): row[key] for key in row.keys() if key.lower() not in ("lines", "issuer_data", "client_data")}
                for row in db.execute("SELECT * FROM invoices ORDER BY issue_date DESC, id DESC LIMIT 500")]
    return {"issuers": all_issuers, "pending": sorted(groups.values(), key=lambda g: (g["month"], g["client"].casefold(), g["plate"])),
            "invoices": invoices}


def save_line(db, data):
    """The billing data of one trip (weight, km, material, rate, total), before its invoice is made."""
    order = item(db, "orders", data.get("order_id"))
    if order["invoice_id"]:
        raise ValueError(f"{order['number']} is already on an invoice. Cancel the invoice to change it.")
    if order["status"] != "Completed":
        raise ValueError(f"{order['number']} is not delivered yet.")
    optional = lambda key, low, high: number(data, key, None, low, high) if data.get(key) not in (None, "") else None
    values = {"bill_weight_kg": optional("weight_kg", 1, 60000), "bill_km": optional("km", 0, 15000),
              "bill_material": text(data, "material", False, 120) if "material" in data else None,
              "bill_rate": optional("rate", 0, 100000), "bill_amount": optional("amount", 0, 1000000)}
    db.execute("UPDATE orders SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?", (*values.values(), order["id"]))
    line = _lines(db, "o.id=?", (order["id"],))[0]
    return {"line": line, "message": f"{order['number']} saved."}


def _money(value):
    return round(value + 1e-9, 2)


def create_invoice(db, data, user=""):
    """The invoice of a Pending group: a number in the issuer's series, the taxes, a copy of every line."""
    try:
        ids = sorted({int(value) for value in data.get("order_ids") or []})
    except (TypeError, ValueError):
        ids = []
    if not ids:
        raise ValueError("Choose the trips of the invoice.")
    lines = _lines(db, f"o.id IN ({','.join('?' * len(ids))})", ids)
    if len(lines) != len(ids):
        raise ValueError("A trip of this invoice no longer exists. Refresh and try again.")
    for line in lines:
        if line["invoice_id"]:
            raise ValueError(f"{line['number']} is already on an invoice.")
        if line["missing"]:
            raise ValueError(f"{line['number']}: enter the {' and '.join(line['missing'])} first.")
    all_issuers = issuers(db)
    keys = {(l["client_id"], l["truck_id"], l["month"], _issuer_for(l["carrier"], all_issuers)["id"]) for l in lines}
    if len(keys) != 1:
        raise ValueError("An invoice has one client, one tractor and one month.")
    client_id, truck_id, month, issuer_id = keys.pop()
    issuer, own = _issuer(db, issuer_id), next(i for i in all_issuers if i["own"])
    client = item(db, "locations", client_id)
    if not issuer["nif"] or not issuer["address"] or not issuer["city"]:
        raise ValueError(f"Add the NIF and address of {issuer['name'] or issuer['carrier']} in Billing › Issuers first: an invoice needs them.")
    if not client.get("nif") or not (client.get("address") or client.get("city")):
        raise ValueError(f"Add the NIF and address of {client['company']} (Clients) first: an invoice needs them.")
    issue_day = str(data.get("issue_date") or date.today().isoformat())[:10]
    try:
        issue_day = date.fromisoformat(issue_day).isoformat()
    except ValueError:
        raise ValueError("Invoice date: use the format YYYY-MM-DD.") from None
    iva_pct = number(data, "iva_pct", issuer["iva_pct"], 0, 30)
    irpf_pct = number(data, "irpf_pct", issuer["irpf_pct"], 0, 30)
    year = issue_day[:4]
    seq = (db.execute("SELECT max(seq) FROM invoices WHERE issuer_id=? AND year=?", (issuer["id"], year)).fetchone()[0] or 0) + 1
    invoice_number = f"{issuer['series']}-{year}-{seq:04d}"
    base = _money(sum(line["amount"] for line in lines))
    iva, irpf = _money(base * iva_pct / 100), _money(base * irpf_pct / 100)
    total = _money(base + iva - irpf)
    issuer_data = {k: issuer[k] for k in ("name", "nif", "address", "postal_code", "city", "country", "iban", "own")}
    if not issuer["own"]:
        issuer_data["on_behalf_by"] = {"name": own["name"], "nif": own["nif"]}
    client_data = {k: client.get(k) or "" for k in ("company", "nif", "address", "postal_code", "city", "country")}
    printed = [{k: line[k] for k in ("number", "day", "from", "to", "reference", "unload_reference", "km", "material",
                                     "weight_kg", "basis", "rate", "amount")} for line in lines]
    cursor = db.execute(
        "INSERT INTO invoices (issuer_id, series, year, seq, number, issue_date, month, client_id, client, truck_id, plate, "
        "base, iva_pct, iva, irpf_pct, irpf, total, notes, lines, issuer_data, client_data, status, created_at, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'Issued',?,?)",
        (issuer["id"], issuer["series"], year, seq, invoice_number, issue_day, month, client_id, client["company"], truck_id,
         lines[0]["plate"], base, iva_pct, iva, irpf_pct, irpf, total, text(data, "notes", False, 500), json.dumps(printed),
         json.dumps(issuer_data), json.dumps(client_data), stamp(datetime.now(UTC)), user))
    invoice_id = cursor.lastrowid
    db.execute(f"UPDATE orders SET invoice_id=? WHERE id IN ({','.join('?' * len(ids))})", (invoice_id, *ids))
    return {"id": invoice_id, "number": invoice_number,
            "message": f"Invoice {invoice_number} made: {len(ids)} trip{'s' if len(ids) > 1 else ''}, total {total:.2f} €."}


def cancel_invoice(db, data, user=""):
    """Cancel an invoice: it keeps its number (marked cancelled) and its trips go back to Pending."""
    row = _invoice(db, data.get("id"))
    if row["status"] == "Cancelled":
        raise ValueError(f"{row['number']} is already cancelled.")
    db.execute("UPDATE invoices SET status='Cancelled', cancelled_at=?, cancelled_by=? WHERE id=?",
               (stamp(datetime.now(UTC)), user, row["id"]))
    db.execute("UPDATE orders SET invoice_id=NULL WHERE invoice_id=?", (row["id"],))
    return {"message": f"{row['number']} cancelled; its trips are Pending again."}


def _invoice(db, invoice_id):
    try:
        row = db.execute("SELECT * FROM invoices WHERE id=?", (int(invoice_id),)).fetchone()
    except (TypeError, ValueError):
        row = None
    if row is None:
        raise ValueError("This invoice no longer exists.")
    return {key.lower(): row[key] for key in row.keys()}


# ------------------------------------------------------------------ PDF
def _eur(value):
    whole, cents = f"{value:,.2f}".split(".")
    return whole.replace(",", ".") + "," + cents + " €"


def _kg(value):
    return f"{value:,.0f}".replace(",", ".") + " kg" if value else ""


def _day(value):
    return f"{value[8:10]}/{value[5:7]}/{value[:4]}"


def invoice_pdf(db, invoice_id):
    """(file name, PDF) of an invoice, from its own copy of the data."""
    inv = _invoice(db, invoice_id)
    issuer, client, lines = json.loads(inv["issuer_data"]), json.loads(inv["client_data"]), json.loads(inv["lines"])
    pages, ops = [], []
    W = 595.28

    def text(font, size, x, y, value, right=False):
        value = str(value)
        if right:
            x -= len(value) * size * (0.53 if font == b"F2" else 0.5)
        ops.append(b"BT /%s %d Tf %.2f %.2f Td (%s) Tj ET" % (font, size, x, y, _pdf_text(value)))

    def party(x, y, title, data, name_key):
        text(b"F2", 8, x, y, title)
        rows = [data.get(name_key, ""), f"NIF: {data.get('nif', '')}", data.get("address", ""),
                " ".join(p for p in (data.get("postal_code", ""), data.get("city", "")) if p), data.get("country", "")]
        for index, value in enumerate(r for r in rows if r):
            text(b"F2" if index == 0 else b"F1", 10 if index == 0 else 9, x, y - 14 - index * 12, value)

    def header():
        text(b"F2", 18, 50, 795, "FACTURA" + (" (ANULADA)" if inv["status"] == "Cancelled" else ""))
        text(b"F1", 10, 50, 778, f"Nº {inv['number']}   ·   Fecha: {_day(inv['issue_date'])}   ·   Vehículo: {inv['plate']}")
        party(50, 750, "EMISOR", issuer, "name")
        party(320, 750, "CLIENTE", client, "company")

    def table_head(y):
        ops.append(b"0.93 g 50 %.2f 495 16 re f 0 g" % (y - 4))
        for x, label, right in ((54, "Fecha", False), (104, "Descripción", False), (336, "Km", True), (396, "Peso", True),
                                (482, "Precio", True), (541, "Importe", True)):
            text(b"F2", 8, x, y, label, right)
        return y - 18

    header()
    y = table_head(660)
    for line in lines:
        rate = f"{line['rate']:.2f} {BASIS_LABEL.get(line['basis'], '')}".replace(".", ",") if line.get("rate") is not None else ""
        description = [f"Transporte {line['from']} → {line['to']}"]
        details = " · ".join(p for p in (line.get("material"), f"Ref. {line['reference']}" if line.get("reference") else "",
                                         line["number"]) if p)
        wrapped = _wrap(description[0], 46) + _wrap(details, 54)
        if y - 12 * len(wrapped) < 150:
            pages.append(ops)
            ops = []
            header()
            y = table_head(660)
        text(b"F1", 8, 54, y, _day(line["day"]))
        first = len(_wrap(description[0], 46))
        for index, part in enumerate(wrapped):
            text(b"F1", 8 if index < first else 7, 104, y - index * 11, part)
        text(b"F1", 8, 336, y, f"{line['km']:.0f}" if line.get("km") else "", True)
        text(b"F1", 8, 396, y, _kg(line.get("weight_kg")), True)
        text(b"F1", 7, 482, y, rate, True)
        text(b"F1", 8, 541, y, _eur(line["amount"]), True)
        y -= 11 * len(wrapped) + 7
        ops.append(b"0.85 G 0.4 w 50 %.2f m 545 %.2f l S 0 G" % (y + 4, y + 4))
    y -= 10
    totals = [("Base imponible", inv["base"]), (f"IVA {inv['iva_pct']:g}%", inv["iva"])]
    if inv["irpf_pct"]:
        totals.append((f"Retención IRPF {inv['irpf_pct']:g}%", -inv["irpf"]))
    for label, value in totals:
        text(b"F1", 9, 440, y, label, True)
        text(b"F1", 9, 541, y, _eur(value), True)
        y -= 14
    ops.append(b"0.6 w 330 %.2f m 545 %.2f l S" % (y + 9, y + 9))
    text(b"F2", 11, 440, y - 4, "TOTAL", True)
    text(b"F2", 11, 541, y - 4, _eur(inv["total"]), True)
    y -= 34
    notes = []
    if issuer.get("iban"):
        notes.append(f"Forma de pago: transferencia bancaria · IBAN {issuer['iban']}")
    if issuer.get("on_behalf_by"):
        by = issuer["on_behalf_by"]
        notes.append(f"Factura expedida por {by['name']} (NIF {by['nif']}) en nombre y por cuenta de {issuer['name']} (NIF {issuer['nif']}).")
    if inv.get("notes"):
        notes.append(inv["notes"])
    for note in notes:
        for part in _wrap(note, 100):
            text(b"F1", 8, 50, y, part)
            y -= 11
        y -= 3
    pages.append(ops)

    fonts = b"<< /F1 %d 0 R /F2 %d 0 R >>"
    count = len(pages)
    font_regular, font_bold = 3 + 2 * count, 4 + 2 * count
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % (3 + 2 * i) for i in range(count)), count)]
    for index, page_ops in enumerate(pages):
        if count > 1:
            page_ops = page_ops + [b"BT /F1 7 Tf 500 30 Td (%s) Tj ET" % _pdf_text(f"Página {index + 1} de {count}")]
        content = b"\n".join(page_ops)
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] /Resources << /Font %s >> /Contents %d 0 R >>"
                       % (fonts % (font_regular, font_bold), 4 + 2 * index))
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    objects.append(b"<< /Title (%s) /Producer (MalcomTrans) >>" % _pdf_text("Factura " + inv["number"]))
    out, offsets = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (index, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, len(objects), xref)
    return f"Factura-{inv['number']}.pdf", bytes(out)
