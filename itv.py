"""ITV (vehicle inspection) register: Excel import, manual edits, expiry status."""

import base64
from datetime import datetime

import xlsx
from common import item, text
from storage import begin_write

# ---------------------------------------------------------------- ITV register
ITV_KINDS = ("Tractora", "Remolque")


def itv_value(raw):
    """(expires_on 'YYYY-MM-DD' or None, expiry_text) from a document cell.
    Non-date text such as 'CADUCADO' or '---------' is kept exactly as written."""
    if raw is None:
        return None, ""
    if hasattr(raw, "isoformat") and not isinstance(raw, str):
        return (raw.date() if isinstance(raw, datetime) else raw).isoformat(), ""
    value = str(raw).strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat(), ""
        except ValueError:
            pass
    return None, value


def parse_itv_workbook(data):
    """Read the ITV Excel: one sheet per vehicle type (sheet name with REMOLQUE / TRACTORA),
    a 'Matricula' column and an expiry ('Fecha', 'Caducidad') column."""
    vehicles, seen = [], set()
    for name, sheet in xlsx.read_workbook(data).items():
        upper = name.upper()
        kind = "Remolque" if ("REMOLQ" in upper or "TRAIL" in upper) else "Tractora" if ("TRACT" in upper or "CAMI" in upper) else None
        rows_ = [[("" if v is None else v) for v in row] for row in sheet]
        if not any(any(str(v).strip() for v in row) for row in rows_):
            continue
        if kind is None:
            raise ValueError(f"Sheet '{name}': name it REMOLQUE or TRACTORA so the vehicle type is clear.")
        header = next((i for i, row in enumerate(rows_) if any("MATRIC" in str(v).upper() for v in row)), None)
        if header is None:
            raise ValueError(f"Sheet '{name}': no 'Matricula' column found.")
        titles = [str(v).upper() for v in rows_[header]]
        plate_col = next(i for i, v in enumerate(titles) if "MATRIC" in v)
        date_col = next((i for i, v in enumerate(titles) if i != plate_col and any(k in v for k in ("FECHA", "CADUC", "ITV", "EXPIR"))),
                        plate_col + 1)
        for row in rows_[header + 1:]:
            plate = str(row[plate_col]).strip() if plate_col < len(row) else ""
            if not plate:
                continue
            key = (kind, plate.upper())
            if key in seen:
                raise ValueError(f"Sheet '{name}': {plate} appears twice.")
            seen.add(key)
            expires_on, expiry_text = itv_value(row[date_col] if date_col < len(row) and row[date_col] != "" else None)
            vehicles.append({"kind": kind, "plate": plate, "expires_on": expires_on, "expiry_text": expiry_text})
    if not vehicles:
        raise ValueError("No vehicles found in the Excel file.")
    return vehicles


def import_itv(db, data):
    """Preview (default) or replace the whole ITV register with the Excel document."""
    try:
        content = base64.b64decode(str(data.get("file", "")), validate=True)
    except ValueError:
        raise ValueError("The file could not be read. Choose the Excel file again.") from None
    vehicles = parse_itv_workbook(content)
    current = db.execute("SELECT count(*) AS n FROM itv_vehicles").fetchone()["n"]
    if not data.get("confirm"):
        return {"vehicles": vehicles, "current": current}
    begin_write(db)
    db.execute("DELETE FROM itv_vehicles")
    for v in vehicles:
        db.execute("INSERT INTO itv_vehicles (kind, plate, expires_on, expiry_text) VALUES (?, ?, ?, ?)",
                   (v["kind"], v["plate"], v["expires_on"], v["expiry_text"]))
    return {"message": f"ITV list replaced: {len(vehicles)} vehicles imported."}


def save_itv(db, data):
    kind = text(data, "kind", True, 20)
    if kind not in ITV_KINDS:
        raise ValueError("Choose Tractora or Remolque.")
    plate = text(data, "plate", True, 30).upper()
    expires_on, expiry_text = itv_value(data.get("expires_on") or None)
    if expiry_text:
        raise ValueError("Enter a valid expiry date.")
    if not expires_on:
        expiry_text = text(data, "expiry_text", False, 40)
    same = db.execute("SELECT id FROM itv_vehicles WHERE kind=? AND upper(plate)=?", (kind, plate)).fetchone()
    if same and str(same["id"]) != str(data.get("id") or ""):
        raise ValueError(f"{kind} {plate} is already in the ITV list.")
    values = (kind, plate, expires_on, expiry_text, text(data, "notes", False, 500))
    if data.get("id"):
        row = item(db, "itv_vehicles", data["id"])
        db.execute("UPDATE itv_vehicles SET kind=?, plate=?, expires_on=?, expiry_text=?, notes=? WHERE id=?", (*values, row["id"]))
        return row["id"]
    return db.execute("INSERT INTO itv_vehicles (kind, plate, expires_on, expiry_text, notes) VALUES (?, ?, ?, ?, ?)", values).lastrowid


def delete_itv(db, data):
    row = item(db, "itv_vehicles", data.get("id"))
    db.execute("DELETE FROM itv_vehicles WHERE id=?", (row["id"],))
    return {"message": f"{row['plate']} removed from the ITV list."}
