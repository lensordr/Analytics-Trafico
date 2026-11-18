"""Company and town names written properly: "FERIMET S.L" → "Ferimet S.L.", "Caldes de montbui" → "Caldes de Montbui".

Only words written all in capitals or all in lower case change; mixed words stay as typed ("PreZero", "DIA%").
Short capital words stay (acronyms: "CSM", "HKS", "XXI"), small words inside a name are lower case ("de", "del",
"la", "i", "et"…), and legal forms are written the standard way ("SL" → "S.L.", "SAU" → "S.A.U.").
A fix changes capitals and legal-form dots only, never the name itself (checked by `same_name`).
"""
import re
import unicodedata

PARTICLES = {"de", "del", "la", "las", "los", "el", "y", "e", "i", "en", "a", "al", "o", "u", "et", "du", "des", "le",
             "les", "sur", "sous", "aux", "au", "do", "da", "dos", "das", "di", "von", "van", "der", "den", "and", "of",
             "the", "per", "amb", "sobre", "etc", "chez"}
ARTICLES = {"el", "la", "las", "los", "les", "le"}  # lower case only after a preposition: "Planta de la Vega", not "Les Franqueses"
PREPOSITIONS = {"de", "del", "a", "al", "en", "sur", "sous", "du", "des", "per", "amb", "sobre", "do", "da", "di", "von", "van"}
LEGAL = {"SL": "S.L.", "SLU": "S.L.U.", "SA": "S.A.", "SAU": "S.A.U.", "SLL": "S.L.L.", "SLNE": "S.L.N.E.",
         "SC": "S.C.", "CB": "C.B.", "SAT": "S.A.T.", "SCOOP": "S. Coop.", "SCCL": "SCCL", "SRL": "S.R.L.",
         "SPA": "S.p.A.", "BV": "B.V.", "NV": "N.V.", "GMBH": "GmbH", "LTD": "Ltd", "SARL": "SARL", "SAS": "SAS",
         "SASU": "SASU", "EURL": "EURL", "AG": "AG", "LDA": "Lda."}
KEEP_UPPER = {"CELSA", "BASF", "SEAT", "ADIF", "RENFE", "IKEA", "UTE", "DHL", "TNT", "AIE", "SCCL"}
SHORT_WORDS = {"san", "sant", "rio", "mar", "sol", "sur", "oro", "vic", "can", "son", "mas", "bon", "nou", "ros",
               "cal", "gran", "bar", "pla", "sta", "sto", "ets", "via", "la", "las", "los", "del", "les", "port", "pol",
               "new", "eco", "bio", "red", "oil", "agro", "hnos", "car", "ter", "cam", "fer", "met", "alu", "pan"}
ROMAN = re.compile(r"[IVXLC]{1,5}")


def _plain(text):
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def same_name(a, b):
    """True when two names differ only in capitals, spaces and dots (a capitals fix, not a new name)."""
    key = lambda s: re.sub(r"[^a-z0-9]", "", _plain(str(s or "")).casefold())
    return key(a) == key(b)


def _word(core, first, legal, after="", last=False):
    if not core:
        return core
    letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", core)
    bare = re.sub(r"[.\s]", "", core).upper()
    if legal and not first and bare in LEGAL and len(bare) >= 2:
        return LEGAL[bare]
    if "'" in core or "’" in core:  # d'Onyar, L'Esquirol
        head, sep, tail = core.replace("’", "'").partition("'")
        if len(head) <= 2 and tail:
            head = head.capitalize() if first else head.lower()
            rest = tail.lower() if tail.lower() in PARTICLES and (tail.isupper() or tail.islower()) else _word(tail, True, False)
            return f"{head}'{rest}"
    if "-" in core:  # Vitoria-Gasteiz, Lons-le-Saunier, TEGA-Soria
        parts = core.split("-")
        return "-".join(_word(p, first, legal) if i == 0 else p.lower() if p.lower() in PARTICLES and (p.isupper() or p.islower())
                        else _word(p, True, legal) for i, p in enumerate(parts))
    if not letters or any(ch.isdigit() for ch in core):
        return core
    inner = core.strip(".")
    if "." in inner and all(len(p) > 1 for p in inner.split(".") if p) and len(letters) > 4:  # ARCELOR.TUBULAR
        return ".".join(_word(p, True, legal) if p else p for p in core.split("."))
    upper, lower = letters.isupper(), letters.islower()
    if not upper and not lower:
        return core  # mixed as typed: PreZero, YourCopperSolutions
    low = core.lower()
    if len(core) == 1 and last:
        return core  # a label at the end: "Warehouse A", "Nave B"
    if low in PARTICLES and not first and (low not in ARTICLES or after in PREPOSITIONS):
        return low
    if upper:
        if core in KEEP_UPPER or (ROMAN.fullmatch(core) and len(core) <= 4 and core not in ("MIX", "VIC", "CIL")):
            return core
        if len(letters) <= 3 and low not in SHORT_WORDS and low not in PARTICLES:
            return core  # acronym: CSM, HKS, ASG
        if not re.search(r"[AEIOUÁÉÍÓÚÀÈÒÜaeiou]", core) and len(letters) <= 5:
            return core  # no vowel: RFM, MSM
    if "." in core and len(letters) <= 3:  # initials: J. / J.A.
        return core.upper()
    return low[:1].upper() + low[1:]


def proper_name(text, legal=True):
    """The name written properly (companies: legal=True; towns: legal=False)."""
    text = " ".join(str(text or "").split())
    if not text:
        return text
    tokens = text.split(" ")
    result = []
    for index, token in enumerate(tokens):
        match = re.fullmatch(r"([\"'(«\[|]*)(.*?)([,;:)\]»\"|]*)", token)
        lead, core, trail = match.groups()
        before = re.sub(r"[^\w]", "", tokens[index - 1]).lower() if index else ""
        result.append(lead + _word(core, index == 0, legal, before, index == len(tokens) - 1) + trail)
    fixed = " ".join(result)
    # "SL" glued after a comma stays "…, S.L."; a lone legal form at the end without dots: already handled above.
    return fixed


def name_fixes(db):
    """Saved places whose company or town would be written differently: [{id, company:{before,after}, city:{…}}]."""
    fixes = []
    for row in db.execute("SELECT id, company, city FROM locations ORDER BY company COLLATE NOCASE"):
        company, city = proper_name(row["company"], True), proper_name(row["city"], False)
        change = {}
        if company != row["company"]:
            change["company"] = {"before": row["company"], "after": company}
        if city != row["city"]:
            change["city"] = {"before": row["city"], "after": city}
        if change:
            fixes.append({"id": row["id"], **change})
    return fixes


def apply_name_fixes(db, data):
    """Save the chosen fixes. Only capitals and legal-form dots may change: the name itself stays the same."""
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > 3000:
        raise ValueError("Choose the names to fix.")
    changes = []
    for entry in items:
        try:
            row = db.execute("SELECT id, company, city FROM locations WHERE id=?", (int(entry.get("id")),)).fetchone()
        except (TypeError, ValueError, AttributeError):
            row = None
        if row is None:
            raise ValueError("A place in the list no longer exists. Refresh and try again.")
        values = {}
        for key in ("company", "city"):
            if key in entry:
                value = " ".join(str(entry[key] or "").split())[:200]
                if not value or not same_name(value, row[key]):
                    raise ValueError(f"“{value or row[key]}” is not the same name as “{row[key]}”. Only capitals and dots can change here; "
                                     "rename a place from its card.")
                if value != row[key]:
                    values[key] = value
        if values:
            changes.append((row["id"], values))
    for location_id, values in changes:
        db.execute("UPDATE locations SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?", (*values.values(), location_id))
    return {"message": f"{len(changes)} {'place' if len(changes) == 1 else 'places'} written properly.", "changed": len(changes)}
