import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from planner import Clock, parse_time, stamp, trip_plan, week_key
from storage import connect, initialize, rows
from upcoming import weekly_availability, subtract_bookings



def make_xlsx(sheets):
    """Tiny .xlsx for tests: {sheet name: rows}; datetime.date cells get a date style."""
    import datetime, io, zipfile
    from xml.sax.saxutils import escape
    def cell(ref, value):
        if value is None:
            return ""
        if isinstance(value, datetime.date):
            return f'<c r="{ref}" s="1"><v>{(value - datetime.date(1899, 12, 30)).days}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>' + "".join(
            f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(sheets, 1)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(
            f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml" Type="ws"/>' for i in range(1, len(sheets) + 1)) + "</Relationships>")
        z.writestr("xl/styles.xml", f'<styleSheet xmlns="{main}"><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
        for i, rows in enumerate(sheets.values(), 1):
            body = "".join(f'<row r="{r}">' + "".join(cell(f"{chr(65 + c)}{r}", v) for c, v in enumerate(row)) + "</row>"
                           for r, row in enumerate(rows, 1))
            z.writestr(f"xl/worksheets/sheet{i}.xml", f'<worksheet xmlns="{main}"><sheetData>{body}</sheetData></worksheet>')
    return buffer.getvalue()


class DispatchTests(unittest.TestCase):
    def setUp(self):
        # No live HERE calls from tests (the key file may exist on this computer).
        here = patch.object(app.routing, "api_key", return_value="")
        here.start()
        self.addCleanup(here.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name)/"test.sqlite3"
        initialize(self.database)
        with connect(self.database) as db:
            self.trailer = app.save_master(db,"trailers",{"plate":"TR-001"})
            self.driver = app.save_master(db,"drivers",{"name":"Test Driver","carrier":"Test Carrier"})
            self.a = app.save_master(db,"locations",{"company":"Warehouse A","city":"Madrid","postal_code":"28001","country":"Spain","lat":40.42,"lon":-3.68})
            self.b = app.save_master(db,"locations",{"company":"Warehouse B","city":"Paris","postal_code":"75001","country":"France","lat":48.86,"lon":2.34})
            self.truck = app.save_master(db,"trucks",{"plate":"TEST-001","trailer":"TR-001","driver_id":self.driver,"operation":"International","location_id":self.a,"available_from":"2030-01-07T08:00"})
        self.payload = {"truck_id":self.truck,"driver_id":self.driver,"trailer":"TR-001","loading_id":self.a,"unloading_id":self.b,
                        "pickup_at":"2030-01-07T08:00","route_mode":"manual","distance_km":1200,"driving_hours":24,
                        "loading_hours":1,"unloading_hours":1,"clock":{},"clock_confirmed":True}

    def tearDown(self):
        self.temp.cleanup()


    def test_itv_excel_import_preview_then_replace(self):
        import base64, datetime
        document = make_xlsx({
            "REMOLQUE": [["Matricula", "Fecha Caducidad"], ["R2321DBL", datetime.date(2027, 2, 25)],
                         ["R5741DBH", "CADUCADO"], ["R1240BBX", "---------"]],
            "TRACTORA": [["MATRICULA", "FECHA CADUC."], ["1812MKN", datetime.date(2027, 1, 23)], ["8798NTR", None]],
            "Notas": []})
        payload = {"file": base64.b64encode(document).decode()}
        with connect(self.database) as db:
            preview = app.import_itv(db, payload)
            self.assertEqual(preview["current"], 0)
            self.assertEqual([(v["kind"], v["plate"], v["expires_on"], v["expiry_text"]) for v in preview["vehicles"]], [
                ("Remolque", "R2321DBL", "2027-02-25", ""), ("Remolque", "R5741DBH", None, "CADUCADO"),
                ("Remolque", "R1240BBX", None, "---------"), ("Tractora", "1812MKN", "2027-01-23", ""),
                ("Tractora", "8798NTR", None, "")])
            self.assertEqual(rows(db, "itv_vehicles"), [])  # a preview writes nothing
            app.save_itv(db, {"kind": "Tractora", "plate": "OLD1234"})
        with connect(self.database) as db:
            self.assertIn("replaced: 5", app.import_itv(db, {**payload, "confirm": True})["message"])
        with connect(self.database) as db:
            # Exactly the document: the old entry is gone, plates are not matched to dispatch vehicles.
            self.assertEqual(sorted(v["plate"] for v in rows(db, "itv_vehicles")),
                             ["1812MKN", "8798NTR", "R1240BBX", "R2321DBL", "R5741DBH"])
            with self.assertRaisesRegex(ValueError, "appears twice"):
                app.parse_itv_workbook(make_xlsx({"TRACTORA": [["Matricula", "Fecha"], ["A1", None], ["a1", None]]}))
            with self.assertRaisesRegex(ValueError, "REMOLQUE or TRACTORA"):
                app.parse_itv_workbook(make_xlsx({"Hoja1": [["Matricula", "Fecha"], ["A1", None]]}))
            with self.assertRaisesRegex(ValueError, "not an Excel"):
                app.import_itv(db, {"file": base64.b64encode(b"plain text").decode()})

    def test_materials_on_loading_stops(self):
        with connect(self.database) as db:
            iron = app.save_material(db, {"code": "07", "name": "HIERRO", "ler_code": "17 04 05"})
            copper = app.save_material(db, {"code": "04", "name": "COBRE"})
            # Repeated codes exist in the real list: allowed, but not the exact same pair twice.
            app.save_material(db, {"code": "07", "name": "Otro"})
            with self.assertRaisesRegex(ValueError, "already in the list"):
                app.save_material(db, {"code": "07", "name": "hierro"})
            c = app.save_master(db, "locations", {"company": "Extra", "city": "Lleida", "postal_code": "25001", "country": "Spain"})
            order_id = app.create_order(db, self.manual_payload(material_id=iron,
                via=[{"kind": "Loading", "location_id": c, "handling_hours": 1, "arrive_at": "2030-01-07T14:00", "material_id": copper},
                     {"kind": "Unloading", "location_id": c, "handling_hours": 1, "arrive_at": "2030-01-07T18:00", "material_id": copper}]))
            stops = app.order_stop_rows(db, order_id)
        self.assertEqual([(s["material_id"], s["material"]) for s in stops],
                         [(iron, "07 · HIERRO"), (copper, "04 · COBRE"), (None, ""), (None, "")])
        with connect(self.database) as db:
            app.save_material(db, {"id": copper, "code": "04", "name": "COBRE LIMPIO"})
            self.assertEqual(app.order_stop_rows(db, order_id)[1]["material"], "04 · COBRE LIMPIO")
            self.assertIn("kept as text on 1 stops", app.delete_material(db, {"id": copper})["message"])
            stop = app.order_stop_rows(db, order_id)[1]
            self.assertEqual((stop["material_id"], stop["material"]), (None, "04 · COBRE LIMPIO"))
            self.assertEqual(len(rows(db, "materials")), 2)
            with self.assertRaisesRegex(ValueError, "no longer exists"):
                app.create_order(db, self.manual_payload(material_id=copper, pickup_at="2030-02-07T08:00", unloading_arrive_at="2030-02-08T15:00"))

    def test_tariffs_and_order_price(self):
        from tariffs import order_amount
        with connect(self.database) as db:
            client = app.save_master(db, "locations", {"company": "Ferimet", "city": "Granollers", "postal_code": "08401", "country": "Spain"})
            tariff = app.save_tariff(db, {"client_id": client, "from_id": self.a, "to_id": self.b, "basis": "loading_t",
                                          "rate": "5.78", "km": "35", "price_date": "2026-02-17"})
            with self.assertRaisesRegex(ValueError, "already has an active tariff"):
                app.save_tariff(db, {"client_id": client, "from_id": self.a, "to_id": self.b, "basis": "loading_t", "rate": 6})
            with self.assertRaisesRegex(ValueError, "rate must be"):
                app.save_tariff(db, {"client_id": client, "from_id": self.b, "to_id": self.a, "basis": "trip", "rate": ""})
            with self.assertRaisesRegex(ValueError, "Choose how"):
                app.save_tariff(db, {"client_id": client, "from_id": self.b, "to_id": self.a, "basis": "km", "rate": 1})
            order_id = app.create_order(db, self.manual_payload(tariff_id=tariff, rate_basis="loading_t", rate="5.78",
                                                                loading_weight_kg=24000))
            order = app.item(db, "orders", order_id)
            stops = app.order_stop_rows(db, order_id)
        # The client comes from the tariff; the order keeps its own copy of the rate.
        self.assertEqual((order["client_id"], order["tariff_id"], order["rate_basis"], order["rate"]), (client, tariff, "loading_t", 5.78))
        self.assertEqual(order_amount(order, stops), 138.72)
        stops[-1]["weight_kg"] = 23500
        self.assertEqual(order_amount({**order, "rate_basis": "unloading_t"}, stops), 138.72)  # billed on the 24 t minimum
        stops[-1]["weight_kg"] = 25000
        self.assertEqual(order_amount({**order, "rate_basis": "unloading_t"}, stops), 144.5)
        self.assertEqual(order_amount({**order}, [{**stop, "weight_kg": None} for stop in stops]), 138.72)  # no weight yet: the minimum
        self.assertEqual(order_amount({**order, "rate_basis": "trip", "rate": 750}, stops), 750)
        self.assertIsNone(order_amount({**order, "rate_basis": "hour", "rate": 60}, stops))
        self.assertEqual(order_amount({**order, "price_eur": 99.5}, stops), 99.5)
        with connect(self.database) as db:
            self.assertIn("keep their price", app.delete_tariff(db, {"id": tariff})["message"])
            order = app.item(db, "orders", order_id)
            self.assertEqual((order["tariff_id"], order["rate"]), (None, 5.78))
            other = app.save_master(db, "locations", {"company": "Other", "city": "Lleida", "postal_code": "25001", "country": "Spain"})
            foreign = app.save_tariff(db, {"client_id": other, "from_id": self.a, "to_id": self.b, "basis": "trip", "rate": 100})
            with self.assertRaisesRegex(ValueError, "another client"):
                app.create_order(db, self.manual_payload(client_id=client, tariff_id=foreign, rate_basis="trip", rate=100,
                                                         pickup_at="2030-02-07T08:00", unloading_arrive_at="2030-02-08T15:00"))

    def test_deca_document_pdf_and_qr(self):
        import deca, qr
        with connect(self.database) as db:
            iron = app.save_material(db, {"code": "21", "name": "XATARRA"})
            shipper = app.save_master(db, "locations", {"company": "Ferimet S.L.U", "city": "Castellbisbal", "postal_code": "08755",
                                                        "country": "Spain", "address": "Calle Ferralla, 12", "nif": "b 17327610"})
            order_id = app.create_order(db, self.manual_payload(material_id=iron))
            # Everything missing is reported at once.
            with self.assertRaises(ValueError) as missing:
                deca.prepare(db, {"order_id": order_id, "shipper_id": shipper})
            for text in ("company name and NIF in Settings", "enter the weight loaded"):
                self.assertIn(text, str(missing.exception))
            with self.assertRaisesRegex(ValueError, "between 1 and 60000"):
                app.create_order(db, self.manual_payload(id=order_id, material_id=iron, loading_weight_kg="-3"))
            app.create_order(db, self.manual_payload(id=order_id, material_id=iron, loading_weight_kg="25560"))
            deca.save_settings(db, {"carrier_name": "Carrier Test SL", "carrier_nif": "b 00000000"})
            with self.assertRaisesRegex(ValueError, "Generate the DeCA"):
                deca.pdf(db, order_id)
            first = deca.prepare(db, {"order_id": order_id, "shipper_id": shipper, "notes": "Big bags"})
            again = deca.prepare(db, {"order_id": order_id, "shipper_id": shipper})
            self.assertEqual(first["id"], again["id"])  # the identifier never changes
            self.assertRegex(first["id"], r"^[0-9a-f]{32}$")
            doc = deca.document(db, order_id)
            name, body = deca.pdf(db, order_id)
        rows = dict(doc["rows"])
        self.assertEqual(rows["Cargador"], "Ferimet S.L.U. (NIF B17327610)")
        self.assertEqual(rows["Domicilio cargador"], "Calle Ferralla, 12, 08755 Castellbisbal, España")
        self.assertEqual(rows["Transportista Efectivo"], "Carrier Test SL (NIF B00000000)")
        self.assertTrue(rows["1. Carga"].endswith("— XATARRA, 25.560 kg"))
        self.assertTrue(rows["2. Descarga"].endswith("— XATARRA, 25.560 kg"))
        self.assertEqual(doc["notes"], "—")  # observations were cleared by the second call
        # After the loading is confirmed the order form is closed: material and weight are set in the DeCA dialog.
        with connect(self.database) as db:
            steel = app.save_material(db, {"code": "06", "name": "INOX"})
            app.set_stop_status(db, {"order_id": order_id, "seq": 1})
            with self.assertRaisesRegex(ValueError, "between 1 and 60000"):
                deca.prepare(db, {"order_id": order_id, "shipper_id": shipper, "loads": [{"seq": 1, "material_id": steel, "weight_kg": "0"}]})
            with self.assertRaisesRegex(ValueError, "Invalid loading stop"):
                deca.prepare(db, {"order_id": order_id, "shipper_id": shipper, "loads": [{"seq": 2, "material_id": steel, "weight_kg": 1}]})
        with connect(self.database) as db:
            deca.prepare(db, {"order_id": order_id, "shipper_id": shipper, "loads": [{"seq": 1, "material_id": steel, "weight_kg": "24100"}]})
            rows = dict(deca.document(db, order_id)["rows"])
        self.assertTrue(rows["1. Carga"].endswith("— INOX, 24.100 kg"))
        # With an online address the QR opens the PDF by its secret link; unknown links give nothing.
        with connect(self.database) as db:
            self.assertEqual(deca.document(db, order_id)["link"], "")
            deca.save_settings(db, {"public_url": "https://example.onrender.com/"})
            self.assertEqual(deca.document(db, order_id)["link"], f"https://example.onrender.com/d/{first['id']}")
            linked_name, linked = deca.pdf_by_link(db, first["id"])
            self.assertTrue(linked.startswith(b"%PDF") and linked_name.endswith(".pdf"))
            for wrong in ("0" * 32, "../etc", ""):
                with self.assertRaisesRegex(ValueError, "not found"):
                    deca.pdf_by_link(db, wrong)
        self.assertTrue(name.endswith(".pdf") and body.startswith(b"%PDF-1.4") and body.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"(Documento de Control \\(DeCA\\)) Tj", body)
        self.assertIn(first["id"].encode(), body)
        self.assertIn("Cargador: Ferimet S.L.U. (NIF B17327610)", deca.qr_text(doc))
        # Two loadings, a partial unloading, then the rest at the final unloading.
        with connect(self.database) as db:
            copper = app.save_material(db, {"code": "04", "name": "COBRE"})
            c = app.save_master(db, "locations", {"company": "Extra", "city": "Lleida", "postal_code": "25001", "country": "Spain"})
            multi = app.create_order(db, self.manual_payload(pickup_at="2030-03-07T08:00", unloading_arrive_at="2030-03-08T15:00",
                material_id=iron, loading_weight_kg="12000",
                via=[{"kind": "Loading", "location_id": c, "handling_hours": 1, "arrive_at": "2030-03-07T12:00", "material_id": copper, "weight_kg": "8000"},
                     {"kind": "Unloading", "location_id": c, "handling_hours": 1, "arrive_at": "2030-03-07T16:00", "weight_kg": "5000"}]))
            lines = [value for label, value in deca.document(db, multi, shipper)["rows"] if label[0].isdigit()]
        self.assertEqual([line.split(" — ")[1] for line in lines],
                         ["XATARRA, 12.000 kg", "COBRE, 8.000 kg", "XATARRA + COBRE, 5.000 kg", "XATARRA + COBRE, 15.000 kg"])
        # Known QR: "hello" in byte mode, level M, version 1 (checked against the qrcode library).
        self.assertEqual(len(qr.matrix("hello")), 21)

    def test_itv_manual_add_edit_validation_delete(self):
        with connect(self.database) as db:
            itv_id = app.save_itv(db, {"kind": "Remolque", "plate": "r0561bcf", "expires_on": "2026-12-01"})
            self.assertEqual(app.item(db, "itv_vehicles", itv_id)["plate"], "R0561BCF")
            with self.assertRaisesRegex(ValueError, "already in the ITV list"):
                app.save_itv(db, {"kind": "Remolque", "plate": "R0561BCF"})
            with self.assertRaisesRegex(ValueError, "Tractora or Remolque"):
                app.save_itv(db, {"kind": "Car", "plate": "X"})
            with self.assertRaisesRegex(ValueError, "valid expiry date"):
                app.save_itv(db, {"kind": "Remolque", "plate": "R1", "expires_on": "soon"})
            app.save_itv(db, {"id": itv_id, "kind": "Remolque", "plate": "R0561BCF", "expires_on": "", "expiry_text": "CADUCADO"})
            saved = app.item(db, "itv_vehicles", itv_id)
            self.assertEqual((saved["expires_on"], saved["expiry_text"]), (None, "CADUCADO"))
            app.delete_itv(db, {"id": itv_id})
            self.assertEqual(rows(db, "itv_vehicles"), [])

    def test_nearby_companies_rank_by_distance_with_history_and_contacts(self):
        with connect(self.database) as db:
            getafe = app.save_master(db,"locations",{"company":"Getafe Foods","city":"Getafe","postal_code":"28901","country":"Spain",
                                                     "lat":40.30,"lon":-3.73,"phone":"+34 600 111 222","notes":"Loads to France"})
            toledo = app.save_master(db,"locations",{"company":"Toledo Steel","city":"Toledo","postal_code":"45001","country":"Spain","lat":39.86,"lon":-4.02})
            app.save_master(db,"locations",{"company":"Truck Stop","city":"Madrid","postal_code":"28002","country":"Spain","lat":40.43,"lon":-3.69,"kind":"Parking"})
            app.save_master(db,"locations",{"company":"No Coords Madrid","city":"Madrid","postal_code":"28003","country":"Spain"})
            app.save_master(db,"locations",{"company":"No Coords Elsewhere","city":"Bilbao","postal_code":"48001","country":"Spain"})
            app.create_order(db,{**self.payload,"loading_id":getafe})
        with connect(self.database) as db:
            app.log_contact(db,{"location_id":toledo,"result":"No answer","note":"try Monday"})
            result = app.nearby_companies(db,{"location_id":self.a,"radius_km":100},now=parse_time("2030-01-01T10:00"))
            names = [c["location"]["company"] for c in result["companies"]]
            # Same site, same-city without coordinates, then by distance; parking and Paris left out.
            self.assertEqual(names,["Warehouse A","No Coords Madrid","Getafe Foods","Toledo Steel"])
            getafe_entry = result["companies"][2]
            self.assertEqual((getafe_entry["loads"],getafe_entry["destinations"]),(1,["Paris (France)"]))
            self.assertEqual(getafe_entry["location"]["phone"],"+34 600 111 222")
            self.assertEqual(result["companies"][3]["last_contact"]["result"],"No answer")
            self.assertEqual(result["missing_coordinates"],1)
            with_parking = app.nearby_companies(db,{"location_id":self.a,"radius_km":100,"include_all":True})
            self.assertIn("Truck Stop",[c["location"]["company"] for c in with_parking["companies"]])
            self.assertEqual(len(app.nearby_companies(db,{"location_id":self.a,"radius_km":20})["companies"]),3)
            with self.assertRaisesRegex(ValueError,"result of the contact"):
                app.log_contact(db,{"location_id":toledo,"result":"Maybe"})

    def test_next_loads_from_saved_routes_and_weekend_home(self):
        from datetime import timedelta
        import deca
        from nextloads import QuickClock, weekend_deadline
        clock = QuickClock(parse_time("2030-01-07T08:00"))
        clock.drive(650)  # 600 min: 4.5 h, break, 4.5 h, daily rest, 1 h
        self.assertEqual(clock.t - parse_time("2030-01-07T08:00"), timedelta(minutes=270 + 45 + 270 + 660 + 60))
        self.assertEqual(stamp(weekend_deadline(parse_time("2030-01-10T08:00"))), stamp(parse_time("2030-01-13T00:00")))
        self.assertEqual(stamp(weekend_deadline(parse_time("2030-01-13T10:00"))), stamp(parse_time("2030-01-20T00:00")))
        with connect(self.database) as db:
            base = app.save_master(db, "locations", {"company": "Our base", "city": "Madrid", "postal_code": "28002", "country": "Spain",
                                                     "lat": 40.43, "lon": -3.69, "kind": "Parking"})
            getafe = app.save_master(db, "locations", {"company": "Getafe Foods", "city": "Getafe", "postal_code": "28901", "country": "Spain",
                                                       "lat": 40.30, "lon": -3.73})
            toledo = app.save_master(db, "locations", {"company": "Toledo Steel", "city": "Toledo", "postal_code": "45001", "country": "Spain",
                                                       "lat": 39.86, "lon": -4.02})
            short = app.save_tariff(db, {"client_id": getafe, "from_id": getafe, "to_id": toledo, "basis": "trip", "rate": 100, "km": 70})
            far = app.save_tariff(db, {"client_id": getafe, "from_id": getafe, "to_id": self.b, "basis": "loading_t", "rate": 60, "km": 1270})
            app.save_tariff(db, {"client_id": getafe, "from_id": self.b, "to_id": self.a, "basis": "trip", "rate": 900, "km": 1270})
            app.save_tariff(db, {"client_id": getafe, "from_id": getafe, "to_id": toledo, "basis": "trip", "rate": 5, "active": False})
            with self.assertRaisesRegex(ValueError, "Home base"):
                deca.save_settings(db, {"home_base_id": "999999"})
            thursday = app.next_loads(db, {"location_id": self.a, "radius_km": 100, "from": "2030-01-10T08:00", "driver_id": self.driver},
                                      now=parse_time("2030-01-01T10:00"))
            self.assertEqual(thursday["base"]["id"], base)  # found by name without a setting
            self.assertEqual([r["tariff"]["id"] for r in thursday["routes"]], [short, far])  # Paris loading and inactive left out
            first, second = thursday["routes"]
            self.assertTrue(thursday["towards_home"])
            self.assertEqual((first["home_for_weekend"], second["home_for_weekend"]), (True, False))
            self.assertEqual(second["revenue"], 1440)  # 60 €/t × the 24 t minimum
            self.assertGreater(second["per_hour"], first["per_hour"])  # better paid, but not home for the weekend
            monday = app.next_loads(db, {"location_id": self.a, "radius_km": 100, "from": "2030-01-07T08:00"}, now=parse_time("2030-01-01T10:00"))
            self.assertEqual([r["tariff"]["id"] for r in monday["routes"]], [far, short])  # early in the week: by € per km
            app.save_master(db, "drivers", {"id": self.driver, "name": "Test Driver", "weekend_away": True})
            away = app.next_loads(db, {"location_id": self.a, "radius_km": 100, "from": "2030-01-10T08:00", "driver_id": self.driver},
                                  now=parse_time("2030-01-01T10:00"))
            self.assertTrue(away["weekend_away"])
            self.assertEqual([r["home_for_weekend"] for r in away["routes"]], [None, None])
            self.assertEqual(away["routes"][0]["tariff"]["id"], far)
            deca.save_settings(db, {"home_base_id": str(toledo)})
            self.assertEqual(app.next_loads(db, {"location_id": self.a, "weekend_away": False})["base"]["id"], toledo)

    def test_transport_company_clients_come_after_direct_clients(self):
        with connect(self.database) as db:
            spot = {"city": "Getafe", "postal_code": "28901", "country": "Spain", "lat": 40.30, "lon": -3.73}
            haulier = app.save_master(db, "locations", {"company": "Transportes Rapidos", **spot, "transport_company": True})
            direct = app.save_master(db, "locations", {"company": "Steel Works", **spot})
            better = app.save_tariff(db, {"client_id": haulier, "from_id": direct, "to_id": self.b, "basis": "trip", "rate": 3000, "km": 1270})
            worse = app.save_tariff(db, {"client_id": direct, "from_id": direct, "to_id": self.b, "basis": "trip", "rate": 1500, "km": 1270})
            result = app.next_loads(db, {"location_id": self.a, "radius_km": 100, "from": "2030-01-07T08:00"}, now=parse_time("2030-01-01T10:00"))
            self.assertEqual([r["tariff"]["id"] for r in result["routes"]], [worse, better])  # direct client first, even if paid less
            self.assertEqual([r["transport_client"] for r in result["routes"]], [False, True])
            app.save_master(db, "locations", {"id": haulier, "company": "Transportes Rapidos", **spot})  # older form: flag kept
            self.assertEqual(app.item(db, "locations", haulier)["transport_company"], 1)

    def test_location_profile_validation_and_kept_on_partial_edit(self):
        with connect(self.database) as db:
            with self.assertRaisesRegex(ValueError,"company type"):
                app.save_master(db,"locations",{"company":"X","city":"Y","postal_code":"1","country":"ES","kind":"Pub"})
            with self.assertRaisesRegex(ValueError,"valid email"):
                app.save_master(db,"locations",{"company":"X","city":"Y","postal_code":"1","country":"ES","email":"nope"})
            app.save_master(db,"locations",{"id":self.a,"company":"Warehouse A","city":"Madrid","postal_code":"28001","country":"Spain",
                                            "contact_name":"Ana","email":"ana@example.com","kind":"Warehouse"})
            # A form without the profile fields (e.g. an older open tab) keeps them.
            app.save_master(db,"locations",{"id":self.a,"company":"Warehouse A","city":"Madrid","postal_code":"28001","country":"Spain","lat":40.42,"lon":-3.68})
            saved = app.item(db,"locations",self.a)
            self.assertEqual((saved["contact_name"],saved["email"],saved["kind"]),("Ana","ana@example.com","Warehouse"))

    def test_trailer_registry_assignment_and_validation(self):
        with connect(self.database) as db:
            self.assertEqual(app.item(db,"trucks",self.truck)["trailer_id"],self.trailer)
            with self.assertRaisesRegex(ValueError,"registered trailer"):
                app.selected_trailer(db,{"trailer":"UNKNOWN"})
            self.assertIsNone(app.selected_trailer(db,{"trailer_id":"","trailer":"TR-001"}))
            app.save_master(db,"trailers",{"id":self.trailer,"plate":"TR-001","active":False})
            with self.assertRaisesRegex(ValueError,"inactive"):
                app.selected_trailer(db,{"trailer_id":self.trailer})

    def test_trailer_rename_preserves_history_and_conflicts(self):
        with connect(self.database) as db:
            order_id=app.create_order(db,self.payload)
            app.save_master(db,"trailers",{"id":self.trailer,"plate":"TR-RENAMED"})
            self.assertEqual(app.item(db,"trucks",self.truck)["trailer"],"TR-RENAMED")
            order=app.item(db,"orders",order_id)
            self.assertEqual(order["trailer"],"TR-001")
            self.assertEqual(order["trailer_id"],self.trailer)
            found=app.conflicts(db,999,999,"TR-RENAMED",parse_time(order["pickup_at"]),parse_time(order["available_at"]),trailer_id=self.trailer)
            self.assertTrue(found)
        initialize(self.database)
        with connect(self.database) as db:
            self.assertEqual(len(rows(db,"trailers")),1)

    def test_archive_keeps_records_and_history_and_can_restore(self):
        with connect(self.database) as db:
            order_id = app.create_order(db,self.payload)
            db.execute("UPDATE orders SET status='Completed' WHERE id=?",(order_id,))
        with connect(self.database) as db:
            self.assertEqual(app.archive_master(db,"trucks",{"id":self.truck,"reason":"Sold","note":"Buyer X"}),{"archived":"TEST-001"})
            truck = app.item(db,"trucks",self.truck)
            self.assertTrue(truck["archived_at"])
            self.assertEqual((truck["archive_reason"],truck["archive_note"],truck["active"]),("Sold","Buyer X",0))
            self.assertEqual(app.item(db,"orders",order_id)["truck_id"],self.truck)  # history intact
            with self.assertRaisesRegex(ValueError,"already archived"):
                app.archive_master(db,"trucks",{"id":self.truck,"reason":"Sold"})
            with self.assertRaisesRegex(ValueError,"Restore it before editing"):
                app.save_master(db,"trucks",{"id":self.truck,"plate":"TEST-001","operation":"Local"})
            with self.assertRaisesRegex(ValueError,r"TEST-001 is archived \(Sold\)"):
                app.save_master(db,"trucks",{"plate":"test-001","operation":"Local"})
            # Archiving a trailer removes it as default trailer from tractors.
            app.archive_master(db,"trailers",{"id":self.trailer,"reason":"Scrapped / end of life"})
            self.assertIsNone(app.item(db,"trucks",self.truck)["trailer_id"])
            with self.assertRaisesRegex(ValueError,"archived"):
                app.selected_trailer(db,{"trailer_id":self.trailer})
            self.assertEqual(app.restore_master(db,"trucks",{"id":self.truck}),{"restored":"TEST-001"})
            truck = app.item(db,"trucks",self.truck)
            self.assertEqual((truck["archived_at"],truck["archive_reason"],truck["active"]),(None,"",1))
            self.assertEqual(len(rows(db,"trucks")),1)

    def test_archive_validation(self):
        with connect(self.database) as db:
            app.create_order(db,self.payload)
        with connect(self.database) as db:
            with self.assertRaisesRegex(ValueError,"still has planned orders: MT-2030-000001"):
                app.archive_master(db,"trucks",{"id":self.truck,"reason":"Sold"})
            spare = app.save_master(db,"trucks",{"plate":"SPARE-1","operation":"Local"})
            with self.assertRaisesRegex(ValueError,"Choose a reason"):
                app.archive_master(db,"trucks",{"id":spare,"reason":"Stolen by aliens"})
            with self.assertRaisesRegex(ValueError,"Note is required"):
                app.archive_master(db,"trucks",{"id":spare,"reason":"Other"})
            with self.assertRaisesRegex(ValueError,"Only tractors and trailers"):
                app.archive_master(db,"drivers",{"id":self.driver,"reason":"Sold"})
            with self.assertRaisesRegex(ValueError,"already exists"):
                app.save_master(db,"trailers",{"plate":"tr-001"})

    def test_new_trailer_typed_as_plain_text(self):
        with connect(self.database) as db:
            # Preview must not create anything; saving creates the trailer once.
            preview = app.selected_trailer(db,{"trailer_id":"","new_trailer":" nw-777 "})
            self.assertEqual((preview["id"],preview["plate"]),(None,"NW-777"))
            self.assertIsNone(app.find_by_plate(db,"trailers","NW-777"))
            truck = app.save_master(db,"trucks",{"plate":"NEW-1","operation":"Local","trailer_id":"","new_trailer":"nw-777"})
            trailer = app.find_by_plate(db,"trailers","NW-777")
            self.assertEqual(app.item(db,"trucks",truck)["trailer_id"],trailer["id"])
            # Typing an existing plate reuses it instead of creating a duplicate.
            again = app.selected_trailer(db,{"trailer_id":"","new_trailer":"NW-777"},create=True)
            self.assertEqual(again["id"],trailer["id"])
            self.assertEqual(len(rows(db,"trailers")),2)

    def add_location(self, db, city, lat, lon):
        return app.save_master(db,"locations",{"company":"Site "+city,"city":city,"postal_code":"00000","country":"Spain","lat":lat,"lon":lon})

    def manual_payload(self, **extra):
        payload = {**self.payload,"timing":"manual","driving_hours":0,"distance_km":0,"clock_confirmed":False,
                   "unloading_arrive_at":"2030-01-08T15:00"}
        payload.update(extra)
        return payload

    def test_driver_reports_delay_from_link(self):
        import driver
        from driverlinks import driver_order, eta_reports, report_delay
        from datetime import datetime, timezone
        now = datetime(2030,1,8,9,0,tzinfo=timezone.utc)  # Tue 10:00 Madrid; unloading planned Tue 15:00
        with connect(self.database) as db:
            order = app.create_order(db,self.manual_payload())
            app.set_stop_status(db,{"order_id":order,"seq":1,"done_at":"2030-01-07T08:30"})
            token = app.driver_link(db,{"order_id":order})["token"]
        def report(delay):
            with connect(self.database) as db:
                o, stops, _ = driver_order(db,token)
                return report_delay(db,o,[s for s in stops if s["status"]!="Done"][0],delay,now)
        report("2")
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT planned_at FROM order_stops WHERE order_id=? AND seq=2",(order,)).fetchone()["planned_at"][:16],"2030-01-08T17:00")
        report("4")  # 17:00 + 4 h = 21:00, after closing → Wednesday 08:00
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT planned_at FROM order_stops WHERE order_id=? AND seq=2",(order,)).fetchone()["planned_at"][:16],"2030-01-09T08:00")
            # Two taps in the same minute: one report per stop and minute, the last one wins.
            self.assertEqual([r["delay"] for r in eta_reports(db,now)],["4"])
            o, stops, places = driver_order(db,token)
        self.assertIn("Запізнюєшся",driver.render(token,o,stops,places,"uk"))
        with self.assertRaises(ValueError):
            report("99")

    def test_driver_link_open_is_recorded_once_per_ten_minutes(self):
        from driverlinks import link_opens, mark_opened
        with connect(self.database) as db:
            order = app.create_order(db,self.manual_payload())
            token = app.driver_link(db,{"order_id":order})["token"]
        with connect(self.database) as db:
            self.assertEqual(link_opens(db),{})
            mark_opened(db,token)
        with connect(self.database) as db:
            mark_opened(db,token)  # again right away: not written
        with connect(self.database) as db:
            opens = link_opens(db)
        self.assertEqual(opens[token]["views"],1)
        self.assertEqual(opens[token]["first_at"],opens[token]["last_at"])

    def test_empty_km_from_previous_unloading(self):
        from orders import build_plan
        with connect(self.database) as db:
            first = app.create_order(db,self.manual_payload())  # A → B, free Tue 16:00 at B
            # Next order loads at A again: empty B → A, estimated from the straight line (HERE is off in tests).
            *_, route, _ = build_plan(db,self.manual_payload(pickup_at="2030-01-09T08:00",unloading_arrive_at="2030-01-09T15:00"))
            empty = route["empty"]
            self.assertEqual((empty["from_id"], empty["source"]), (self.b, "estimate"))
            self.assertEqual(empty["after"], app.item(db,"orders",first)["number"])
            self.assertEqual(empty["km"], round(app.routing.air_distance(app.item(db,"locations",self.b),app.item(db,"locations",self.a))*1.25,1))
            # Before the first order: from the tractor's reported position (A), which is the loading point.
            *_, route, _ = build_plan(db,self.manual_payload(pickup_at="2030-01-01T08:00",unloading_arrive_at="2030-01-01T15:00"))
            self.assertEqual((route["empty"]["km"], route["empty"]["source"]), (0, "same place"))

    def test_unloading_after_closing_moves_to_next_working_morning(self):
        from orders import build_plan
        stop = lambda plan: plan["stops"][-1]
        with connect(self.database) as db:
            # Empty arrival: estimated. Mon 14:00 + 1 h loading + 5 h driving (with a break) = Mon 20:45 → Tue 08:00.
            *_, route, plan = build_plan(db, self.manual_payload(pickup_at="2030-01-07T14:00", driving_hours=5, unloading_arrive_at=""))
            self.assertEqual((stop(plan)["arrival_at"][:16], stop(plan)["start_at"][:16]), ("2030-01-07T20:45", "2030-01-08T08:00"))
            self.assertTrue(stop(plan)["next_day"] and stop(plan)["estimated"])
            self.assertTrue(any("after closing" in note for note in route["notices"]))
            # A time typed by the dispatcher is kept, but the unloading is the next morning.
            *_, plan = build_plan(db, self.manual_payload(unloading_arrive_at="2030-01-08T18:45"))
            self.assertEqual(stop(plan)["start_at"][:16], "2030-01-09T08:00")
            self.assertEqual(build_plan(db, self.manual_payload(unloading_arrive_at="2030-01-08T18:30"))[-1]["stops"][-1]["next_day"], False)
            # Saturday evening and Sunday: Monday 08:00. Saturday morning stays, with a warning.
            *_, plan = build_plan(db, self.manual_payload(pickup_at="2030-01-12T08:00", unloading_arrive_at="2030-01-12T19:00"))
            self.assertEqual(stop(plan)["start_at"][:16], "2030-01-14T08:00")
            *_, route, plan = build_plan(db, self.manual_payload(pickup_at="2030-01-12T08:00", unloading_arrive_at="2030-01-12T11:00"))
            self.assertEqual(stop(plan)["start_at"][:16], "2030-01-12T11:00")
            self.assertTrue(any("Saturday" in note for note in route["notices"]))
            # Driving-rules estimate: the night wait at the closed place counts as the daily rest.
            *_, plan = build_plan(db, {**self.payload, "pickup_at": "2030-01-07T14:00", "driving_hours": 5})
            self.assertEqual(stop(plan)["start_at"][:16], "2030-01-08T08:00")
            with self.assertRaises(ValueError):  # nothing to estimate from
                build_plan(db, self.manual_payload(unloading_arrive_at=""))

    def test_next_trip_can_be_planned_before_delivery_with_empty_distance(self):
        # The form sends an empty distance and no driving hours when times are manual.
        form = lambda **extra: {**self.manual_payload(**extra),"distance_km":""}
        del_hours = lambda payload: {k:v for k,v in payload.items() if k!="driving_hours"}
        with connect(self.database) as db:
            first = app.create_order(db,del_hours(form()))  # unloading Tue 15:00, free 16:00
        with connect(self.database) as db:
            self.assertEqual(app.item(db,"orders",first)["status"],"Planned")
            second = app.create_order(db,del_hours(form(loading_id=self.b,unloading_id=self.a,pickup_at="2030-01-08T17:00",
                                                         unloading_arrive_at="2030-01-09T12:00")))
            self.assertTrue(second)
        with connect(self.database) as db:
            with self.assertRaisesRegex(ValueError,"already assigned"):
                app.create_order(db,del_hours(form(pickup_at="2030-01-08T14:00",unloading_arrive_at="2030-01-08T16:30")))

    def test_swap_undo_without_starting_trailer_and_trailer_moves_between_tractors(self):
        with connect(self.database) as db:
            other = app.save_master(db,"trucks",{"plate":"OTHER-1","trailer_id":"","new_trailer":"TR-SWAP","operation":"Local"})
            swap_trailer = app.item(db,"trucks",other)["trailer_id"]
            solo = app.save_master(db,"trucks",{"plate":"SOLO-1","trailer_id":"","driver_id":self.driver,"operation":"Local"})
            c = self.add_location(db,"Zaragoza",41.65,-0.88)
        with connect(self.database) as db:
            order = app.create_order(db,{**self.manual_payload(truck_id=solo,trailer_id="",
                via=[{"kind":"Trailer swap","location_id":c,"handling_hours":0.5,"arrive_at":"2030-01-07T14:00",
                      "pick_trailer_id":swap_trailer}])})
        for seq in (1,2):
            with connect(self.database) as db:
                app.set_stop_status(db,{"order_id":order,"seq":seq,"done_at":f"2030-01-07T{8+seq*3:02d}:00"})
        with connect(self.database) as db:
            # Picked up by SOLO-1: OTHER-1 no longer has it as its current trailer.
            self.assertEqual(app.item(db,"trucks",solo)["trailer_id"],swap_trailer)
            self.assertIsNone(app.item(db,"trucks",other)["trailer_id"])
            app.set_stop_status(db,{"order_id":order,"seq":2,"done":False})
        with connect(self.database) as db:
            self.assertIsNone(app.item(db,"trucks",solo)["trailer_id"])
            self.assertEqual(app.item(db,"trailers",swap_trailer)["parked_location_id"],c)

    def test_trailer_left_at_swap_can_go_out_with_another_tractor(self):
        # Loaded trailer TR-001 is dropped at the base; another tractor takes it to the unloading.
        with connect(self.database) as db:
            base = self.add_location(db,"Base",41.65,-0.88)
            other_driver = app.save_master(db,"drivers",{"name":"Second Driver","carrier":"Test Carrier"})
            other = app.save_master(db,"trucks",{"plate":"OTHER-2","trailer":"","driver_id":other_driver,"operation":"Local"})
        with connect(self.database) as db:
            first = app.create_order(db,self.manual_payload(trailer_id=self.trailer,
                via=[{"kind":"Trailer swap","location_id":base,"handling_hours":0.5,"arrive_at":"2030-01-07T14:00",
                      "pick_trailer_id":"","new_pick_trailer":"TR-EMPTY"}]))
        relay = lambda pickup: self.manual_payload(truck_id=other,driver_id=other_driver,trailer_id=self.trailer,trailer="",
                                                   loading_id=base,pickup_at=pickup,unloading_arrive_at="2030-01-09T15:00")
        with connect(self.database) as db:
            # No time limit from the first order: the dispatcher decides when the other tractor takes it.
            app.create_order(db,relay("2030-01-07T12:00"))
        with connect(self.database) as db:
            # The trailer picked up at the swap stays busy with the first tractor until it is free.
            with self.assertRaisesRegex(ValueError,"already assigned"):
                app.create_order(db,{**relay("2030-01-08T09:00"),"trailer_id":app.find_by_plate(db,"trailers","TR-EMPTY")["id"],
                                     "unloading_arrive_at":"2030-01-08T12:00"})
            self.assertTrue(first)

    def test_tractor_change_splits_the_order_between_tractors(self):
        import driver
        from driverlinks import driver_order
        from upcoming import weekly_availability
        with connect(self.database) as db:
            base = self.add_location(db,"Base",41.65,-0.88)
            second_driver = app.save_master(db,"drivers",{"name":"Second Driver","carrier":"Malcom Trans"})
            second = app.save_master(db,"trucks",{"plate":"RELAY-2","trailer":"","driver_id":second_driver,"operation":"International"})
            third = app.save_master(db,"trucks",{"plate":"OTHER-3","trailer":"","driver_id":"","operation":"Local"})
        relay = lambda **extra: self.manual_payload(trailer_id=self.trailer,
            via=[{"kind":"Tractor change","location_id":base,"handling_hours":0.5,"arrive_at":"2030-01-07T14:00",**extra}])
        with connect(self.database) as db:
            with self.assertRaisesRegex(ValueError,"arrival times you enter"):
                app.create_order(db,{**relay(truck_id=second),"timing":"rules"})
            with self.assertRaisesRegex(ValueError,"different tractor"):
                app.create_order(db,relay(truck_id=self.truck))
            order = app.create_order(db,relay(truck_id=second))
            stops = app.order_stop_rows(db,order)
            self.assertEqual((stops[1]["kind"],stops[1]["truck_id"],stops[1]["driver_id"]),("Tractor change",second,second_driver))
        with connect(self.database) as db:
            # The first tractor is free after leaving the trailer at the base; the second one is busy until delivery.
            free = dict(self.manual_payload(trailer="",trailer_id="",pickup_at="2030-01-07T16:00",unloading_arrive_at="2030-01-07T20:00"))
            app.create_order(db,free)
            with self.assertRaisesRegex(ValueError,"already assigned"):
                app.create_order(db,{**free,"truck_id":second,"driver_id":second_driver,"pickup_at":"2030-01-08T08:00",
                                     "unloading_arrive_at":"2030-01-08T12:00"})
            weeks = weekly_availability(db,{"week_offset":0,"week_count":1},now=app.parse_time("2030-01-07T06:00"))
            first_free = [w for w in weeks["weeks"][0]["windows"] if w["truck_id"]==second]
            self.assertEqual(first_free[0]["location"]["id"],self.b)  # free at the unloading
            self.assertEqual(first_free[0]["from"],"2030-01-08T16:00+01:00")
        with connect(self.database) as db:
            first_link = app.driver_link(db,{"order_id":order})["token"]
            second_link = app.driver_link(db,{"order_id":order,"seq":2})["token"]
            self.assertNotEqual(first_link,second_link)
            a, a_stops, a_places = driver_order(db,first_link)
            b, b_stops, b_places = driver_order(db,second_link)
        self.assertEqual(([s["seq"] for s in a_stops],[s["seq"] for s in b_stops]),([1,2],[2,3]))
        self.assertEqual((a["plate"],b["plate"]),("TEST-001","RELAY-2"))
        self.assertIn("HE DEJADO EL REMOLQUE",driver.render(first_link,a,[{**s,"status":"Done"} if s["seq"]==1 else s for s in a_stops],a_places,"es"))
        self.assertIn("HE COGIDO EL REMOLQUE",driver.render(second_link,b,b_stops,b_places,"es"))
        for seq in (1,2):
            with connect(self.database) as db:
                app.set_stop_status(db,{"order_id":order,"seq":seq,"done_at":f"2030-01-07T{8+seq*3:02d}:00"})
        with connect(self.database) as db:
            self.assertEqual(app.item(db,"trucks",second)["trailer_id"],self.trailer)  # the trailer is on the second tractor
            self.assertIsNone(app.item(db,"trucks",self.truck)["trailer_id"])
            app.set_stop_status(db,{"order_id":order,"seq":2,"done":False})
        with connect(self.database) as db:
            self.assertEqual(app.item(db,"trucks",self.truck)["trailer_id"],self.trailer)  # undo: back on the first one
            # Tractor chosen later (or changed) while the order is on its way; the new driver gets a new link.
            with self.assertRaisesRegex(ValueError,"different tractor"):
                app.assign_tractor(db,{"order_id":order,"seq":2,"truck_id":self.truck})
            app.assign_tractor(db,{"order_id":order,"seq":2,"truck_id":third})
            stop = app.order_stop_rows(db,order)[1]
            self.assertEqual((stop["truck_id"],stop["driver_id"],stop["driver_token"]),(third,None,None))
            app.assign_tractor(db,{"order_id":order,"seq":2,"truck_id":""})
            waiting = weekly_availability(db,{"week_offset":0,"week_count":1},now=app.parse_time("2030-01-07T06:00"))["waiting"]
            self.assertEqual([(w["order_id"],w["seq"]) for w in waiting],[(order,2)])

    def test_trailer_pickup_order_shows_pickup_to_driver(self):
        import driver
        from driverlinks import driver_order
        with connect(self.database) as db:
            pickup = app.create_order(db,self.manual_payload(trailer_pickup="1"))
            normal = app.create_order(db,self.manual_payload(pickup_at="2030-01-09T08:00",unloading_arrive_at="2030-01-10T15:00"))
            self.assertEqual((app.item(db,"orders",pickup)["trailer_pickup"],app.item(db,"orders",normal)["trailer_pickup"]),(1,0))
            token = app.driver_link(db,{"order_id":pickup})["token"]
            order, stops, places = driver_order(db,token)
        page = driver.render(token,order,stops,places,"es")
        self.assertIn("Recoges el remolque cargado",page)
        self.assertIn("HE COGIDO EL REMOLQUE",page)
        self.assertNotIn("HE CARGADO",page)
        self.assertIn("Забираешь загруженный прицеп",driver.render(token,order,stops,places,"ru"))
        ukrainian = driver.render(token,order,stops,places,"uk")
        self.assertIn("ПРИЧІП ЗАБРАНО",ukrainian)
        self.assertIn('?l=ru">RU</a>',ukrainian)
        self.assertEqual(driver.lang_of("uk"),"uk")

    def test_delivery_confirmation_frees_truck_from_confirmed_time(self):
        with connect(self.database) as db:
            first = app.create_order(db,self.manual_payload())  # unloading Tue 15:00, planned free 16:00
        with connect(self.database) as db:
            app.set_stop_status(db,{"order_id":first,"seq":1,"done_at":"2030-01-07T08:30"})
        with connect(self.database) as db:
            result = app.set_stop_status(db,{"order_id":first,"seq":2,"done_at":"2030-01-08T11:00"})
            self.assertEqual((result["status"],result["warning"]),("Completed",""))
        with connect(self.database) as db:
            self.assertEqual(app.item(db,"orders",first)["available_at"][:16],"2030-01-08T11:00")
            # Free from 11:00 now: a loading at 12:00 fits, which the plan (16:00) did not allow.
            second = app.create_order(db,self.manual_payload(loading_id=self.b,unloading_id=self.a,
                                                             pickup_at="2030-01-08T12:00",unloading_arrive_at="2030-01-09T10:00"))
        with connect(self.database) as db:
            app.set_stop_status(db,{"order_id":first,"seq":2,"done":False})
        with connect(self.database) as db:
            self.assertEqual(app.item(db,"orders",first)["available_at"][:16],"2030-01-08T16:00")
            # Delivered late: the truck is busy longer and the next order is flagged.
            late = app.set_stop_status(db,{"order_id":first,"seq":2,"done_at":"2030-01-08T13:00"})
            self.assertIn(app.item(db,"orders",second)["number"],late["warning"])

    def test_manual_times_need_no_driver_clock(self):
        with connect(self.database) as db:
            c = self.add_location(db,"Zaragoza",41.65,-0.88)
        with connect(self.database) as db:
            order_id = app.create_order(db,self.manual_payload(
                distance_km=300,via=[{"kind":"Loading","location_id":c,"handling_hours":1,"leg_km":320,"arrive_at":"2030-01-07T14:00"}]))
            order = [o for o in rows(db,"orders") if o["id"]==order_id][0]
        self.assertEqual([s["planned_at"][:16] for s in order["stops"]],["2030-01-07T08:00","2030-01-07T14:00","2030-01-08T15:00"])
        self.assertEqual(order["available_at"][:16],"2030-01-08T16:00")
        self.assertEqual(order["distance_km"],620)
        self.assertFalse(app.uses_rules(order["plan"]))
        with connect(self.database) as db:
            # The next order of the same driver has no clock to carry and may also use rules.
            self.assertIn("manual times",app.carry_clock(db,{**self.payload,"pickup_at":"2030-01-09T08:00"})["message"])
            app.create_order(db,{**self.payload,"pickup_at":"2030-01-09T08:00"})

    def test_manual_times_validation(self):
        with connect(self.database) as db:
            with self.assertRaisesRegex(ValueError,"planned arrival"):
                app.create_order(db,self.manual_payload(unloading_arrive_at=""))
        with connect(self.database) as db:
            with self.assertRaisesRegex(ValueError,"cannot be before the previous stop"):
                app.create_order(db,self.manual_payload(unloading_arrive_at="2030-01-07T08:30"))

    def test_update_eta_moves_later_stops(self):
        with connect(self.database) as db:
            c = self.add_location(db,"Zaragoza",41.65,-0.88)
        with connect(self.database) as db:
            order_id = app.create_order(db,self.manual_payload(
                via=[{"kind":"Unloading","location_id":c,"handling_hours":1,"arrive_at":"2030-01-07T14:00"}]))
        with connect(self.database) as db:
            app.set_stop_status(db,{"order_id":order_id,"seq":1,"done_at":"2030-01-07T08:30"})
        with connect(self.database) as db:
            result = app.update_stop_eta(db,{"order_id":order_id,"seq":2,"planned_at":"2030-01-07T16:30"})
            order = [o for o in rows(db,"orders") if o["id"]==order_id][0]
        self.assertIn("16:30",result["message"])
        self.assertEqual([s["planned_at"][:16] for s in order["stops"]],["2030-01-07T08:00","2030-01-07T16:30","2030-01-08T17:30"])
        self.assertEqual(order["unloading_at"][:16],"2030-01-08T17:30")
        self.assertEqual(order["available_at"][:16],"2030-01-08T18:30")
        self.assertEqual(order["status"],"In transit")
        with connect(self.database) as db:
            with self.assertRaisesRegex(ValueError,"already confirmed"):
                app.update_stop_eta(db,{"order_id":order_id,"seq":1,"planned_at":"2030-01-07T09:00"})
            with self.assertRaisesRegex(ValueError,"before the previous confirmed"):
                app.update_stop_eta(db,{"order_id":order_id,"seq":2,"planned_at":"2030-01-07T08:00"})

    def test_multi_stop_journey_counts_as_one_order(self):
        with connect(self.database) as db:
            c = self.add_location(db,"Zaragoza",41.65,-0.88)
        with connect(self.database) as db:
            payload = {**self.payload,"distance_km":300,"driving_hours":4,
                       "via":[{"kind":"Loading","location_id":c,"handling_hours":1,"leg_km":320,"leg_hours":4}]}
            order_id = app.create_order(db,payload)
            order = [o for o in rows(db,"orders") if o["id"]==order_id][0]
        self.assertEqual([s["kind"] for s in order["stops"]],["Loading","Loading","Unloading"])
        self.assertEqual([s["location_id"] for s in order["stops"]],[self.a,c,self.b])
        times = [s["planned_at"] for s in order["stops"]]
        self.assertEqual(times,sorted(times))
        self.assertEqual((order["distance_km"],order["driving_hours"]),(620,8))
        self.assertEqual(order["unloading_id"],self.b)
        self.assertEqual(len(order["plan"]["stops"]),3)

    def test_trailer_swap_confirmation_parks_old_trailer(self):
        with connect(self.database) as db:
            swap = self.add_location(db,"Zaragoza",41.65,-0.88)
        with connect(self.database) as db:
            payload = {**self.payload,"trailer_id":self.trailer,"distance_km":300,"driving_hours":4,
                       "via":[{"kind":"Trailer swap","location_id":swap,"handling_hours":0.5,"leg_km":320,"leg_hours":4,
                               "pick_trailer_id":"","new_pick_trailer":"tr-new"}]}
            order_id = app.create_order(db,payload)
            stops = app.order_stop_rows(db,order_id)
            new_trailer = app.find_by_plate(db,"trailers","TR-NEW")
            self.assertEqual((stops[1]["drop_trailer_id"],stops[1]["pick_trailer_id"]),(self.trailer,new_trailer["id"]))
            with self.assertRaisesRegex(ValueError,"Confirm stop 1"):
                app.set_stop_status(db,{"order_id":order_id,"seq":2})
            self.assertEqual(app.set_stop_status(db,{"order_id":order_id,"seq":1,"done_at":"2030-01-07T09:00"})["status"],"In transit")
            app.set_stop_status(db,{"order_id":order_id,"seq":2,"done_at":"2030-01-07T14:00"})
            old = app.item(db,"trailers",self.trailer)
            self.assertEqual((old["parked_location_id"],old["parked_at"]),(swap,"2030-01-07T14:00+01:00"))
            self.assertEqual(app.item(db,"trucks",self.truck)["trailer_id"],new_trailer["id"])
        with connect(self.database) as db:
            with self.assertRaisesRegex(ValueError,"Only planned orders can be edited"):
                app.create_order(db,{**payload,"id":order_id})
        with connect(self.database) as db:
            # Undo the swap: the old trailer is back on the tractor, the new one waits at the swap point.
            app.set_stop_status(db,{"order_id":order_id,"seq":2,"done":False})
            self.assertIsNone(app.item(db,"trailers",self.trailer)["parked_location_id"])
            self.assertEqual(app.item(db,"trailers",new_trailer["id"])["parked_location_id"],swap)
            self.assertEqual(app.item(db,"trucks",self.truck)["trailer_id"],self.trailer)
            with self.assertRaisesRegex(ValueError,"Only the last confirmed stop"):
                app.set_stop_status(db,{"order_id":order_id,"seq":2,"done":False})
            app.set_stop_status(db,{"order_id":order_id,"seq":2,"done_at":"2030-01-07T14:00"})
            with self.assertRaisesRegex(ValueError,"cannot be before the previous stop"):
                app.set_stop_status(db,{"order_id":order_id,"seq":3,"done_at":"2030-01-07T13:00"})
            result = app.set_stop_status(db,{"order_id":order_id,"seq":3,"done_at":"2030-01-07T19:30"})
            order = app.item(db,"orders",order_id)
            self.assertEqual((result["status"],order["status"],order["delivered_at"]),("Completed","Completed","2030-01-07T19:30+01:00"))
            # The picked-up trailer is busy for this journey: another order cannot use it at the same time.
            found = app.conflicts(db,999,999,"",parse_time("2030-01-07T15:00"),parse_time("2030-01-07T16:00"),trailer_id=new_trailer["id"])
            self.assertEqual(found,[order["number"]])

    def test_legacy_orders_get_stops_and_completed_stays_completed(self):
        with connect(self.database) as db:
            order_id = app.create_order(db,self.payload)
            db.execute("DELETE FROM order_stops")
            db.execute("UPDATE orders SET status='Completed' WHERE id=?",(order_id,))
        initialize(self.database)
        initialize(self.database)
        with connect(self.database) as db:
            stops = app.order_stop_rows(db,order_id)
            order = app.item(db,"orders",order_id)
        self.assertEqual([(s["seq"],s["kind"],s["status"]) for s in stops],[(1,"Loading","Done"),(2,"Unloading","Done")])
        self.assertEqual(order["delivered_at"],order["unloading_at"])

    def test_legacy_trailers_import_once(self):
        with connect(self.database) as db:
            db.execute("UPDATE trucks SET trailer=' legacy-123 ',trailer_id=NULL WHERE id=?",(self.truck,))
        initialize(self.database)
        initialize(self.database)
        with connect(self.database) as db:
            truck=app.item(db,"trucks",self.truck)
            self.assertEqual(app.item(db,"trailers",truck["trailer_id"])["plate"],"LEGACY-123")
            self.assertEqual(truck["trailer"]," legacy-123 ")
            self.assertEqual(len(rows(db,"trailers")),2)

    def test_24_driving_hours_cross_days_and_rest(self):
        plan=trip_plan(parse_time("2030-01-07T08:00"),24)
        self.assertGreater(plan["unloading_at"][:10],"2030-01-07")
        self.assertEqual(sum(e["minutes"] for e in plan["timeline"] if e["kind"]=="Driving"),1440)
        self.assertEqual(sum(e["minutes"] for e in plan["timeline"] if e["kind"]=="Daily rest"),1320)
        self.assertTrue(all(e["minutes"]<=270 for e in plan["timeline"] if e["kind"]=="Driving"))

    def test_driver_document_create_edit_and_old_client(self):
        with connect(self.database) as db:
            driver_id=app.save_master(db,"drivers",{"name":"Document Test","dni_nie":" x1234567a "})
            self.assertEqual(app.item(db,"drivers",driver_id)["dni_nie"],"X1234567A")
            app.save_master(db,"drivers",{"id":driver_id,"name":"Document Test Updated"})
            self.assertEqual(app.item(db,"drivers",driver_id)["dni_nie"],"X1234567A")
            app.save_master(db,"drivers",{"id":driver_id,"name":"Document Test Updated","dni_nie":""})
            self.assertEqual(app.item(db,"drivers",driver_id)["dni_nie"],"")

    def test_old_database_migration_preserves_drivers(self):
        legacy=Path(self.temp.name)/"legacy.sqlite3"
        with connect(legacy) as db:
            db.execute("CREATE TABLE drivers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', carrier TEXT NOT NULL DEFAULT 'MalcomTrans', active INTEGER NOT NULL DEFAULT 1)")
            db.execute("INSERT INTO drivers (id,name) VALUES (17,'Existing Driver')")
        initialize(legacy)
        initialize(legacy)
        with connect(legacy) as db:
            driver=app.item(db,"drivers",17)
            self.assertEqual(driver["name"],"Existing Driver")
            self.assertEqual(driver["dni_nie"],"")

    def test_upcoming_week_bounds_and_carryover(self):
        now=parse_time("2030-01-07T08:00")
        with connect(self.database) as db:
            result=weekly_availability(db,{"week_offset":0,"week_count":2},now)
        self.assertEqual([w["start"] for w in result["weeks"]],["2030-01-07","2030-01-14"])
        self.assertEqual(len(result["weeks"][0]["windows"]),1)
        second=result["weeks"][1]["windows"][0]
        self.assertEqual(second["from"][:16],"2030-01-14T00:00")
        self.assertEqual(second["until"][:16],"2030-01-21T00:00")
        self.assertTrue(second["week_end_boundary"])

    def test_upcoming_rejects_outside_two_week_horizon(self):
        with connect(self.database) as db:
            for offset,count in [(3,1),(-1,1),(2,2),(0,3)]:
                with self.assertRaises(ValueError):
                    weekly_availability(db,{"week_offset":offset,"week_count":count},parse_time("2030-01-07T08:00"))

    def test_upcoming_excludes_bookings_and_uses_dropoff(self):
        with connect(self.database) as db:
            order_id=app.create_order(db,{**self.payload,"driving_hours":4,"distance_km":250})
        with connect(self.database) as db:
            order=app.item(db,"orders",order_id)
            result=weekly_availability(db,{"week_offset":0,"week_count":1},parse_time("2030-01-07T08:00"))
        window=result["weeks"][0]["windows"][0]
        self.assertEqual(window["from"],order["available_at"])
        self.assertEqual(window["location"]["id"],self.b)
        self.assertEqual(window["source_order_id"],order_id)

    def test_upcoming_infers_driver_from_order_without_default_assignment(self):
        with connect(self.database) as db:
            app.create_order(db,{**self.payload,"driving_hours":4,"distance_km":250})
        with connect(self.database) as db:
            db.execute("UPDATE trucks SET driver_id=NULL, trailer='' WHERE id=?",(self.truck,))
            result=weekly_availability(db,{"week_offset":0,"week_count":1},parse_time("2030-01-07T08:00"))
        window=result["weeks"][0]["windows"][0]
        self.assertEqual(window["driver_id"],self.driver)
        self.assertEqual(window["trailer"],"TR-001")
        self.assertEqual(result["unknown"],[])

    def test_upcoming_tracks_tractor_even_without_driver(self):
        with connect(self.database) as db:
            db.execute("UPDATE trucks SET driver_id=NULL WHERE id=?",(self.truck,))
            result=weekly_availability(db,{"week_offset":0,"week_count":1},parse_time("2030-01-07T08:00"))
        self.assertEqual(result["weeks"][0]["windows"][0]["plate"],"TEST-001")
        self.assertIsNone(result["weeks"][0]["windows"][0]["driver_id"])
        self.assertEqual(result["unknown"],[])

    def test_upcoming_other_tractor_driver_booking_does_not_hide_truck(self):
        with connect(self.database) as db:
            app.create_order(db,{**self.payload,"driving_hours":4,"distance_km":250})
        with connect(self.database) as db:
            other=app.save_master(db,"trucks",{"plate":"TEST-OTHER","driver_id":self.driver,"operation":"Local","location_id":self.a,"available_from":"2030-01-07T08:00"})
        with connect(self.database) as db:
            result=weekly_availability(db,{"week_offset":0,"week_count":1},parse_time("2030-01-07T08:00"))
        windows=[w for w in result["weeks"][0]["windows"] if w["truck_id"]==other]
        self.assertEqual(windows[0]["from"][:16],"2030-01-07T08:00")

    def test_upcoming_expired_gaps_do_not_reappear(self):
        with connect(self.database) as db:
            first=app.create_order(db,{**self.payload,"driving_hours":1,"distance_km":50})
        with connect(self.database) as db:
            following={**self.payload,"pickup_at":"2030-01-08T08:00","driving_hours":1,"distance_km":50}
            following["clock"]=app.carry_clock(db,following)["clock"]
            app.create_order(db,following)
        now=parse_time("2030-01-09T08:00")
        with connect(self.database) as db:
            result=weekly_availability(db,{"week_offset":0,"week_count":1},now)
        windows=result["weeks"][0]["windows"]
        self.assertEqual(len(windows),1)
        self.assertTrue(all(parse_time(w["from"])>=now for w in windows))
        self.assertNotEqual(windows[0]["source_order_id"],first)

    def test_upcoming_overlapping_driver_trailer_bookings_are_merged(self):
        start=parse_time("2030-01-07T08:00")
        end=parse_time("2030-01-07T20:00")
        bookings=[(parse_time("2030-01-07T09:00"),parse_time("2030-01-07T12:00")),
                  (parse_time("2030-01-07T11:00"),parse_time("2030-01-07T15:00"))]
        windows=list(subtract_bookings(start,end,bookings))
        self.assertEqual([(stamp(a)[11:16],stamp(b)[11:16]) for a,b in windows],[("08:00","09:00"),("15:00","20:00")])

    def test_exact_break_boundary_and_daily_limit(self):
        plan=trip_plan(parse_time("2030-01-07T08:00"),4.5,0,0)
        self.assertEqual(plan["rest_hours"],0)
        plan=trip_plan(parse_time("2030-01-07T08:00"),9.01,0,0)
        self.assertTrue(any(e["kind"]=="Daily rest" for e in plan["timeline"]))

    def test_existing_clock_and_weekly_limits(self):
        initial={"drive_today":4,"drive_since_break":4,"duty_today":5,"drive_week":55,"drive_previous_week":30}
        plan=trip_plan(parse_time("2030-01-07T08:00"),2,0,0,initial)
        self.assertTrue(any(e["kind"]=="Weekly-limit rest" for e in plan["timeline"]))
        end=Clock(parse_time(plan["available_at"]),state=plan["end_state"])
        for amount in end.weeks.values():
            self.assertLessEqual(amount,3360)

    def test_fortnight_and_due_weekly_rest(self):
        initial={"drive_week":40,"drive_previous_week":50,"weekly_rest_due_in":1}
        plan=trip_plan(parse_time("2030-01-07T08:00"),2,0,0,initial)
        self.assertTrue(any(e["kind"]=="Weekly-limit rest" for e in plan["timeline"]))
        plan=trip_plan(parse_time("2030-01-07T08:00"),5,0,0,{"weekly_rest_due_in":2})
        self.assertTrue(any(e["kind"]=="Weekly rest" for e in plan["timeline"]))

    def test_dst_nonexistent_and_ambiguous(self):
        with self.assertRaises(ValueError): parse_time("2030-03-31T02:30")
        with self.assertRaises(ValueError): parse_time("2030-10-27T02:30")
        self.assertEqual(stamp(parse_time("2030-03-31T03:30")),"2030-03-31T03:30+02:00")

    def test_order_number_history_and_collision(self):
        with connect(self.database) as db:
            first=app.create_order(db,self.payload)
        with connect(self.database) as db:
            order=app.item(db,"orders",first)
            self.assertEqual(order["number"],"MT-2030-000001")
            with self.assertRaisesRegex(ValueError,"already assigned"):
                app.create_order(db,self.payload)
        with connect(self.database) as db:
            self.assertEqual(len(rows(db,"orders")),1)
            with self.assertRaisesRegex(ValueError,"preserve order history"):
                app.save_master(db,"locations",{"id":self.a,"company":"Changed","city":"Madrid","postal_code":"28001","country":"Spain"})

    def test_same_trailer_and_driver_cannot_overlap(self):
        with connect(self.database) as db:
            app.create_order(db,self.payload)
        with connect(self.database) as db:
            d2=app.save_master(db,"drivers",{"name":"Second Driver"})
            t2=app.save_master(db,"trucks",{"plate":"TEST-002","operation":"Local","driver_id":d2})
        with connect(self.database) as db:
            with self.assertRaisesRegex(ValueError,"already assigned"):
                app.create_order(db,{**self.payload,"truck_id":t2,"driver_id":d2})

    def test_carry_forward_prevents_fresh_clock_reset(self):
        with connect(self.database) as db:
            first=app.create_order(db,{**self.payload,"driving_hours":4,"distance_km":250})
        with connect(self.database) as db:
            o=app.item(db,"orders",first)
            following={**self.payload,"pickup_at":o["available_at"],"driving_hours":1,"distance_km":50}
            with self.assertRaisesRegex(ValueError,"higher driving/duty clock"):
                app.build_plan(db,following)
            clock=app.carry_clock(db,following)["clock"]
            result=app.build_plan(db,{**following,"clock":clock})
            self.assertGreaterEqual(result[-1]["rest_hours"],.75)

    def test_inserting_earlier_order_protects_future_clocks(self):
        with connect(self.database) as db:
            app.create_order(db,{**self.payload,"pickup_at":"2030-01-14T08:00","driving_hours":1,"distance_km":50})
        with connect(self.database) as db:
            with self.assertRaisesRegex(ValueError,"Add jobs chronologically"):
                app.create_order(db,self.payload)

    def test_here_route_profile_and_units(self):
        with patch.object(app.routing,"request",return_value={"routes":[{"sections":[{"summary":{"length":123000,"duration":7200}}]}]}) as request:
            route=app.routing.truck_route({"lat":1,"lon":2},{"lat":3,"lon":4},{"height":4,"width":2.55,"length":16.5,"weight":40000,"trailer":"TR"},"2030-01-07T09:00+01:00")
            self.assertEqual(route["distance_km"],123)
            self.assertEqual(route["driving_hours"],2)
            query=request.call_args.args[1]
            self.assertEqual(query["transportMode"],"truck")
            self.assertEqual(query["vehicle[height]"],400)
            self.assertEqual(query["vehicle[grossWeight]"],40000)

    def test_here_departure_includes_seconds(self):
        self.assertEqual(app.routing.routing_departure("2026-11-12T09:00+01:00"),"2026-11-12T09:00:00+01:00")
        self.assertEqual(app.routing.routing_departure("2026-11-12T08:00Z"),"2026-11-12T08:00:00+00:00")
        self.assertEqual(app.routing.routing_departure("any"),"any")
        with self.assertRaisesRegex(ValueError,"timezone"):
            app.routing.routing_departure("2026-11-12T09:00")
        with patch.object(app.routing,"request",return_value={"routes":[{"sections":[{"summary":{"length":1000,"duration":60}}]}]}) as request:
            app.routing.truck_route({"lat":1,"lon":2},{"lat":3,"lon":4},{"height":4,"width":2.55,"length":16.5,"weight":40000,"trailer":"T"},"2026-11-12T09:00+01:00")
            self.assertEqual(request.call_args.args[1]["departureTime"],"2026-11-12T09:00:00+01:00")

    def test_here_error_explains_parameter_without_disclosing_key(self):
        import io
        body=json.dumps({"cause":"Invalid departureTime; key=test-secret. https://example.test/?apiKey=test-secret"}).encode()
        error=HTTPError("https://example.test/?apiKey=test-secret",400,"Bad Request",{},io.BytesIO(body))
        with patch.object(app.routing,"api_key",return_value="test-secret"),patch.object(app.routing,"urlopen",side_effect=error):
            with self.assertRaises(ValueError) as result:
                app.routing.request("https://example.test",{})
        self.assertIn("departureTime",str(result.exception))
        self.assertNotIn("test-secret",str(result.exception))
        self.assertNotIn("https://",str(result.exception))

    def test_http_security_save_read_backup(self):
        server=app.ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        server.database=self.database
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        url=f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(url) as response:
                self.assertIn(b"Daily Traking MalcomTrans",response.read())
            with urlopen(url+"/api/state") as response:
                state=json.load(response)
            self.assertEqual(state["trailers"][0]["plate"],"TR-001")
            for asset in ("/analytics.js","/search-select.js"):
                with urlopen(url+asset) as response:
                    self.assertEqual(response.status,200)
            request=Request(url+"/api/trailers",data=json.dumps({"plate":"HTTP-TRAILER"}).encode(),headers={"X-CSRF-Token":state["token"],"Content-Type":"application/json"})
            with urlopen(request) as response:
                self.assertTrue(json.load(response)["id"])
            with self.assertRaises(HTTPError) as err:
                urlopen(Request(url+"/api/orders",data=b"{}"))
            self.assertEqual(err.exception.code,403)
            request=Request(url+"/api/orders",data=json.dumps(self.payload).encode(),headers={"X-CSRF-Token":state["token"],"Content-Type":"application/json"})
            with urlopen(request) as response:
                self.assertTrue(json.load(response)["id"])
            with urlopen(url+"/api/backup") as response:
                backup=Path(self.temp.name)/"backup.sqlite3"
                backup.write_bytes(response.read())
            with connect(backup) as db:
                self.assertEqual(len(rows(db,"orders")),1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_tractor_maintenance_km_are_saved_and_service_done_sets_the_next(self):
        from masters import _today, service_done
        base = {"id":self.truck,"plate":"TEST-001","trailer_id":"","operation":"International","location_id":self.a}
        with connect(self.database) as db:
            truck = app.item(db,"trucks",self.truck)
            self.assertEqual((truck["service_every_km"], truck["service_due_km"], truck["odometer_on"]), (60000, None, None))
            app.save_master(db,"trucks",{**base,"service_due_km":"540000","odometer_km":"512300","monthly_km":"11000","service_every_km":""})
            truck = app.item(db,"trucks",self.truck)
            self.assertEqual((truck["service_due_km"], truck["odometer_km"], truck["monthly_km"], truck["service_every_km"], truck["odometer_on"]),
                             (540000, 512300, 11000, 60000, _today()))
            db.execute("UPDATE trucks SET odometer_on='2026-01-01' WHERE id=?", (self.truck,))
            app.save_master(db,"trucks",{**base,"service_due_km":"540000","odometer_km":"512300","monthly_km":"12000"})
            self.assertEqual(app.item(db,"trucks",self.truck)["odometer_on"], "2026-01-01")  # same reading: keeps its day
            app.save_master(db,"trucks",{**base})  # an older form without the fields keeps them
            self.assertEqual(app.item(db,"trucks",self.truck)["service_due_km"], 540000)
            with self.assertRaises(ValueError):
                app.save_master(db,"trucks",{**base,"odometer_km":"-5"})
            self.assertIn("Next one at 601.000 km", service_done(db,{"id":self.truck,"km":"541000","day":"2026-11-20"})["message"])
            truck = app.item(db,"trucks",self.truck)
            self.assertEqual((truck["odometer_km"], truck["odometer_on"], truck["service_due_km"]), (541000, "2026-11-20", 601000))
            with self.assertRaises(ValueError):
                service_done(db,{"id":self.truck,"km":"541000","day":"20.11.2026"})

    def test_witruck_import_ranks_key_clients_and_lanes_near_a_truck(self):
        from datetime import datetime
        from market import import_witruck, key_clients, lanes_near, number, postal
        from nextloads import next_loads
        from planner import UTC, ZONE
        self.assertEqual((number("5,78"), number("2.200"), number("0"), postal("2.08755"), postal("08210."), postal("2408 ZC")),
                         (5.78, 2200, None, "08755", "08210", "2408ZC"))
        routes = ("S.No;numero;orden de carga;fecha;estado;distancia;tractora;conductor;tipo transportista;comentarios;comentarios oc;"
                  "nombre del origen;direccion origen;cp origen;poblacion origen;provincia origen;pais origen;fecha prev carga;"
                  "nombre del destino;direccion destino;cp destino;poblacion destino;provincia destino;pais destino;fecha prev descarga;"
                  "cif transportista;nombre fiscal\r\n")
        billing = ("S.No;orden de ruta;orden de carga;fecha oc;estado oc;comentarios oc;tractora;remolque;conductor;tipo transportista;"
                   "nombre cliente;cif cliente;sede;poblacion origen or;cp origen or;poblacion destino or;cp destino or;material;"
                   "codigo de material;peso origen;peso destino;precio unitario oc;precio total oc;tipo de precio oc;precio total or\r\n")
        with connect(self.database) as db:
            client = app.save_master(db,"locations",{"company":"FERIMET S.L.","city":"Madrid","postal_code":"28001","country":"Spain","kind":"Client"})
            app.save_tariff(db,{"client_id":client,"from_id":self.a,"to_id":self.b,"basis":"trip","rate":100})
        day = datetime.now(UTC).astimezone(ZONE).date().isoformat()
        for n in range(3):
            own = "I" if n == 0 else "A"
            carrier = "" if own == "I" else "GROUPTRANS S. COOP."
            routes += (f"{n};{900+n};1;{day};FINALIZADA;1270;7377NNV;Javier;{own};;;WAREHOUSE A;Calle 1;28001;Madrid;Madrid;España;;"
                       f"Warehouse B;Rue 2;75001;Paris;Paris;Francia;;B1;{carrier}\r\n")
            billing += (f"{n};{900+n};1;{day};FACTURADA;;7377NNV;R1;Javier;{own};FERIMET S.L.;B17327610;;Madrid;28001;Paris;75001;"
                        f"XATARRA;21;24;;6,5;156;Peso origen (Tn) x €;156\r\n")
        with connect(self.database, fresh=True) as db:
            self.assertIn("3 Witruck routes", import_witruck(db, {"files": [routes, billing]})["message"])
        with connect(self.database, fresh=True) as db:
            self.assertIn("0 new", import_witruck(db, {"files": [billing]})["message"])  # the same routes again: updated, not doubled
        with self.assertRaises(ValueError):
            with connect(self.database, fresh=True) as db:
                import_witruck(db, {"files": ["a;b\r\n1;2\r\n"]})
        with connect(self.database) as db:
            ranking = key_clients(db)
            self.assertEqual(ranking["total"], 3)
            top = ranking["clients"][0]
            self.assertEqual((top["name"], top["client_id"], top["trips"], top["own"], top["revenue"]), ("Ferimet S.L.", client, 3, 1, 468))
            lanes = lanes_near(db, app.item(db, "locations", self.a), 50)
            self.assertEqual(len(lanes), 1)
            lane = lanes[0]
            self.assertEqual((lane["client_id"], lane["from"]["id"], lane["to"]["id"], lane["trips"], lane["own"], lane["carriers"]),
                             (client, self.a, self.b, 3, 1, ["GROUPTRANS S. COOP."]))
            self.assertEqual(lanes_near(db, app.item(db, "locations", self.b), 50), [])  # nothing loads near Paris
            self.assertEqual(next_loads(db, {"location_id": self.a})["market"][0]["trips"], 3)

    def test_client_link_shows_only_own_routes_without_prices(self):
        from clientlinks import client_link, client_routes, load_requests, set_request_status
        with connect(self.database) as db:
            ferimet=app.save_master(db,"locations",{"company":"FERIMET S.L.","city":"Barcelona","postal_code":"08001","country":"Spain","kind":"Client"})
            other=app.save_master(db,"locations",{"company":"Other Client","city":"Bilbao","postal_code":"48001","country":"Spain","kind":"Client"})
            secret=app.save_master(db,"locations",{"company":"Secret Destination","city":"Lyon","postal_code":"69001","country":"France"})
            app.save_tariff(db,{"client_id":ferimet,"from_id":self.a,"to_id":self.b,"basis":"trip","rate":1777,"notes":"internal 1777 eur"})
            app.save_tariff(db,{"client_id":other,"from_id":self.b,"to_id":secret,"basis":"trip","rate":999})
            link=client_link(db,{"client_id":ferimet})
            self.assertEqual(link["routes"],1)
            self.assertEqual(client_link(db,{"client_id":ferimet})["token"],link["token"])  # the same link every time
            self.assertEqual([(r["from"]["id"],r["to"]["id"]) for r in client_routes(db,ferimet)],[(self.a,self.b)])
            # A branch without routes of its own points to the company that has them.
            branch=app.save_master(db,"locations",{"company":"Ferimet Sabadell","city":"Sabadell","postal_code":"08202","country":"Spain","kind":"Client"})
            self.assertEqual([r["id"] for r in client_link(db,{"client_id":branch})["related"]],[ferimet])
        server=app.ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        server.database=self.database
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        url=f"http://127.0.0.1:{server.server_port}/c/{link['token']}"
        try:
            with urlopen(url+"?l=es") as response:
                page=response.read().decode()
            self.assertIn("Warehouse A · Madrid",page)
            self.assertIn("Warehouse B · Paris (France)",page)
            self.assertIn('value="24 t"',page)
            for hidden in ("Secret Destination","Other Client","1777","999","internal","€"):
                self.assertNotIn(hidden,page)
            with urlopen(url+"?l=en") as response:
                self.assertIn("Transport request",response.read().decode())
            with self.assertRaises(HTTPError) as err:
                urlopen(f"http://127.0.0.1:{server.server_port}/c/{'x'*24}")
            self.assertEqual(err.exception.code,404)
            # Another client's place cannot be sent through this link.
            form={"l":"es","from_id":str(self.b),"to_id":str(secret),"load_date":"2030-01-08","trucks":"1","weight":"24 t"}
            with patch("clientlinks.datetime") as clock:
                clock.now.return_value=parse_time("2030-01-07T09:00")
                with self.assertRaises(HTTPError) as err:
                    urlopen(Request(url,data=app.urlencode(form).encode()))
                self.assertEqual(err.exception.code,400)
                form.update(from_id=str(self.a),to_id=str(self.b),weight="26 t",reference="PO-77",trucks="2")
                with urlopen(Request(url,data=app.urlencode(form).encode())) as response:
                    self.assertIn("Solicitud enviada",response.read().decode())
                form.update(to_id="other",to_other="Nueva Planta, Calle 1, Zaragoza 50001",trucks="1")
                urlopen(Request(url,data=app.urlencode(form).encode())).read()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        with connect(self.database) as db:
            requests=load_requests(db,parse_time("2030-01-07T10:00"))
            self.assertEqual(len(requests),2)
            new_route,known=requests
            self.assertEqual((known["from_id"],known["to_id"],known["weight"],known["trucks"]),(self.a,self.b,"26 t",2))
            self.assertTrue(known["tariff_id"])
            self.assertIsNone(new_route["to_id"])
            self.assertIn("Zaragoza",new_route["to_text"])
            # Orders created from the request are linked; with 2 trucks it is planned after the second order.
            first=app.create_order(db,{**self.payload,"request_id":known["id"]})
            app.link_order(db,known["id"],first)
            self.assertEqual(load_requests(db,parse_time("2030-01-07T10:00"))[1]["status"],"New")
            app.link_order(db,known["id"],first+1)
            self.assertEqual(load_requests(db,parse_time("2030-01-07T10:00"))[1]["status"],"Planned")
            set_request_status(db,{"id":new_route["id"],"status":"Rejected","reason":"No trucks that day"})
        # The client sees the assembly that loads (tractor + trailer) and the day once the order is linked.
        server=app.ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        server.database=self.database
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/c/{link['token']}?l=en") as response:
                page=response.read().decode()
            self.assertIn("Truck <strong>TEST-001</strong> + trailer <strong>TR-001</strong> · loads on 07/01/2030",page)
            self.assertIn("No trucks that day",page)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        with connect(self.database) as db:
            app.revoke_client_link(db,{"client_id":ferimet})
            self.assertIsNone(app.client_by_token(db,link["token"]))

    def test_company_and_town_names_written_properly(self):
        from names import apply_name_fixes, name_fixes, proper_name
        cases={"FERIMET S.L":"Ferimet S.L.","TEGA GESTIÓ AMBIENTAL SL":"Tega Gestió Ambiental S.L.","Caldes de montbui":"Caldes de Montbui",
               "BEFESA LES FRANQUESES":"Befesa Les Franqueses","PLANTA DE LA VEGA":"Planta de la Vega","CSM Centre service métaux":"CSM Centre Service Métaux",
               "Planta PreZero Zona Franca":"Planta PreZero Zona Franca","METALES UNZUETA II S.L.":"Metales Unzueta II S.L.","MEGASIDER Zaragoza SAU":"Megasider Zaragoza S.A.U."}
        for before,after in cases.items():
            self.assertEqual(proper_name(before),after)
        for before,after in {"LONS-LE-SAUNIER":"Lons-le-Saunier","VILOBÍ D'ONYAR":"Vilobí d'Onyar","L'ESQUIROL":"L'Esquirol","SENTMENAT":"Sentmenat"}.items():
            self.assertEqual(proper_name(before,False),after)
        with connect(self.database) as db:
            created=app.save_master(db,"locations",{"company":"RECUPERACIONES PEREZ SL","city":"ARANJUEZ","postal_code":"28300","country":"Spain"})
            self.assertEqual((app.item(db,"locations",created)["company"],app.item(db,"locations",created)["city"]),("Recuperaciones Perez S.L.","Aranjuez"))
            db.execute("UPDATE locations SET company='WAREHOUSE B SL', city='PARIS' WHERE id=?",(self.b,))
            fixes={f["id"]:f for f in name_fixes(db)}
            self.assertEqual(fixes[self.b]["company"]["after"],"Warehouse B S.L.")
            with self.assertRaises(ValueError):  # a different name is not a capitals fix
                apply_name_fixes(db,{"items":[{"id":self.b,"company":"Another Company"}]})
            apply_name_fixes(db,{"items":[{"id":self.b,"company":"WareHouse B S.L.","city":"Paris"}]})
            self.assertEqual((app.item(db,"locations",self.b)["company"],app.item(db,"locations",self.b)["city"]),("WareHouse B S.L.","Paris"))

    def test_here_critical_notice_warns_instead_of_blocking(self):
        answer={"routes":[{"sections":[{"summary":{"length":250000,"duration":12600},
                 "notices":[{"code":"violatedVehicleRestriction","severity":"critical"}]}]}]}
        with patch.object(app.routing,"request",return_value=answer):
            leg=app.routing.truck_route({"lat":40.4,"lon":-3.7},{"lat":41.6,"lon":-0.9},{"height":4,"width":2.55,"length":16.5,"weight":40000},"any")
        self.assertEqual(leg["distance_km"],250.0)
        self.assertTrue(leg["critical"])
        self.assertIn("truck restriction",leg["notices"][0])

    def test_claude_connector_tools_read_the_planning(self):
        import assistant
        with connect(self.database) as db:
            app.create_order(db,self.payload)
        now=parse_time("2030-01-07T06:00")
        with connect(self.database) as db:
            fleet=assistant.call(db,"fleet_status",{},now)
            truck=fleet["tractors"][0]
            self.assertEqual(truck["plate"],"TEST-001")
            self.assertEqual(truck["free_at"]["city"],"Paris")
            self.assertEqual(len(truck["bookings"]),1)
            self.assertEqual(assistant.call(db,"find_places",{"query":"warehouse par"},now)["places"][0]["id"],self.b)
            suggestion=assistant.call(db,"suggest_tractors",{"location_id":self.a,"at":"2030-01-07 10:00"},now)
            self.assertEqual(suggestion["options"][0]["plate"],"TEST-001")
            loads=assistant.call(db,"loads_near",{"plate":"test 001"},now)
            self.assertEqual(loads["from"]["city"],"Paris")
            with self.assertRaises(ValueError):
                assistant.call(db,"loads_near",{"plate":"NOPE"},now)
        # Arriving at night or after 18:30: the loading starts at 08:00.
        self.assertEqual(stamp(assistant.can_work(parse_time("2030-01-08T02:10"))),"2030-01-08T08:00+01:00")
        self.assertEqual(stamp(assistant.can_work(parse_time("2030-01-08T19:00"))),"2030-01-09T08:00+01:00")

    def test_claude_connector_needs_sign_in_and_pkce(self):
        import base64, hashlib
        from urllib.parse import parse_qs, urlencode, urlsplit
        from urllib.request import HTTPRedirectHandler, build_opener
        server=app.ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        server.database=self.database
        server.auth=app.Auth(None,"test-secret",{"ana":"long password 1"})
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        url=f"http://127.0.0.1:{server.server_port}"
        class Stay(HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):
                return None
        opener=build_opener(Stay)
        def send(path,data,form=False,headers=None):
            body=urlencode(data).encode() if form else json.dumps(data).encode()
            request=Request(url+path,data=body,headers={"Content-Type":"application/x-www-form-urlencoded" if form else "application/json",**(headers or {})})
            try:
                with opener.open(request) as response:
                    return response.status,response.headers,response.read()
            except HTTPError as error:
                return error.code,error.headers,error.read()
        try:
            status,headers,_=send("/mcp",{"jsonrpc":"2.0","id":1,"method":"tools/list"})
            self.assertEqual(status,401)
            self.assertIn("oauth-protected-resource",headers["WWW-Authenticate"])
            self.assertEqual(send("/oauth/register",{"redirect_uris":["https://evil.example/cb"]})[0],400)
            client=json.loads(send("/oauth/register",{"redirect_uris":["https://claude.ai/api/mcp/auth_callback"]})[2])["client_id"]
            verifier="v"*50
            challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            ask={"response_type":"code","client_id":client,"redirect_uri":"https://claude.ai/api/mcp/auth_callback","state":"s1",
                 "code_challenge":challenge,"code_challenge_method":"S256"}
            self.assertEqual(send("/oauth/authorize",{**ask,"user":"ana","password":"wrong"},True)[0],401)
            status,headers,_=send("/oauth/authorize",{**ask,"user":"ana","password":"long password 1"},True)
            self.assertEqual(status,303)
            self.assertTrue(headers["Location"].startswith("https://claude.ai/api/mcp/auth_callback?"))
            code=parse_qs(urlsplit(headers["Location"]).query)["code"][0]
            exchange={"grant_type":"authorization_code","code":code,"redirect_uri":ask["redirect_uri"],"client_id":client}
            self.assertEqual(send("/oauth/token",{**exchange,"code_verifier":"wrong"},True)[0],400)
            status,_,body=send("/oauth/token",{**exchange,"code_verifier":verifier},True)
            self.assertEqual(status,200)
            tokens=json.loads(body)
            self.assertEqual(send("/oauth/token",{**exchange,"code_verifier":verifier},True)[0],400)  # a code works once
            bearer={"Authorization":"Bearer "+tokens["access_token"]}
            status,_,body=send("/mcp",{"jsonrpc":"2.0","id":2,"method":"tools/list"},headers=bearer)
            self.assertEqual({tool["name"] for tool in json.loads(body)["result"]["tools"]},
                             {"find_places","fleet_status","suggest_tractors","loads_near"})
            status,_,body=send("/mcp",{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"fleet_status","arguments":{}}},headers=bearer)
            self.assertIn("TEST-001",json.loads(body)["result"]["content"][0]["text"])
            self.assertEqual(send("/mcp",{"jsonrpc":"2.0","id":4,"method":"ping"},headers={"Authorization":"Bearer "+tokens["refresh_token"]})[0],401)
            # A removed dispatcher loses access.
            server.auth=app.Auth(None,"test-secret",{"other":"long password 2"})
            self.assertEqual(send("/mcp",{"jsonrpc":"2.0","id":5,"method":"ping"},headers=bearer)[0],401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_driver_link_page_confirms_next_stop_only(self):
        from auth import Auth
        import deca, http.client
        with connect(self.database) as db:
            order_id = app.create_order(db, self.manual_payload())
            self.assertEqual(app.driver_link(db, {"order_id": order_id})["url"], "")  # no online address yet
            deca.save_settings(db, {"public_url": "https://example.onrender.com/"})
            self.assertEqual(deca.settings(db)["carrier_name"], "")  # other settings untouched
            link = app.driver_link(db, {"order_id": order_id})
            self.assertEqual(app.driver_link(db, {"order_id": order_id})["token"], link["token"])  # same link every time
        self.assertEqual(link["url"], "https://example.onrender.com/s/" + link["token"])
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        server.database = self.database
        server.auth = Auth("dispatcher-password", "secret")  # online: dispatcher pages need login, the driver page does not
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_port
        def call(method, path, body=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/x-www-form-urlencoded"})
            response = connection.getresponse()
            data = response.read().decode(errors="replace")  # the DeCA PDF is binary
            connection.close()
            return response.status, dict(response.getheaders()), data
        token = link["token"]
        try:
            status, _, page = call("GET", f"/s/{token}")
            self.assertEqual(status, 200)
            self.assertIn("AM ÎNCĂRCAT", page)
            self.assertIn("google.com/maps", page)
            self.assertEqual(call("GET", "/s/not-a-real-token")[0], 404)
            self.assertIn("HE CARGADO", call("GET", f"/s/{token}?l=es")[2])
            # Two taps: the button asks "Confirmi?", then the form confirms.
            self.assertIn("DA, CONFIRM", call("GET", f"/s/{token}?c=1")[2])
            # Only the next stop can be confirmed.
            self.assertEqual(call("POST", f"/s/{token}", b"seq=2&l=ro")[0], 303)
            with connect(self.database) as db:
                self.assertEqual([s["status"] for s in app.order_stop_rows(db, order_id)], ["Planned", "Planned"])
            status, headers, _ = call("POST", f"/s/{token}", b"seq=1&l=ro")
            # After a loading the optional DeCA form is offered.
            self.assertEqual((status, headers["Location"]), (303, f"/s/{token}?l=ro&ok=1&deca=1"))
            page = call("GET", f"/s/{token}?l=ro&ok=1&deca=1")[2]
            self.assertIn("AM DESCĂRCAT", page)
            self.assertIn("SALVEAZĂ ȘI FĂ DeCA", page)
            self.assertNotIn("SALVEAZĂ ȘI FĂ DeCA", call("GET", f"/s/{token}?l=ro")[2])  # skipped: just the trip
            self.assertIn("Completează DeCA", call("GET", f"/s/{token}?l=ro")[2])
            self.assertIn("între 1 și 60000", call("POST", f"/s/{token}", "action=deca&seq=1&l=ro&weight_kg=0".encode())[2])
            # Saved, but the office still has to add what only it knows (company details, shipper NIF).
            status, headers, _ = call("POST", f"/s/{token}", "action=deca&seq=1&l=ro&material=Chatarra%20mixta&weight_kg=24500&dni=x%201234567l".encode())
            self.assertEqual((status, headers["Location"]), (303, f"/s/{token}?l=ro&dc=0"))
            self.assertIn("îl termină biroul", call("GET", f"/s/{token}?l=ro&dc=0")[2])
            with connect(self.database) as db:
                loading = app.order_stop_rows(db, order_id)[0]
                self.assertEqual((loading["material"], loading["material_id"], loading["weight_kg"]), ("Chatarra mixta", None, 24500))
                self.assertEqual(app.item(db, "drivers", self.driver)["dni_nie"], "X1234567L")
                deca.save_settings(db, {"carrier_name": "Carrier Test SL", "carrier_nif": "B00000000"})
                db.execute("UPDATE locations SET nif='B17327610' WHERE id=?", (self.a,))
            status, headers, _ = call("POST", f"/s/{token}", b"action=deca&seq=1&l=es")
            self.assertEqual(headers["Location"], f"/s/{token}?l=es&dc=1")
            with connect(self.database) as db:
                deca_id = app.item(db, "orders", order_id)["deca_id"]
                self.assertIn("— Chatarra mixta, 24.500 kg", dict(deca.document(db, order_id)["rows"])["1. Carga"])
            self.assertIn(f"/d/{deca_id}", call("GET", f"/s/{token}?l=es&dc=1")[2])
            status, headers, _ = call("GET", f"/d/{deca_id}")
            self.assertEqual((status, headers["Content-Type"]), (200, "application/pdf"))
            call("POST", f"/s/{token}", b"seq=1&l=ro")  # double tap: ignored
            call("POST", f"/s/{token}", b"seq=2&l=ro")
            with connect(self.database) as db:
                self.assertEqual([s["status"] for s in app.order_stop_rows(db, order_id)], ["Done", "Done"])
                self.assertEqual(app.item(db, "orders", order_id)["status"], "Completed")
            self.assertIn("finalizată", call("GET", f"/s/{token}")[2])
            # The dispatcher API still needs the password.
            self.assertEqual(call("GET", "/api/state")[0], 401)
            # A DeCA QR link opens without signing in; an unknown one is simply not found (no login page).
            self.assertEqual(call("GET", "/d/" + "0" * 32)[0], 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_driver_link_expires_a_week_after_delivery(self):
        from datetime import timedelta
        with connect(self.database) as db:
            order_id = app.create_order(db, self.manual_payload())
            token = app.driver_link(db, {"order_id": order_id})["token"]
            app.set_stop_status(db, {"order_id": order_id, "seq": 1})
            app.set_stop_status(db, {"order_id": order_id, "seq": 2})
            self.assertIsNotNone(app.driver_order(db, token)[0])  # just delivered: still visible
            old = app.stamp(app.parse_time(app.item(db, "orders", order_id)["delivered_at"]) - timedelta(days=8))
            db.execute("UPDATE orders SET delivered_at=? WHERE id=?", (old, order_id))
            self.assertIsNone(app.driver_order(db, token)[0])

    def test_automatic_backup_daily_and_pruned(self):
        import autobackup, os, time as clock
        folder = Path(self.temp.name) / "auto"
        first = autobackup.run_once(self.database, folder)
        self.assertTrue(first.exists())
        with connect(first) as db:
            self.assertEqual(len(rows(db, "trucks")), 1)
        self.assertIsNone(autobackup.run_once(self.database, folder))  # already one today
        # Old copies: only the last 30 are kept.
        for day in range(35):
            path = folder / f"MalcomTrans-2020-01-{day + 1:02d}.sqlite3" if day < 31 else folder / f"MalcomTrans-2020-02-{day - 30:02d}.sqlite3"
            path.write_bytes(b"x")
            os.utime(path, (clock.time() - 86400 * 40, clock.time() - 86400 * 40))
        os.utime(first, (clock.time() - 86400 * 2, clock.time() - 86400 * 2))
        made = autobackup.run_once(self.database, folder, now=clock.time())
        self.assertTrue(made.exists())
        self.assertEqual(len(autobackup.files(folder)), autobackup.KEEP)
        self.assertFalse(list(folder.glob("*.partial")))

    def test_security_headers_modules_and_visitor_address(self):
        from auth import Auth
        import http.client
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        server.database = self.database
        server.auth = Auth("correct horse battery", "secret")
        server.public_hosts = {"malcomtrans.onrender.com"}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_port
        def call(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
            response = connection.getresponse()
            data = response.read()
            connection.close()
            return response.status, dict(response.getheaders()), data
        try:
            status, headers, _ = call("GET", "/login", headers={"X-Forwarded-Proto": "https"})
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertIn("max-age", headers["Strict-Transport-Security"])
            self.assertEqual(headers["Server"], "MalcomTrans")
            # Interface modules load after login; nothing outside static/js can be read.
            self.assertEqual(call("GET", "/js/core.js")[0], 303)
            form = {"Content-Type": "application/x-www-form-urlencoded", "CF-Connecting-IP": "198.51.100.7"}
            _, headers, _ = call("POST", "/login", b"password=correct+horse+battery", form)
            session = {"Cookie": headers["Set-Cookie"].split(";")[0]}
            status, headers, body = call("GET", "/js/core.js", headers=session)
            self.assertEqual((status, headers["Content-Type"].split(";")[0]), (200, "text/javascript"))
            # Text is sent compressed when the browser accepts it; unchanged state is answered with 304.
            import gzip
            status, headers, body = call("GET", "/api/state", headers={**session, "Accept-Encoding": "gzip, deflate"})
            self.assertEqual((status, headers["Content-Encoding"]), (200, "gzip"))
            self.assertIn("orders", json.loads(gzip.decompress(body)))
            tag = headers["ETag"]
            status, headers, body = call("GET", "/api/state", headers={**session, "If-None-Match": tag})
            self.assertEqual((status, body, headers["ETag"]), (304, b"", tag))
            with connect(self.database) as db:
                app.save_master(db, "trailers", {"plate": "TR-NEW"})
            self.assertEqual(call("GET", "/api/state", headers={**session, "If-None-Match": tag})[0], 200)  # changed: sent again
            self.assertNotIn("Content-Encoding", call("GET", "/api/state", headers=session)[1])  # no gzip asked: plain
            for bad in ("/js/../app.py", "/js/missing.js", "/js/Core.js", "/app.js"):
                self.assertEqual(call("GET", bad, headers=session)[0], 404, bad)
            # Map tiles come through the server (HERE key stays there), only when signed in, cached by the browser.
            import routing
            with patch.object(routing, "map_tile", return_value=b"\x89PNG tile") as tile:
                status, headers, body = call("GET", "/tiles/6/32/23.png", headers=session)
                self.assertEqual((status, headers["Content-Type"], body, headers["Cache-Control"]),
                                 (200, "image/png", b"\x89PNG tile", "private, max-age=604800"))
                tile.assert_called_once_with(6, 32, 23)
                self.assertNotEqual(call("GET", "/tiles/6/32/23.png")[0], 200)  # not signed in
            self.assertEqual(call("GET", "/tiles/30/0/0.png", headers=session)[0], 404)  # outside the world
            # The lockout follows Cloudflare's address: a faked X-Forwarded-For does not get around it.
            with patch.object(app.time, "sleep"):
                for attempt in range(8):
                    call("POST", "/login", b"password=nope", {**form, "X-Forwarded-For": f"10.0.0.{attempt}"})
                self.assertEqual(call("POST", "/login", b"password=correct+horse+battery", {**form, "X-Forwarded-For": "10.9.9.9"})[0], 429)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_online_login_session_and_lockout(self):
        from auth import Auth
        import http.client
        server=app.ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        server.database=self.database
        server.auth=Auth("correct horse battery","fixed-secret")
        server.public_hosts={"malcomtrans.onrender.com"}
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        port=server.server_port
        def call(method,path,body=None,headers=None,host=None):
            connection=http.client.HTTPConnection("127.0.0.1",port,timeout=10)
            connection.request(method,path,body=body,headers={"Host":host or f"127.0.0.1:{port}",**(headers or {})})
            response=connection.getresponse()
            data=response.read()
            connection.close()
            return response.status,dict(response.getheaders()),data
        try:
            # Health check for the uptime monitor: no login, no database.
            self.assertEqual(call("GET","/healthz")[0],200)
            status,headers,body=call("HEAD","/healthz")  # UptimeRobot checks with HEAD
            self.assertEqual((status,body),(200,b""))
            # Not signed in: pages go to the login page, the API answers 401, the stylesheet still loads.
            status,headers,_=call("GET","/")
            self.assertEqual((status,headers["Location"]),(303,"/login"))
            self.assertEqual(call("GET","/api/state")[0],401)
            self.assertEqual(call("POST","/api/trailers",b"{}",{"Content-Type":"application/json"})[0],401)
            self.assertEqual(call("GET","/style.css")[0],200)
            self.assertIn(b"Sign in",call("GET","/login")[2])
            # The public host is accepted, any other host is refused.
            self.assertEqual(call("GET","/healthz",host="malcomtrans.onrender.com")[0],200)
            self.assertEqual(call("GET","/healthz",host="evil.example")[0],403)
            form={"Content-Type":"application/x-www-form-urlencoded"}
            with patch.object(app.time,"sleep"):
                status,_,page=call("POST","/login",b"password=wrong",form)
                self.assertEqual(status,401)
                self.assertIn(b"Wrong password",page)
                status,headers,_=call("POST","/login",b"password=correct+horse+battery",{**form,"X-Forwarded-Proto":"https"})
                self.assertEqual((status,headers["Location"]),(303,"/"))
                cookie=headers["Set-Cookie"]
                self.assertIn("HttpOnly",cookie)
                self.assertIn("Secure",cookie)
                session={"Cookie":cookie.split(";")[0]}
                status,_,body=call("GET","/api/state",headers=session)
                self.assertEqual(status,200)
                self.assertTrue(json.loads(body)["auth"])
                # A tampered cookie is rejected.
                tampered=session["Cookie"][:-1]+("1" if session["Cookie"].endswith("0") else "0")  # always a different last character
                self.assertEqual(call("GET","/api/state",headers={"Cookie":tampered})[0],401)
                # Too many wrong passwords from one address: blocked, even with the right one.
                for _ in range(8):
                    call("POST","/login",b"password=nope",{**form,"X-Forwarded-For":"203.0.113.9"})
                self.assertEqual(call("POST","/login",b"password=correct+horse+battery",{**form,"X-Forwarded-For":"203.0.113.9"})[0],429)
            status,headers,_=call("GET","/logout",headers=session)
            self.assertEqual((status,headers["Location"]),(303,"/login"))
            self.assertIn("Max-Age=0",headers["Set-Cookie"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_dispatcher_users_sign_in_with_their_own_name(self):
        from auth import Auth, parse_users
        import http.client
        users = parse_users(" Malcom1:first secret pw ; malcom2:second: secret ")
        self.assertEqual(users, {"malcom1": "first secret pw ", "malcom2": "second: secret "})
        for bad in ("nopassword", "bad name:secret", ":secret"):
            with self.assertRaisesRegex(ValueError, "APP_USERS"):
                parse_users(bad)
        users = {"malcom1": "first-secret-1", "malcom2": "second-secret-2"}
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        server.database = self.database
        server.auth = Auth(None, "fixed-secret", users)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_port
        def call(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
            response = connection.getresponse()
            data = response.read()
            connection.close()
            return response.status, dict(response.getheaders()), data
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            self.assertIn(b'name="user"', call("GET", "/login")[2])
            with patch.object(app.time, "sleep"):
                # Another user's password does not work, nor does a missing name.
                self.assertEqual(call("POST", "/login", b"user=malcom1&password=second-secret-2", form)[0], 401)
                self.assertEqual(call("POST", "/login", b"password=first-secret-1", form)[0], 401)
                sessions = {}
                for name, password in users.items():
                    _, headers, _ = call("POST", "/login", f"user={name.upper()}&password={password}".encode(), form)
                    sessions[name] = {"Cookie": headers["Set-Cookie"].split(";")[0]}
            # Both are signed in at the same time, each as themselves.
            for name, session in sessions.items():
                status, _, body = call("GET", "/api/state", headers=session)
                self.assertEqual((status, json.loads(body)["user"]), (200, name))
            # A user removed from APP_USERS is signed out; a shared-password cookie is not accepted.
            server.auth.users.pop("malcom2")
            self.assertEqual(call("GET", "/api/state", headers=sessions["malcom2"])[0], 401)
            shared = Auth("shared-password", "fixed-secret").new_cookie(False).split(";")[0]
            self.assertEqual(call("GET", "/api/state", headers={"Cookie": shared})[0], 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__=="__main__":
    unittest.main()
