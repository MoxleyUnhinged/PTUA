import unittest

from ballarat_planner.model import Leg, Plan, Route, Stop, seed_plan
from ballarat_planner.timing import Timeline, fmt_clock, parse_clock, timeline
from ballarat_planner.routing import approx_legs, route_points


def require_timeline(route: Route) -> Timeline:
    result = timeline(route)
    if result is None:
        raise AssertionError("Expected a timeline for a routed route")
    return result


def make_route(loop=False):
    r = Route("T", "#000", [Stop("A", -37.56, 143.85), Stop("B", -37.57, 143.86), Stop("C", -37.58, 143.87)])
    r.loop = loop
    n = 3 if loop else 2
    r.legs = [Leg(2000, 300, [[0, 0], [1, 1]]) for _ in range(n)]
    r.legs_sig = r.signature()
    return r


class Core(unittest.TestCase):
    def test_parse_clock_rejects_malformed_values(self):
        self.assertEqual(parse_clock("07:30"), 7 * 3600 + 30 * 60)
        self.assertEqual(parse_clock("bad"), 7 * 3600)
        self.assertEqual(parse_clock("25:00"), 7 * 3600)

    def test_timeline_open_route(self):
        tl = require_timeline(make_route())
        # 2 legs * 300 s * 1.35 + 20 s dwell at middle stop only (not terminus)
        self.assertAlmostEqual(tl.run, 2 * 405 + 20)
        self.assertEqual(fmt_clock(tl.end_arrive), "07:14")
        self.assertEqual(tl.buses, 2)          # (2*830 + 2*300)/1800 -> 2
        self.assertAlmostEqual(tl.dist, 4000)

    def test_timeline_loop(self):
        tl = require_timeline(make_route(loop=True))
        self.assertAlmostEqual(tl.run, 3 * 405 + 2 * 20)   # dwell at stops 2 and 3
        self.assertEqual(len(tl.rows), 3)

    def test_return_via_same_stops_routing_and_geometry(self):
        r = make_route()
        r.return_via_stops = True
        points = route_points(r)
        self.assertEqual(points, [
            (-37.56, 143.85), (-37.57, 143.86), (-37.58, 143.87),
            (-37.57, 143.86), (-37.56, 143.85),
        ])
        self.assertEqual(len(r.leg_coords()), 4)
        self.assertEqual(r.leg_coords()[2], [[-37.58, 143.87], [-37.57, 143.86]])

    def test_return_timeline_uses_routed_reverse_legs(self):
        r = make_route()
        r.return_via_stops = True
        r.stops[1].dwell = 35
        r.legs = [Leg(2000, 300, [[0, 0], [1, 1]]) for _ in range(2)]
        r.legs.extend([Leg(2000, 100, [[1, 1], [0, 0]]),
                       Leg(2000, 200, [[0, 0], [-1, -1]])])
        r.legs_sig = r.signature()

        tl = require_timeline(r)
        self.assertAlmostEqual(tl.run, 2 * 405 + 35)
        self.assertAlmostEqual(tl.cycle, tl.run + 300 * 1.35 + 35 + 2 * 5 * 60)
        self.assertAlmostEqual(tl.dist, 4000)
        self.assertEqual(len(tl.rows), 3)

    def test_return_route_mode_round_trip_and_cache_signature(self):
        r = make_route()
        r.return_via_stops = True
        r.legs = [Leg(100, 100, [[0, 0], [1, 1]]) for _ in range(4)]
        r.legs_sig = r.signature()
        p = Plan([r], r.id)
        q = Plan.from_json(p.to_json())
        self.assertTrue(q.routes[0].return_via_stops)
        self.assertIsNotNone(q.routes[0].valid_legs())
        q.routes[0].return_via_stops = False
        self.assertIsNone(q.routes[0].valid_legs())

    def test_stale_legs_ignored(self):
        r = make_route()
        r.stops[0].lat += 0.001
        self.assertIsNone(timeline(r))
        self.assertEqual(len(r.leg_coords()), 2)   # straight-line fallback

    def test_json_round_trip(self):
        p = Plan([make_route()], None)
        p.selected = p.routes[0].id
        q = Plan.from_json(p.to_json())
        self.assertEqual(q.routes[0].stops[1].name, "B")
        self.assertIsNotNone(q.routes[0].valid_legs())

    def test_seed_and_approx(self):
        p = seed_plan()
        legs = approx_legs(route_points(p.routes[0]))
        self.assertEqual(len(legs), 2)
        self.assertGreater(legs[0].dist, 500)


if __name__ == "__main__":
    unittest.main()
