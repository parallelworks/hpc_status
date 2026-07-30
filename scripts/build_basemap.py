"""Simplify Natural Earth state boundaries into a small bundled basemap.

Natural Earth is public domain, so the output can ship inside the repo —
which it must, since these deployments are frequently air-gapped and cannot
fetch a tile server.
"""

import json
import math
import sys
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "ne_110m_admin_1_states_provinces.geojson")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "us-states.json")
# Optional third input: admin-0 countries, used for the national outline.
# State rings alone cannot give a clean coastline — every internal border
# would be drawn at coastline weight.
COUNTRY_SRC = Path(sys.argv[3]) if len(sys.argv) > 3 else None

EPSILON = 0.09          # degrees; ~10 km, invisible at dashboard zoom levels
MIN_RING_POINTS = 5     # drop slivers that survive simplification
MIN_RING_SPAN = 0.35    # degrees; drops small offshore islands
WEST_LIMIT = -170.0     # clip the Aleutians, which cross the antimeridian


def perpendicular_distance(point, start, end):
    (px, py), (sx, sy), (ex, ey) = point, start, end
    dx, dy = ex - sx, ey - sy
    if dx == 0 and dy == 0:
        return math.hypot(px - sx, py - sy)
    t = ((px - sx) * dx + (py - sy) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (sx + t * dx), py - (sy + t * dy))


def douglas_peucker(points, epsilon):
    if len(points) < 3:
        return points
    worst, index = 0.0, 0
    for i in range(1, len(points) - 1):
        distance = perpendicular_distance(points[i], points[0], points[-1])
        if distance > worst:
            worst, index = distance, i
    if worst <= epsilon:
        return [points[0], points[-1]]
    left = douglas_peucker(points[: index + 1], epsilon)
    right = douglas_peucker(points[index:], epsilon)
    return left[:-1] + right


def rings_of(geometry):
    kind = geometry["type"]
    if kind == "Polygon":
        return [geometry["coordinates"][0]]
    if kind == "MultiPolygon":
        return [polygon[0] for polygon in geometry["coordinates"]]
    return []


def span(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def simplify_rings(geometry):
    kept = []
    for ring in rings_of(geometry):
        ring = [p for p in ring if p[0] >= WEST_LIMIT]
        if len(ring) < MIN_RING_POINTS or span(ring) < MIN_RING_SPAN:
            continue
        simplified = douglas_peucker([tuple(p) for p in ring], EPSILON)
        if len(simplified) < MIN_RING_POINTS:
            continue
        kept.append([[round(x, 2), round(y, 2)] for x, y in simplified])
    return kept


features = json.loads(SRC.read_text())["features"]
states = []
for feature in features:
    name = feature["properties"].get("name")
    postal = feature["properties"].get("postal")
    kept = simplify_rings(feature["geometry"])
    if kept:
        states.append({"n": name, "p": postal, "r": kept})

country = []
if COUNTRY_SRC and COUNTRY_SRC.exists():
    for feature in json.loads(COUNTRY_SRC.read_text())["features"]:
        props = feature["properties"]
        if props.get("ADMIN") == "United States of America" or props.get("admin") == "United States of America":
            country = simplify_rings(feature["geometry"])
            break

payload = {
    "name": "United States — national outline and state boundaries",
    "source": "Natural Earth (naturalearthdata.com), 1:110m admin-1, public domain",
    "simplified": f"Douglas-Peucker epsilon={EPSILON} degrees, coordinates rounded to 2dp",
    "country": country,
    "states": states,
}
OUT.write_text(json.dumps(payload, separators=(",", ":")))

points = sum(len(ring) for state in states for ring in state["r"])
points += sum(len(ring) for ring in country)
print(
    f"{OUT}: {len(country)} national rings, {len(states)} states, "
    f"{points} points, {OUT.stat().st_size / 1024:.1f} KB"
)
