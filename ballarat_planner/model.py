"""Data model. Pure Python, no Qt. JSON format matches the browser version."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

COLORS = ["#e53935", "#1e88e5", "#43a047", "#fb8c00",
          "#8e24aa", "#00897b", "#6d4c41", "#d81b60"]


def uid() -> str:
    return uuid.uuid4().hex[:7]


@dataclass
class Stop:
    name: str
    lat: float
    lng: float
    note: str = ""
    dwell: Optional[int] = None          # seconds; None = use route default
    id: str = field(default_factory=uid)


@dataclass
class Leg:
    dist: float                          # metres
    dur: float                           # seconds at car speed
    coords: list                         # [[lat, lng], ...]


@dataclass
class Route:
    name: str
    color: str
    stops: List[Stop] = field(default_factory=list)
    visible: bool = True
    loop: bool = False
    factor: float = 1.35                 # bus time = car time * factor
    dwell: int = 20                      # default seconds stopped per stop
    start: str = "07:00"                 # first departure HH:MM
    headway: float = 30                  # minutes between buses
    layover: float = 5                   # minutes at terminus
    notes: str = ""
    id: str = field(default_factory=uid)
    # cached routing result (valid only while legs_sig == signature())
    legs: Optional[List[Leg]] = None
    legs_sig: str = ""
    approx: bool = False
    # runtime-only
    status: str = field(default="", repr=False)
    tok: int = field(default=0, repr=False)
    return_via_stops: bool = False

    def signature(self) -> str:
        s = "|".join(f"{p.lat:.6f},{p.lng:.6f}" for p in self.stops)
        return s + ("|L" if self.loop else "|R" if self.return_via_stops else "")

    def valid_legs(self) -> Optional[List[Leg]]:
        return self.legs if (self.legs and self.legs_sig == self.signature()) else None

    def leg_coords(self) -> List[list]:
        """Polyline per leg; straight lines where road routing isn't available."""
        n = len(self.stops)
        legs = self.valid_legs()
        if self.loop and n > 1:
            pairs = [(i, (i + 1) % n) for i in range(n)]
        elif self.return_via_stops and n > 1:
            pairs = ([(i, i + 1) for i in range(n - 1)]
                     + [(i, i - 1) for i in range(n - 1, 0, -1)])
        else:
            pairs = [(i, i + 1) for i in range(n - 1)]
        out = []
        for i, (start, end) in enumerate(pairs):
            if legs and i < len(legs):
                out.append(legs[i].coords)
            else:
                a, b = self.stops[start], self.stops[end]
                out.append([[a.lat, a.lng], [b.lat, b.lng]])
        return out

    def to_dict(self, include_calc: bool = True) -> dict:
        d = {
            "id": self.id, "name": self.name, "color": self.color,
            "visible": self.visible, "loop": self.loop,
            "return_via_stops": self.return_via_stops, "factor": self.factor,
            "dwell": self.dwell, "start": self.start, "headway": self.headway,
            "layover": self.layover, "notes": self.notes,
            "stops": [{"id": s.id, "name": s.name, "note": s.note,
                       "lat": s.lat, "lng": s.lng, "dwell": s.dwell} for s in self.stops],
            "calc": None,
        }
        if include_calc and self.legs:
            d["calc"] = {"sig": self.legs_sig, "approx": self.approx,
                         "legs": [{"dist": l.dist, "dur": l.dur, "coords": l.coords} for l in self.legs]}
        return d

    @classmethod
    def from_dict(cls, d: dict, index: int = 0) -> "Route":
        r = cls(name=d.get("name") or f"Route {index + 1}",
                color=d.get("color") or COLORS[index % len(COLORS)])
        for k in ("visible", "loop", "notes", "start"):
            if k in d and d[k] is not None:
                setattr(r, k, d[k])
        r.return_via_stops = bool(d.get("return_via_stops", False)) and not r.loop
        for k, t in (("factor", float), ("dwell", int), ("headway", float), ("layover", float)):
            if d.get(k) is not None:
                setattr(r, k, t(d[k]))
        r.id = d.get("id") or uid()
        r.stops = [Stop(name=s.get("name", "Stop"), lat=float(s["lat"]), lng=float(s["lng"]),
                        note=s.get("note", ""), dwell=s.get("dwell"), id=s.get("id") or uid())
                   for s in d.get("stops", [])]
        calc = d.get("calc")
        if calc and calc.get("legs"):
            r.legs = [Leg(float(l["dist"]), float(l["dur"]), l["coords"]) for l in calc["legs"]]
            r.legs_sig = calc.get("sig", "")
            r.approx = bool(calc.get("approx"))
        return r


@dataclass
class Plan:
    routes: List[Route] = field(default_factory=list)
    selected: Optional[str] = None

    def route(self, route_id: Optional[str]) -> Optional[Route]:
        return next((r for r in self.routes if r.id == route_id), None)

    def to_json(self, include_calc: bool = True, indent: Optional[int] = 2) -> str:
        return json.dumps({"selected": self.selected,
                           "routes": [r.to_dict(include_calc) for r in self.routes]}, indent=indent)

    @classmethod
    def from_json(cls, text: str) -> "Plan":
        d = json.loads(text)
        if not isinstance(d, dict) or not isinstance(d.get("routes"), list):
            raise ValueError("Not a planner file")
        p = cls(routes=[Route.from_dict(r, i) for i, r in enumerate(d["routes"])],
                selected=d.get("selected"))
        if p.route(p.selected) is None:
            p.selected = p.routes[0].id if p.routes else None
        return p

    def save(self, path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    @classmethod
    def load(cls, path) -> "Plan":
        with open(path, encoding="utf-8") as f:
            return cls.from_json(f.read())


def seed_plan() -> Plan:
    r = Route("Sample: Station → Sovereign Hill", COLORS[0], [
        Stop("Ballarat Station", -37.5588, 143.8580, "Start. Connects with V/Line trains."),
        Stop("Sturt St (city centre)", -37.5620, 143.8520),
        Stop("Sovereign Hill", -37.5795, 143.8570, "Tourist peak: check capacity."),
    ])
    r.notes = ("Sample route — drag the numbered stops, drag the line to add a bend, "
               "and edit these notes. Stop locations are approximate.")
    return Plan([r], r.id)
