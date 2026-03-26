VFROnePager
===========

Generate a one-page VFR trip plan (A5 panels on duplex A4) using free data sources.

Summary
-------
- Single-file generator: `vfr_onepager.py`
- Produces a duplex-ready PDF with a front VFR log and a rotated back frequency panel.
- Low-ink design and Spanish labels by default.

Requirements
------------
Install into a Python 3.9+ environment:

```bash
pip install -r requirements.txt
```

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


What the script does
--------------------
- Downloads OurAirports CSVs (airports, frequencies, runways).
- Samples terrain along the route via Open-Elevation to compute leg minimums.
- Queries Nominatim/OSM for short landmark names (rate-limited to 1 req/s).
- Calculates magnetic variation via the local geomag WMM library.
- Builds legs (default 5-minute cruise legs). The first leg uses a climb-time factor (default `1.3`).
- Recommends a cruise altitude (snapped to 500 ft steps) that is at least 300 ft above the highest terrain and not below any leg's minimum.
- Adds a `Track` column with the per-segment magnetic heading and shows `T.Plan` as cumulative minutes from origin.
- Inserts waypoint marker rows (with cumulative time/distance) and a final destination marker row in the trip table.


Notes & tips
-----------
- Nominatim rate limit: the script sleeps 1s per reverse-geocode request. Building long routes may take time.
- Open-Elevation is used for terrain sampling; network failures fall back to 0 m.
- If an alternate airport shows no frequency, it is because OurAirports has no registered freq for that field. The script now prefers alternates that have at least one frequency.
- The generated PDF filename defaults to `<ORIG>_<DEST>_vfr.pdf` if `-o` is not provided.

Customization
-------------
- `LEG_MINUTES` and `CLIMB_SPEED_FACTOR` live near the top of `vfr_onepager.py` for easy tuning.
- To require a minimum runway length for alternates, extend `closest_airport()` to cross-reference `runways.csv`.
 
Output notes
------------
- The generated PDF is duplex A4 landscape with two A5 panels: the left front panel contains the trip log (Spanish labels), the right back panel contains frequency information.
- The alternative cell layout was tightened to be more compact (smaller leading) to fit multi-line alternative entries.

Caveats / Legal
---------------
This tool is for planning and simulation only. Always verify frequencies, procedures and terrain with official sources before flight.

License
-------
Use and modify freely for personal and educational use. No warranty.
