"""Download and extract current Ballarat bus and train paths from Victorian GTFS."""
from __future__ import annotations

import csv
import io
import json
import tempfile
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

DATA_DIR = Path.home() / ".ballarat_bus_planner"
PACKAGE_URL = "https://opendata.transport.vic.gov.au/api/3/action/package_show?id=gtfs-schedule"
BALLARAT_BOUNDS = (-37.72, -37.43, 143.60, 144.10)  # south, north, west, east
USER_AGENT = "BallaratBusPlanner/1.0 (PTV route reference overlay)"
FEED_MODE_DIRS = {"1": "Train", "4": "Bus", "6": "Bus"}
CHUNK_SIZE = 1024 * 1024


def _feed_resource() -> Tuple[str, str, Optional[int]]:
    request = urllib.request.Request(PACKAGE_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        package = json.loads(response.read().decode("utf-8"))
    if not package.get("success"):
        raise RuntimeError("Transport Victoria did not return GTFS dataset metadata")
    resources = package.get("result", {}).get("resources", [])
    resource = next((r for r in resources
                     if r.get("format", "").upper() == "ZIP"
                     and r.get("name", "").lower() == "gtfs schedule"), None)
    if not resource or not resource.get("url") or not resource.get("last_modified"):
        raise RuntimeError("Could not find the current GTFS Schedule download")
    size = resource.get("size")
    return resource["url"], resource["last_modified"], int(size) if size else None


def _cached_routes(path: Path, updated: str) -> Optional[List[dict]]:
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if (isinstance(cached, dict) and cached.get("updated") == updated
            and isinstance(cached.get("routes"), list)):
        return cached["routes"]
    return None


def _write_cache(path: Path, updated: str, routes: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix="ptv-routes-", suffix=".tmp",
                                         delete=False) as temp:
            temp_path = Path(temp.name)
            json.dump({"updated": updated, "routes": routes}, temp, separators=(",", ":"))
        temp_path.replace(path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def _download(url: str, destination: Path, size: Optional[int],
              progress: Optional[Callable[[str], None]]) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    written = 0
    last_reported = 0
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as target:
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            target.write(chunk)
            written += len(chunk)
            if progress and written - last_reported >= 8 * CHUNK_SIZE:
                total = f" of {size / 1_000_000:.0f} MB" if size else ""
                progress(f"Downloading official GTFS feed: {written / 1_000_000:.0f} MB{total}")
                last_reported = written
    if size is not None and written != size:
        raise RuntimeError(f"GTFS download was incomplete ({written} of {size} bytes)")


def _rows(feed: zipfile.ZipFile, filename: str) -> Iterable[dict]:
    matches = [name for name in feed.namelist()
               if Path(name).name.lower() == filename.lower()]
    if not matches:
        raise RuntimeError(f"GTFS feed is missing {filename}")
    if len(matches) > 1:
        raise RuntimeError(f"GTFS feed contains multiple {filename} files")
    with feed.open(matches[0]) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        yield from csv.DictReader(text)


def _inside_ballarat(lat: float, lng: float, bounds: Tuple[float, float, float, float]) -> bool:
    south, north, west, east = bounds
    return south <= lat <= north and west <= lng <= east


def _parse_mode_feed(feed: zipfile.ZipFile, folder: str, mode: str,
                     bounds: Tuple[float, float, float, float]) -> List[dict]:
    routes = {}
    for row in _rows(feed, "routes.txt"):
        route_id = row.get("route_id", "")
        if route_id:
            short = row.get("route_short_name", "").strip()
            long = row.get("route_long_name", "").strip()
            name = short or long or route_id
            if short and long and short.casefold() not in long.casefold():
                name = f"{short} — {long}"
            routes[route_id] = name

    shape_routes: Dict[str, Set[str]] = defaultdict(set)
    for row in _rows(feed, "trips.txt"):
        route_id, shape_id = row.get("route_id", ""), row.get("shape_id", "")
        if route_id in routes and shape_id:
            shape_routes[shape_id].add(route_id)
    if not shape_routes:
        return []

    local_shapes: Set[str] = set()
    for row in _rows(feed, "shapes.txt"):
        shape_id = row.get("shape_id", "")
        if shape_id not in shape_routes:
            continue
        try:
            lat, lng = float(row["shape_pt_lat"]), float(row["shape_pt_lon"])
        except (KeyError, ValueError):
            continue
        if _inside_ballarat(lat, lng, bounds):
            local_shapes.add(shape_id)
    if not local_shapes:
        return []

    geometry: Dict[str, List[Tuple[int, float, float]]] = defaultdict(list)
    for row in _rows(feed, "shapes.txt"):
        shape_id = row.get("shape_id", "")
        if shape_id not in local_shapes:
            continue
        try:
            sequence = int(row["shape_pt_sequence"])
            lat, lng = float(row["shape_pt_lat"]), float(row["shape_pt_lon"])
        except (KeyError, ValueError):
            continue
        geometry[shape_id].append((sequence, lat, lng))

    out = []
    for shape_id, route_ids in shape_routes.items():
        if shape_id not in local_shapes:
            continue
        coords = [[lat, lng] for _, lat, lng in sorted(geometry[shape_id])]
        if len(coords) < 2:
            continue
        for route_id in sorted(route_ids):
            out.append({
                "id": f"{folder}:{route_id}:{shape_id}",
                "mode": mode,
                "name": routes[route_id],
                "coords": coords,
            })
    return out


def parse_gtfs_archive(archive: zipfile.ZipFile,
                       bounds: Tuple[float, float, float, float] = BALLARAT_BOUNDS) -> List[dict]:
    """Extract route shapes in the Ballarat area from the official nested GTFS ZIP."""
    routes = []
    entries = archive.namelist()
    for folder, mode in FEED_MODE_DIRS.items():
        name = f"{folder}/google_transit.zip"
        if name not in entries:
            continue
        with archive.open(name) as nested_zip:
            with zipfile.ZipFile(nested_zip) as feed:
                routes.extend(_parse_mode_feed(feed, folder, mode, bounds))
    return routes


def load_ballarat_routes(cache_dir: Path = DATA_DIR,
                         progress: Optional[Callable[[str], None]] = None) -> Tuple[List[dict], str]:
    """Load cached Ballarat route shapes or download the latest official GTFS feed."""
    url, updated, size = _feed_resource()
    cache_path = cache_dir / "ptv_routes.json"
    cached = _cached_routes(cache_path, updated)
    if cached is not None:
        return cached, updated

    cache_dir.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=cache_dir, prefix="gtfs-schedule-",
                                         suffix=".zip", delete=False) as temp:
            temp_path = Path(temp.name)
        if progress:
            progress("Downloading official GTFS feed (about 310 MB on first use)…")
        _download(url, temp_path, size, progress)
        if progress:
            progress("Extracting Ballarat bus and train route shapes…")
        with zipfile.ZipFile(temp_path) as archive:
            routes = parse_gtfs_archive(archive)
        _write_cache(cache_path, updated, routes)
        return routes, updated
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
