"""Shared input helpers: text fields, ids and loading one record."""

def text(data, key, required=True, limit=200):
    value = str(data.get(key, "")).strip()
    if (required and not value) or len(value) > limit:
        raise ValueError(f"{key.replace('_', ' ').title()} is required (maximum {limit} characters).")
    return value


def identity(value):
    try:
        result = int(value)
        if result <= 0:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise ValueError("Choose a valid item from the list.")


def item(db, table, value):
    if table not in {"drivers", "trucks", "trailers", "locations", "orders", "itv_vehicles", "materials"}:
        raise ValueError("Invalid item type.")
    row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (identity(value),)).fetchone()
    if row is None:
        raise ValueError("The selected item no longer exists. Refresh and try again.")
    # Lowercase keys: a database restored from a Turso backup can name the column PLAN.
    return {key.lower(): row[key] for key in row.keys()}
