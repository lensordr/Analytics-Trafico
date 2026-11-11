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

    def test_recommendation_proximity_unknown_and_busy(self):
        with connect(self.database) as db:
            result=app.recommend(db,{"location_id":self.a,"pickup_at":"2030-01-08T08:00"})
            self.assertEqual(result["candidates"][0]["truck_id"],self.truck)
            self.assertIsNone(result["candidates"][0]["feasible"])
            app.create_order(db,self.payload)
        with connect(self.database) as db:
            result=app.recommend(db,{"location_id":self.a,"pickup_at":"2030-01-08T08:00"})
            self.assertEqual(result["candidates"],[])

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


if __name__=="__main__":
    unittest.main()
