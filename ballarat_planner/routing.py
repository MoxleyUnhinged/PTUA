#Road routing (OSRM demo server) and place search (Nominatim). No Qt.

#Both services are free public servers meant for light use. For heavy use,
#point OSRM_URL at your own OSRM instance.
#
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from typing import List, Tuple

from .model import Leg, Route

OSRM_URL = "https://router.project-osrm.org/route/v1/driving/"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "BallaratBusPlanner/1.0 (personal route planning tool)"
BALLARAT_VIEWBOX = "143.70,-37.46,143.99,-37.66"   # left,top,right,bottom


def route_points(route: Route) -> List[Tuple[float, float]]:
    """(lat, lng) waypoints, including any loop or out-and-back return."""
    pts = [(s.lat, s.lng) for s in route.stops]
    if route.loop and len(pts) >= 2:
        pts.append(pts[0])
    elif route.return_via_stops and len(pts) >= 2:
        pts.extend(reversed(pts[:-1]))
    return pts


def _get_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_legs(points: List[Tuple[float, float]], timeout: float = 15) -> List[Leg]:
    """Road-routed legs between consecutive points. Raises on failure."""
    coords = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in points)
    data = _get_json(f"{OSRM_URL}{coords}?overview=false&steps=true&geometries=geojson", timeout)
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM: {data.get('code')}")
    legs = []
    for leg in data["routes"][0]["legs"]:
        line = [[c[1], c[0]] for step in leg["steps"] for c in step["geometry"]["coordinates"]]
        legs.append(Leg(leg["distance"], leg["duration"], line))
    return legs


def haversine(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    r = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def approx_legs(points: List[Tuple[float, float]]) -> List[Leg]:
    """Offline fallback: straight line x1.3 detour at 28 km/h."""
    legs = []
    for a, b in zip(points, points[1:]):
        d = haversine(a, b) * 1.3
        legs.append(Leg(d, d / (28 / 3.6), [[a[0], a[1]], [b[0], b[1]]]))
    return legs


def search_places(query: str, timeout: float = 10) -> List[dict]:
    """Search within the Ballarat area. Returns [{'name', 'full', 'lat', 'lng'}]."""
    qs = urllib.parse.urlencode({"format": "jsonv2", "limit": 8, "bounded": 1,
                                 "viewbox": BALLARAT_VIEWBOX, "q": query})
    out = []
    for x in _get_json(f"{NOMINATIM_URL}?{qs}", timeout):
        full = x.get("display_name", "")
        out.append({"name": x.get("name") or full.split(",")[0], "full": full,
                    "lat": float(x["lat"]), "lng": float(x["lon"])})
    return out
