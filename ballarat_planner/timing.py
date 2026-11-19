# Timetable and fleet estimates. Pure Python, no Qt.
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from .model import Route


def parse_clock(s: str) -> int:
    try:
        parts = str(s or "07:00").strip().split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        if h < 0 or h > 23 or m < 0 or m > 59:
            raise ValueError
        return h * 3600 + m * 60
    except (TypeError, ValueError, IndexError):
        return 7 * 3600


def _minutes(sec: float) -> int:
    return int(math.floor(sec / 60 + 0.5))


def fmt_clock(sec: float) -> str:
    m = _minutes(sec)
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


def fmt_dur(sec: float) -> str:
    m = _minutes(sec)
    if m < 1:
        return "<1 min"
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d} m"


def fmt_km(metres: float) -> str:
    return f"{metres / 1000:.2f} km" if metres < 10000 else f"{metres / 1000:.1f} km"


@dataclass
class Row:
    arrive: float                       # seconds since midnight
    depart: float
    leg_dist: Optional[float] = None    # metres to next stop
    leg_sec: Optional[float] = None     # bus seconds to next stop


@dataclass
class Timeline:
    rows: List[Row]
    end_arrive: float
    run: float            # seconds, first departure -> end
    dist: float           # metres
    moving: float
    dwell_total: float
    cycle: float          # seconds for a full round trip incl. layovers
    buses: Optional[int]
    avg_kmh: float


def _dwell_at(route: Route, stop_index: int) -> int:
    dwell = route.stops[stop_index].dwell
    return route.dwell if dwell is None else dwell


def timeline(route: Route) -> Optional[Timeline]:
    n = len(route.stops)
    legs = route.valid_legs()
    if not legs or n < 2:
        return None
    expected_legs = n if route.loop else (2 * (n - 1) if route.return_via_stops else n - 1)
    if len(legs) != expected_legs:
        return None
    start = parse_clock(route.start)
    t = float(start)
    dist = moving = dwell_total = 0.0
    rows = [Row(t, t)]
    outbound_legs = legs[:n - 1] if route.return_via_stops else legs
    for i, leg in enumerate(outbound_legs):
        bus = leg.dur * route.factor
        rows[i].leg_dist, rows[i].leg_sec = leg.dist, bus
        t += bus
        dist += leg.dist
        moving += bus
        if i + 1 < n:
            is_terminus = (i + 1 == n - 1) and not route.loop
            d = 0 if is_terminus else _dwell_at(route, i + 1)
            rows.append(Row(t, t + d))
            t += d
            dwell_total += d
    run = t - start
    lay = route.layover * 60
    if route.loop:
        cycle = run + lay
    elif route.return_via_stops:
        return_moving = sum(leg.dur * route.factor for leg in legs[n - 1:])
        return_dwell = sum(_dwell_at(route, i) for i in range(1, n - 1))
        cycle = run + return_moving + return_dwell + 2 * lay
    else:
        cycle = 2 * run + 2 * lay
    buses = max(1, math.ceil(cycle / (route.headway * 60))) if route.headway > 0 else None
    avg = (dist / 1000) / (run / 3600) if run > 0 else 0.0
    return Timeline(rows, t, run, dist, moving, dwell_total, cycle, buses, avg)
