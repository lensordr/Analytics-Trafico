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
        self.assertEqual(rows["Cargador"], "Ferimet S.L.U (NIF B17327610)")
        self.assertEqual(rows["Domicilio cargador"], "Calle Ferralla, 12, 08755 Castellbisbal, España")
        self.assertEqual(rows["Transportista Efectivo"], "Carrier Test SL (NIF B00000000)")
        self.assertTrue(rows["1. Carga"].endswith("— XATARRA, 25.560 kg"))
        self.assertTrue(rows["2. Descarga"].endswith("— XATARRA, 25.560 kg"))
        self.assertEqual(doc["notes"], "—")  # observations were cleared by the second call
        self.assertTrue(name.endswith(".pdf") and body.startswith(b"%PDF-1.4") and body.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"(Documento de Control \\(DeCA\\)) Tj", body)
        self.assertIn(first["id"].encode(), body)
        self.assertIn("Cargador: Ferimet S.L.U (NIF B17327610)", deca.qr_text(doc))
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
                self.assertEqual(call("GET","/api/state",headers={"Cookie":session["Cookie"][:-1]+"0"})[0],401)
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


if __name__=="__main__":
    unittest.main()
