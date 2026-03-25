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

What the script does
--------------------
- Downloads OurAirports CSVs (airports, frequencies, runways).
- Samples terrain along the route via Open-Elevation to compute leg minimums.
- Queries Nominatim/OSM for short landmark names (rate-limited to 1 req/s).
- Calculates magnetic variation via the local geomag WMM library.
- Builds legs (default 5-minute cruise legs). The first leg uses a climb-time factor (default `1.3`).
- Recommends a cruise altitude (snapped to 500 ft steps) that is at least 300 ft above the highest terrain and not below any leg's minimum.

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

Caveats / Legal
---------------
This tool is for planning and simulation only. Always verify frequencies, procedures and terrain with official sources before flight.

License
-------
Use and modify freely for personal and educational use. No warranty.
