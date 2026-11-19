import io
import zipfile
import unittest

from ballarat_planner.transit import parse_gtfs_archive


def gtfs_feed(mode, content):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as feed:
        for name, text in content.items():
            feed.writestr(name, text)
    return mode, buffer.getvalue()


class Transit(unittest.TestCase):
    def test_extracts_local_bus_and_train_shapes_from_nested_feeds(self):
        routes = "route_id,route_short_name,route_long_name\nr1,5,Ballarat Station - Wendouree\n"
        trips = "route_id,service_id,trip_id,shape_id\nr1,s1,t1,nearby\n"
        shapes = (
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
            "nearby,5,5,2\n"
            "nearby,0,0,1\n"
            "outside,8,8,1\n"
            "outside,9,9,2\n"
        )
        files = {"routes.txt": routes, "trips.txt": trips, "shapes.txt": shapes}
        feeds = [gtfs_feed("1", files), gtfs_feed("4", files), gtfs_feed("6", files)]
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_STORED) as archive:
            for mode, payload in feeds:
                archive.writestr(f"{mode}/google_transit.zip", payload)

        with zipfile.ZipFile(io.BytesIO(archive_buffer.getvalue())) as archive:
            result = parse_gtfs_archive(archive, bounds=(-1, 1, -1, 1))

        self.assertEqual(len(result), 3)
        self.assertCountEqual([item["mode"] for item in result], ["Train", "Bus", "Bus"])
        self.assertEqual([0.0, 0.0], result[0]["coords"][0])
        self.assertTrue(all(item["name"] == "5 — Ballarat Station - Wendouree" for item in result))


if __name__ == "__main__":
    unittest.main()
