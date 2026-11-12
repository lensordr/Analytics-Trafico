"""Minimal .xlsx reader (standard library only): sheet name -> rows of cell values.

Dates come back as `datetime.date`, numbers as int/float, text as str. Enough for simple
lists such as the ITV document; formulas return their cached value."""
import datetime
import io
import re
import zipfile
import xml.etree.ElementTree as ET

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_MAX_UNPACKED = 20_000_000  # refuse zip bombs


def _column(ref):
    letters = re.match(r"[A-Z]+", ref).group()
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - 64
    return index - 1


def _date_styles(archive):
    if "xl/styles.xml" not in archive.namelist():
        return set()
    styles = ET.fromstring(archive.read("xl/styles.xml"))
    custom = {int(n.get("numFmtId")): n.get("formatCode", "") for n in styles.findall("m:numFmts/m:numFmt", _NS)}
    result = set()
    for index, xf in enumerate(styles.findall("m:cellXfs/m:xf", _NS)):
        format_id = int(xf.get("numFmtId", 0))
        code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", custom.get(format_id, "")).lower()
        if 14 <= format_id <= 22 or 45 <= format_id <= 47 or (code and re.search(r"[dy]", code)):
            result.add(index)
    return result


def read_workbook(data):
    """Return {sheet name: [row values...]} for an .xlsx file given as bytes."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("This is not an Excel .xlsx file.") from None
    if sum(info.file_size for info in archive.infolist()) > _MAX_UNPACKED:
        raise ValueError("The Excel file is too large.")
    try:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            for item in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in item.iter("{%s}t" % _NS["m"])))
        dates = _date_styles(archive)
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        links = {r.get("Id"): r.get("Target") for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        sheets = {}
        for sheet in workbook.findall("m:sheets/m:sheet", _NS):
            target = links[sheet.get("{%s}id" % _NS["r"])].lstrip("/")
            path = target if target.startswith("xl/") else "xl/" + target
            rows = []
            for row in ET.fromstring(archive.read(path)).findall("m:sheetData/m:row", _NS):
                values = []
                for cell in row.findall("m:c", _NS):
                    index = _column(cell.get("r", "A1")) if cell.get("r") else len(values)
                    values += [None] * (index - len(values))
                    kind, raw = cell.get("t"), cell.find("m:v", _NS)
                    if kind == "inlineStr":
                        value = "".join(t.text or "" for t in cell.iter("{%s}t" % _NS["m"]))
                    elif raw is None:
                        value = None
                    elif kind == "s":
                        value = shared[int(raw.text)]
                    elif kind in ("str", "e"):
                        value = raw.text
                    elif kind == "b":
                        value = raw.text == "1"
                    else:
                        number = float(raw.text)
                        if int(cell.get("s", 0)) in dates:
                            value = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(number))
                        else:
                            value = int(number) if number.is_integer() else number
                    values.append(value)
                rows.append(values)
            sheets[sheet.get("name")] = rows
        return sheets
    except (KeyError, ET.ParseError, ValueError, IndexError) as exc:
        raise ValueError(f"The Excel file could not be read ({exc}).") from None
