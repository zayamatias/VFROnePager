VFROnePager
===========

Generate a one-page VFR trip plan (A5 panels on duplex A4) using free data sources.

Summary
-------
- Single-file generator: `vfr_onepager.py`
- Produces a duplex-ready PDF with a front VFR log and a back frequency panel.
Preview on a single face:
```bash
# To render both panels side-by-side on one A4 page (useful for single-sided preview):
python3 vfr_onepager.py LEPP LEZG 100 10 --one-face -o preview.pdf
```
- Low-ink design and Spanish labels by default.

Recent changes (2026-04-01)
---------------------------
- Local SRTM1 DEM support: the generator now downloads and caches AWS "elevation-tiles-prod/skadi" HGT tiles (SRTM1) and samples terrain locally instead of always calling an elevation API. This greatly reduces network usage and improves reliability.
- Overpass: switched to a compact node-only query and an ordered list of public Overpass instances (with a short per-endpoint timeout). Disk cache is checked before any network call so cached POIs are reused immediately.
- PDF layout: fixed a blank-page bug in `--one-face` mode that previously inserted an empty page between the panels and the tile maps.
- Landmark placement: visual landmark dots are now chosen to the LEFT of the aircraft and preferentially ahead-left of the track when possible.

------------
Install into a Python 3.9+ environment:

```bash
pip install -r requirements.txt
```

Note: the leg minimap page requires `Pillow` (the repository's `requirements.txt` already includes `Pillow>=9.0`).

Quick usage
-----------
```bash
python3 vfr_onepager.py ORIG_ICAO DEST_ICAO CRUISE_KTS FUEL_GPH [-o output.pdf]
# Example:
python3 vfr_onepager.py LEPP LEZG 100 10 -o trip.pdf
```
- `ORIG_ICAO` / `DEST_ICAO` : ICAO codes (e.g. `LEPP`, `LEZG`).
- `CRUISE_KTS` : cruise speed in knots (IAS).
- `FUEL_GPH` : fuel consumption in US gallons per hour.

Advanced usage & examples
-------------------------
The script supports intermediate route waypoints (`--via`) and produces additional information in the trip log:

- `Track` column: the constant magnetic heading for the current segment (origin → WP1, WP1 → WP2, … → destination).
- `T.Plan`: time shown is cumulative minutes from origin (keeps `T+` prefix for the descent marker).
- Waypoint marker rows: full-width shaded rows showing the waypoint name, cumulative time from origin and total distance flown so far.
- Final destination marker: a shaded row at the end showing total ETE and total distance.

Examples:

```bash
# Simple direct trip
python3 vfr_onepager.py LEPP LEZG 100 10 -o trip.pdf

# With two intermediate waypoints
python3 vfr_onepager.py LEPP LEZG 110 6 \
	--via "EMBALSE,42.09395134930251,-1.0864194804110534" \
	--via "LUCENI,41.828027058802995,-1.2391816773319912" \
	-o trip_via.pdf
```

Multiple waypoints (VIA)
------------------------
You can include intermediate waypoints using the `--via` option. Repeat `--via` for multiple points in the order you want them flown (origin → WP1 → WP2 → … → destination).

Format: `NAME,lat,lon` (brackets optional). Examples:

```bash
# Single intermediate waypoint
python3 vfr_onepager.py LEPP LEZG 110 6 --via "LONDON,51.51407373693925,-0.11524800056813268"

# Multiple waypoints (order matters)
python3 vfr_onepager.py LEPP LEZG 110 6 \
	--via "LONDON,51.5140737,-0.1152480" \
	--via "BRUSSELS,50.6738742,4.3744017" \
	-o trip_via.pdf
```

Behavior notes:
- The route is split into segments: ORIGIN → WP1 → WP2 → … → DEST.
- Leg division (default 5-minute legs) resets at each waypoint so each segment is segmented independently.
- The climb-time factor only applies to the very first leg of the entire route (departure).
- Waypoint markers are shown as shaded full-width rows in the PDF with the new track (magnetic) to the next point.

- Wind averaging: per-leg winds (shown in the trip table) are averaged using a
	vector mean (u,v) to produce a single representative wind speed and a
	``from`` direction for the summary box. This avoids incorrect results when
	averaging circular directions arithmetically.

 - Alternate entries now include airport elevation (e.g. "Elev: 1234 ft") when available.


What the script does
--------------------
- Downloads OurAirports CSVs (airports, frequencies, runways).
 - Downloads OurAirports CSVs (airports, frequencies, runways).
 - Samples terrain via local SRTM1 HGT tiles (auto-downloaded from AWS `elevation-tiles-prod/skadi`) and caches them under `~/.vfr_tile_cache/dem/`. If a tile cannot be downloaded the script falls back to the Open-Topo-Data API for those points.
 - Queries Overpass (preferred) and Nominatim for short landmark names. Cache is checked before any network request and the script will rotate public Overpass instances when a service fails. If Overpass is unavailable the script falls back to Nominatim.
- Calculates magnetic variation via the local geomag WMM library.
- Builds legs (default 5-minute cruise legs). The first real leg's duration is adjusted to represent
	climb time to the recommended cruise altitude (computed from origin elevation and `--climb-rate`);
	you can also override the leg interval with `--leg-minutes`.
- Recommends a cruise altitude (snapped to 500 ft steps) that is at least 300 ft above the highest terrain and not below any leg's minimum.
- Adds a `Track` column with the per-segment magnetic heading and shows `T.Plan` as cumulative minutes from origin.
- Inserts waypoint marker rows (with cumulative time/distance) and a final destination marker row in the trip table.

**Leg minimap tiles (new)**
- The PDF now includes an additional A4-landscape page with 4×3 leg minimap tiles (track-up). Each tile covers ~10 NM and is rendered using stitched OSM raster tiles.
- Tiles are generated only if `Pillow` is available. Raster tiles and Overpass POI responses are cached under `~/.vfr_tile_cache/` to reduce network requests.
- The tile generator displaces the track to the right so a nearby POI/landmark is visible on the left; the track is drawn through the tile to show continuity.


Notes & tips
-----------
- Nominatim rate limit: the script sleeps 1s per reverse-geocode request. Building long routes may take time.
 - Open-Topo-Data is used only as a fallback for points whose SRTM tile could not be downloaded.
- If an alternate airport shows no frequency, it is because OurAirports has no registered freq for that field. The script now prefers alternates that have at least one frequency.
- The generated PDF filename defaults to `<ORIG>_<DEST>_vfr.pdf` if `-o` is not provided.

Troubleshooting Overpass / tiles
 - If you see Overpass failures frequently, the script will automatically try alternative Overpass instances and use a cached response when available. You can rerun the command; cached POIs and raster tiles will be reused.
 - Tile cache location: `~/.vfr_tile_cache/` (remove or clear this directory to force fresh tile downloads).
 - To disable tile pages, run in an environment without `Pillow`; the rest of PDF generation will continue.

Dependencies
------------
Install the runtime dependencies listed in `requirements.txt` into a Python 3.9+ virtualenv:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If you don't want the minimap tiles, omit `Pillow` from the environment (the main PDF is still generated).

Additional behavior and CLI options
----------------------------------
- `--leg-minutes`: override the default leg interval (minutes) used to split the route into legs (default: 5). Useful to produce coarser or finer leg tables.
- `--climb-rate`: climb rate in ft/min (default: 500). The script computes climb time from origin elevation to the recommended cruise altitude and adjusts the first real leg's elapsed time and fuel accordingly; this affects total ETE and fuel required displayed in the plan.
- First leg handling: the first real leg's elapsed time represents the time to reach cruise altitude (computed from `--climb-rate` and origin elevation) rather than a fixed-leg interval; this replaces the earlier fixed climb-factor behaviour.

Customization
-------------
- `LEG_MINUTES` and `CLIMB_SPEED_FACTOR` live near the top of `vfr_onepager.py` for easy tuning.
- To require a minimum runway length for alternates, extend `closest_airport()` to cross-reference `runways.csv`.
 
Output notes
------------
- The generated PDF defaults to duplex A4 landscape with two A5 panels: the first page is the front panel, the second page is the back panel. The back panel is drawn unrotated so a normal duplex printer should print it on the back of page 1.
- The `--one-face` flag renders both panels side-by-side on a single A4 page for single-sided previewing or quick checks.
- The alternative cell layout was tightened to be more compact (smaller leading) to fit multi-line alternative entries; alternates also show elevation when available.

- `Viento` column format: the trip table's wind column now shows two lines when wind data is available: top line `SS/DDD` (speed/direction FROM in degrees true) and bottom line `HW/CW` where `HW` is the head/tail component (negative = headwind/slows, positive = tailwind/adds speed) and `CW` is the signed crosswind (negative = from left, positive = from right).

Caveats / Legal
---------------
This tool is for planning and simulation only. Always verify frequencies, procedures and terrain with official sources before flight.

License
-------
Use and modify freely for personal and educational use. No warranty.
