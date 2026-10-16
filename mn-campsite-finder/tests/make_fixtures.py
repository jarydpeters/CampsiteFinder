"""Generate SYNTHETIC test inputs shaped like the real downloads (invented geometry, real-looking schema).

Usage: python tests/make_fixtures.py   ->  tests/fixtures/
These are for testing the pipeline only. They are not real Minnesota data.
"""
import math
import random
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point, box

random.seed(7)
np.random.seed(7)
OUT = Path(__file__).resolve().parent / "fixtures"
(OUT / "land").mkdir(parents=True, exist_ok=True)
(OUT / "lakes").mkdir(exist_ok=True)
(OUT / "roads").mkdir(exist_ok=True)
(OUT / "sky").mkdir(exist_ok=True)

W, S, E, N = -95.0, 46.8, -93.6, 47.8  # fake "state" extent in northern MN

# state outline
gpd.GeoDataFrame({"name": ["Test state"]}, geometry=[box(W, S, E, N)], crs="EPSG:4326").to_file(OUT / "state.gpkg", driver="GPKG")

# PAD-US-like file
nf = box(-94.8, 47.0, -94.0, 47.6)           # a "national forest"
wild = box(-94.5, 47.3, -94.2, 47.5)         # wilderness inside it
nf_prock = box(-94.9, 46.9, -93.9, 47.7)     # proclamation boundary (includes private land)
local = box(-93.9, 46.85, -93.7, 47.0)       # county land
pad = gpd.GeoDataFrame(
    {
        "Des_Tp": ["NF", "WA", "NF", "LOC"],
        "Category": ["Fee", "Designation", "Proclamation", "Fee"],
        "Own_Type": ["FED", "FED", "FED", "LOC"],
        "Unit_Nm": ["Test National Forest", "Test Wilderness", "Test National Forest", "Test County Forest"],
        "Mang_Name": ["USFS", "USFS", "USFS", "CNTY"],
    },
    geometry=[nf, wild, nf_prock, local],
    crs="EPSG:4326",
)
pad.to_file(OUT / "land" / "padus_mn.gpkg", driver="GPKG")

# DNR state forest lands (overlaps nothing but touches the NF edge)
sf = box(-94.0, 47.1, -93.7, 47.5)
gpd.GeoDataFrame({"FOREST_NAME": ["Test State Forest"]}, geometry=[sf], crs="EPSG:4326").to_file(
    OUT / "land" / "dnr_state_forest_lands.gpkg", driver="GPKG")

# lakes: mix of sizes, some under the 10 ha threshold
lakes, names = [], []
centers = [(-94.6, 47.15, 900), (-94.35, 47.5, 400), (-94.1, 47.25, 1500), (-93.85, 47.4, 600),
           (-94.7, 47.45, 3), (-94.45, 47.1, 5), (-93.8, 47.2, 250), (-94.0, 47.05, 80)]
for i, (x, y, ha) in enumerate(centers):
    r_m = math.sqrt(ha * 1e4 / math.pi)
    rx = r_m / (111320 * math.cos(math.radians(y)))
    ry = r_m / 110540
    pts = [(x + rx * math.cos(t) * (1 + 0.15 * math.sin(3 * t)), y + ry * math.sin(t) * (1 + 0.15 * math.cos(2 * t)))
           for t in np.linspace(0, 2 * math.pi, 48, endpoint=False)]
    from shapely.geometry import Polygon
    lakes.append(Polygon(pts))
    names.append(f"Test Lake {i + 1}")
gpd.GeoDataFrame({"pw_basin_name": names}, geometry=lakes, crs="EPSG:4326").to_file(OUT / "lakes" / "dnr_lakes.gpkg", driver="GPKG")

# roads as OSM XML (read through the same OGR OSM driver as a real .pbf)
nodes, ways = [], []
nid = [1]


def add_way(coords, tags):
    ids = []
    for lon, lat in coords:
        nodes.append((nid[0], lat, lon))
        ids.append(nid[0])
        nid[0] += 1
    ways.append((len(ways) + 1, ids, tags))


def line(a, b, n=8):
    return [(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t) for t in np.linspace(0, 1, n)]


add_way(line((-94.9, 47.2), (-93.7, 47.2)), {"highway": "secondary", "name": "County Road 5", "surface": "asphalt"})
add_way(line((-94.65, 47.0), (-94.65, 47.55)), {"highway": "unclassified", "name": "Forest Road 2200", "surface": "gravel"})
add_way(line((-94.4, 47.05), (-94.3, 47.3)), {"highway": "track", "surface": "dirt"})
add_way(line((-94.2, 47.45), (-94.0, 47.35)), {"highway": "track"})
add_way(line((-93.95, 47.0), (-93.95, 47.55)), {"highway": "tertiary", "name": "County Road 9"})
add_way(line((-94.9, 47.58), (-93.6, 47.58)), {"highway": "primary", "name": "Highway 71"})
add_way(line((-94.3, 47.12), (-94.2, 47.18)), {"highway": "service", "access": "private"})
add_way(line((-94.5, 47.5), (-94.45, 47.52)), {"highway": "service", "service": "driveway"})
add_way(line((-94.7, 47.3), (-94.75, 47.35)), {"highway": "footway"})

# a gravel track hugging each lake, ~250 m beyond its shore, so lake and road zones overlap
for (x, y, ha), _n in zip(centers, names):
    r_m = math.sqrt(ha * 1e4 / math.pi) * 1.15 + 250
    dx = r_m / (111320 * math.cos(math.radians(y)))
    dy = 0.8 * r_m / 110540
    add_way(line((x + dx, y - dy), (x + dx, y + dy)), {"highway": "track", "surface": "gravel"})

xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="fixture">']
for i, lat, lon in nodes:
    xml.append(f'<node id="{i}" lat="{lat:.7f}" lon="{lon:.7f}" version="1" timestamp="2024-01-01T00:00:00Z"/>')
for wid, ids, tags in ways:
    xml.append(f'<way id="{wid}" version="1" timestamp="2024-01-01T00:00:00Z">')
    xml += [f'<nd ref="{i}"/>' for i in ids]
    xml += [f'<tag k="{k}" v="{v}"/>' for k, v in tags.items()]
    xml.append("</way>")
xml.append("</osm>")
(OUT / "roads" / "test.osm").write_text("\n".join(xml))

# Falchi-like raster, mcd/m2 artificial brightness, 30 arc-seconds, with a glow around a fake city
res = 1 / 120
pad_deg = 0.2
x0, y0 = W - pad_deg, N + pad_deg
cols = int((E - W + 2 * pad_deg) / res)
rows = int((N - S + 2 * pad_deg) / res)
xs = x0 + (np.arange(cols) + 0.5) * res
ys = y0 - (np.arange(rows) + 0.5) * res
X, Y = np.meshgrid(xs, ys)
city = (-93.7, 46.85)  # south-east corner
d_km = np.hypot((X - city[0]) * 76, (Y - city[1]) * 111)
glow = 0.5 * np.exp(-d_km / 18) + 0.0007  # ratio ~0.004 at darkest
glow += 0.02 * np.exp(-np.hypot((X + 94.4) * 76, (Y - 47.5) * 111) / 6)  # small town in the north
with rasterio.open(OUT / "sky" / "falchi.tif", "w", driver="GTiff", height=rows, width=cols, count=1, dtype="float32",
                   crs="EPSG:4326", transform=from_origin(x0, y0, res, res), nodata=-9999) as dst:
    dst.write(glow.astype("float32"), 1)

print("Wrote synthetic fixtures to", OUT)
