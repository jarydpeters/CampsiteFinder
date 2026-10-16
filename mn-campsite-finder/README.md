# Minnesota dark-camp finder

Overlays four vector criteria and writes the surviving areas as polygons you can open in an interactive map:

1. **Land**: national forest and state forest land you can camp on (wilderness and other zones removable)
2. **Night sky**: the brightness raster, classified into bands and polygonized (the only raster input)
3. **Lakes**: within a distance band of a lake of a minimum size
4. **Roads**: close enough to drive to, far enough from busy highways

"Private land" is not a dataset. It is drawn as everything that isn't listed as public.

Everything is controlled from `config.toml`. Rerun any time with one command.

## Setup

Python 3.11 or newer.

```
pip install -r requirements.txt
```

## Data to download

Put the files where `config.toml` expects them (or change the paths there). `python -m campfinder check` lists what's missing.

| Criterion | File | Where to get it |
|---|---|---|
| Public land | `data/land/padus_mn.gpkg` | USGS PAD-US, Minnesota extract ([how to get it](https://www.protectedlands.net/how-to-get-pad-us/)). Convert to GeoPackage if it comes as a geodatabase. |
| State forest land | `data/land/dnr_state_forest_lands.gpkg` | [MN Geospatial Commons](https://gisdata.mn.gov), DNR datasets. Use **land the DNR administers**. The [State Forest Statutory Boundaries](https://gisdata.mn.gov/dataset/bdry-state-forest) layer includes private inholdings, so it overstates what's public. |
| Lakes | `data/lakes/dnr_lakes.gpkg` | MN Geospatial Commons, DNR hydrography / lakes polygons (or USGS NHD waterbodies). |
| Roads | `data/roads/minnesota-latest.osm.pbf` | [Geofabrik Minnesota extract](https://download.geofabrik.de/north-america/us/minnesota.html) |
| Night sky | `data/sky/falchi.tif` | Falchi et al., *New World Atlas of Artificial Night Sky Brightness*, GFZ Data Services (DOI 10.5880/GFZ.1.4.2016.001). A VIIRS annual composite also works, see the sky comments in `config.toml`. |

Any of the vector files can be GeoPackage, shapefile, GeoJSON or a geodatabase.

## Workflow

```
python -m campfinder check                      # which inputs are present
python -m campfinder inspect data/land/padus_mn.gpkg   # real column names and values
# edit the land queries in config.toml to match what inspect shows
python -m campfinder build                      # writes output/
python -m campfinder serve                      # opens the map at http://127.0.0.1:8000
```

`python -m campfinder run` does build then serve.

`build` prints a **funnel** showing how many hectares survive each criterion. When nothing survives, that tells you which criterion to loosen:

```
python -m campfinder build --set criteria.sky.max_band=3 --set criteria.lakes.max_distance_m=1000
python -m campfinder build --set criteria.roads.enabled=false
```

`--set` overrides one run without touching the file. Edit `config.toml` to make a change permanent.

## Output (`output/`)

| File | Contents |
|---|---|
| `candidates.geojson` | The result polygons, with rank, land unit, area, sky band, nearest lake and road, distances, and a centre point |
| `shortlist.csv` | The same, one row per area, ranked, with lat/lon and a map link |
| `public_land`, `private_land`, `lakes`, `roads`, `sky` `.geojson` | Context layers for the viewer |
| `manifest.json` | Run summary the viewer reads |

All layers are vector. Geometry is simplified by 10 m for display (`outputs.simplify_m`; set 0 to disable).

## Status: what has and hasn't been tested

Tested end to end on **synthetic** Minnesota-shaped data (`tests/make_fixtures.py`): the overlay, the exclusions, the OSM road reading, the sky polygonizing, the viewer in a browser, and an independent check that sampled points inside every result satisfy every criterion (`tests/verify.py`).

**Not yet tested on the real downloads**, because the build environment couldn't reach the state data servers. So:

- The PAD-US and DNR column names and codes in `config.toml` come from the USGS/DNR documentation and may need adjusting. That is what `inspect` is for. A query that matches nothing prints a warning naming the file.
- A statewide OSM extract is read straight from the `.pbf`. Expect that step to take a few minutes. If it's too slow, clip the extract to northern Minnesota first.
- If the GeoJSON layers get too big for the browser, the next step is converting them to PMTiles with tippecanoe. The viewer would need a small change to read them.

To rerun the synthetic test: `python tests/make_fixtures.py`, then `python -m campfinder build --config tests/config.test.toml`, then `python tests/verify.py`.

## Caveats worth knowing

- **Public does not mean you can camp.** Rules differ by agency, forest and designation, and change. The tool narrows where to look. Check the managing agency before you go. Leave the wilderness exclusion on: the Boundary Waters and similar areas are permit and entry-point systems.
- **Sky bands are approximate.** Falchi is modelled from satellite data and is coarse (about 1 km cells), so contours are blocky by design. The Bortle labels are rough. Compare the map against dark places you know and adjust `breaks`.
- **OSM tracks** may be impassable, gated or seasonal. The `surface` shown is whatever a mapper tagged.
- **Lake setback** (60 m) follows common Leave No Trace guidance, not a legal limit.
