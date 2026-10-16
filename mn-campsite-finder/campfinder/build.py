"""Build candidate campsite polygons by intersecting land, sky, roads and lake criteria."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

from .geo import (
    area_ha, extent_polygon, load_lakes, load_roads, log, polygons_only,
    read_vector, sky_polygons, warn,
)


def _empty(crs):
    return gpd.GeoDataFrame({"source": [], "unit": []}, geometry=[], crs=crs)


def _load_sources(cfg, key, crs, extent):
    """Load land sources in priority order; later sources lose any area an earlier one already claimed."""
    frames, claimed = [], None
    for s in cfg.get(f"land.{key}", []):
        p = cfg.resolve(s["path"])
        if not p.exists():
            warn(f"land.{key} '{s['name']}': {p} not found, skipping")
            continue
        gdf = polygons_only(read_vector(p, s.get("layer"), s.get("query"), crs))
        if gdf.empty:
            warn(f"land.{key} '{s['name']}': no features after query {s.get('query')!r} "
                 f"(try: python -m campfinder inspect \"{p}\")")
            continue
        nf = s.get("name_field")
        if nf and nf in gdf.columns:
            unit = gdf[nf].astype(str).values
        else:
            if nf:
                warn(f"land.{key} '{s['name']}': name_field {nf!r} not in file; using source name")
            unit = [s["name"]] * len(gdf)
        gdf = gpd.GeoDataFrame({"source": s["name"], "unit": unit}, geometry=gdf.geometry.values, crs=crs)
        gdf = polygons_only(gpd.clip(gdf, extent))
        if claimed is not None and not gdf.empty:
            gdf["geometry"] = gdf.geometry.difference(claimed)
            gdf = polygons_only(gdf)
        if gdf.empty:
            continue
        u = shapely.union_all(gdf.geometry.values)
        claimed = u if claimed is None else shapely.union(claimed, u)
        log(f"  {key:9s} {s['name']}: {len(gdf):,} polygons, {area_ha(gdf):,.0f} ha")
        frames.append(gdf)
    if not frames:
        return _empty(crs)
    return gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry", crs=crs)


def _zone(geoms, dist, quad_segs=8):
    return shapely.union_all(shapely.buffer(geoms, dist, quad_segs=quad_segs))


def _as_gdf(geom, crs):
    return gpd.GeoDataFrame(geometry=list(shapely.get_parts(geom)), crs=crs)


def _write_geojson(gdf, path: Path, simplify_m: float):
    g = gdf.copy()
    if simplify_m and len(g):
        g["geometry"] = g.geometry.simplify(simplify_m, preserve_topology=True)
        g = g[g.geometry.notna() & ~g.geometry.is_empty]
    if len(g) == 0:
        path.write_text('{"type":"FeatureCollection","features":[]}')
        return 0
    g = g.to_crs("EPSG:4326")
    g.to_file(path, driver="GeoJSON", engine="pyogrio", COORDINATE_PRECISION=6)
    return len(g)


def build(cfg):
    t0 = time.time()
    crs = cfg.get("project.crs", "EPSG:26915")
    out_dir = cfg.resolve(cfg.get("outputs.dir", "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    simplify_m = float(cfg.get("outputs.simplify_m", 10))

    sky_on = bool(cfg.get("criteria.sky.enabled", True))
    lakes_on = bool(cfg.get("criteria.lakes.enabled", True))
    roads_on = bool(cfg.get("criteria.roads.enabled", True))

    log("Loading extent and land")
    extent = extent_polygon(cfg, crs)
    ext_geom = extent.geometry.iloc[0]

    land = _load_sources(cfg, "campable", crs, extent)
    if land.empty:
        raise SystemExit("No campable land loaded. Check [[land.campable]] paths and queries in config.toml.")
    excluded = _load_sources(cfg, "exclude", crs, extent)
    context = _load_sources(cfg, "context", crs, extent)

    if not excluded.empty:
        ex_union = shapely.union_all(excluded.geometry.values)
        land["geometry"] = land.geometry.difference(ex_union)
        land = polygons_only(land)
    funnel = [("Campable public land", area_ha(land))]

    # Region of interest: only load roads and lakes near land we might use.
    reach = 0.0
    if roads_on:
        reach = max(reach, float(cfg.get("criteria.roads.max_distance_m", 400)))
    if lakes_on:
        reach = max(reach, float(cfg.get("criteria.lakes.max_distance_m", 600)))
    land_union = shapely.union_all(land.geometry.values)
    roi = land_union.simplify(100).buffer(reach + 2000)
    roi_wgs = gpd.GeoSeries([roi], crs=crs).to_crs("EPSG:4326").total_bounds

    cands = land.copy()

    # ---- night sky
    sky = None
    sky_path = cfg.resolve(cfg.get("inputs.sky.path", ""))
    if sky_on or sky_path.exists():
        log("Polygonizing night-sky raster")
        sky = sky_polygons(cfg, crs, extent)
    if sky_on:
        max_band = int(cfg.get("criteria.sky.max_band", 2))
        sky_ok = sky[sky["band"] <= max_band]
        cands = gpd.overlay(cands, sky_ok, how="intersection", keep_geom_type=True)
        labels = list(cfg.get("inputs.sky.labels", []))
        funnel.append((f"+ sky band <= {max_band}" + (f" ({labels[max_band]})" if max_band < len(labels) else ""),
                       area_ha(cands)))
    else:
        cands["band"] = -1
        cands["label"] = "Not filtered"

    # ---- roads
    lakes = roads_access = None
    roads_all = None
    if roads_on or Path(cfg.resolve(cfg.get("inputs.roads.path", ""))).exists():
        log("Loading roads (OSM extracts can take a few minutes)")
        roads_all = load_roads(cfg, crs, roi_wgs, roi)
        acc = set(cfg.get("criteria.roads.access_classes", []))
        busy = set(cfg.get("criteria.roads.busy_classes", []))
        roads_access = roads_all[roads_all["highway"].isin(acc)]
        log(f"  roads: {len(roads_access):,} access segments, {int(roads_all['highway'].isin(busy).sum()):,} busy-highway segments")
    if roads_on:
        r_max = float(cfg.get("criteria.roads.max_distance_m", 400))
        r_busy = float(cfg.get("criteria.roads.busy_min_distance_m", 0))
        if roads_access.empty:
            cands = cands.iloc[0:0]
        else:
            zone = _zone(roads_access.geometry.values, r_max)
            if r_busy > 0:
                busy_geoms = roads_all[roads_all["highway"].isin(busy)].geometry.values
                if len(busy_geoms):
                    zone = shapely.difference(zone, _zone(busy_geoms, r_busy))
            cands = gpd.overlay(cands, _as_gdf(zone, crs), how="intersection", keep_geom_type=True)
        funnel.append((f"+ within {r_max:.0f} m of a road, >= {r_busy:.0f} m from busy highways", area_ha(cands)))

    # ---- lakes
    if lakes_on or cfg.resolve(cfg.get("inputs.lakes.path", "")).exists():
        log("Loading lakes")
        lakes = load_lakes(cfg, crs, roi)
        log(f"  lakes: {len(lakes):,} at or above {cfg.get('criteria.lakes.min_area_ha', 0)} ha")
    if lakes_on:
        l_max = float(cfg.get("criteria.lakes.max_distance_m", 600))
        l_set = float(cfg.get("criteria.lakes.shore_setback_m", 0))
        if lakes.empty:
            cands = cands.iloc[0:0]
        else:
            lg = lakes.geometry.values
            zone = shapely.difference(_zone(lg, l_max), _zone(lg, max(l_set, 0.01)))
            cands = gpd.overlay(cands, _as_gdf(zone, crs), how="intersection", keep_geom_type=True)
        funnel.append((f"+ {l_set:.0f} to {l_max:.0f} m from a lake", area_ha(cands)))

    # ---- finish candidates
    cands = polygons_only(cands) if len(cands) else cands
    if len(cands):
        cands["unit"] = cands["unit"].fillna("")
        cands = cands.dissolve(by=["source", "unit", "band", "label"], as_index=False)
        cands = cands.explode(index_parts=False).reset_index(drop=True)
        cands["area_ha"] = (cands.geometry.area / 1e4).round(2)
        cands = cands[cands["area_ha"] >= float(cfg.get("candidates.min_area_ha", 1))].reset_index(drop=True)
    funnel.append((f"Final candidate pieces >= {cfg.get('candidates.min_area_ha', 1)} ha", area_ha(cands)))

    log("\nCriteria funnel (hectares remaining):")
    for name, ha in funnel:
        log(f"  {ha:>14,.0f}  {name}")

    cands = _annotate(cands, lakes, roads_access, crs, cfg)

    # ---- write outputs
    log("\nWriting layers")
    layers = {}

    def emit(name, gdf, simplify=simplify_m):
        p = out_dir / f"{name}.geojson"
        n = _write_geojson(gdf, p, simplify)
        layers[name] = {"file": p.name, "features": int(n), "bytes": p.stat().st_size}
        log(f"  {name:13s} {n:>8,} features  {p.stat().st_size / 1e6:7.2f} MB")

    emit("candidates", cands, simplify=min(simplify_m, 5))

    pub_frames = []
    for kind, g in (("context", context), ("excluded", excluded), ("campable", land)):
        if len(g):
            g = g[["source", "unit", "geometry"]].copy()
            g["kind"] = kind
            pub_frames.append(g)
    public = gpd.GeoDataFrame(pd.concat(pub_frames, ignore_index=True), geometry="geometry", crs=crs)
    emit("public_land", public)

    if cfg.get("outputs.private_land", True):
        pub_simpl = public.geometry.simplify(max(simplify_m, 1)) if simplify_m else public.geometry
        priv = shapely.difference(ext_geom, shapely.union_all(pub_simpl.values))
        emit("private_land", gpd.GeoDataFrame({"kind": ["private_or_unlisted"]}, geometry=[priv], crs=crs))

    if sky is not None:
        emit("sky", sky)
    if lakes is not None:
        emit("lakes", lakes)
    if roads_all is not None:
        scope = cfg.get("outputs.roads_scope", "candidates")
        r = roads_all
        if scope == "candidates":
            if len(cands):
                near = shapely.union_all(cands.geometry.simplify(50).values).buffer(
                    float(cfg.get("outputs.roads_margin_m", 1500)))
                shapely.prepare(near)
                r = r[shapely.intersects(r.geometry.values, near)]
            else:
                r = r.iloc[0:0]
        emit("roads", r, simplify=min(simplify_m, 5))

    # shortlist
    if len(cands):
        cols = ["rank", "source", "unit", "area_ha", "sky_band", "sky_label", "lake_name", "dist_lake_m",
                "road_class", "road_name", "road_surface", "dist_road_m", "lat", "lon", "map_link"]
        cands[[c for c in cols if c in cands.columns]].to_csv(out_dir / "shortlist.csv", index=False)
        log(f"  shortlist.csv {len(cands):>8,} rows")
    else:
        log("\n  No candidates survived. Relax a criterion, e.g.:\n"
            "    python -m campfinder build --set criteria.sky.max_band=3 --set criteria.lakes.max_distance_m=1000")

    b = extent.to_crs("EPSG:4326").total_bounds
    manifest = {
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bounds": [float(x) for x in b],
        "layers": layers,
        "sky": {"labels": list(cfg.get("inputs.sky.labels", [])), "max_band": cfg.get("criteria.sky.max_band")},
        "funnel": [{"step": s, "hectares": round(h)} for s, h in funnel],
        "criteria": {k: cfg.get(f"criteria.{k}") for k in ("sky", "lakes", "roads")},
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    log(f"\nDone in {time.time() - t0:.0f}s. Output: {out_dir}\nView it with: python -m campfinder serve")


def _annotate(cands, lakes, roads, crs, cfg):
    if cands.empty:
        return cands
    pts = gpd.GeoDataFrame(geometry=cands.geometry.representative_point(), crs=crs)

    cands = cands.rename(columns={"band": "sky_band", "label": "sky_label"})
    cands["lake_name"], cands["dist_lake_m"] = None, float("nan")
    cands["road_class"] = cands["road_name"] = cands["road_surface"] = None
    cands["dist_road_m"] = float("nan")

    if lakes is not None and len(lakes):
        j = gpd.sjoin_nearest(pts, lakes[["lake_name", "geometry"]], how="left", distance_col="d")
        j = j[~j.index.duplicated()]
        cands["lake_name"] = j["lake_name"].values
        cands["dist_lake_m"] = j["d"].round(0).values
    if roads is not None and len(roads):
        j = gpd.sjoin_nearest(pts, roads[["highway", "name", "surface", "geometry"]], how="left", distance_col="d")
        j = j[~j.index.duplicated()]
        cands["road_class"] = j["highway"].values
        cands["road_name"] = j["name"].values
        cands["road_surface"] = j["surface"].values
        cands["dist_road_m"] = j["d"].round(0).values

    ll = pts.to_crs("EPSG:4326")
    cands["lon"] = ll.geometry.x.round(6).values
    cands["lat"] = ll.geometry.y.round(6).values
    cands["map_link"] = [f"https://www.google.com/maps?q={a},{b}" for a, b in zip(cands["lat"], cands["lon"])]

    order = [c for c in cfg.get("candidates.sort_by", ["sky_band"]) if c in cands.columns]
    cands = cands.sort_values(order, na_position="last").reset_index(drop=True)
    cands.insert(0, "rank", range(1, len(cands) + 1))
    return cands
