# Ballarat Bus Route Planner (Python desktop app)

Plan bus routes through Ballarat on a map: drag stops and route lines, take notes on
routes and stops, and get run-time, timetable and fleet-size estimates.

## Run it

    python -m venv .venv
    # Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
    pip install -r requirements.txt
    python -m ballarat_planner

Needs Python 3.9+ and an internet connection (OpenStreetMap tiles, OSRM routing, place search).

If the map stays blank on Linux, try:  `QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox" python -m ballarat_planner`

## Using it

| Do this | To get this |
|---|---|
| **＋ Route** | New route, and "Add stops" turns on |
| Click the map (Add stops on) | Append a stop; click the route line to insert one mid-route |
| Drag a numbered stop | Move it; the road route recalculates |
| Drag the selected route line (or drag its white handle) | Add a via-stop and recalculate the road route |
| Drag rows in the **Stops** tab | Reorder stops |
| **Stops** tab → select a stop | Name, per-stop dwell time, notes |
| **Notes** tab | Free-form notes for the route |
| **Route** tab | Start time, bus÷car factor, default dwell, headway, layover, loop or out-and-back |
| 🔍 Find place | Search Ballarat and optionally add the result as a stop |
| **PTV routes** | Show Ballarat bus and train route paths as map references |
| Ctrl+Z | Undo (structure changes) |

Your work autosaves to `~/.ballarat_bus_planner/plan.json`. **Save as… / Open…** use the same
JSON format as the browser version of the planner, so plans can move between them.

PTV route references come from the [Transport Victoria GTFS Schedule](https://opendata.transport.vic.gov.au/dataset/gtfs-schedule)
dataset (CC BY 4.0), which is updated weekly. Enable **PTV routes** to fetch the current feed;
the first use downloads about 310 MB. Only the Ballarat bus and train shapes are cached in
`~/.ballarat_bus_planner/ptv_routes.json`. These are scheduled route paths, not live vehicle locations.

## How estimates work

* Road distance and car travel time per leg come from OSRM (car profile).
* Bus time per leg = car time × the *bus ÷ car* factor (default 1.35).
* Dwell time is added at each intermediate stop (not at the terminus of an open route).
* **Loop route** routes from the last stop directly back to the first. **Return via same stops**
  routes back through the stops in reverse order, using road routing for each return leg.
* Fleet size = ceil(cycle time ÷ headway). Out-and-back cycles use the routed return-leg times;
  ordinary open routes assume the return run matches the outbound run.
* If OSRM can't be reached, legs fall back to straight-line × 1.3 at 28 km/h and are drawn dashed.
* No live traffic, bus-lane or turn-restriction data. Calibrate the factor against a real timetable.

## Using the code from your own scripts

`model.py`, `timing.py` and `routing.py` don't import Qt:

```python
from ballarat_planner.model import Plan
from ballarat_planner.timing import timeline, fmt_clock

plan = Plan.load("ballarat-bus-plan.json")
for route in plan.routes:
    tl = timeline(route)           # None until the route has been road-routed
    if tl:
        print(route.name, f"{tl.dist/1000:.1f} km", fmt_clock(tl.end_arrive), tl.buses, "buses")
```

To route a plan from a script:

```python
from ballarat_planner import routing
legs = routing.fetch_legs(routing.route_points(route))
route.legs, route.legs_sig = legs, route.signature()
```

## Layout

    ballarat_planner/
      model.py       Stop / Leg / Route / Plan dataclasses + JSON load/save
      timing.py      timetable, run time, fleet-size maths
      routing.py     OSRM routing, offline fallback, Nominatim search
      transit.py     current Ballarat bus/train references from Victorian GTFS
      app.py         PyQt6 window, undo, background routing, save/open/CSV
      map_view.html  Leaflet map page (talks to app.py through QWebChannel)
    tests/test_core.py   python -m unittest discover -s tests

OSRM's demo server and Nominatim are free public services for light use; point
`routing.OSRM_URL` at your own server for heavy use.
