"""Independent check of the synthetic-fixture build: sample points inside every candidate polygon and test
each criterion directly against the raw fixtures (not through the pipeline code).

Run after: python -m campfinder build --config tests/config.test.toml
"""
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import shapely

root = Path(__file__).resolve().parent
fx = root / "fixtures"
out = root.parent / "test_output"
CRS = "EPSG:26915"

cands = gpd.read_file(out / "candidates.geojson").to_crs(CRS)
assert len(cands), "no candidates to verify"

nf = gpd.read_file(fx / "land/padus_mn.gpkg").to_crs(CRS)
sf = gpd.read_file(fx / "land/dnr_state_forest_lands.gpkg").to_crs(CRS)
land = shapely.union_all(list(nf[(nf.Des_Tp == "NF") & (nf.Category == "Fee")].geometry) + list(sf.geometry))
wild = shapely.union_all(nf[nf.Des_Tp == "WA"].geometry.values)
lakes = gpd.read_file(fx / "lakes/dnr_lakes.gpkg").to_crs(CRS)
lakes = lakes[lakes.area / 1e4 >= 10]
lake_u = shapely.union_all(lakes.geometry.values)
roads = gpd.read_file(fx / "roads/test.osm", layer="lines", engine="pyogrio").to_crs(CRS)
other = roads["other_tags"].fillna("")
roads = roads[~other.str.contains('"access"=>"private"') & ~other.str.contains('"service"=>"driveway"')]
acc = shapely.union_all(roads[roads.highway.isin(["track", "unclassified", "service", "residential", "tertiary", "secondary"])].geometry.values)
busy = shapely.union_all(roads[roads.highway.isin(["motorway", "trunk", "primary"])].geometry.values)

with rasterio.open(fx / "sky/falchi.tif") as src:
    sky = src.read(1)
    tf = src.transform
    sky_crs = src.crs

rng = np.random.default_rng(1)
fails, n = 0, 0
tol = 15  # simplification tolerance plus polygon rounding, meters
for geom in cands.geometry:
    minx, miny, maxx, maxy = geom.bounds
    pts = []
    for _ in range(200):
        p = shapely.Point(rng.uniform(minx, maxx), rng.uniform(miny, maxy))
        if geom.contains(p):
            pts.append(p)
        if len(pts) >= 25:
            break
    for p in pts:
        n += 1
        ll = gpd.GeoSeries([p], crs=CRS).to_crs("EPSG:4326").iloc[0]
        row, col = rasterio.transform.rowcol(tf, ll.x, ll.y)
        ratio = float(sky[row, col]) / 0.174
        d_lake = p.distance(lake_u)
        problems = []
        if not land.buffer(tol).contains(p): problems.append("outside campable land")
        if wild.buffer(-tol).contains(p): problems.append("inside wilderness")
        if ratio >= 0.19 + 0.02: problems.append(f"sky ratio {ratio:.3f} too bright")
        if not (60 - tol <= d_lake <= 600 + tol): problems.append(f"lake distance {d_lake:.0f} m")
        if p.distance(acc) > 400 + tol: problems.append(f"road distance {p.distance(acc):.0f} m")
        if p.distance(busy) < 300 - tol: problems.append(f"busy highway {p.distance(busy):.0f} m away")
        if problems:
            fails += 1
            print("FAIL", ll.x, ll.y, problems)

print(f"checked {n} points in {len(cands)} candidate pieces, {fails} failures")
print("sources:", sorted(cands.source.unique()), "| sky bands:", sorted(cands.sky_band.unique()))
print("lake names:", sorted(set(cands.lake_name.dropna())))
sys.exit(1 if fails else 0)
