# Daily Traking MalcomTrans

English-language localhost dispatch planning for a single dispatcher, built with
Python and SQLite. No web-framework packages are required.

## Start

Requires Python 3.10+. Double-click **Start MalcomTrans.cmd**. On first run it
calls **Setup MalcomTrans.cmd**, which creates `.venv` and installs
`requirements.txt` (tzdata, libsql). The browser opens http://127.0.0.1:5000;
keep the terminal open and stop with Ctrl+C.

Alternatively, in PowerShell in this project folder:

    .venv\Scripts\python.exe app.py

Another port: `.venv\Scripts\python.exe app.py --port 5001`.

## Database: local SQLite or Turso

Without configuration the app uses the local file `data/malcomtrans.sqlite3`.

To use Turso, copy `.env.example` to `.env` and fill in:

- `TURSO_DATABASE_URL` — the `libsql://...` URL of the database;
- `TURSO_AUTH_TOKEN` — a token for that database (database → Create Token), not
  the organisation/platform API token.

The app then works on a local embedded replica (`data/turso-replica.db`):
reads are local and fast, writes go to Turso, and the replica syncs on start,
after every change and at least every 30 seconds while in use. An internet
connection is required for saving.

First-time move of existing data (app stopped, Turso database empty):

    .venv\Scripts\python.exe scripts\migrate_to_turso.py

It copies every table, verifies the row counts and never changes the local file.
`.env`, `data/` and `backups/` are excluded from git; never commit tokens.

## First use

1. Add Drivers & carriers.
   Each driver has an optional DNI / NIE field, available when adding or editing
   a driver and visible in the driver directory. It is stored locally in SQLite.
2. Add Locations: free-text company, city, postal code, country, optional address.
3. Add registrations in Trailers, then add Tractors, assign a saved trailer and
   driver, and choose International/National/Local. Tractor and trailer pickers
   support registration search; choose a result to assign it. An empty trailer
   means unassigned. Existing trailer registrations are imported automatically.
   Enter a known truck location and availability time for trucks without orders.
4. Create a New loading order. Choose tractor, trailer, driver and loading/dropoff
   points; enter the loading appointment and handling durations.
5. Enter road kilometres and pure driving hours, or choose HERE truck routing.
6. Review the driving clock at loading. Zeros assume qualifying fresh rest.
   Carry forward previous planning for subsequent orders; verify the assumptions.
7. Calculate ETA and save. The unique MT-year-number is generated on save.

Daily planning opens on today's date, with two separate lists: Loading today
and Unloading today. Choose a date, Previous day, Next day or Today to change
both lists together. Loading orders are sorted by pickup time; unloading orders
by estimated unloading time. A same-day trip appears once in each list, using
the same loading-order number. Each row keeps the complete journey visible and
highlights the stop relevant to that list. Availability after unloading appears
alongside the route. Filters by registration, location, operation and status
apply to both lists. Use Upcoming planning for calendar-week availability.
Each trip row shows a compact completed-trips badge for that tractor and selected
day, against a target of 4. It counts only Completed orders on their unloading
date, across all locations and operations, independently of visible filters.
Loading and unloading rows for the same order do not count as separate trips.
The badge is amber below 4 and green at 4 or more; no financial charge is created.
Open an order to view its timeline, print loading instructions, edit, complete
or cancel it. Cancellation keeps history. Later dependent driver orders must
be cancelled before earlier orders can be changed or cancelled.

Analytics opens on the current calendar month with Completed selected. Filter
by unloading date range, tractor, trailer, driver, operation, status and loading
or unloading city/company/postcode. Group results by tractor, calendar week
(Monday–Sunday), or month. Each order counts once on its unloading date. Weekly
and monthly groups respect the selected date range, including partial periods.
Completed distance is the sum of saved route estimates, not actual odometer data.
Trip history preserves the trailer plate recorded when the order was saved,
even if its registration is subsequently changed in the trailer directory.
Add each driver's jobs chronologically. Inserting an earlier job requires
cancelling and recreating later jobs so their stored clocks remain consistent.

Reload opportunities show open planned windows after unloading, allowing at
least two hours before the next assigned pickup. This means availability for
planning, not confirmation that the driver has driving time left.
When a carried driver clock cannot fit the next loading before daily/weekly
rest is due, the order form proposes a loading time after qualifying rest.
Use suggested loading time updates the draft appointment and clock, preserves
calendar-week totals and clears the confirmation checkbox for review. An
unassigned gap after the previous job is credited only as assumed uninterrupted
rest. No order is created until the dispatcher reviews and saves it.

## Journeys, stops and confirmation

Every order is a journey of ordered stops: the loading point, optional extra
stops, and the final unloading. Extra stops can be an **extra loading**, an
**extra unloading** or a **trailer swap** (leave the current trailer, pick up
another; a new trailer plate can be typed and is created on save). Each leg has
its own kilometres and driving time (manual) or HERE truck route.

Stops are confirmed one by one, in order: ✓ Loaded, ✓ Swap done, ✓ Unloaded and
✓ Delivered. Quick buttons in Daily planning and on the Fleet board use the
current time; the order detail accepts the actual time. The first confirmation
sets the order **In transit**; ✓ Delivered on the last stop sets it **Completed**
and records the delivery time. Only the last confirmation can be undone, and
only orders without confirmed stops can be edited.

A confirmed trailer swap marks the left trailer **Parked** at that location and
makes the picked-up trailer the tractor's current trailer. Trailers show their
position (parked where and since when, or on which tractor); it can also be set
manually in the trailer form.

A journey counts as one trip however many stops it has, on its delivery date.
Analytics history lists every stop with its confirmation time.

The **Fleet board** shows one row per tractor for the chosen day: each journey as
a chain of stops (green = confirmed, outlined = next, red = overdue), plus free
tractors and where they are. Existing orders were converted automatically into
a loading and an unloading stop.

## Upcoming planning by calendar week

Upcoming planning groups future free windows by Monday–Sunday calendar weeks.
Choose This week, Next week or In 2 weeks. Show one week or two consecutive
weeks; the selection never extends past the end of the week two weeks ahead.
For example, Next week + 2 consecutive weeks shows next week and the week after.

Windows are calculated from saved dropoffs or a reported base location and
availability time. Availability is calculated per tractor registration, using
that tractor's loading orders. Driver names are informational; missing driver
assignments do not hide a known truck position. Past gaps and windows shorter than 2 hours are excluded. Windows
crossing Sunday are split between selected weeks. Each card shows its known
position source, the free interval and the next booking when applicable.

Filter by truck or operation. Open daily plan opens that date for the selected
truck. Refresh fetches the latest saved planning. Trucks missing a
known location/time are listed separately. This is planned availability, not
live tracking or confirmation of remaining driving time.

“Companies nearby” on each free window lists the saved companies around the
truck's free position, closest first (straight-line distance; coordinates
required, otherwise only the same city). Each company shows its type, contact
details, notes, load history (how many loads, where they went) and the last
logged contact. “Log contact” records the result of a call. No live GPS.

## Materials

The Materials tab holds what you load: your own code, the name, and an optional
LER code (European waste list, for waste documents). Codes may repeat in the
source list, so duplicates are flagged but not blocked. Each loading stop
(including extra loadings) can name the material; the stop keeps the material
text, so deleting a material does not change past orders.

## Online (Render)

When the `PORT` environment variable is set (Render sets it), the app listens on
all interfaces and requires a dispatcher password. Environment variables:

- `APP_PASSWORD` — dispatcher password, at least 10 characters (required online)
- `SESSION_SECRET` — long random text; keeps you signed in across restarts
- `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` — the database
- `RENDER_EXTERNAL_HOSTNAME` is set by Render; other domains go in `PUBLIC_HOST`

Render settings: Build `pip install -r requirements.txt`, Start `python app.py`,
Health check path `/healthz` (also used by the uptime monitor). Python version
comes from `.python-version`. Without `PORT` (your own computer) nothing changes.

## Driver confirmation page

“💬 WhatsApp” on an order adds a secret link (`/s/<token>`, one per order) to
the message. The driver opens it without logging in and sees the stops, Google
Maps buttons and one big button for the next step (loaded / swapped / unloaded),
confirmed with a second tap. Only the next stop of that order can be confirmed;
the dispatcher can undo it in the app. The link uses the online address saved in
Settings (filled in automatically by the online app).

## Documento de Control (DeCA)

Order detail → “DeCA” builds the Documento de Control as a one-page PDF with a QR
code. It needs: your company name and NIF (Settings & backup), the shipper's NIF
(Saved locations), and the material and weight of every loading stop. Unloading
weights are optional: empty means everything still on the truck. The identifier
is assigned once per order; the PDF always reflects the current order data. The
QR code contains the document text itself (no link), so it can be read offline.
`qr.py` is a small standard-library QR encoder (byte mode, level M).

## HERE integration (optional)

Create your own HERE API key with Geocoding & Search and Routing access. In the
same PowerShell terminal that starts the app:

    $env:HERE_API_KEY = 'your-key-here'
    python app.py

Keys stay server-side. You can also save the key as a single line in
`data/here_api_key.txt`. The environment variable takes priority when present.
The local key file is read for each request, so replacing it supports key rotation
without a restart, unless an environment override is active. The app never stores
keys in SQLite or sends them to the browser. Provider usage may incur charges.
The integration has been checked with fixtures and public-address live requests.
Source ZIPs do not contain this local key file.

Use Find address in Locations, confirm the correct returned address, then save.
When HERE truck routing is selected in an order, missing coordinates are also
looked up directly inside the order form. Confirm the correct loading/unloading
address from the returned choices; the coordinates are saved to that location
and reused for later orders. Search can be refined inline without leaving the
draft order. ETA calculation and order creation wait for both locations to be
confirmed, rather than failing with a missing-coordinates error.
You can instead enter verified entrance latitude and longitude manually. City
and postal-code coordinates are approximate; they may not be the site entrance.
Routes use truck mode plus combined vehicle height, width, length, and gross
weight. Gross weight is also used conservatively as current laden weight.
Critical restriction notices reject the route; other provider notices are shown.
No car routing or fabricated road distances are used as a fallback.

## Planning model and limits

All input/display uses ONE company dispatch clock, **Europe/Madrid**. Convert
foreign local appointments first. UTC elapsed-time calculations handle DST;
nonexistent/ambiguous local times are rejected, so choose another hour.

The conservative solo-driver model applies 9 h driving per daily period,
45 minutes break after 4.5 h, 11 h daily rest, and a 13 h duty window including
loading/unloading. It applies 56 h per ISO calendar week, 90 h across adjacent
weeks, regular weekly rest of 45 h, and the supplied time until weekly rest is
due. Weekly rest does not reset calendar-week driving totals.

The clock form requires actual hours already driven today/since break/this week/
last week, duty elapsed today, and hours remaining until weekly rest is due.
Defaults of zero/144 are a fresh-rest assumption. Carry forward uses prior
planned jobs, assuming unassigned gaps are rest/breaks. All other work and driving
outside this app must be entered manually. The app rejects reducing a carried
clock below the minimum implied by the previous order.

Not modelled: 10-hour extensions, split or reduced rests, rest compensation,
multi-manning, average working-time rules, national/holiday bans, ferries,
exceptional derogations, parking availability or live tachograph data.
This is a planner, not a legal-compliance/tachograph system. ETA and reload
suggestions require dispatcher/driver confirmation. Long loading/unloading work
can be interrupted by modelled rest; check appointment feasibility in the timeline.

References:
- https://transport.ec.europa.eu/transport-modes/road/social-provisions/driving-time-and-rest-periods_en
- https://docs.here.com/routing/docs/routing-v8-truck-routing
- https://docs.here.com/geocoding-and-search/docs/code-geocode-address

## Data and backups

SQLite: `data/malcomtrans.sqlite3`, created automatically. Existing generic
starter records are preserved in their original table, but not shown in the new
dispatch UI. No sample fleet or driver data is added to the real database.

Settings & backup → Download database backup creates a consistent SQLite copy
while running. Restore with the server stopped: preserve the current file,
replace it with the backup, restart. Keep backups of this project: it currently
lives INSIDE an existing venv; deleting/rebuilding that venv could remove it.

The app binds only to 127.0.0.1. It has no multi-user authentication or permission
system. It validates local host headers, requires a per-process CSRF token for
mutations, escapes UI text, uses SQL parameters and checks assignment conflicts.
Do not expose the development server on a public interface.

## Checks

    python -m unittest discover -s tests -v

Checks use an isolated temporary database, including driver-clock boundaries,
weekly limits, daylight saving, assignment conflicts, history protection, clock
carry-forward, nearby-company ranking, HERE request units (fixture),
HTTP CSRF protection and SQLite backup consistency.
