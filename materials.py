"""Materials loaded at clients (goods code, optional LER code)."""

from common import item, text

def material_label(material):
    return f"{material['code']} · {material['name']}" if material["code"] else material["name"]


def save_material(db, data):
    """Materials loaded at clients. The code is your own (it may repeat in the source list)."""
    values = {"code": text(data, "code", False, 20), "name": text(data, "name", True, 120),
              "ler_code": text(data, "ler_code", False, 20), "notes": text(data, "notes", False, 1000),
              "active": 1 if data.get("active", True) else 0}
    same = db.execute("SELECT id FROM materials WHERE code=? AND lower(name)=lower(?)", (values["code"], values["name"])).fetchone()
    if same and str(same["id"]) != str(data.get("id") or ""):
        raise ValueError(f"{values['code']} · {values['name']} is already in the list.")
    if data.get("id"):
        material = item(db, "materials", data["id"])
        db.execute("UPDATE materials SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                   (*values.values(), material["id"]))
        # Planned and future stops show the corrected name; confirmed history keeps what was loaded.
        db.execute("UPDATE order_stops SET material=? WHERE material_id=? AND status!='Done'",
                   (material_label(values), material["id"]))
        return material["id"]
    return db.execute("INSERT INTO materials (" + ",".join(values) + ") VALUES (" + ",".join("?" for _ in values) + ")",
                      tuple(values.values())).lastrowid


def delete_material(db, data):
    material = item(db, "materials", data.get("id"))
    used = db.execute("SELECT count(*) FROM order_stops WHERE material_id=?", (material["id"],)).fetchone()[0]
    # Orders keep the material text they were created with.
    db.execute("UPDATE order_stops SET material_id=NULL WHERE material_id=?", (material["id"],))
    db.execute("DELETE FROM materials WHERE id=?", (material["id"],))
    return {"message": f"{material_label(material)} removed" + (f" (kept as text on {used} stops)." if used else ".")}
