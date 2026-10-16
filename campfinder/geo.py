"""Vector and raster loading helpers."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import box


def warn(msg: str):
    print(f"  ! {msg}", file=sys.stderr)


def log(msg: str):
    print(msg, flush=True)


def area_ha(geoms) -> float:
    """Total area in hectares of a GeoSeries/GeoDataFrame in a metric CRS."""
    if hasattr(geoms, "geometry"):
        geoms = geoms.geometry
    if len(geoms) == 0:
        return 0.0
    return float(geoms.area.sum()) / 1e4


def _polys(g):
    if g is None or g.is_empty:
        return None
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g
    if g.geom_type == "GeometryCollection":
        parts = [p for p in g.geoms if p.geom_type in ("Polygon", "MultiPolygon")]
        return shapely.union_all(parts) if parts else None
    return None


def polygons_only(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Repair geometries and keep polygons only."""
    gdf = gdf[gdf.geometry.notna()].copy()
    gdf["geometry"] = shapely.make_valid(gdf.geometry.values)
    gdf["geometry"] = gdf.geometry.apply(_polys)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    return gdf


def read_vector(path: Path, layer=None, query=None, crs=None, **kwargs) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path, layer=layer or None, engine="pyogrio", **kwargs)
    if query:
        try:
            gdf = gdf.query(query, engine="python")
        except Exception as e:
            cols = ", ".join(map(str, gdf.columns))
            raise SystemExit(
                f"Query failed for {path.name}: {query!r}\n  {e}\n  Columns available: {cols}\n"
                f"  Run: python -m campfinder inspect \"{path}\""
            )
    if crs is not None:
        if gdf.crs is None:
            raise SystemExit(f"{path} has no CRS defined; cannot reproject.")
        gdf = gdf.to_crs(crs)
    return gdf


def extent_polygon(cfg, crs) -> gpd.GeoDataFrame:
    """Minnesota outline: a supplied boundary file, or the lon/lat bbox."""
    sb = cfg.get("project.state_boundary", "")
    if sb:
        p = cfg.resolve(sb)
        if p.exists():
            gdf = polygons_only(read_vector(p, crs=crs))
            geom = shapely.union_all(gdf.geometry.values)
            return gpd.GeoDataFrame({"name": ["extent"]}, geometry=[geom], crs=crs)
        warn(f"state_boundary {p} not found; using bbox instead")
    w, s, e, n = cfg.get("project.bbox", [-97.3, 43.4, -89.4, 49.5])
    return gpd.GeoDataFrame({"name": ["extent"]}, geometry=[box(w, s, e, n)], crs="EPSG:4326").to_crs(crs)


# ---------------------------------------------------------------- night sky

def sky_polygons(cfg, crs, extent: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Classify the brightness raster into bands and polygonize them (vector)."""
    import rasterio
    from affine import Affine
    from rasterio.enums import Resampling
    from rasterio.features import shapes
    from rasterio.windows import Window, from_bounds

    path = cfg.resolve(cfg.get("inputs.sky.path"))
    if not path.exists():
        raise SystemExit(f"Night-sky raster not found: {path}")
    breaks = list(cfg.get("inputs.sky.breaks"))
    labels = list(cfg.get("inputs.sky.labels", []))
    if len(labels) != len(breaks) + 1:
        labels = [f"Band {i}" for i in range(len(breaks) + 1)]
    divide_by = float(cfg.get("inputs.sky.divide_by", 1.0))
    up = max(1, int(cfg.get("inputs.sky.upsample", 1)))

    with rasterio.open(path) as src:
        minx, miny, maxx, maxy = extent.to_crs(src.crs).total_bounds
        pad = 2 * max(src.res)
        win = from_bounds(minx - pad, miny - pad, maxx + pad, maxy + pad, src.transform)
        win = win.round_offsets().round_lengths().intersection(Window(0, 0, src.width, src.height))
        out_shape = (int(win.height * up), int(win.width * up))
        arr = src.read(
            int(cfg.get("inputs.sky.band", 1)),
            window=win,
            out_shape=out_shape,
            resampling=Resampling.bilinear if up > 1 else Resampling.nearest,
            masked=True,
        )
        transform = src.window_transform(win) * Affine.scale(win.width / out_shape[1], win.height / out_shape[0])
        raster_crs = src.crs

    vals = np.ma.filled(arr.astype("float64"), np.nan) / divide_by
    valid = np.isfinite(vals)
    band = np.digitize(np.where(valid, vals, 0), breaks).astype("uint8")

    recs = [
        {"band": int(v), "geometry": shapely.geometry.shape(g)}
        for g, v in shapes(band, mask=valid, transform=transform, connectivity=4)
    ]
    if not recs:
        raise SystemExit("Night-sky raster produced no valid cells inside the extent. Check its CRS and path.")
    gdf = gpd.GeoDataFrame(recs, crs=raster_crs).to_crs(crs)
    gdf["geometry"] = shapely.make_valid(gdf.geometry.values)
    gdf = gdf.dissolve("band", as_index=False)
    gdf = gpd.clip(gdf, extent)
    gdf = polygons_only(gdf)
    gdf["label"] = gdf["band"].map(lambda b: labels[b] if b < len(labels) else f"Band {b}")
    return gdf[["band", "label", "geometry"]].reset_index(drop=True)


# -------------------------------------------------------------------- roads

_TAG = {k: re.compile(rf'"{k}"=>"([^"]*)"') for k in ("surface", "access", "service")}


def _tag(series: pd.Series, key: str) -> pd.Series:
    return series.fillna("").str.extract(_TAG[key], expand=False)


def load_roads(cfg, crs, roi_wgs_bounds, roi) -> gpd.GeoDataFrame:
    """Roads with columns highway, name, surface. OSM (.pbf/.osm) or any vector file."""
    path = cfg.resolve(cfg.get("inputs.roads.path"))
    if not path.exists():
        raise SystemExit(f"Roads file not found: {path}")
    keep = set(cfg.get("criteria.roads.access_classes", [])) | set(cfg.get("criteria.roads.busy_classes", []))

    if path.suffix.lower() in (".pbf", ".osm"):
        gdf = gpd.read_file(
            path,
            engine="pyogrio",
            sql="SELECT highway, name, other_tags FROM lines WHERE highway IS NOT NULL",
            sql_dialect="OGRSQL",
            bbox=tuple(roi_wgs_bounds),
        )
        gdf = gdf[gdf["highway"].isin(keep)].copy()
        gdf["surface"] = _tag(gdf["other_tags"], "surface")
        access = _tag(gdf["other_tags"], "access")
        service = _tag(gdf["other_tags"], "service")
        gdf = gdf[~access.isin(["private", "no"]) & ~service.isin(["driveway", "parking_aisle", "drive-through"])]
        gdf = gdf[["highway", "name", "surface", "geometry"]]
    else:
        cf = cfg.get("inputs.roads.class_field", "highway")
        nf = cfg.get("inputs.roads.name_field", "name")
        sf = cfg.get("inputs.roads.surface_field", "surface")
        gdf = gpd.read_file(path, engine="pyogrio")
        gdf = gdf.rename(columns={cf: "highway"})
        gdf = gdf[gdf["highway"].isin(keep)].copy()
        gdf["name"] = gdf[nf] if nf in gdf.columns else None
        gdf["surface"] = gdf[sf] if sf in gdf.columns else None
        gdf = gdf[["highway", "name", "surface", "geometry"]]

    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    gdf = gdf.to_crs(crs)
    shapely.prepare(roi)
    gdf = gdf[shapely.intersects(gdf.geometry.values, roi)]
    return gdf.reset_index(drop=True)


def load_lakes(cfg, crs, roi) -> gpd.GeoDataFrame:
    path = cfg.resolve(cfg.get("inputs.lakes.path"))
    if not path.exists():
        raise SystemExit(f"Lakes file not found: {path}")
    gdf = polygons_only(read_vector(path, cfg.get("inputs.lakes.layer"), cfg.get("inputs.lakes.query"), crs))
    nf = cfg.get("inputs.lakes.name_field", "")
    gdf["lake_name"] = gdf[nf] if nf and nf in gdf.columns else None
    if nf and nf not in gdf.columns:
        warn(f"lakes name_field {nf!r} not in file; lake names will be blank")
    gdf["lake_ha"] = gdf.geometry.area / 1e4
    gdf = gdf[gdf["lake_ha"] >= float(cfg.get("criteria.lakes.min_area_ha", 0))]
    shapely.prepare(roi)
    gdf = gdf[shapely.intersects(gdf.geometry.values, roi)]
    return gdf[["lake_name", "lake_ha", "geometry"]].reset_index(drop=True)
