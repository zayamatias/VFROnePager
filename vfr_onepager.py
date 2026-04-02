#!/usr/bin/env python3
"""
vfr_onepager.py
===============
Generates a VFR trip one-pager as a duplex A4-landscape PDF.

  Page 1 (front): A5 VFR Trip Log
  Page 2 (back) : A5 Frequency Panel, rotated 180° for double-sided fold

Data sources (all free, no API key required):
  - Airport/frequency data : OurAirports CSV  (https://ourairports.com/data/)
  - Terrain elevation       : SRTM3 HGT tiles (auto-downloaded from USGS, cached locally)
  - Reverse geocoding       : Nominatim / OpenStreetMap
  - Magnetic variation      : NOAA WMM via the 'geomag' library (local)
  - Great-circle math       : geopy

Dependencies (install with pip):
    pip install reportlab geopy requests geomag
"""

import argparse
import array as _array_mod
import csv
import datetime
import gzip as _gzip
import io
import math
import sys
import time
import json
import zipfile as _zipfile
from dataclasses import dataclass, field
from typing import Optional

import requests
from geopy.distance import great_circle
from geopy.point import Point
import geomag                       # pip install geomag

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm, cm
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame,
    Table, TableStyle, Paragraph, Spacer, KeepInFrame,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.pdfgen import canvas

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OURAIRPORTS_AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OURAIRPORTS_FREQS_URL    = "https://davidmegginson.github.io/ourairports-data/airport-frequencies.csv"
OURAIRPORTS_RUNWAYS_URL  = "https://davidmegginson.github.io/ourairports-data/runways.csv"
OPEN_ELEVATION_URL       = "https://api.open-elevation.com/api/v1/lookup"
OPEN_TOPO_DATA_URL       = "https://api.opentopodata.org/v1/srtm30m"  # fallback
NOMINATIM_URL            = "https://nominatim.openstreetmap.org/reverse"
OPEN_METEO_URL           = "https://api.open-meteo.com/v1/forecast"

NM_PER_DEGREE   = 60.0          # 1° lat ≈ 60 NM
FEET_PER_METER  = 3.28084
KNOTS_TO_NM_PER_MIN = 1.0 / 60  # 1 kt = 1 NM/h = 1/60 NM/min
LEG_MINUTES       = 5           # leg interval in minutes (cruise)
CLIMB_SPEED_FACTOR = 1.3        # cruise_speed / climb_speed (first leg)
TERRAIN_BUFFER_FT = 500         # recommended min altitude above terrain

# --- Leg tile minimap constants ---
import os as _os
# CARTO basemap URLs – kept as two separate layers so we can rotate the
# geometry (basemap) while keeping text labels horizontal.
# Both layers are freely usable without an API key (CARTO free tier).
OSM_TILE_URL        = "https://a.basemaps.cartocdn.com/rastertiles/voyager_nolabels/{z}/{x}/{y}.png"
OSM_TILE_URL_LABELS = "https://a.basemaps.cartocdn.com/rastertiles/voyager_only_labels/{z}/{x}/{y}.png"
OSM_TILE_ZOOM      = 12          # zoom level for leg tiles (good detail/speed balance)
TILE_CACHE_DIR     = _os.path.join(_os.path.expanduser("~"), ".vfr_tile_cache")
DEM_CACHE_DIR      = _os.path.join(TILE_CACHE_DIR, "dem")   # SRTM3 HGT tiles
SAT_CACHE_DIR      = _os.path.join(TILE_CACHE_DIR, "sat")   # ESRI World Imagery tiles

# ESRI World Imagery – free satellite basemap, no API key required.
# Tile order: z / y / x  (note: NOT the usual z/x/y)
ESRI_SAT_URL = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
                "World_Imagery/MapServer/tile/{z}/{y}/{x}")
TILE_DISPLAY_NM    = 10.0        # geographic coverage per tile side (NM)
TILE_DISPLAY_PX    = 560         # rendered pixels per tile side
TILE_CHKPT_X_FRAC  = 0.65        # checkpoint horiz pos (from left); track displaced right
TILE_CHKPT_Y_FRAC  = 0.55        # checkpoint vert pos (from top); near midpoint, look ahead

# Overpass endpoint state (simple in-memory rate-limiter / cooldown)
OVERPASS_ENDPOINT_COOLDOWN_SEC = 60   # seconds to cool an endpoint after failure
OVERPASS_STATE = {}  # maps endpoint -> last_failed_timestamp

_DEM_TILE_CACHE: dict = {}  # tile_name -> (array.array of int16, n) — in-memory tile cache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def nm_to_deg_lat(nm: float) -> float:
    """Convert nautical miles to approximate degrees of latitude."""
    return nm / NM_PER_DEGREE


def bearing_to_destination(lat1: float, lon1: float,
                            lat2: float, lon2: float) -> float:
    """
    Compute the initial true course (bearing) in degrees [0, 360) from
    (lat1, lon1) to (lat2, lon2) using the spherical law of sines.
    All angles in decimal degrees.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)

    y = math.sin(dlambda) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda))
    bearing = math.degrees(math.atan2(y, x))
    return (bearing + 360) % 360


def intermediate_point(lat1: float, lon1: float,
                        lat2: float, lon2: float,
                        fraction: float) -> tuple[float, float]:
    """
    Return the lat/lon of the point a given fraction (0..1) along the
    great-circle path from (lat1,lon1) to (lat2,lon2).
    """
    phi1    = math.radians(lat1)
    lam1    = math.radians(lon1)
    phi2    = math.radians(lat2)
    lam2    = math.radians(lon2)

    d = 2 * math.asin(math.sqrt(
        math.sin((phi2 - phi1) / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin((lam2 - lam1) / 2) ** 2
    ))

    if d < 1e-10:
        return lat1, lon1

    A = math.sin((1 - fraction) * d) / math.sin(d)
    B = math.sin(fraction * d) / math.sin(d)

    x = A * math.cos(phi1) * math.cos(lam1) + B * math.cos(phi2) * math.cos(lam2)
    y = A * math.cos(phi1) * math.sin(lam1) + B * math.cos(phi2) * math.sin(lam2)
    z = A * math.sin(phi1) + B * math.sin(phi2)

    lat = math.degrees(math.atan2(z, math.sqrt(x * x + y * y)))
    lon = math.degrees(math.atan2(y, x))
    return lat, lon


def gc_distance_nm(lat1: float, lon1: float,
                   lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    return great_circle((lat1, lon1), (lat2, lon2)).nm


# ---------------------------------------------------------------------------
# OurAirports data download & cache
# ---------------------------------------------------------------------------

_airports_cache: Optional[list[dict]] = None
_freqs_cache:    Optional[list[dict]] = None
_runways_cache:  Optional[list[dict]] = None


def _fetch_csv(url: str) -> list[dict]:
    """Download a CSV from *url* and return a list of row dicts."""
    print(f"  Fetching {url} …", flush=True)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    return list(reader)


def get_airports() -> list[dict]:
    """Return (cached) OurAirports airports list."""
    global _airports_cache
    if _airports_cache is None:
        _airports_cache = _fetch_csv(OURAIRPORTS_AIRPORTS_URL)
    return _airports_cache


def get_frequencies() -> list[dict]:
    """Return (cached) OurAirports frequencies list."""
    global _freqs_cache
    if _freqs_cache is None:
        _freqs_cache = _fetch_csv(OURAIRPORTS_FREQS_URL)
    return _freqs_cache


def get_runways_data() -> list[dict]:
    """Return (cached) OurAirports runways list."""
    global _runways_cache
    if _runways_cache is None:
        _runways_cache = _fetch_csv(OURAIRPORTS_RUNWAYS_URL)
    return _runways_cache


def get_airport_runways(airport_id: str, icao: str) -> list[str]:
    """
    Devuelve una lista de identificadores de pista para el aeropuerto dado,
    en formato 'XX/YY' (extremo LE / extremo HE).
    """
    rwy_set: list[str] = []
    seen: set[str] = set()
    for row in get_runways_data():
        if row.get("airport_ident", "").upper() == icao.upper() or \
           row.get("airport_ref", "") == airport_id:
            le = row.get("le_ident", "").strip()
            he = row.get("he_ident", "").strip()
            key = le + "/" + he
            if key not in seen and (le or he):
                seen.add(key)
                rwy_set.append(key)
    return rwy_set


def runways_str(airport_id: str, icao: str) -> str:
    """Cadena compacta de pistas, p.ej. '09/27 · 03/21'.  Devuelve '' si no hay datos."""
    rwys = get_airport_runways(airport_id, icao)
    return " · ".join(rwys) if rwys else ""


def best_departure_runway_mag(airport_id: str, icao: str,
                               wind_from_deg_true: float,
                               mag_var: float = 0.0) -> Optional[float]:
    """
    Return the magnetic heading of the runway end with the most headwind
    (i.e. whose true heading is closest to the wind direction).
    Returns None if no runway data with headings is available.
    """
    best_hdg_true: Optional[float] = None
    best_hw: float = -999.0
    best_is_mag: bool = False
    for row in get_runways_data():
        if row.get("airport_ident", "").upper() != icao.upper() and \
           row.get("airport_ref", "") != airport_id:
            continue
        for hdg_key, ident_key in (("le_heading_degT", "le_ident"),
                                    ("he_heading_degT", "he_ident")):
            raw = row.get(hdg_key, "").strip()
            if raw:
                try:
                    hdg_true = float(raw)
                except ValueError:
                    continue
                is_mag = False  # OurAirports stores true headings
            else:
                # Fallback: derive heading from runway designator (e.g. "11" → 110°M)
                ident = row.get(ident_key, "").strip()
                num_str = "".join(c for c in ident if c.isdigit())
                if not num_str:
                    continue
                num = int(num_str)
                if not 1 <= num <= 36:
                    continue
                hdg_true = num * 10.0
                is_mag = True  # designator is already magnetic
            # Headwind component: positive = into wind (preferred)
            # For true headings adjust by mag_var; for magnetic headings compare directly
            wind_from = wind_from_deg_true if not is_mag else (wind_from_deg_true - mag_var + 360) % 360
            hw = math.cos(math.radians(wind_from - hdg_true))
            if hw > best_hw:
                best_hw = hw
                best_hdg_true = hdg_true
                best_is_mag = is_mag
    if best_hdg_true is None:
        return None
    if best_is_mag:
        return best_hdg_true  # already magnetic
    return (best_hdg_true - mag_var + 360) % 360


def lookup_airport(icao: str) -> dict:
    """
    Find an airport by ICAO code in the OurAirports dataset.
    Returns a dict with keys: name, icao, lat, lon, elevation_ft.
    Raises ValueError when not found.
    """
    icao = icao.upper().strip()
    for row in get_airports():
        if row.get("gps_code", "").upper() == icao or \
           row.get("ident", "").upper() == icao:
            try:
                elev = float(row.get("elevation_ft") or 0)
            except ValueError:
                elev = 0.0
            apt_id = row.get("id", "")
            return {
                "name": row.get("name", icao),
                "icao": icao,
                "lat":  float(row["latitude_deg"]),
                "lon":  float(row["longitude_deg"]),
                "elevation_ft": elev,
                "id":   apt_id,
                "runways": runways_str(apt_id, icao),
            }
    raise ValueError(f"Airport '{icao}' not found in OurAirports dataset.")


def get_airport_frequencies(airport_id: str, icao: str) -> list[dict]:
    """
    Return a list of frequency dicts {type, freq_mhz} for the given
    airport (matched by the integer OurAirports airport_id or ICAO).
    """
    results = []
    for row in get_frequencies():
        if row.get("airport_ident", "").upper() == icao.upper() or \
           row.get("airport_ref", "") == airport_id:
            results.append({
                "type":     row.get("type", ""),
                "desc":     row.get("description", ""),
                "freq_mhz": row.get("frequency_mhz", ""),
            })
    return results


def best_freq(freqs: list[dict], preferred: tuple[str, ...]) -> str:
    """
    Among a list of frequency dicts, return the first whose 'type'
    contains any of the *preferred* keywords (case-insensitive).
    Falls back to the first entry or '—'.
    """
    for kw in preferred:
        for f in freqs:
            if kw.lower() in f["type"].lower() or kw.lower() in f["desc"].lower():
                return f"{f['freq_mhz']} ({f['type']})"
    if freqs:
        return f"{freqs[0]['freq_mhz']} ({freqs[0]['type']})"
    return ""


def closest_airport(lat: float, lon: float,
                    exclude_icaos: tuple[str, ...] = (),
                    cruise_speed_kts: float = 0.0) -> dict:
    """
    Encuentra el aeropuerto alternativo mas adecuado cerca de (lat, lon).

    Estrategia: recoge los aeropuertos GA mas cercanos (maximo 120 NM),
    luego da prioridad al mas cercano QUE TENGA datos de frecuencia en
    OurAirports, dentro de 2× la distancia al aeropuerto geometricamente
    mas cercano.  Asi se evita mostrar pistas sin comms (p.ej. campos
    privados sin frecuencia registrada) cuando existe una alternativa con
    comunicaciones a poca distancia adicional.
    Si ningun candidato tiene frecuencia, devuelve el mas cercano.
    """
    MAX_SEARCH_NM = 120.0

    candidates = []  # (dist, row, alat, alon)
    for row in get_airports():
        apt_type = row.get("type", "")
        # Only consider fields usable by a normal GA fixed-wing airplane.
        # Exclude heliports (rotor-only) and seaplane bases (water-only).
        if apt_type not in ("small_airport", "medium_airport", "large_airport"):
            continue
        icao = (row.get("gps_code") or row.get("ident") or "").upper()
        if icao in (e.upper() for e in exclude_icaos):
            continue
        try:
            alat = float(row["latitude_deg"])
            alon = float(row["longitude_deg"])
        except (ValueError, KeyError):
            continue
        dist = gc_distance_nm(lat, lon, alat, alon)
        if dist <= MAX_SEARCH_NM:
            candidates.append((dist, row, alat, alon))

    if not candidates:
        return {"name": "—", "icao": "—", "distance_nm": 0, "freq": "",
                "bearing_true": 0.0, "time_min": 0.0, "runways": ""}

    candidates.sort(key=lambda x: x[0])
    closest_dist = candidates[0][0]

    def _build_result(dist, row, alat, alon):
        icao   = (row.get("gps_code") or row.get("ident") or "UNKN").upper()
        apt_id = row.get("id", "")
        freqs  = get_airport_frequencies(apt_id, icao)
        freq_str = best_freq(freqs, ("TWR", "CTAF", "UNICOM", "ATF", "AFIS", "RDO"))
        if not freq_str:
            freq_str = best_freq(freqs, ())   # absolute fallback: any freq
        brg = bearing_to_destination(lat, lon, alat, alon)
        time_min = (dist / cruise_speed_kts * 60.0) if cruise_speed_kts > 0 else 0.0
        try:
            elev = float(row.get("elevation_ft") or 0.0)
        except (ValueError, TypeError):
            elev = 0.0
        return {
            "name":         row.get("name", icao),
            "icao":         icao,
            "distance_nm":  round(dist, 1),
            "freq":         freq_str,
            "freqs":        freqs,
            "bearing_true": round(brg, 0),
            "time_min":     round(time_min, 0),
            "runways":      runways_str(apt_id, icao),
            "elevation_ft": round(elev),
        }

    # Prefer the closest airport that has frequency data (within MAX_SEARCH_NM).
    # For VFR planning, an airport with comms is always more useful than a
    # closer private strip with no radio frequency registered.
    for dist, row, alat, alon in candidates:
        icao   = (row.get("gps_code") or row.get("ident") or "").upper()
        apt_id = row.get("id", "")
        if get_airport_frequencies(apt_id, icao):
            return _build_result(dist, row, alat, alon)

    # Fallback: no airport with freq data found — use geometrically closest
    dist, row, alat, alon = candidates[0]
    return _build_result(dist, row, alat, alon)


# ---------------------------------------------------------------------------
# Terrain elevation — local SRTM1 HGT tiles (auto-downloaded on first use)
# ---------------------------------------------------------------------------
# SRTM1 tiles: 1°×1°, 3601×3601 big-endian int16 grid, ~25 MB raw / ~10 MB gzip.
# Tiles are cached to DEM_CACHE_DIR and read directly without any API call.
# Source: AWS Open Data elevation-tiles-prod (free, no registration, no API key).
# Falls back to Open-Topo-Data API only if tile download fails.

# AWS elevation-tiles-prod: https://s3.amazonaws.com/elevation-tiles-prod/skadi/
# Tiles are SRTM1 (1 arcsec ≈ 30m) stored as individual gzip-compressed HGT files.
_SRTM_AWS_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{tdir}/{tile}.hgt.gz"


def _srtm3_tile_name(lat: float, lon: float) -> tuple:
    """Return (tile_name, lat_floor, lon_floor) for the 1° tile containing (lat, lon)."""
    lat0 = int(math.floor(lat))
    lon0 = int(math.floor(lon))
    ns = "N" if lat0 >= 0 else "S"
    ew = "E" if lon0 >= 0 else "W"
    return f"{ns}{abs(lat0):02d}{ew}{abs(lon0):03d}", lat0, lon0


def _load_srtm3_tile(tile_name: str):
    """Load SRTM1 tile from local cache or download from AWS Open Data.

    Returns (array.array of signed int16, n) where n=3601 (SRTM1) or 1201 (SRTM3).
    Returns None if the tile could not be loaded (e.g. ocean tile with no land data).
    """
    if tile_name in _DEM_TILE_CACHE:
        return _DEM_TILE_CACHE[tile_name]

    try:
        _os.makedirs(DEM_CACHE_DIR, exist_ok=True)
    except Exception:
        pass

    cache_path = _os.path.join(DEM_CACHE_DIR, f"{tile_name}.hgt")
    raw: bytes | None = None

    # --- Try local cache first -----------------------------------------------
    if _os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as fh:
                raw = fh.read()
        except Exception:
            raw = None

    # --- Download from AWS if not cached -------------------------------------
    if not raw:
        # Determine the tile directory component (e.g. "N42" from "N42W002")
        tdir = tile_name[:3]   # first 3 chars: N42, S05, etc.
        url = _SRTM_AWS_URL.format(tdir=tdir, tile=tile_name)
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 404:
                # Tile doesn't exist (ocean or polar): cache sentinel, don't retry
                _DEM_TILE_CACHE[tile_name] = None
                return None
            resp.raise_for_status()
            raw = _gzip.decompress(resp.content)
            # Persist to disk
            try:
                with open(cache_path, "wb") as fh:
                    fh.write(raw)
                print(f"    [dem] {tile_name}: saved {len(raw)//1024} KB → {cache_path}",
                      file=sys.stderr)
            except OSError as e:
                print(f"    [dem] {tile_name}: download OK but could not save ({e})",
                      file=sys.stderr)
        except Exception as exc:
            print(f"    [dem] {tile_name}: download failed ({exc})", file=sys.stderr)
            _DEM_TILE_CACHE[tile_name] = None
            return None

    if not raw:
        _DEM_TILE_CACHE[tile_name] = None
        return None

    # --- Parse HGT binary (big-endian int16) ---------------------------------
    # SRTM1 = 3601×3601 = 25,934,402 bytes;  SRTM3 = 1201×1201 = 2,884,802 bytes
    n = 3601 if len(raw) >= 25_000_000 else 1201
    arr = _array_mod.array("h", raw)   # signed short, native endian after byteswap
    arr.byteswap()                      # big-endian → native
    result = (arr, n)
    _DEM_TILE_CACHE[tile_name] = result
    return result


def _sample_srtm3(arr, n: int, lat: float, lon: float, lat0: int, lon0: int) -> float:
    """Bilinear interpolation from an SRTM tile. Returns elevation in metres.

    SRTM row 0 = northernmost (lat0+1), row n-1 = southernmost (lat0).
    Col 0 = westernmost (lon0), col n-1 = easternmost (lon0+1).
    No-data sentinel = -32768 → treated as 0 m.
    """
    row_f = (lat0 + 1.0 - lat) * (n - 1)
    col_f = (lon - lon0) * (n - 1)
    r = max(0, min(int(row_f), n - 2))
    c = max(0, min(int(col_f), n - 2))
    dr = row_f - r
    dc = col_f - c

    def _v(row: int, col: int) -> float:
        val = arr[row * n + col]
        return max(0.0, float(val)) if val != -32768 else 0.0

    return (_v(r, c) * (1 - dr) * (1 - dc)
            + _v(r, c + 1) * (1 - dr) * dc
            + _v(r + 1, c) * dr * (1 - dc)
            + _v(r + 1, c + 1) * dr * dc)


def _get_elevations_opentopodata(locations: list[dict]) -> list[float]:
    """Open-Topo-Data SRTM30m API — used only as fallback when SRTM tile is unavailable."""
    loc_str = "|".join(f"{loc['latitude']},{loc['longitude']}" for loc in locations)
    resp = requests.post(OPEN_TOPO_DATA_URL, json={"locations": loc_str}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "OK":
        raise ValueError(f"Open-Topo-Data status: {data.get('status')}")
    results = data["results"]
    if len(results) != len(locations):
        raise ValueError(f"Open-Topo-Data returned {len(results)} for {len(locations)} points")
    return [r["elevation"] or 0.0 for r in results]


def get_elevations_m(points: list[tuple[float, float]]) -> list[float]:
    """Return terrain elevation in metres for each (lat, lon) in *points*.

    Tries local SRTM3 HGT tiles first (auto-downloaded from USGS on first use,
    then served entirely from ~DEM_CACHE_DIR without any API call).
    Falls back to Open-Topo-Data API only for points whose tile could not be loaded.
    """
    if not points:
        return []

    results: list[float] = [0.0] * len(points)
    api_fallback_indices: list[int] = []

    for i, (lat, lon) in enumerate(points):
        tile_name, lat0, lon0 = _srtm3_tile_name(lat, lon)
        tile = _load_srtm3_tile(tile_name)
        if tile is not None:
            arr, n = tile
            results[i] = _sample_srtm3(arr, n, lat, lon, lat0, lon0)
        else:
            api_fallback_indices.append(i)

    if api_fallback_indices:
        fallback_pts = [points[i] for i in api_fallback_indices]
        locations = [{"latitude": lat, "longitude": lon} for lat, lon in fallback_pts]
        try:
            api_elev = _get_elevations_opentopodata(locations)
            for j, idx in enumerate(api_fallback_indices):
                results[idx] = api_elev[j]
        except Exception as exc:
            print(f"    [warn] SRTM tile missing and API fallback failed: {exc}",
                  file=sys.stderr)

    return results


def max_terrain_elevation_ft(lat1: float, lon1: float,
                              lat2: float, lon2: float,
                              samples: int = 5) -> float:
    """
    Sample terrain elevation along the great-circle segment from
    (lat1,lon1) to (lat2,lon2) and return the maximum in feet.
    """
    pts = [
        intermediate_point(lat1, lon1, lat2, lon2, i / (samples - 1))
        for i in range(samples)
    ]
    elev_m = get_elevations_m(pts)
    return max(elev_m) * FEET_PER_METER


# ---------------------------------------------------------------------------
# Reverse geocoding (Nominatim)
def offset_point(lat: float, lon: float,
                 bearing_deg: float, dist_nm: float) -> tuple[float, float]:
    """
    Devuelve la posicion (lat, lon) que se encuentra a 'dist_nm' NM
    desde (lat, lon) en la direccion 'bearing_deg' (grados verdaderos).
    Usa la formula de punto de destino en esfera.
    """
    R = 180.0 * 60.0  # radio de la Tierra en NM  (aprox 10800 NM)
    d = dist_nm / R   # distancia en radianes
    b = math.radians(bearing_deg)
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)

    phi2 = math.asin(
        math.sin(phi1) * math.cos(d)
        + math.cos(phi1) * math.sin(d) * math.cos(b)
    )
    lam2 = lam1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(phi1),
        math.cos(d) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), math.degrees(lam2)


# ---------------------------------------------------------------------------

def get_landmark_with_coords(lat: float, lon: float, zoom: int = 10) -> tuple:
    """
    Nominatim reverse geocoding. Returns (name, nom_lat, nom_lon) where
    nom_lat/nom_lon are the coordinates of the matched OSM object (the actual
    place centre), NOT the query point.
    Includes a polite 1-second delay to respect Nominatim usage policy.
    """
    time.sleep(1)  # Nominatim rate-limit: max 1 req/s
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={
                    "lat": lat,
                    "lon": lon,
                    "format": "json",
                    "zoom": zoom,
                    "addressdetails": 0,
                },
            headers={"User-Agent": "VFROnePager/1.0 (educational use)"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        display = data.get("display_name", "")
        parts = [p.strip() for p in display.split(",")]
        short = ", ".join(parts[:2]) if len(parts) >= 2 else display
        nom_lat = float(data.get("lat", lat))
        nom_lon = float(data.get("lon", lon))
        return short[:40], nom_lat, nom_lon
    except Exception as exc:
        print(f"    [warn] Nominatim query failed: {exc}", file=sys.stderr)
        return f"{lat:.3f}°N {lon:.3f}°E", lat, lon


def get_landmark(lat: float, lon: float, zoom: int = 10) -> str:
    """Convenience wrapper — returns only the name string."""
    name, _, _ = get_landmark_with_coords(lat, lon, zoom)
    return name


def _query_overpass(lat: float, lon: float, radius_m: int = 8000) -> list[dict]:
    """
    Query Overpass API for POIs around (lat, lon) within radius_m meters.
    Returns a list of candidate dicts with keys: name, lat, lon, tags.
    """
    # Try a list of known Overpass instances (rotate on failure).
    OVERPASS_ENDPOINTS = [
        "https://z.overpass-api.de/api/interpreter",
        "https://lz4.overpass-api.de/api/interpreter",
        "https://overpass-api.de/api/interpreter",
    ]

    # Node-only query: peaks/viewpoints/places/historic/water are stored as OSM nodes;
    # node queries are fast and avoid expensive way/rel area scans.
    def _cln(tag: str) -> str:
        return f'node(around:{{r}},{{lat}},{{lon}}){tag}["name"];'

    tag_clauses = "".join([
        _cln('["natural"="peak"]'),
        _cln('["tourism"="viewpoint"]'),
        _cln('["historic"]'),
        # place nodes: exactly the coord CARTO uses for settlement label rendering
        _cln('["place"~"^(city|town|village|hamlet)$"]'),
        _cln('["natural"="water"]'),
        _cln('["waterway"="river"]'),
    ])

    q = (
        "[out:json][timeout:30];(" + tag_clauses.format(r=radius_m, lat=lat, lon=lon) + ");out;"
    )

    # Simple disk cache to reuse previous successful queries when endpoints are flaky.
    cache_dir = _os.path.join(TILE_CACHE_DIR, "overpass_cache")
    try:
        _os.makedirs(cache_dir, exist_ok=True)
    except Exception:
        cache_dir = None

    cache_key = f"ov_{lat:.5f}_{lon:.5f}_{radius_m}"
    cache_file = _os.path.join(cache_dir, cache_key + ".json") if cache_dir else None

    # Helper to parse resp json into results list
    def _parse_overpass_json(data_json):
        elems = data_json.get("elements", [])
        results = []
        for el in elems:
            tags = el.get("tags") or {}
            name = tags.get("name") or tags.get("ref")
            if not name:
                continue
            if el.get("type") == "node":
                lat_e = el.get("lat")
                lon_e = el.get("lon")
            else:
                ctr = el.get("center") or el.get("bounds")
                if isinstance(ctr, dict) and "lat" in ctr:
                    lat_e = ctr.get("lat")
                    lon_e = ctr.get("lon")
                else:
                    continue
            try:
                results.append({"name": name, "lat": float(lat_e), "lon": float(lon_e), "tags": tags})
            except Exception:
                continue
        return results

    # Check disk cache FIRST — avoid hitting the network when data is already on disk.
    if cache_file and _os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf8") as fh:
                data = json.load(fh)
            results = _parse_overpass_json(data)
            if results:
                return results
        except Exception:
            pass  # corrupt cache — fall through to live query

    # Try endpoints in order; one attempt each, fast-fail on HTTP errors.
    for endpoint in OVERPASS_ENDPOINTS:
        # Skip endpoints still in cooldown after recent failures
        last_fail = OVERPASS_STATE.get(endpoint)
        if last_fail is not None and (time.time() - last_fail) < OVERPASS_ENDPOINT_COOLDOWN_SEC:
            continue

        try:
            time.sleep(0.5)   # brief pause to avoid Overpass 504 rate-limiting
            resp = requests.post(endpoint, data=q, timeout=30)
            if resp.status_code in (429, 503, 504):
                # Rate-limited or server overloaded — cool this endpoint, try next immediately
                OVERPASS_STATE[endpoint] = time.time()
                continue
            resp.raise_for_status()
            data = resp.json()
            results = _parse_overpass_json(data)
            # On success, clear any recorded failure and cache the response
            OVERPASS_STATE.pop(endpoint, None)
            if cache_file and results:
                try:
                    with open(cache_file, "w", encoding="utf8") as fh:
                        json.dump(data, fh)
                except Exception:
                    pass
            return results
        except Exception:
            # Connection/timeout error — mark cooldown and try next endpoint
            OVERPASS_STATE[endpoint] = time.time()
            continue

    # All endpoints failed (cache was already checked above).

    # Nothing available; caller should fall back (e.g. to Nominatim via get_landmark)
    print("    [warn] All Overpass endpoints failed and no cache available.", file=sys.stderr)
    return []


def find_best_landmark(lat: float, lon: float, zoom: int = 12, radius_nm: float = 8.0,
                       track_deg: Optional[float] = None,
                       ac_lat: Optional[float] = None,
                       ac_lon: Optional[float] = None) -> tuple:
    """
    Find a prominent nearby landmark preferring visible peaks/lakes/POIs via Overpass.

    Returns (label: str, poi_lat: float, poi_lon: float).
    label is truncated to 40 chars.  On failure falls back to Nominatim reverse-geocode,
    using the search-centre (lat, lon) as the coordinate.

    If track_deg + ac_lat/ac_lon are supplied, candidates are filtered to those
    strictly LEFT of the track (relative bearing 180°–360° from the aircraft).
    Within that half-plane, ahead-left (270°–360°) candidates receive a +20 score
    bonus over behind-left ones — so we prefer landmarks the pilot can see ahead.
    When no candidate sits in the left half-plane, the full candidate list is used
    (better to show something than nothing).
    """
    # Iteratively expand search radius if no suitable left-side candidate is found.
    max_radius_nm = 20.0
    search_radii_nm = []
    r = float(radius_nm)
    while r <= max_radius_nm:
        search_radii_nm.append(r)
        r *= 2.0
    # ensure the original radius is first and cap list
    search_radii_nm = sorted(set(search_radii_nm), key=lambda x: x)

    # Aggregate candidates across increasing radii (prefer left-side matches even at larger range)
    candidates_all = []
    for r_nm in search_radii_nm:
        radius_m = int(r_nm * 1852)
        try:
            found = _query_overpass(lat, lon, radius_m=radius_m)
        except Exception as _ov_exc:
            found = []
        if found:
            candidates_all.extend(found)
            break   # stop expanding once we have results at this radius
    candidates = candidates_all
    if not candidates:
        # no POIs found in any radius — fall back to reverse-geocode at the
        # search centre (lat, lon is the 1-NM-left offset point from the caller).
        # Use get_landmark_with_coords so we get the actual OSM object position.
        name, nom_lat, nom_lon = get_landmark_with_coords(lat, lon, zoom=zoom)
        return name, nom_lat, nom_lon, "poi", []

    # Priority ordering for tag types
    def score_candidate(c):
        t = c.get("tags", {})
        # Towns/cities are the most useful VFR references — identifiable on any chart.
        # Peaks/water are secondary (good landmarks but harder to name from the air).
        place = t.get("place", "")
        if place == "city":
            base = 100
        elif place == "town":
            base = 95
        elif place == "village":
            base = 90
        elif place == "hamlet":
            base = 80
        elif t.get("natural") == "peak":
            base = 70
        elif t.get("water") == "lake" or t.get("natural") == "water":
            base = 65
        elif t.get("tourism") == "viewpoint":
            base = 55
        elif "historic" in t:
            base = 50
        elif t.get("man_made") == "water_tower":
            base = 40
        elif t.get("amenity") == "place_of_worship":
            base = 30
        else:
            base = 10
        # Bonus: closest to 270° (directly left of track), falls off toward edges.
        # A large bonus (up to +60) lets a nearby left-side historic/ruin beat a
        # right-side town whose base score is higher but whose direction is poor.
        if track_deg is not None and ac_lat is not None and ac_lon is not None:
            try:
                rb = _rel_bearing(c)
                # Angular distance from direct-left (270°): 0=direct left, 90=ahead/behind
                left_angle = abs(((rb - 270 + 180) % 360) - 180)
                if left_angle < 90:
                    base += max(0, 60 - int(left_angle * 2 / 3))
            except Exception:
                pass
        return base

    # Use ONLY left-window candidates: relative bearing 225°–315° (port side).
    # Falls back to broader left half (180°–360°), then to a Nominatim lookup
    # at a point 1 NM to the left of the aircraft.
    if track_deg is not None and ac_lat is not None and ac_lon is not None:
        try:
            left_window = [c for c in candidates
                           if 225 <= _rel_bearing(c) <= 315]
        except Exception:
            left_window = []
        if left_window:
            candidates = left_window
        else:
            # Broaden to full left half-plane (anything to port)
            try:
                left_half = [c for c in candidates if _rel_bearing(c) >= 180]
            except Exception:
                left_half = []
            if left_half:
                candidates = left_half
            else:
                # Nothing to port — fall back to Nominatim at 1 NM left offset
                _lb = (track_deg - 90 + 360) % 360
                fb_lat, fb_lon = offset_point(
                    ac_lat if ac_lat is not None else lat,
                    ac_lon if ac_lon is not None else lon,
                    _lb, 1.0)
                name, nom_lat, nom_lon = get_landmark_with_coords(fb_lat, fb_lon, zoom=zoom)
                return name, nom_lat, nom_lon, "poi", []

    # Collect extra settlement candidates for tile labels — LEFT side only.
    # Using the same 170°–360° half-plane filter so right-of-track towns like
    # Arróniz never appear as orientation labels on the tile.
    PLACE_TYPES = {"city", "town", "village", "hamlet"}
    if track_deg is not None and ac_lat is not None and ac_lon is not None:
        _ep_list = []
        for _c in candidates_all:
            if _c.get("tags", {}).get("place", "") not in PLACE_TYPES:
                continue
            try:
                if _rel_bearing(_c) >= 170:
                    _ep_list.append(_c)
            except Exception:
                pass
        extra_places = _ep_list
    else:
        extra_places = [
            c for c in candidates_all
            if c.get("tags", {}).get("place", "") in PLACE_TYPES
        ]

    # Evaluate visibility: prefer candidates that are not clearly occluded by terrain.
    # Settlements (towns/villages) are ALWAYS visible VFR references — skip terrain
    # check for them so a valley town is never beaten by a peak solely on sight-line.
    visible = []
    search_lat = ac_lat if ac_lat is not None else lat
    search_lon = ac_lon if ac_lon is not None else lon
    for c in candidates:
        try:
            tags = c.get("tags", {})
            # Settlements: always considered visible (you can always identify a town)
            if tags.get("place", "") in PLACE_TYPES:
                visible.append((score_candidate(c), c))
                continue
            # For peaks / viewpoints etc.: check terrain line-of-sight
            ele = tags.get("ele")
            if ele is not None:
                poi_elev_ft = float(ele)
            else:
                poi_elev_m = get_elevations_m([(c["lat"], c["lon"])])[0]
                poi_elev_ft = poi_elev_m * FEET_PER_METER
            max_terr = max_terrain_elevation_ft(search_lat, search_lon,
                                                c["lat"], c["lon"], samples=9)
            if poi_elev_ft >= max_terr - 50:
                visible.append((score_candidate(c), c))
        except Exception:
            continue

    if not visible:
        # none confidently visible — fallback to best-named candidate by score
        candidates.sort(key=lambda x: score_candidate(x), reverse=True)
        best = candidates[0]
    else:
        visible.sort(key=lambda x: x[0], reverse=True)
        best = visible[0][1]

    if not extra_places:
        extra_places = []

    label = best.get("name")
    poi_lat = best.get("lat", lat)
    poi_lon = best.get("lon", lon)
    # derive a simple poi_type from tags for iconography
    tags = best.get("tags", {})
    if tags.get("natural") == "peak":
        poi_type = "peak"
    elif tags.get("water") == "lake" or tags.get("natural") == "water":
        poi_type = "lake"
    elif tags.get("waterway") == "river":
        poi_type = "river"
    elif tags.get("tourism") == "viewpoint":
        poi_type = "viewpoint"
    elif tags.get("aeroway") in ("aerodrome", "airport"):
        poi_type = "airport"
    elif "historic" in tags:
        poi_type = "historic"
    elif tags.get("man_made") == "water_tower":
        poi_type = "water_tower"
    elif tags.get("amenity") == "place_of_worship":
        poi_type = "place_of_worship"
    else:
        place = tags.get("place", "")
        if place in ("city", "town", "village", "hamlet"):
            poi_type = "town"
        else:
            poi_type = "poi"

    return (label[:40] if label else get_landmark(lat, lon, zoom=zoom), poi_lat, poi_lon, poi_type, extra_places)


# ---------------------------------------------------------------------------
# Magnetic variation
# ---------------------------------------------------------------------------

def magnetic_variation(lat: float, lon: float,
                       altitude_m: float = 0.0) -> float:
    """
    Return the magnetic variation in degrees at (lat, lon).
    Positive  = East  (subtract from true course to get magnetic)
    Negative  = West  (add absolute value to get magnetic)
    Uses the geomag WMM library for local computation.
    """
    try:
        var = geomag.declination(lat, lon, altitude_m)
        return var
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Route segmentation
# ---------------------------------------------------------------------------

def build_legs(origin: dict, destination: dict,
               cruise_speed_ias: float,
               fuel_consumption_gph: float,
               mag_var: float = 0.0,
               waypoints: Optional[list[dict]] = None) -> list[dict]:
    """
    Divide la ruta en intervalos de LEG_MINUTES minutos, respetando los
    waypoints intermedios proporcionados.  Para cada segmento (origen →
    wp1 → wp2 → … → destino) se generan tramos independientes y el
    contador de tiempo se reinicia en cada waypoint.

    Los waypoints intermedios deben ser dicts con: name, lat, lon.
    En el primer tramo del primer segmento se aplica CLIMB_SPEED_FACTOR;
    en el resto del viaje se usa velocidad de crucero completa.
    """
    waypoints = waypoints or []
    stops: list[dict] = [origin] + waypoints + [destination]

    speed_nm_per_min    = cruise_speed_ias * KNOTS_TO_NM_PER_MIN
    fuel_per_leg_cruise = fuel_consumption_gph * (LEG_MINUTES / 60.0)
    first_leg_min       = round(LEG_MINUTES * CLIMB_SPEED_FACTOR, 1)  # e.g. 6.5 min
    fuel_per_leg_climb  = fuel_consumption_gph * (first_leg_min / 60.0)

    all_legs: list[dict] = []
    cumulative_min: float = 0.0   # running total for descent-insertion ordering
    cumulative_dist: float = 0.0  # running total distance in NM

    for seg_idx in range(len(stops) - 1):
        seg_from = stops[seg_idx]
        seg_to   = stops[seg_idx + 1]
        is_first_segment = (seg_idx == 0)

        seg_nm  = gc_distance_nm(seg_from["lat"], seg_from["lon"],
                                 seg_to["lat"],   seg_to["lon"])
        seg_min = seg_nm / speed_nm_per_min
        n_legs  = max(1, int(seg_min / LEG_MINUTES))

        # Segment track (true and magnetic) — constant for the whole segment
        seg_track_true = bearing_to_destination(seg_from["lat"], seg_from["lon"],
                               seg_to["lat"],   seg_to["lon"])
        seg_track_mag = int(round((seg_track_true - mag_var + 360) % 360))

        # ── Waypoint marker row (inserted before each segment after the first) ─
        if seg_idx > 0:
            new_tc  = bearing_to_destination(seg_from["lat"], seg_from["lon"],
                                             seg_to["lat"],   seg_to["lon"])
            new_mag = (new_tc - mag_var + 360) % 360
            wp_name = seg_from.get("name") or seg_from.get("icao") or "WPT"
            all_legs.append({
                "is_waypoint":    True,
                "waypoint_name":  wp_name,
                "new_track_true": round(new_tc, 1),
                "new_track_mag":  round(new_mag, 1),
                "segment_track_true": seg_track_true,
                "segment_track_mag": seg_track_mag,
                "leg_num":        0,
                "elapsed_min":    int(round(cumulative_min)),
                "_cumulative_min": cumulative_min,
                "_cumulative_dist": cumulative_dist,
                "cum_dist_nm":     round(cumulative_dist, 1),
                "lat":            seg_from["lat"],
                "lon":            seg_from["lon"],
                "landmark":       f"\u25ba WPT: {wp_name}  \u2192  {int(new_mag):03d}\u00b0M",
                "max_terrain_ft": 0,
                "min_alt_ft":     0,
                "fuel_burned_gal": "",
                "alt_icao":       "",
                "alt_name":       "",
                "alt_dist_nm":    0,
                "alt_freq":       "",
                "alt_bearing_mag": None,
                "alt_time_min":   None,
                "alt_runways":    "",
            })

        print(f"  Segmento {seg_idx+1}/{len(stops)-1}: "
              f"{seg_from.get('name', seg_from.get('icao','?'))} \u2192 "
              f"{seg_to.get('name', seg_to.get('icao','?'))}  "
              f"({seg_nm:.1f} NM, {n_legs} tramos) ...", flush=True)

        prev_lat = seg_from["lat"]
        prev_lon = seg_from["lon"]

        for i in range(1, n_legs + 1):
            fraction = min(i * LEG_MINUTES / seg_min, 1.0)
            lat, lon = intermediate_point(
                seg_from["lat"], seg_from["lon"],
                seg_to["lat"],   seg_to["lon"],
                fraction,
            )

            print(f"    Tramo {i}/{n_legs}: ({lat:.3f}, {lon:.3f})", flush=True)

            # Terreno
            max_terr = max_terrain_elevation_ft(prev_lat, prev_lon, lat, lon)
            min_alt  = max_terr + TERRAIN_BUFFER_FT

            # Visual reference: search centred ~1 NM to the LEFT of the
            # checkpoint (port window, 90° left of track).
            track_deg    = bearing_to_destination(prev_lat, prev_lon, lat, lon)
            _left_bearing = (track_deg - 90 + 360) % 360
            _search_lat, _search_lon = offset_point(lat, lon, _left_bearing, 1.0)
            # find_best_landmark returns (label, poi_lat, poi_lon, poi_type, extra_places).
            # The poi_lat/lon are actual OSM node coordinates, NOT the search centre.
            _lm_places = []
            try:
                landmark, _lm_lat_store, _lm_lon_store, _lm_type, _lm_places = find_best_landmark(
                    _search_lat, _search_lon, zoom=12, radius_nm=4.0,
                    track_deg=track_deg, ac_lat=lat, ac_lon=lon)
            except Exception:
                _fb_lat, _fb_lon = offset_point(lat, lon, _left_bearing, 1.0)
                landmark, _lm_lat_store, _lm_lon_store = get_landmark_with_coords(_fb_lat, _fb_lon, zoom=12)
                _lm_type = "poi"

            # Alternativa mas cercana (excluyendo origen y destino)
            alt = closest_airport(lat, lon,
                                   exclude_icaos=(origin["icao"], destination["icao"]),
                                   cruise_speed_kts=cruise_speed_ias)
            alt_brg_mag = (alt["bearing_true"] - mag_var + 360) % 360

            # Tiempo desde el ultimo waypoint/origen (se reinicia en cada segmento).
            # Solo el primer tramo de la salida lleva factor de ascenso.
            if is_first_segment and i == 1:
                elapsed        = first_leg_min
                fuel_this_leg  = fuel_per_leg_climb
                leg_duration   = first_leg_min
            else:
                elapsed        = i * LEG_MINUTES
                fuel_this_leg  = fuel_per_leg_cruise
                leg_duration   = LEG_MINUTES

            # Distancia del tramo desde el punto previo al punto actual
            leg_dist = gc_distance_nm(prev_lat, prev_lon, lat, lon)
            cumulative_dist += leg_dist

            cumulative_min += leg_duration

            all_legs.append({
                "is_waypoint":      False,
                "leg_num":          i,
                "elapsed_min":      elapsed,
                "_cumulative_min":  cumulative_min,
                "segment_track_true": seg_track_true,
                "segment_track_mag":  seg_track_mag,
                "_cumulative_dist": cumulative_dist,
                "cum_dist_nm":      round(cumulative_dist, 1),
                "lat":              lat,
                "lon":              lon,
                "landmark":         landmark,
                "max_terrain_ft":   round(max_terr),
                "min_alt_ft":       round(min_alt / 100) * 100,
                "fuel_burned_gal":  round(fuel_this_leg, 1),
                "alt_icao":         alt["icao"],
                "alt_name":         alt["name"],
                "alt_dist_nm":      alt["distance_nm"],
                "alt_freq":         alt["freq"],
                "alt_elevation_ft": alt.get("elevation_ft", 0),
                "alt_bearing_mag":  round(alt_brg_mag),
                "alt_time_min":     int(alt["time_min"]),
                "alt_runways":      alt["runways"],
                "lm_lat":           _lm_lat_store,
                "lm_lon":           _lm_lon_store,
                "lm_type":          _lm_type,
                "lm_places":        _lm_places,
            })

            prev_lat, prev_lon = lat, lon

    return all_legs


def recommended_cruise_altitude(legs: list[dict]) -> int:
    """
    Calcula la altitud de crucero recomendada para el viaje.
    Sube al siguiente escalon de 500 ft garantizando:
      - Al menos 300 ft sobre el terreno mas alto del viaje
      - Nunca por debajo del alt. minima de ningun tramo (terreno + buffer 500 ft)
    Ejemplos: terreno max 3200 ft, min_alt tramo 3700 ft
              -> baseline = max(3200+300, 3700) = 3700
              -> ceil(3700/500)*500 = 4000 ft
    """
    real_legs = [l for l in legs if not l.get("is_waypoint")]
    if not real_legs:
        return 1000
    max_terrain = max(leg["max_terrain_ft"] for leg in real_legs)
    max_leg_min = max(leg["min_alt_ft"] for leg in real_legs)
    min_safe = max(max_terrain + TERRAIN_BUFFER_FT, max_leg_min)
    return int(math.ceil(min_safe / 500) * 500)


def compute_descent_leg(legs: list[dict],
                        route_stops: list[dict],
                        cruise_speed_ias: float,
                        fuel_consumption_gph: float,
                        total_ete_min: float,
                        cruise_altitude_ft: Optional[int] = None) -> Optional[dict]:
    """
    Calcula el tramo de inicio de descenso siguiendo la ruta de multiples
    segmentos definida por route_stops (lista de dicts con lat/lon, el ultimo
    siendo el destino).
      - Altitud crucero recomendada: máxima Alt.Mín de los tramos
      - Altitud objetivo sobre destino: elevación + 1000 ft AGL
      - Velocidad de descenso: 500 ft/min
      - Margen final: 2 minutos antes del destino
    Devuelve un dict con los mismos campos que los tramos normales
    más 'is_descent': True.
    """
    if not legs or len(route_stops) < 2:
        return None

    destination = route_stops[-1]
    real_legs = [l for l in legs if not l.get("is_waypoint")]

    if cruise_altitude_ft is not None:
        cruise_alt_ft = cruise_altitude_ft
    else:
        cruise_alt_ft = max(leg["min_alt_ft"] for leg in real_legs) if real_legs else 1000
    target_alt_ft = destination["elevation_ft"] + 1000   # 1000 ft AGL sobre destino
    alt_to_lose   = max(0.0, cruise_alt_ft - target_alt_ft)
    time_desc_min = alt_to_lose / 500.0                  # a 500 ft/min
    lead_time_min = time_desc_min + 2.0                  # + 2 min de margen antes del campo

    descent_start_min = total_ete_min - lead_time_min
    if descent_start_min <= 0:
        descent_start_min = total_ete_min * 0.5

    # Calcular ETE acumulado por segmento para encontrar el punto de inicio de descenso
    speed_nm_per_min = cruise_speed_ias * KNOTS_TO_NM_PER_MIN
    stop_etimes = [0.0]
    for k in range(1, len(route_stops)):
        seg_nm = gc_distance_nm(route_stops[k-1]["lat"], route_stops[k-1]["lon"],
                                route_stops[k]["lat"],   route_stops[k]["lon"])
        stop_etimes.append(stop_etimes[-1] + seg_nm / speed_nm_per_min)

    # Encontrar el segmento que contiene descent_start_min
    lat, lon = route_stops[-1]["lat"], route_stops[-1]["lon"]
    for k in range(1, len(route_stops)):
        if stop_etimes[k] >= descent_start_min:
            seg_from    = route_stops[k - 1]
            seg_to      = route_stops[k]
            seg_start   = stop_etimes[k - 1]
            seg_dur     = stop_etimes[k] - seg_start
            if seg_dur > 0:
                seg_frac = min((descent_start_min - seg_start) / seg_dur, 0.99)
            else:
                seg_frac = 0.99
            lat, lon = intermediate_point(
                seg_from["lat"], seg_from["lon"],
                seg_to["lat"],   seg_to["lon"],
                seg_frac,
            )
            break

    cumulative_fuel = round(fuel_consumption_gph * (descent_start_min / 60.0), 1)

    return {
        "is_waypoint":     False,
        "leg_num":         0,
        "elapsed_min":     round(descent_start_min),
        "_cumulative_min": descent_start_min,
        "lat":             lat,
        "lon":             lon,
        "landmark":        "\u25bc INICIO DESCENSO",
        "max_terrain_ft":  0,
        "min_alt_ft":      int(cruise_alt_ft),
        "fuel_burned_gal": cumulative_fuel,
        "alt_icao":        destination["icao"],
        "alt_name":        destination["name"],
        "alt_dist_nm":     0,
        "alt_freq":        "",
        "is_descent":      True,
    }


# ---------------------------------------------------------------------------
# Wind-effect calculation
# ---------------------------------------------------------------------------

def compute_wind_effect(tc_true: float, tas_kts: float,
                        wind_from_deg: float, wind_speed_kts: float,
                        total_nm: float, ete_no_wind_min: float) -> dict:
    """
    Calculate the effect of a steady wind on the route.

    Parameters
    ----------
    tc_true        : True course of the route (degrees)
    tas_kts        : True airspeed (knots)
    wind_from_deg  : Wind direction FROM (degrees true), e.g. 270 = westerly
    wind_speed_kts : Wind speed (knots)
    total_nm       : Total route distance (NM)
    ete_no_wind_min: ETE without any wind (minutes)

    Returns a dict with all values needed for the display box.
    """
    # Angle between wind-from and true course
    # Physical headwind component (positive = headwind). We store hw
    # with the *opposite* sign so that: hw < 0 => headwind (slows),
    # hw > 0 => tailwind (adds speed).
    angle_rad = math.radians(wind_from_deg - tc_true)
    hw_phys = wind_speed_kts * math.cos(angle_rad)   # + headwind / - tailwind (physical)
    hw = -hw_phys
    xw  = wind_speed_kts * math.sin(angle_rad)       # + from right / - from left

    # Wind Correction Angle (WCA) to maintain track
    wca_rad = math.asin(max(-1.0, min(1.0, xw / tas_kts)))
    wca_deg = math.degrees(wca_rad)              # negative = correct left

    # Ground speed after correcting for crosswind. Note that `hw` is
    # signed with tailwind positive, headwind negative, so add it.
    gs = tas_kts * math.cos(wca_rad) + hw
    gs = max(1.0, gs)                            # safety floor

    # Adjusted ETE
    new_ete_min = (total_nm / gs) * 60.0
    delta_min   = new_ete_min - ete_no_wind_min  # + = slower, - = faster

    # Cross-track drift per 5 min if NO correction is applied
    drift_nm_per_5 = xw * (5.0 / 60.0)          # NM sideways in 5 min

    # Determine qualitative descriptions
    if abs(hw) < 1.0:
        hw_label = "Sin componente frontal/trasero"
    elif hw < 0:
        hw_label = f"Viento en cara  {abs(hw):.0f} kt  → más lento"
    else:
        hw_label = f"Viento de cola  {hw:.0f} kt  → más rápido"

    if abs(xw) < 1.0:
        xw_label = "Sin componente cruzada"
    elif xw > 0:
        xw_label = f"Cruzado desde la derecha  {abs(xw):.0f} kt"
    else:
        xw_label = f"Cruzado desde la izquierda  {abs(xw):.0f} kt"

    wca_label = (
        f"Corrección de rumbo: {abs(wca_deg):.1f}° a la "
        + ("derecha" if wca_deg > 0 else "izquierda")
    ) if abs(wca_deg) >= 0.5 else "Corrección de rumbo: insignificante"

    drift_dir = "derecha" if drift_nm_per_5 > 0 else "izquierda"

    return {
        "wind_from":     wind_from_deg,
        "wind_speed":    wind_speed_kts,
        "hw":            hw,
        "xw":            xw,
        "wca_deg":       wca_deg,
        "gs":            gs,
        "new_ete_min":   new_ete_min,
        "delta_min":     delta_min,
        "drift_nm_per_5": abs(drift_nm_per_5),
        "drift_dir":     drift_dir,
        "hw_label":      hw_label,
        "xw_label":      xw_label,
        "wca_label":     wca_label,
        "source":        "",          # filled by caller
        "pressure_hpa":  "",          # filled by caller
    }


def ias_to_tas(ias_kts: float, pressure_alt_ft: float) -> float:
    """
    Simple IAS -> TAS approximation.

    Uses a rule-of-thumb of +2% TAS per 1,000 ft pressure altitude.
    This is a light-weight approximation (no temperature/density-alt correction).
    Returns TAS in knots.
    """
    if ias_kts is None:
        return 0.0
    factor = 1.0 + 0.02 * (float(pressure_alt_ft) / 1000.0)
    return float(ias_kts) * factor


# ---------------------------------------------------------------------------
# Open-Meteo real-time wind
# ---------------------------------------------------------------------------

def _alt_to_pressure_level(altitude_ft: float) -> str:
    """
    Map cruise altitude in feet to the nearest Open-Meteo pressure level.
    Returns the level as a string, e.g. '850'.
    """
    if altitude_ft < 3_500:
        return "925"
    elif altitude_ft < 7_500:
        return "850"
    elif altitude_ft < 14_000:
        return "700"
    else:
        return "500"


def fetch_route_wind(lat: float, lon: float,
                    altitude_ft: float) -> tuple[float, float, str]:
    """
    Fetch the current en-route wind from Open-Meteo at (lat, lon) and the
    pressure level nearest to cruise altitude.

    Returns (speed_kts, direction_from_deg_true, pressure_level_hpa_str).
    Raises on network or data errors.
    """
    level   = _alt_to_pressure_level(altitude_ft)
    spd_var = f"wind_speed_{level}hPa"
    dir_var = f"wind_direction_{level}hPa"

    params = {
        "latitude":        round(lat, 4),
        "longitude":       round(lon, 4),
        "hourly":          f"{spd_var},{dir_var}",
        "wind_speed_unit": "kn",
        "timezone":        "UTC",
        "forecast_days":   1,
    }
    resp = requests.get(OPEN_METEO_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    hourly = data["hourly"]
    times  = hourly["time"]       # list of "YYYY-MM-DDTHH:00" strings (UTC)
    speeds = hourly[spd_var]
    dirs   = hourly[dir_var]

    # Pick the entry whose hour is closest to now (UTC)
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:00")
    idx = times.index(now_str) if now_str in times else 0

    speed = speeds[idx]
    direction = dirs[idx]
    if speed is None or direction is None:
        raise ValueError(f"Open-Meteo returned null wind at index {idx}")

    return float(speed), float(direction), level


def fetch_winds_for_legs(legs: list[dict], altitude_ft: float) -> None:
    """
    Fetch real-time wind from Open-Meteo for every non-waypoint leg point
    using a single batched API request.  Updates each leg dict in-place with:
        leg["wind_speed_kt"]   – speed in knots
        leg["wind_from_deg"]   – direction FROM in degrees true
    Waypoint-marker legs are skipped (no lat/lon to query).
    Falls back gracefully: legs without wind keep key absent.
    """
    level   = _alt_to_pressure_level(altitude_ft)
    spd_var = f"wind_speed_{level}hPa"
    dir_var = f"wind_direction_{level}hPa"

    # Collect real legs only (skip waypoint-marker rows)
    real_legs = [(i, leg) for i, leg in enumerate(legs)
                 if not leg.get("is_waypoint") and "lat" in leg and "lon" in leg]
    if not real_legs:
        return

    lats = ",".join(str(round(leg["lat"], 4)) for _, leg in real_legs)
    lons = ",".join(str(round(leg["lon"], 4)) for _, leg in real_legs)

    params = {
        "latitude":        lats,
        "longitude":       lons,
        "hourly":          f"{spd_var},{dir_var}",
        "wind_speed_unit": "kn",
        "timezone":        "UTC",
        "forecast_days":    1,
    }
    resp = requests.get(OPEN_METEO_URL, params=params, timeout=20)
    resp.raise_for_status()
    results = resp.json()   # list when multiple locations
    if isinstance(results, dict):
        results = [results]  # single-location fallback

    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:00")

    for (orig_idx, leg), loc_data in zip(real_legs, results):
        try:
            hourly = loc_data["hourly"]
            times  = hourly["time"]
            speeds = hourly[spd_var]
            dirs   = hourly[dir_var]
            t_idx  = times.index(now_str) if now_str in times else 0
            spd    = speeds[t_idx]
            drn    = dirs[t_idx]
            if spd is not None and drn is not None:
                leg["wind_speed_kt"]  = round(float(spd))
                leg["wind_from_deg"]  = round(float(drn))
                # Compute components relative to the leg track (true)
                seg_track = leg.get("segment_track_true")
                try:
                    if seg_track is not None:
                        ang = math.radians(leg["wind_from_deg"] - seg_track)
                        phys_hw = leg["wind_speed_kt"] * math.cos(ang)   # physical: + head / - tail
                        # Store with convention: negative=headwind (slows), positive=tailwind
                        hw_stored = -phys_hw
                        xw = leg["wind_speed_kt"] * math.sin(ang)       # + from right / - from left
                        leg["wind_hw_kt"] = round(hw_stored, 1)
                        leg["wind_xw_kt"] = round(xw, 1)
                except Exception:
                    pass
        except Exception:
            pass   # leave leg without wind keys on any error


# ---------------------------------------------------------------------------
# PDF generation (ReportLab)
# ---------------------------------------------------------------------------

# A4 landscape dimensions
PAGE_W, PAGE_H = landscape(A4)
# A5 panel is half of A4 landscape width
PANEL_W = PAGE_W / 2
PANEL_H = PAGE_H

MARGIN = 8 * mm
INNER_W = PANEL_W - 2 * MARGIN
FULL_INNER_W = PAGE_W - 2 * MARGIN   # full A4 landscape inner width ≈ 281 mm

# Paleta de bajo consumo de tinta
# Cabeceras: letra negra sobre fondo blanco, solo con linea inferior
# Cuadricula: gris muy claro
# Sin rellenos de fila alternada ni fondos de color
COL_HEADER_BG  = colors.white          # sin relleno
COL_HEADER_FG  = colors.black          # texto negro
COL_ALT_ROW    = colors.white          # sin relleno alternado
COL_BORDER     = colors.HexColor("#888888")  # gris medio
COL_ACCENT     = colors.black          # etiquetas en negro

STYLES = getSampleStyleSheet()


def _style(name: str, **kwargs) -> ParagraphStyle:
    """Create a named ParagraphStyle with optional overrides."""
    base = STYLES["Normal"]
    return ParagraphStyle(name, parent=base, **kwargs)


TITLE_STYLE = _style("title",
                     fontSize=11, fontName="Helvetica-Bold",
                     textColor=COL_HEADER_FG, alignment=TA_CENTER,
                     leading=14)

SUBTITLE_STYLE = _style("subtitle",
                        fontSize=7, fontName="Helvetica",
                        textColor=COL_HEADER_FG, alignment=TA_CENTER,
                        leading=9)

BODY_STYLE = _style("body",
                    fontSize=7, fontName="Helvetica", leading=9)

SMALL_STYLE = _style("small",
                     fontSize=6, fontName="Helvetica", leading=8)

ALT_CELL_STYLE = _style("alt_cell",
                        fontSize=6.5, fontName="Helvetica", leading=7.5)

FREQ_TITLE_STYLE = _style("freq_title",
                          fontSize=9, fontName="Helvetica-Bold",
                          textColor=COL_ACCENT, leading=11)


def _table_style_base(row_count: int) -> TableStyle:
    """Estilo de tabla de bajo consumo de tinta: solo bordes y negrita en cabecera."""
    cmds = [
        ("BACKGROUND",  (0, 0), (-1, 0), colors.white),
        ("TEXTCOLOR",   (0, 0), (-1, 0), colors.black),
        ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, 0), 6.5),
        ("ALIGN",       (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
        ("FONTSIZE",    (0, 1), (-1, -1), 6),
        ("FONTNAME",    (0, 1), (-1, -1), "Helvetica"),
        ("LINEBELOW",   (0, 0), (-1, 0), 0.6, colors.black),
        ("GRID",        (0, 0), (-1, -1), 0.2, COL_BORDER),
        ("TOPPADDING",  (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]
    return TableStyle(cmds)


# ---------------------------------------------------------------------------
# Flight data container – passed to draw_* functions instead of long arg lists
# ---------------------------------------------------------------------------

@dataclass
class FlightData:
    origin:              dict
    destination:         dict
    tc:                  float
    mag_var:             float
    mh:                  float
    total_nm:            float
    ete_min:             float
    fuel_required_gal:   float
    origin_freqs:        list
    dest_freqs:          list
    legs:                list
    descent_leg:         Optional[dict]
    cruise_alt_ft:       int            = 0
    wind_effect:         Optional[dict] = None
    cruise_speed_ias:    float          = 0.0
    fuel_consumption_gph: float         = 0.0


def _draw_wind_box(c: canvas.Canvas,
                   we: dict,
                   tc: float,
                   mh: float,
                   box_x: float,
                   box_y: float) -> None:
    """Draw the projected-wind summary box at the given position."""
    wind_box_h = 18 * mm
    wy = box_y + wind_box_h

    c.setStrokeColor(COL_BORDER)
    c.setLineWidth(0.4)
    c.rect(box_x, box_y, INNER_W, wind_box_h)

    # Header
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 6)
    src_tag = we.get("source", "")
    lvl_tag = we.get("pressure_hpa", "")
    src_str = f" [{src_tag} {lvl_tag} hPa]" if lvl_tag else (f" [{src_tag}]" if src_tag else "")
    c.drawString(box_x + 2 * mm, wy - 3 * mm,
                 f"VIENTO EN RUTA{src_str}  \u2014  "
                 f"{we['wind_speed']:.0f} kt desde {we['wind_from']:.0f}\u00b0V  "
                 f"(RC verdadero: {tc:.0f}\u00b0V)")

    # Disclaimer
    c.setFont("Helvetica-Oblique", 5)
    c.setFillColor(colors.HexColor("#555555"))
    disclaimer = ("\u26a0 Tiempo real Open-Meteo \u2013 verificar antes del vuelo"
                  if src_tag == "Open-Meteo"
                  else "\u26a0 MANUAL \u2013 el viento real puede cambiar en cualquier momento")
    c.drawRightString(box_x + INNER_W - 2 * mm, wy - 3 * mm, disclaimer)

    # Separator
    c.setStrokeColor(colors.HexColor("#AAAAAA"))
    c.setLineWidth(0.3)
    c.line(box_x, wy - 4.5 * mm, box_x + INNER_W, wy - 4.5 * mm)

    # Three data columns
    c.setFont("Helvetica", 5.5)
    c.setFillColor(colors.black)
    col3  = INNER_W / 3
    cx1   = box_x + 2 * mm
    cx2   = box_x + col3 + 2 * mm
    cx3   = box_x + 2 * col3 + 2 * mm
    lh    = 3.4 * mm
    ly    = wy - 7 * mm

    delta_sign = "+" if we["delta_min"] >= 0 else ""
    c.drawString(cx1, ly, we["hw_label"])
    c.drawString(cx1, ly - lh, f"Vel. sobre tierra (GS): {we['gs']:.0f} kt")
    c.drawString(cx1, ly - 2 * lh,
                 f"TEE ajustado: {int(round(we['new_ete_min']))} min "
                 f"({delta_sign}{we['delta_min']:.0f} min)")

    c.drawString(cx2, ly, we["xw_label"])
    c.drawString(cx2, ly - lh, we["wca_label"])
    c.drawString(cx2, ly - 2 * lh,
                 f"Sin correcc.: deriva {we['drift_nm_per_5']:.1f} NM/5 min "
                 f"a la {we['drift_dir']}")

    wca_mag = we["wca_deg"]
    new_mh  = (mh + wca_mag + 360) % 360
    c.drawString(cx3, ly, f"RM sin viento: {mh % 360:.0f}\u00b0M")
    c.drawString(cx3, ly - lh, f"RM corregido:  {new_mh:.0f}\u00b0M")
    c.drawString(cx3, ly - 2 * lh,
                 f"WCA: {abs(wca_mag):.1f}\u00b0 a la "
                 + ("der." if wca_mag > 0 else "izq."))


def draw_front_panel(c: canvas.Canvas,
                     data: "FlightData",
                     x_offset: float = 0.0) -> bool:
    """
    Dibuja la Hoja de Vuelo VFR en el panel A5 izquierdo (en español).
    Incluye columnas de registro manual (H.Real, C.Real) y fila de descenso.
    Returns True if the wind summary box was deferred (doesn't fit on this page)
    so the caller can draw it later (page 3).
    """
    # Unpack for readability
    origin       = data.origin
    destination  = data.destination
    tc           = data.tc
    mag_var      = data.mag_var
    mh           = data.mh
    total_nm     = data.total_nm
    ete_min      = data.ete_min
    fuel_gal     = data.fuel_required_gal
    origin_freqs = data.origin_freqs
    dest_freqs   = data.dest_freqs
    legs         = data.legs
    descent_leg  = data.descent_leg
    cruise_alt_ft = data.cruise_alt_ft
    wind_effect  = data.wind_effect

    c.saveState()
    c.translate(x_offset, 0)

    # ── Cabecera: linea superior + texto, sin relleno ───────────────────────
    hdr_h = 20 * mm
    # Linea superior
    c.setStrokeColor(colors.black)
    c.setLineWidth(1)
    c.line(0, PAGE_H - hdr_h, PANEL_W, PAGE_H - hdr_h)

    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(PANEL_W / 2, PAGE_H - 7 * mm,
                        f"HOJA DE VUELO VFR  ·  {origin['icao']} \u2192 {destination['icao']}")

    c.setFont("Helvetica", 6.5)
    var_sym = "E" if mag_var >= 0 else "O"
    alt_str = f"  |  Alt.Rec: {cruise_alt_ft:,} ft" if cruise_alt_ft else ""
    line2 = (
        f"RC: {tc:.0f}\u00b0V  |  Var: {mag_var:+.1f}\u00b0{var_sym}  |  "
        f"RM: {mh % 360:.0f}\u00b0M  |  "
        f"Dist: {total_nm:.1f} NM  |  TEE: {int(ete_min)} min  |  "
        f"Comb: {fuel_gal:.1f} gal{alt_str}"
    )
    c.drawCentredString(PANEL_W / 2, PAGE_H - 13 * mm, line2)

    c.setFont("Helvetica", 6)
    otwr  = best_freq(origin_freqs, ("TWR", "CTAF", "UNICOM")) or "\u2014"
    dtwr  = best_freq(dest_freqs,   ("TWR", "CTAF", "UNICOM")) or "\u2014"
    oatis = best_freq(origin_freqs, ("ATIS",)) or "\u2014"
    datis = best_freq(dest_freqs,   ("ATIS",)) or "\u2014"
    freq_line = (
        f"{origin['icao']} Torre: {otwr}  |  ATIS: {oatis}   "
        f"     {destination['icao']} Torre: {dtwr}  |  ATIS: {datis}"
    )
    c.setFillColor(colors.black)
    c.drawCentredString(PANEL_W / 2, PAGE_H - 18 * mm, freq_line)
    # Linea separadora bajo la cabecera
    c.setStrokeColor(COL_BORDER)
    c.setLineWidth(0.4)
    c.line(MARGIN, PAGE_H - hdr_h, PANEL_W - MARGIN, PAGE_H - hdr_h)

    # ── Info aeropuertos: 2 columnas paralelas ────────────────────────────────
    y = PAGE_H - hdr_h - 3 * mm
    col_half = INNER_W / 2
    # 9 text lines × 3.8 mm step + padding to avoid overlap with the legs table
    apt_info_h = 38 * mm
    for apt, freqs, label, x_off in [
        (origin,      origin_freqs, "SALIDA",  MARGIN),
        (destination, dest_freqs,   "LLEGADA", MARGIN + col_half + 1 * mm),
    ]:
        yy = y
        c.setFont("Helvetica-Bold", 6.5)
        c.setFillColor(colors.black)
        c.drawString(x_off, yy, f"[{label}]  {apt['icao']}")
        yy -= 3.8 * mm
        c.setFont("Helvetica", 5.5)
        c.setFillColor(colors.black)
        name_str = apt["name"]
        if len(name_str) > 28:
            name_str = name_str[:27] + "\u2026"
        c.drawString(x_off, yy, name_str)
        yy -= 3.8 * mm
        c.drawString(x_off, yy, f"Elevaci\u00f3n: {apt['elevation_ft']:.0f} ft")
        yy -= 3.8 * mm
        rwys = apt.get("runways", "")
        c.drawString(x_off, yy, "Pistas:   " + (rwys if rwys else "____________"))
        yy -= 3.8 * mm
        twr_v  = best_freq(freqs, ("TWR", "CTAF", "UNICOM"))
        gnd_v  = best_freq(freqs, ("GND", "GROUND"))
        atis_v = best_freq(freqs, ("ATIS",))
        c.drawString(x_off, yy, "TWR/CTAF: " + (twr_v  if twr_v  else "____________"))
        yy -= 3.8 * mm
        c.drawString(x_off, yy, "GND:      " + (gnd_v  if gnd_v  else "____________"))
        yy -= 3.8 * mm
        c.drawString(x_off, yy, "ATIS:     " + (atis_v if atis_v else "____________"))
        yy -= 3.8 * mm
        # Wind / QNH / Squawk fields for manual writing (no long underscores)
        c.drawString(x_off, yy, "Viento:")
        yy -= 3.8 * mm
        c.setFont("Helvetica", 6)
        c.drawString(x_off, yy, "QNH:")
        c.drawString(x_off + 32 * mm, yy, "Squawk:")

    # Reduce gap between airport info and trip table to raise table slightly
    y -= apt_info_h - 4 * mm


    # Table headers and column widths for the trip log (9 columns)
    headers = [
        "T.Plan", "Rumbo", "Referencia", "Alt.", "Comb.", "Viento",
        "Alternativo", "H.Real", "C.Real",
    ]
    # Column widths: INNER_W ≈ 132.5 mm; fixed cols sum to 108 mm.
    _cw_fixed = (9 + 11 + 35 + 10 + 9 + 16 + 9 + 9) * mm   # = 108 mm
    col_widths = [
         9 * mm,  # T.Plan
        11 * mm,  # Rumbo
        35 * mm,  # Referencia / landmark
        10 * mm,  # Alt.
         9 * mm,  # Comb.
        16 * mm,  # Viento cell (two-line)
        INNER_W - _cw_fixed,  # Alternativo – remaining (~24.5 mm)
         9 * mm,  # H.Real
         9 * mm,  # C.Real
    ]

    # Build the ordered list of table entries, inserting the descent marker at the
    # right position (by cumulative time).
    descent_cum = (
        float(descent_leg.get("elapsed_min", 0))
        if descent_leg
        else float("inf")
    )
    all_entries: list[dict] = []
    descent_inserted = False
    for leg in legs:
        cum = float(leg.get("_cumulative_min", leg.get("elapsed_min", 0)))
        if descent_leg and not descent_inserted and cum >= descent_cum:
            all_entries.append({"is_descent": True, "is_waypoint": False, "data": descent_leg})
            descent_inserted = True
        all_entries.append({
            "is_descent": bool(leg.get("is_descent", False)),
            "is_waypoint": bool(leg.get("is_waypoint")),
            "data": leg,
        })
    # If descent wasn't inserted yet (e.g. it falls after all legs), append at end
    if descent_leg and not descent_inserted:
        all_entries.append({"is_descent": True, "is_waypoint": False, "data": descent_leg})

    rows = [headers]
    descent_row_idx:  Optional[int] = None
    waypoint_row_idxs: list[int]    = []

    for idx, entry in enumerate(all_entries):
        is_d  = entry["is_descent"]
        is_wp = entry["is_waypoint"]
        leg   = entry["data"]

        if is_wp:
            # Waypoint marker: full-width spanning row with new heading
            lm = leg.get("landmark", "")
            # Append cumulative time and distance to the waypoint marker
            cum_min = int(round(leg.get("_cumulative_min", 0)))
            cum_dist = leg.get("cum_dist_nm", 0.0)
            lm_full = f"{lm}  | T+{cum_min}min  {cum_dist:.1f}NM"
            # Insert row with extra columns (blank) – 9 cols now
            rows.append([lm_full, "", "", "", "", "", "", "", ""])
            waypoint_row_idxs.append(len(rows) - 1)
            continue

        lm = leg.get("landmark", "")
        # Use the segment track (constant per segment) if available
        track_val = leg.get("segment_track_mag")
        track_str = f"{int(track_val):03d}\u00b0M" if track_val not in (None, "") else ""
        if not is_d and len(lm) > 22:
            lm = lm[:21] + "\u2026"

        alt_icao = leg.get("alt_icao", "\u2014")
        alt_freq = leg.get("alt_freq", "")
        if not alt_freq or alt_freq == "\u2014":
            alt_freq = ""
        alt_rwys = leg.get("alt_runways", "")
        alt_brg  = leg.get("alt_bearing_mag", None)
        alt_tmin = leg.get("alt_time_min", None)

        # Linea 1: ICAO + pistas
        alt_l1 = alt_icao
        if alt_rwys:
            alt_l1 += f" [{alt_rwys}]"
        # Linea 2: rumbo magnetico + tiempo
        if alt_brg is not None and alt_tmin is not None and not is_d:
            alt_l2 = f"{int(alt_brg):03d}\u00b0M  {int(alt_tmin)}min"
        else:
            alt_l2 = ""
        # Linea 3: frecuencia
        alt_l3 = alt_freq if alt_freq else "___________"
        alt_cell_text = "\n".join(filter(None, [alt_l1, alt_l2, alt_l3]))
        alt_cell = Paragraph(alt_cell_text, ALT_CELL_STYLE)

        # T.Plan: show cumulative time from origin for every row.
        cum_min_val = int(round(leg.get("_cumulative_min", leg.get("elapsed_min", 0))))
        if is_d:
            t_plan_str = f"T+{cum_min_val}"
        else:
            t_plan_str = str(cum_min_val)

        # Wind cell: "SPD / DIR°" – show if data available, else blank
        ws = leg.get("wind_speed_kt")
        wd = leg.get("wind_from_deg")
        # Compact format: SS/CCC (speed/direction) to save space, e.g. 15/270
        if ws is not None and wd is not None and not is_d:
            try:
                # Top line: speed/direction (compact)
                top = f"{int(round(ws))}/{int(round(wd)):03d}"
                # Bottom line: headwind / crosswind (signed). Use stored components if available.
                hw = leg.get("wind_hw_kt")
                xw = leg.get("wind_xw_kt")
                if hw is not None and xw is not None:
                    bottom = f"{int(round(hw))}/{int(round(xw))}"
                    wind_cell = top + "\n" + bottom
                else:
                    wind_cell = top
            except Exception:
                wind_cell = f"{ws}/{wd}"
        else:
            wind_cell = ""

        # Display altitudes rounded to the nearest 100 ft for readability
        alt_val = leg.get("min_alt_ft")
        if alt_val:
            alt_disp = f"{int(round(float(alt_val) / 100.0) * 100):,}"
        else:
            alt_disp = ""
        rows.append([
            t_plan_str,
            track_str,
            lm,
            alt_disp,
            str(leg.get("fuel_burned_gal", "")),
            wind_cell,
            alt_cell,
            "",   # H. Real – escritura manual
            "",   # C. Real – escritura manual
        ])
        if is_d:
            descent_row_idx = len(rows) - 1

    n_rows = len(rows)
    style_cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0), colors.white),
        ("TEXTCOLOR",     (0, 0), (-1, -1), colors.black),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 7),
        ("FONTSIZE",      (0, 1), (-1, -1), 6.5),
        ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW",     (0, 0), (-1, 0), 0.5, colors.black),
        ("GRID",          (0, 0), (-1, -1), 0.2, COL_BORDER),
        ("TOPPADDING",    (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 2),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
        # Alineación izquierda en Referencia (col 2) y Alternativo (col 6)
        ("ALIGN",         (2, 0), (2, -1), "LEFT"),
        ("ALIGN",         (6, 0), (6, -1), "LEFT"),
    ]
    if descent_row_idx is not None:
        style_cmds += [
            ("FONTNAME",   (0, descent_row_idx), (6, descent_row_idx), "Helvetica-Bold"),
            ("LINEABOVE",  (0, descent_row_idx), (-1, descent_row_idx), 0.5, colors.black),
            ("LINEBELOW",  (0, descent_row_idx), (-1, descent_row_idx), 0.5, colors.black),
        ]
    for wp_row in waypoint_row_idxs:
        style_cmds += [
            ("SPAN",       (0, wp_row), (8, wp_row)),  # span all 9 cols
            ("FONTNAME",   (0, wp_row), (-1, wp_row), "Helvetica-Bold"),
            ("FONTSIZE",   (0, wp_row), (-1, wp_row), 6),
            ("ALIGN",      (0, wp_row), (-1, wp_row), "CENTER"),
            ("BACKGROUND", (0, wp_row), (-1, wp_row), colors.HexColor("#EEEEEE")),
            ("LINEABOVE",  (0, wp_row), (-1, wp_row), 0.8, colors.black),
            ("LINEBELOW",  (0, wp_row), (-1, wp_row), 0.8, colors.black),
        ]

    tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle(style_cmds))

    available_h = y - MARGIN - 5 * mm
    w, h = tbl.wrapOn(c, INNER_W, available_h)
    overflow: list = []
    if h > available_h:
        parts = tbl.split(INNER_W, available_h)
        if parts:
            _pw, ph = parts[0].wrapOn(c, INNER_W, available_h)
            parts[0].drawOn(c, MARGIN, y - ph)
        overflow = list(parts[1:]) if len(parts) > 1 else []
    else:
        tbl.drawOn(c, MARGIN, y - h)

    # ── Viento proyectado ───────────────────────────────────────────────────
    # Draw inline only if the table fits on this page and wind fits below it.
    wind_deferred = False
    if wind_effect:
        wind_box_h     = 18 * mm
        desired_bottom = MARGIN + 6 * mm
        table_bottom   = y - h if not overflow else (y - available_h)
        if not overflow and table_bottom >= (desired_bottom + wind_box_h + 2 * mm):
            _draw_wind_box(c, wind_effect, tc, mh,
                           box_x=MARGIN, box_y=desired_bottom)
        else:
            wind_deferred = True  # generate_pdf will draw it on page 3

    # ── Pie de p\u00e1gina ─────────────────────────────────────────────────────────
    c.setFont("Helvetica", 5)
    c.setFillColor(colors.grey)
    c.drawCentredString(PAGE_W / 2, 3 * mm,
                        "Datos: OurAirports \u00b7 Open-Elevation \u00b7 Nominatim/OSM  "
                        "| Solo para planificaci\u00f3n/simulaci\u00f3n \u2013 verificar datos antes del vuelo")

    c.restoreState()
    return wind_deferred, overflow


def draw_back_panel(c: canvas.Canvas,
                    data: "FlightData",
                    enroute_airports: list,
                    x_offset: float = 0.0,
                    rotate: bool = True) -> None:
    """
    Dibuja el Panel de Frecuencias, rotado 180 grados para impresion duplex
    con pliegue vertical.  Todo el texto en espanol.
    """
    origin       = data.origin
    destination  = data.destination
    origin_freqs = data.origin_freqs
    dest_freqs   = data.dest_freqs

    c.saveState()
    if rotate:
        # Rotar 180 grados respecto al centro del panel A5 (for duplex printing)
        cx = x_offset + PANEL_W / 2
        cy = PAGE_H / 2
        c.translate(cx, cy)
        c.rotate(180)
        c.translate(-PANEL_W / 2, -PAGE_H / 2)
    else:
        # No rotation: just translate to the right panel offset
        c.translate(x_offset, 0)

    # Use A5 panel width for the frequency panel
    panel_w = PANEL_W
    inner_w = INNER_W

    # ── Cabecera: linea + texto negro, sin relleno ──────────────────────
    hdr_h = 18 * mm
    c.setStrokeColor(colors.black)
    c.setLineWidth(1)
    c.line(0, PAGE_H - hdr_h, panel_w, PAGE_H - hdr_h)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(panel_w / 2, PAGE_H - 7 * mm,
                        f"PANEL DE FRECUENCIAS  \u00b7  {origin['icao']} \u2192 {destination['icao']}")
    c.setFont("Helvetica", 6)
    c.setFillColor(colors.black)
    c.drawCentredString(panel_w / 2, PAGE_H - 13 * mm,
                        "Verificar todas las frecuencias en publicaciones vigentes antes del vuelo")
    # Linea separadora bajo cabecera
    c.setStrokeColor(COL_BORDER)
    c.setLineWidth(0.4)
    c.line(MARGIN, PAGE_H - hdr_h, panel_w - MARGIN, PAGE_H - hdr_h)

    # ── Tablas de frecuencias ────────────────────────────────────────────────
    y = PAGE_H - hdr_h - 5 * mm
    BLANK_ROWS = 3   # filas en blanco para escritura si no hay datos
    COL_WRITEIN = colors.white  # sin relleno

    def _freq_table_style(n: int, blank: bool) -> TableStyle:
        cmds = [
            ("BACKGROUND",    (0, 0), (-1, -1), colors.white),
            ("TEXTCOLOR",     (0, 0), (-1, -1), colors.black),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0), 6),
            ("FONTSIZE",      (0, 1), (-1, -1), 6),
            ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW",     (0, 0), (-1, 0), 0.5, colors.black),
            ("GRID",          (0, 0), (-1, -1), 0.2, COL_BORDER),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING",   (0, 0), (-1, -1), 2),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
        ]
        return TableStyle(cmds)

    def draw_freq_section(label: str, apt: dict, freqs: list[dict]) -> None:
        nonlocal y
        c.setFont("Helvetica-Bold", 7.5)
        c.setFillColor(colors.black)
        title_str = f"{label}: {apt['icao']} \u2013 {apt['name']}"
        if len(title_str) > 54:
            title_str = title_str[:53] + "\u2026"
        c.drawString(MARGIN, y, title_str)
        y -= 4.5 * mm
        # Pistas
        rwys = apt.get("runways", "")
        c.setFont("Helvetica", 6)
        c.setFillColor(colors.black)
        c.drawString(MARGIN, y,
                     "Pistas: " + (rwys if rwys else "____________") +
                     f"    Elev: {apt.get('elevation_ft', 0):.0f} ft")
        y -= 5 * mm
        # Wind / QNH / Squawk fields for manual writing (no long underscores)
        c.setFont("Helvetica", 6)
        c.drawString(MARGIN, y, "Viento:")
        y -= 4.5 * mm
        c.drawString(MARGIN, y, "QNH:")
        c.drawString(MARGIN + 32 * mm, y, "Squawk:")
        y -= 5 * mm

        col_w = [18 * mm, inner_w - 18 * mm - 24 * mm, 24 * mm]
        if freqs:
            rows = [["Tipo", "Descripci\u00f3n", "Frec. (MHz)"]]
            for f in freqs:
                rows.append([f["type"], (f["desc"][:28] if f["desc"] else ""), f["freq_mhz"]])
            rh = None
            blank = False
        else:
            rows = [["Tipo", "Descripci\u00f3n", "Frec. (MHz)"]]
            for _ in range(BLANK_ROWS):
                rows.append(["", "", ""])
            rh = [None] + [8 * mm] * BLANK_ROWS
            blank = True

        tbl = Table(rows, colWidths=col_w, rowHeights=rh)
        tbl.setStyle(_freq_table_style(len(rows), blank))
        w, h = tbl.wrapOn(c, inner_w, y - MARGIN)
        tbl.drawOn(c, MARGIN, y - h)
        y -= h + 5 * mm

    draw_freq_section("SALIDA", origin, origin_freqs)
    draw_freq_section("LLEGADA", destination, dest_freqs)

    # ── Alternativas en ruta ──────────────────────────────────────────────────
    if y > MARGIN + 15 * mm:
        c.setFont("Helvetica-Bold", 7.5)
        c.setFillColor(colors.black)
        c.drawString(MARGIN, y, "ALTERNATIVAS EN RUTA")
        y -= 5 * mm

        col_w2 = [14 * mm, inner_w - 14 * mm - 18 * mm - 34 * mm, 18 * mm, 34 * mm]
        if enroute_airports:
            rows2 = [["ICAO", "Aeropuerto / Pistas", "Dist/Rum/T", "Frecuencia"]]
            for apt in enroute_airports:
                icao = (apt.get("alt_icao") or "\u2014")[:6]
                name = (apt.get("alt_name") or "\u2014")[:20]
                rwys = apt.get("alt_runways", "")
                elev = apt.get("alt_elevation_ft")
                name_cell = name
                if rwys:
                    name_cell += f"\n{rwys}"
                if elev is not None and elev != 0:
                    name_cell += f"\nElev: {elev:.0f} ft"
                dist = str(apt.get("alt_dist_nm", "\u2014"))
                brg  = apt.get("alt_bearing_mag", None)
                tmin = apt.get("alt_time_min", None)
                dist_cell = dist + " NM"
                if brg is not None:
                    dist_cell += f"\n{int(brg):03d}\u00b0M"
                if tmin is not None:
                    dist_cell += f" {int(tmin)}min"
                freq = apt.get("alt_freq") or ""
                if not freq or freq == "\u2014":
                    freq = "___________"
                rows2.append([icao, name_cell, dist_cell, freq[:22]])
            rh2 = None
            blank2 = False
        else:
            rows2 = [["ICAO", "Aeropuerto", "Dist (NM)", "Frecuencia"]]
            for _ in range(3):
                rows2.append(["", "", "", ""])
            rh2 = [None] + [8 * mm] * 3
            blank2 = True

        tbl2 = Table(rows2, colWidths=col_w2, rowHeights=rh2)
        tbl2.setStyle(_freq_table_style(len(rows2), blank2))
        w, h = tbl2.wrapOn(c, inner_w, y - MARGIN)
        tbl2.drawOn(c, MARGIN, y - h)

    # ── Pie de p\u00e1gina ─────────────────────────────────────────────────────────
    c.setFont("Helvetica", 5)
    c.setFillColor(colors.grey)
    c.drawCentredString(panel_w / 2, 3 * mm,
                        "Datos: OurAirports  |  Solo para planificaci\u00f3n/simulaci\u00f3n")

    c.restoreState()


def draw_fold_guide(c: canvas.Canvas) -> None:
    """Dibuja la guia de pliegue en el centro de la hoja A4."""
    c.saveState()
    c.setStrokeColor(colors.lightgrey)
    c.setLineWidth(0.3)
    c.setDash([2, 4], 0)
    mid_x = PAGE_W / 2
    c.line(mid_x, 0, mid_x, PAGE_H)
    c.setFont("Helvetica", 5)
    c.setFillColor(colors.lightgrey)
    c.drawCentredString(mid_x, PAGE_H - 4 * mm, "\u25bc doblar aqu\u00ed \u25bc")
    c.restoreState()


# ---------------------------------------------------------------------------
# Leg minimap tiles
# ---------------------------------------------------------------------------

def _osm_tile_num(lat: float, lon: float, zoom: int) -> tuple:
    """Return OSM tile (x, y) integer coordinates for the given lat/lon/zoom."""
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def _tile_nw_latlon(tx: int, ty: int, zoom: int) -> tuple:
    """Return (lat, lon) of the north-west corner of tile (tx, ty) at zoom."""
    n = 2 ** zoom
    lon = tx / n * 360.0 - 180.0
    lat_r = math.atan(math.sinh(math.pi * (1.0 - 2.0 * ty / n)))
    return math.degrees(lat_r), lon


def _fetch_osm_tile(z: int, x: int, y: int, labels: bool = False):
    """
    Fetch (with disk cache) a single 256×256 CARTO tile, return PIL Image.
    labels=False  → basemap geometry (roads, terrain, no text)
    labels=True   → transparent labels-only overlay (text on transparent bg)
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    sub      = "labels" if labels else "base"
    tile_dir = _os.path.join(TILE_CACHE_DIR, sub)
    _os.makedirs(tile_dir, exist_ok=True)
    cache_path = _os.path.join(tile_dir, f"{z}_{x}_{y}.png")
    if _os.path.exists(cache_path):
        try:
            return Image.open(cache_path).convert("RGBA")
        except Exception:
            # corrupted cache — remove so next run will refetch
            try:
                _os.remove(cache_path)
            except Exception:
                pass
    url_tmpl = OSM_TILE_URL_LABELS if labels else OSM_TILE_URL
    url      = url_tmpl.format(z=z, x=x, y=y)
    try:
        resp = requests.get(url, headers={"User-Agent": "VFROnePager/1.0 (educational)"}, timeout=10)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
        img.save(cache_path)
        return img
    except Exception as exc:
        print(f"    [warn] OSM tile {'lbl' if labels else 'base'} {z}/{x}/{y}: {exc}", file=sys.stderr)
        # Transparent fallback for labels layer; grey fallback for base
        return Image.new("RGBA", (256, 256), (0, 0, 0, 0) if labels else (220, 220, 220, 255))


def _get_stitched_map(center_lat: float, center_lon: float,
                      half_nm: float, zoom: int = OSM_TILE_ZOOM):
    """
    Fetch and stitch CARTO tiles covering ±half_nm from center.
    Returns (base_img, labels_img, geo_info) where:
      base_img   – geometry-only (roads, terrain) RGBA image
      labels_img – transparent labels-only RGBA image (same size)
      geo_info   – dict with lat_top, lon_left, lat_per_px, lon_per_px
    Returns (None, None, None) if Pillow is unavailable.
    """
    try:
        from PIL import Image
    except ImportError:
        return None, None, None

    half_lat = half_nm / 60.0
    half_lon = half_lat / math.cos(math.radians(center_lat))

    lat_top  = center_lat + half_lat
    lat_bot  = center_lat - half_lat
    lon_left = center_lon - half_lon
    lon_right= center_lon + half_lon

    tx_left,  ty_top = _osm_tile_num(lat_top, lon_left,  zoom)
    tx_right, ty_bot = _osm_tile_num(lat_bot, lon_right, zoom)
    tx_left,  tx_right = min(tx_left, tx_right), max(tx_left, tx_right)
    ty_top,   ty_bot   = min(ty_top,  ty_bot),   max(ty_top,  ty_bot)

    n_x = tx_right - tx_left + 1
    n_y = ty_bot   - ty_top  + 1

    TILE_PX  = 256
    img_size = (n_x * TILE_PX, n_y * TILE_PX)
    stitched        = Image.new("RGBA", img_size, (220, 220, 220, 255))
    stitched_labels = Image.new("RGBA", img_size, (0, 0, 0, 0))
    for ix in range(n_x):
        for iy in range(n_y):
            pos = (ix * TILE_PX, iy * TILE_PX)
            t = _fetch_osm_tile(zoom, tx_left + ix, ty_top + iy, labels=False)
            if t:
                stitched.paste(t, pos)
            tl = _fetch_osm_tile(zoom, tx_left + ix, ty_top + iy, labels=True)
            if tl:
                stitched_labels.paste(tl, pos)

    nw_lat, nw_lon = _tile_nw_latlon(tx_left,     ty_top,   zoom)
    se_lat, se_lon = _tile_nw_latlon(tx_right + 1, ty_bot + 1, zoom)

    w, h = stitched.size
    geo = {
        "lat_top":   nw_lat,
        "lon_left":  nw_lon,
        "lat_per_px": (nw_lat - se_lat) / h,
        "lon_per_px": (se_lon - nw_lon) / w,
    }
    return stitched, stitched_labels, geo


def _latlon_to_px(lat: float, lon: float, geo: dict) -> tuple:
    """Convert lat/lon to pixel coords in a stitched map given its geo_info."""
    px = (lon - geo["lon_left"]) / geo["lon_per_px"]
    py = (geo["lat_top"] - lat)  / geo["lat_per_px"]
    return int(round(px)), int(round(py))


def _draw_poi_icon(draw, cx: int, cy: int, r: int, lm_type: str) -> None:
    """
    Draw a small cartographic symbol for the given landmark type.
    Uses PIL polygon/ellipse primitives — no letter shortcuts.
    """
    WHITE  = (255, 255, 255, 255)
    if lm_type == "peak":
        # Brown/orange mountain triangle with white snow cap
        pts = [(cx, cy - r), (cx - r, cy + r), (cx + r, cy + r)]
        draw.polygon(pts, fill=(160, 100, 35, 235), outline=WHITE)
        cap = [(cx, cy - r), (cx - r // 2, cy - r + r // 2),
               (cx + r // 2, cy - r + r // 2)]
        draw.polygon(cap, fill=(240, 240, 240, 220))
    elif lm_type in ("lake", "water"):
        # Solid blue oval
        draw.ellipse([cx - r, cy - int(r * 0.7), cx + r, cy + int(r * 0.7)],
                     fill=(0, 110, 210, 230), outline=WHITE, width=2)
        # Highlight stripe
        draw.arc([cx - r + 4, cy - int(r * 0.5), cx + r - 4, cy],
                 start=200, end=340, fill=(180, 220, 255, 200), width=2)
    elif lm_type == "river":
        # Blue wavy line — draw as a thicker sinusoidal stroke
        draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                     fill=(0, 130, 200, 210), outline=WHITE, width=2)
        step = max(1, r // 4)
        for xi in range(-r + 3, r - 2):
            yi = int((r * 0.35) * math.sin(xi * math.pi / (r * 0.6)))
            draw.point((cx + xi, cy + yi), fill=WHITE)
            draw.point((cx + xi, cy + yi + 1), fill=WHITE)
    elif lm_type == "viewpoint":
        # Yellow diamond
        pts = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
        draw.polygon(pts, fill=(220, 170, 0, 235), outline=WHITE)
        draw.ellipse([cx - r // 3, cy - r // 3, cx + r // 3, cy + r // 3],
                     fill=(80, 40, 0, 240))
    elif lm_type == "historic":
        # Dark-red castle tower with battlements
        tw = max(4, r // 2)
        draw.rectangle([cx - tw, cy - r + r // 3, cx + tw, cy + r],
                       fill=(140, 50, 30, 230), outline=WHITE, width=1)
        for mx in [cx - tw, cx - tw // 2, cx + 1]:
            draw.rectangle([mx, cy - r, mx + tw - 2, cy - r + r // 3],
                            fill=(140, 50, 30, 230), outline=WHITE, width=1)
    elif lm_type == "town":
        # Simple house: rectangle body + triangular roof
        hw = max(3, int(r * 0.6))
        hh = max(3, int(r * 0.6))
        draw.rectangle([cx - hw, cy, cx + hw, cy + hh],
                       fill=(100, 100, 110, 220), outline=WHITE, width=1)
        roof = [(cx - r, cy), (cx, cy - r), (cx + r, cy)]
        draw.polygon(roof, fill=(180, 60, 50, 220), outline=WHITE)
    elif lm_type == "place_of_worship":
        # Cross on a base
        tw = max(2, r // 3)
        draw.rectangle([cx - tw, cy - r, cx + tw, cy + r],
                       fill=(140, 0, 100, 230), outline=WHITE, width=1)
        draw.rectangle([cx - r, cy - r + r // 3, cx + r, cy - r + r // 3 + tw * 2],
                       fill=(140, 0, 100, 230), outline=WHITE, width=1)
    else:
        pass  # unknown type — draw nothing


def _rotate_point_cw(px: float, py: float, cx: float, cy: float,
                     bearing_deg: float) -> tuple:
    """
    Rotate point (px, py) clockwise by bearing_deg degrees around center (cx, cy).
    In image space (y increases down), CW rotation corresponds to PIL.rotate(-bearing).
    """
    ang = math.radians(bearing_deg)
    dx, dy = px - cx, py - cy
    rx =  dx * math.cos(ang) + dy * math.sin(ang)
    ry = -dx * math.sin(ang) + dy * math.cos(ang)
    return cx + rx, cy + ry


def _zoom_from_altitude(alt_ft: float, lat: float) -> int:
    """
    Return an ESRI tile zoom level so satellite tiles show approximately
    the ground footprint visible to a pilot at the given altitude.

    Assumes a 60° horizontal field of view (2 × tan 30° ≈ 1.155 × altitude).
    Result is clamped to [10, 17] to avoid fetching hundreds of tiles or
    requesting zoom levels ESRI doesn't support in remote areas.
    """
    H_m       = max(alt_ft, 300) * 0.3048           # clamp to avoid edge cases
    vis_m     = H_m * 2 * math.tan(math.radians(30))  # 60° FOV ground width
    nm_vis    = vis_m / 1852.0
    TILE_PX   = 256
    cos_lat   = math.cos(math.radians(lat))
    # smallest z where crop_px >= TILE_DISPLAY_PX (resize is a downsample, not upsample)
    # sat_nm_per_px(z) = (360 / (TILE_PX * 2^z)) * 60 * cos_lat
    # crop_px = nm_vis / sat_nm_per_px = nm_vis * TILE_PX * 2^z / (360 * 60 * cos_lat)
    # crop_px >= TILE_DISPLAY_PX  =>  z >= log2(TILE_DISPLAY_PX * 360 * 60 * cos_lat / (TILE_PX * nm_vis))
    z_ideal = math.log2(TILE_DISPLAY_PX * 360 * 60 * cos_lat / (TILE_PX * nm_vis))
    return int(min(17, max(10, round(z_ideal))))


def _fetch_esri_sat_tile(z: int, x: int, y: int):
    """
    Fetch (with disk cache) a single 256×256 ESRI World Imagery satellite tile.
    Returns a PIL RGBA Image, or a grey fallback if unavailable.
    Note: ESRI tile URL uses z/y/x order.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    _os.makedirs(SAT_CACHE_DIR, exist_ok=True)
    cache_path = _os.path.join(SAT_CACHE_DIR, f"{z}_{x}_{y}.jpg")
    if _os.path.exists(cache_path):
        try:
            return Image.open(cache_path).convert("RGBA")
        except Exception:
            try:
                _os.remove(cache_path)
            except Exception:
                pass
    url = ESRI_SAT_URL.format(z=z, y=y, x=x)
    # Try a few times to avoid transient network/errors or remote rate-limits
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "VFROnePager/1.0 (educational)"},
                timeout=12,
            )
            resp.raise_for_status()
            # Quick sanity: ensure response is an image-like payload
            ctype = resp.headers.get("Content-Type", "")
            if not ctype.startswith("image/") and len(resp.content) < 500:
                raise ValueError(f"unexpected content type: {ctype}")
            img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
            try:
                img.save(cache_path, "JPEG", quality=85)
            except Exception:
                pass
            return img
        except Exception as exc:
            last_exc = exc
            # brief backoff
            time.sleep(0.8 + attempt * 0.5)
            continue

    # All attempts failed — log and return a neutral-grey fallback tile
    print(f"    [warn] ESRI sat tile {z}/{y}/{x} failed after retries: {last_exc}", file=sys.stderr)
    return Image.new("RGBA", (256, 256), (100, 100, 100, 255))


def _get_stitched_satellite(center_lat: float, center_lon: float,
                            half_nm: float, zoom: int = OSM_TILE_ZOOM):
    """
    Fetch and stitch ESRI World Imagery tiles covering ±half_nm from center.
    Returns (sat_img, geo_info) with the same geo_info format as _get_stitched_map,
    or (None, None) if Pillow is unavailable.
    sat_img is RGBA so it can be alpha-composited with the OSM layers.
    """
    try:
        from PIL import Image
    except ImportError:
        return None, None

    half_lat = half_nm / 60.0
    half_lon = half_lat / math.cos(math.radians(center_lat))

    lat_top  = center_lat + half_lat
    lat_bot  = center_lat - half_lat
    lon_left = center_lon - half_lon
    lon_right = center_lon + half_lon

    tx_left,  ty_top = _osm_tile_num(lat_top,  lon_left,  zoom)
    tx_right, ty_bot = _osm_tile_num(lat_bot,  lon_right, zoom)
    tx_left,  tx_right = min(tx_left, tx_right), max(tx_left, tx_right)
    ty_top,   ty_bot   = min(ty_top,  ty_bot),   max(ty_top,  ty_bot)

    n_x = tx_right - tx_left + 1
    n_y = ty_bot   - ty_top  + 1

    TILE_PX  = 256
    stitched = Image.new("RGBA", (n_x * TILE_PX, n_y * TILE_PX), (80, 80, 80, 255))
    for ix in range(n_x):
        for iy in range(n_y):
            t = _fetch_esri_sat_tile(zoom, tx_left + ix, ty_top + iy)
            if t:
                stitched.paste(t, (ix * TILE_PX, iy * TILE_PX))

    nw_lat, nw_lon = _tile_nw_latlon(tx_left,      ty_top,    zoom)
    se_lat, se_lon = _tile_nw_latlon(tx_right + 1, ty_bot + 1, zoom)

    w, h = stitched.size
    geo = {
        "lat_top":    nw_lat,
        "lon_left":   nw_lon,
        "lat_per_px": (nw_lat - se_lat) / h,
        "lon_per_px": (se_lon - nw_lon) / w,
    }
    return stitched, geo


def _build_tile_image(leg_lat: float, leg_lon: float,
                      bearing_deg: float,
                      lm_lat: float, lm_lon: float,
                      lm_label: str, lm_type: str,
                      leg_num: int, cum_min: int,
                      is_dest: bool = False,
                      bg_mode: str = "osm",
                      alt_ft: float = 5000.0,
                      extra_places: list = None):
    """
    Build a track-up minimap PIL Image for a single leg checkpoint.

    Layout (track-up, A4 landscape print):
      - Checkpoint (red circle) at (TILE_CHKPT_X_FRAC, TILE_CHKPT_Y_FRAC)
        → track displaced to the right, leaving left area for the POI.
      - Track line enters from bottom of tile along the same bearing.
      - Landmark (blue flag) placed at its actual geographic position.
      - Leg number + cumulative time shown in the tile header.
    Returns a PIL Image, or None if Pillow is unavailable.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("  [warn] Pillow not installed – skipping leg tile minimap.", file=sys.stderr)
        return None

    # ── Step 1: fetch stitched map centred on the LANDMARK (visual reference) ──
    CANVAS_HALF_NM = TILE_DISPLAY_NM * 1.5   # large enough to survive any rotation
    raw_img, raw_labels, geo = _get_stitched_map(lm_lat, lm_lon, CANVAS_HALF_NM)
    if raw_img is None or geo is None:
        return None

    orig_w, orig_h = raw_img.size

    # ── Step 2: compute the GEOGRAPHIC (Mercator-conformal) pixel scale ───
    # OSM tiles in web-Mercator are conformal: nm/px is equal in x and y.
    # 1° lon at latitude φ = 60·cos(φ) NM.  geo["lon_per_px"] is deg/px in x.
    nm_per_px = geo["lon_per_px"] * 60.0 * math.cos(math.radians(lm_lat))
    if nm_per_px <= 0:
        return None

    # Raw-image pixels that span exactly TILE_DISPLAY_NM (the crop square side).
    crop_px = max(64, int(round(TILE_DISPLAY_NM / nm_per_px)))

    # ── Step 3: pixel coords of checkpoint & landmark in the RAW image ────
    # DO NOT resize to square here – that would distort the Mercator aspect ratio
    # and make all bearings wrong after rotation.
    img_cx = orig_w / 2.0
    img_cy = orig_h / 2.0
    cx_raw = (leg_lon - geo["lon_left"]) / geo["lon_per_px"]
    cy_raw = (geo["lat_top"] - leg_lat)  / geo["lat_per_px"]
    lx_raw = (lm_lon  - geo["lon_left"]) / geo["lon_per_px"]
    ly_raw = (geo["lat_top"] - lm_lat)   / geo["lat_per_px"]

    # ── Step 4: rotate image CCW by bearing_deg → track points UP ─────────
    # PIL.rotate(+angle) = CCW.  For a northbound track (bearing=0) no rotation
    # is needed; for eastbound (bearing=90) rotating CCW 90° puts east at the top.
    rotated = raw_img.rotate(
        bearing_deg,
        resample=Image.BICUBIC,
        expand=False,
        center=(int(img_cx), int(img_cy)),
        fillcolor=(235, 235, 230, 255),
    )

    # ── Step 5: rotate the point coords with the SAME CCW transform ───────
    # _rotate_point_cw(px, py, cx, cy, angle) computes CCW-in-screen-space,
    # which is exactly what PIL.rotate(angle) does to pixel positions.
    cx_r, cy_r = _rotate_point_cw(cx_raw, cy_raw, img_cx, img_cy, bearing_deg)
    lx_r, ly_r = _rotate_point_cw(lx_raw, ly_raw, img_cx, img_cy, bearing_deg)

    # ── Step 6: crop placing the LANDMARK at (0.30, 0.50) in the tile ──────
    # Landmark is to the LEFT of track (port side); aircraft is to the right.
    # Placing landmark at x=0.30 keeps it visible on the left while giving
    # 70% of tile width for the aircraft/track line on the right.
    _LM_X = 0.30
    _LM_Y = 0.50
    crop_l = int(round(lx_r - crop_px * _LM_X))
    crop_t = int(round(ly_r - crop_px * _LM_Y))
    disp_scale = TILE_DISPLAY_PX / crop_px

    # ── Track-visibility clamp ─────────────────────────────────────────────
    # If the checkpoint (track line) would land outside a 10% inset from either
    # edge, shift crop_l so the track is always visible in the tile.
    _TRACK_MARGIN = crop_px * 0.10
    _raw_chk_x = cx_r - crop_l
    if _raw_chk_x < _TRACK_MARGIN:
        crop_l = int(round(cx_r - _TRACK_MARGIN))
    elif _raw_chk_x > crop_px - _TRACK_MARGIN:
        crop_l = int(round(cx_r - (crop_px - _TRACK_MARGIN)))

    # Pad the rotated image so the crop window is always within valid pixels.
    pad = crop_px
    padded = Image.new("RGBA", (orig_w + 2 * pad, orig_h + 2 * pad), (235, 235, 230, 255))
    padded.paste(rotated, (pad, pad))
    tile = padded.crop((crop_l + pad, crop_t + pad,
                        crop_l + pad + crop_px, crop_t + pad + crop_px))

    # ── Step 7: resize the square crop to TILE_DISPLAY_PX ─────────────────
    tile = tile.resize((TILE_DISPLAY_PX, TILE_DISPLAY_PX), Image.LANCZOS)

    # Landmark and checkpoint pixel positions derived from the (clamped) crop origin.
    lx_tile = (lx_r - crop_l) * disp_scale
    ly_tile = (ly_r - crop_t) * disp_scale
    chkpt_x = int(round((cx_r - crop_l) * disp_scale))
    chkpt_y = int(round((cy_r - crop_t) * disp_scale))

    # _lbl_crop_px: how many OSM-scale pixels correspond to the final tile viewport.
    # For OSM mode this equals crop_px; for satellite we override it below so
    # the rotated labels layer covers the same geographic area as the sat view.
    _lbl_crop_px = crop_px

    # ── Step 7b: satellite background (only when bg_mode == "satellite") ───
    # Uses a SEPARATE rotation/crop pipeline from the OSM one (different zoom
    # level and geographic footprint).  The zoom is derived from flight altitude
    # so the resulting tile shows roughly what the pilot sees out the window.
    # lx_tile / ly_tile are updated to match the satellite coordinate space.
    if bg_mode == "satellite":
        try:
            sat_zoom    = _zoom_from_altitude(alt_ft, leg_lat)
            # Geographic footprint for the satellite tile (altitude-based)
            H_m         = max(alt_ft, 300) * 0.3048
            sat_disp_nm = H_m * 2 * math.tan(math.radians(30)) / 1852.0
            sat_canvas  = sat_disp_nm * 1.5          # canvas margin (survive rotation)
            # Labels crop in OSM-scale pixels matching the satellite viewport
            _lbl_crop_px = max(64, int(round(sat_disp_nm / nm_per_px)))
            sat_raw, sat_geo = _get_stitched_satellite(
                lm_lat, lm_lon, sat_canvas, zoom=sat_zoom)
            if sat_raw is not None and sat_geo is not None:
                sat_ow, sat_oh  = sat_raw.size
                sat_icx = sat_ow / 2.0
                sat_icy = sat_oh / 2.0
                # Checkpoint pixel in satellite image
                sat_cx_raw = (leg_lon - sat_geo["lon_left"]) / sat_geo["lon_per_px"]
                sat_cy_raw = (sat_geo["lat_top"] - leg_lat)  / sat_geo["lat_per_px"]
                # Landmark pixel in satellite image
                sat_lx_raw = (lm_lon - sat_geo["lon_left"]) / sat_geo["lon_per_px"]
                sat_ly_raw = (sat_geo["lat_top"] - lm_lat)  / sat_geo["lat_per_px"]
                # NM per pixel at this zoom level
                sat_nm_px  = sat_geo["lon_per_px"] * 60.0 * math.cos(math.radians(leg_lat))
                sat_crop_px = max(64, int(round(sat_disp_nm / sat_nm_px)))
                # Rotate satellite canvas CCW by bearing_deg (track-up)
                # Same as OSM: PIL.rotate(+θ) = CCW, which puts the track
                # direction at the top of the tile.
                sat_rotated = sat_raw.rotate(
                    bearing_deg,
                    resample=Image.BICUBIC,
                    expand=False,
                    center=(int(sat_icx), int(sat_icy)),
                    fillcolor=(80, 80, 80, 255),
                )
                # Rotate coords with same transform
                sat_cx_r, sat_cy_r = _rotate_point_cw(
                    sat_cx_raw, sat_cy_raw, sat_icx, sat_icy, bearing_deg)
                sat_lx_r, sat_ly_r = _rotate_point_cw(
                    sat_lx_raw, sat_ly_raw, sat_icx, sat_icy, bearing_deg)
                # Crop placing landmark at (_LM_X, _LM_Y) — same as OSM pipeline.
                sat_crop_l = int(round(sat_lx_r - sat_crop_px * _LM_X))
                sat_crop_t = int(round(sat_ly_r - sat_crop_px * _LM_Y))
                sat_disp_scale = TILE_DISPLAY_PX / sat_crop_px
                # Track-visibility clamp for satellite pipeline
                _sat_track_margin = sat_crop_px * 0.10
                _sat_raw_chk_x = sat_cx_r - sat_crop_l
                if _sat_raw_chk_x < _sat_track_margin:
                    sat_crop_l = int(round(sat_cx_r - _sat_track_margin))
                elif _sat_raw_chk_x > sat_crop_px - _sat_track_margin:
                    sat_crop_l = int(round(sat_cx_r - (sat_crop_px - _sat_track_margin)))
                sat_pad    = sat_crop_px
                sat_padded = Image.new(
                    "RGBA",
                    (sat_ow + 2 * sat_pad, sat_oh + 2 * sat_pad),
                    (80, 80, 80, 255),
                )
                sat_padded.paste(sat_rotated, (sat_pad, sat_pad))
                sat_cropped = sat_padded.crop((
                    sat_crop_l + sat_pad, sat_crop_t + sat_pad,
                    sat_crop_l + sat_pad + sat_crop_px,
                    sat_crop_t + sat_pad + sat_crop_px,
                ))
                tile = sat_cropped.resize(
                    (TILE_DISPLAY_PX, TILE_DISPLAY_PX), Image.LANCZOS
                ).convert("RGBA")
                # Pixel positions derived from the (clamped) satellite crop origin.
                lx_tile = (sat_lx_r - sat_crop_l) * sat_disp_scale
                ly_tile = (sat_ly_r - sat_crop_t) * sat_disp_scale
                chkpt_x = int(round((sat_cx_r - sat_crop_l) * sat_disp_scale))
                chkpt_y = int(round((sat_cy_r - sat_crop_t) * sat_disp_scale))
        except Exception as _sat_exc:
            print(f" [sat-warn: {_sat_exc}]", end="", file=sys.stderr)
            pass  # satellite fetch failed → keep OSM fallback

    # ── Step 8: composite CARTO labels (correctly rotated) ──────────────────
    # Rotate the labels layer with the SAME transform as the base image so
    # town/city names appear in their correct track-up positions.
    # Skipped for satellite mode — labels are too large/opaque over aerial photos;
    # the small settlement dots drawn later provide sufficient orientation.
    if raw_labels is not None and bg_mode != "satellite":
        try:
            rot_lbl = raw_labels.rotate(
                bearing_deg,
                resample=Image.BICUBIC,
                expand=False,
                center=(int(img_cx), int(img_cy)),
                fillcolor=(0, 0, 0, 0),
            )
            lbl_pad = crop_px
            lbl_padded = Image.new(
                "RGBA", (orig_w + 2 * lbl_pad, orig_h + 2 * lbl_pad), (0, 0, 0, 0))
            lbl_padded.paste(rot_lbl, (lbl_pad, lbl_pad))
            # Use the same (potentially clamped) crop origin as the base tile
            # so labels are always registered to the visible map area.
            lbl_crop_l = crop_l
            lbl_crop_t = crop_t
            lbl_crop = lbl_padded.crop((
                lbl_crop_l + lbl_pad,
                lbl_crop_t + lbl_pad,
                lbl_crop_l + lbl_pad + _lbl_crop_px,
                lbl_crop_t + lbl_pad + _lbl_crop_px,
            ))
            lbl_tile = lbl_crop.resize((TILE_DISPLAY_PX, TILE_DISPLAY_PX), Image.LANCZOS)
            tile = Image.alpha_composite(tile.convert("RGBA"), lbl_tile)
        except Exception:
            pass  # labels composite failure is non-fatal

    # ── Draw overlays ──────────────────────────────────────────────────────
    draw = ImageDraw.Draw(tile)

    # Track line:
    #  - en-route: full height (shows track continuing beyond checkpoint)
    #  - destination: only bottom → checkpoint (you're arriving, not continuing)
    # chkpt_x/y were computed from geometry above (both OSM and satellite modes).
    if is_dest:
        draw.line([(chkpt_x, TILE_DISPLAY_PX), (chkpt_x, chkpt_y)],
                  fill=(0, 160, 0, 220), width=3)
    else:
        draw.line([(chkpt_x, TILE_DISPLAY_PX), (chkpt_x, 0)],
                  fill=(220, 0, 0, 220), width=3)

    # Checkpoint circle: green for destination, red for en-route
    R = 10 if is_dest else 8
    circ_fill    = (0, 160, 0, 240)    if is_dest else (220, 0, 0, 230)
    circ_outline = (255, 255, 255, 255)
    draw.ellipse([chkpt_x - R, chkpt_y - R, chkpt_x + R, chkpt_y + R],
                 fill=circ_fill, outline=circ_outline, width=2)
    if is_dest:
        # Draw a small cross inside the circle to make it look like an airport
        draw.line([(chkpt_x - R + 3, chkpt_y), (chkpt_x + R - 3, chkpt_y)],
                  fill=(255, 255, 255, 255), width=2)
        draw.line([(chkpt_x, chkpt_y - R + 3), (chkpt_x, chkpt_y + R - 3)],
                  fill=(255, 255, 255, 255), width=2)

    # Landmark dot (blue) + centered label below dot
    lx_i, ly_i = int(round(lx_tile)), int(round(ly_tile))
    if 10 < lx_i < TILE_DISPLAY_PX - 10 and 10 < ly_i < TILE_DISPLAY_PX - 10:
        R2 = 7
        draw.ellipse([lx_i - R2, ly_i - R2, lx_i + R2, ly_i + R2],
                     fill=(0, 80, 200, 220), outline=(255, 255, 255, 255), width=2)

        lm_short = (lm_label[:22] + "\u2026") if len(lm_label) > 23 else lm_label

        # Load a Unicode-capable TrueType font so accented chars (ñ, é …) render
        # correctly. Try common macOS/Linux paths, then fall back gracefully.
        _font = None
        _font_size = 13
        for _fp in [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/SFNS.ttf",
            "/System/Library/Fonts/SFNSDisplay.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]:
            try:
                _font = ImageFont.truetype(_fp, _font_size)
                break
            except Exception:
                continue
        if _font is None:
            try:
                _font = ImageFont.load_default(size=_font_size)  # Pillow ≥ 9.2
            except Exception:
                _font = ImageFont.load_default()

        # Draw a small cartographic icon next to the landmark dot to indicate
        # the POI type (peak symbol, church cross, lake, etc.).
        try:
            icon_r = 11
            ix = lx_i + R2 + icon_r + 4
            iy = ly_i - icon_r
            if ix + icon_r > TILE_DISPLAY_PX - 6:
                ix = lx_i - R2 - icon_r - 4
            _draw_poi_icon(draw, ix, iy, icon_r, lm_type)
        except Exception:
            pass

        # Measure text (getbbox preferred; fall back to getsize for older Pillow)
        try:
            _bb = _font.getbbox(lm_short)
            tx_w, tx_h = _bb[2] - _bb[0], _bb[3] - _bb[1]
        except Exception:
            try:
                tx_w, tx_h = _font.getsize(lm_short)
            except Exception:
                tx_w = max(20, int(len(lm_short) * 7)); tx_h = 14

        pad2 = 4
        txt_img = Image.new("RGBA", (tx_w + pad2 * 2, tx_h + pad2 * 2), (0, 0, 0, 0))
        td = ImageDraw.Draw(txt_img)
        # Thick white outline: draw text 8 times offset by 1 px
        for ox, oy in [(-1,-1),(0,-1),(1,-1),(-1,0),(1,0),(-1,1),(0,1),(1,1)]:
            td.text((pad2 + ox, pad2 + oy), lm_short,
                    fill=(255, 255, 255, 255), font=_font)
        # Black text on top
        td.text((pad2, pad2), lm_short, fill=(0, 0, 0, 255), font=_font)

        rx_w, rx_h = txt_img.size
        # Center label horizontally on dot, place just below it
        paste_x = int(lx_i - rx_w / 2)
        paste_y = int(ly_i + R2 + 3)
        paste_x = max(2, min(paste_x, TILE_DISPLAY_PX - rx_w - 2))
        paste_y = max(2, min(paste_y, TILE_DISPLAY_PX - rx_h - 2))
        tile.paste(txt_img, (paste_x, paste_y), txt_img)

    # ── Draw extra settlement labels (towns/villages from Overpass candidates) ──
    # These give the pilot additional orientation references beyond the main landmark.
    # Only draw settlements that fall within the tile's visible area.
    if extra_places:
        try:
            _efont = _font  # reuse the same font loaded above
        except Exception:
            _efont = None
        for ep in extra_places:
            ep_lat = ep.get("lat")
            ep_lon = ep.get("lon")
            ep_name = ep.get("name", "")
            if ep_lat is None or ep_lon is None or not ep_name:
                continue
            # Skip if this is the main landmark (already drawn with full icon)
            if abs(ep_lat - lm_lat) < 0.0001 and abs(ep_lon - lm_lon) < 0.0001:
                continue
            try:
                # Pixel in raw image
                ep_x_raw = (ep_lon - geo["lon_left"]) / geo["lon_per_px"]
                ep_y_raw = (geo["lat_top"] - ep_lat)  / geo["lat_per_px"]
                # Apply same rotation as the base image
                ep_x_r, ep_y_r = _rotate_point_cw(ep_x_raw, ep_y_raw, img_cx, img_cy, bearing_deg)
                # Convert to tile pixel coords (using OSM pipeline geometry)
                ep_tx = int(round((ep_x_r - lx_r + crop_px * _LM_X) * disp_scale))
                ep_ty = int(round((ep_y_r - ly_r + crop_px * _LM_Y) * disp_scale))
                margin = 20
                if not (margin < ep_tx < TILE_DISPLAY_PX - margin and
                        margin < ep_ty < TILE_DISPLAY_PX - margin):
                    continue
                # Small grey settlement dot
                R_ep = 4
                draw.ellipse([ep_tx - R_ep, ep_ty - R_ep, ep_tx + R_ep, ep_ty + R_ep],
                             fill=(80, 80, 200, 180), outline=(255, 255, 255, 200), width=1)
                # Small label with white outline
                ep_short = ep_name[:22]
                if _efont:
                    try:
                        _eb = _efont.getbbox(ep_short)
                        ew, eh = _eb[2] - _eb[0], _eb[3] - _eb[1]
                    except Exception:
                        ew = max(16, len(ep_short) * 6); eh = 11
                    ep_lbl = Image.new("RGBA", (ew + 6, eh + 4), (0, 0, 0, 0))
                    ed = ImageDraw.Draw(ep_lbl)
                    for ox, oy in [(-1,-1),(0,-1),(1,-1),(-1,0),(1,0),(-1,1),(0,1),(1,1)]:
                        ed.text((3 + ox, 2 + oy), ep_short, fill=(255,255,255,230), font=_efont)
                    ed.text((3, 2), ep_short, fill=(30, 30, 120, 255), font=_efont)
                    px2 = max(2, min(ep_tx - ew // 2, TILE_DISPLAY_PX - ew - 6))
                    py2 = max(2, min(ep_ty + R_ep + 2, TILE_DISPLAY_PX - eh - 4))
                    tile.paste(ep_lbl, (px2, py2), ep_lbl)
            except Exception:
                continue

    # Header bar (semi-transparent dark strip at top of tile)
    header_h = 22
    hdr_bg = (0, 100, 0, 190) if is_dest else (30, 30, 30, 180)
    header_overlay = Image.new("RGBA", (TILE_DISPLAY_PX, header_h), hdr_bg)
    tile.paste(header_overlay, (0, 0), header_overlay)
    draw2 = ImageDraw.Draw(tile)
    header_text = "DEST" if is_dest else f"T+{cum_min}min"
    draw2.text((6, 5), header_text, fill=(255, 255, 255, 240))
    # Leg number at top-right (suppress for dest tile)
    if not is_dest:
        leg_label = f"#{leg_num}"
        draw2.text((TILE_DISPLAY_PX - 32, 5), leg_label, fill=(255, 220, 0, 240))

    # North arrow (top-right corner, small)
    na_x, na_y = TILE_DISPLAY_PX - 22, header_h + 14
    arrow_len = 12
    # Bearing = angle clockwise from north; in track-up image north is rotated
    north_bearing_in_tile = (-bearing_deg + 360) % 360  # direction to north in tile coords
    ang_r = math.radians(north_bearing_in_tile)
    tip_x = na_x + arrow_len * math.sin(ang_r)
    tip_y = na_y - arrow_len * math.cos(ang_r)
    draw2.line([(na_x, na_y), (int(tip_x), int(tip_y))],
               fill=(200, 0, 0, 220), width=2)
    # 'N' label: always horizontal (consistent with all other labels on the tile).
    draw2.text((int(tip_x) - 3, int(tip_y) - 10), "N", fill=(200, 0, 0, 220))

    return tile.convert("RGB")


# ---------------------------------------------------------------------------
# Terrain silhouette (forward-view from aircraft)
# ---------------------------------------------------------------------------

def _build_silhouette_image(ac_lat: float, ac_lon: float,
                            bearing_deg: float,
                            alt_ft: float,
                            width_px: int = 400,
                            height_px: int = 220,
                            fov_deg: float = 60.0,
                            max_range_nm: float = 30.0,
                            ray_steps: int = 300) -> "Image":
    """
    Render a forward-looking terrain silhouette from the aircraft position.

    The view is centred on 'bearing_deg' with a horizontal FOV of 'fov_deg'.
    For each column of pixels the highest terrain angle above/below horizontal
    is found by ray-marching outward to max_range_nm.  Terrain is drawn as a
    filled dark shape below the sky.

    Sky: light blue gradient (bright near horizon, dark blue at top).
    Terrain: dark brownish silhouette gradient (lighter = closer to viewer).
    Bearing tick + horizon line drawn in the centre column.

    Returns a Pillow RGBA Image or None on error.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    img = Image.new("RGBA", (width_px, height_px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ----- sky gradient (top=deep blue, middle=light sky) -----
    sky_top    = (30,  60, 140)
    sky_horiz  = (160, 200, 240)
    for y in range(height_px):
        t = y / (height_px - 1)          # 0=top, 1=bottom
        r = int(sky_top[0] + (sky_horiz[0] - sky_top[0]) * t)
        g = int(sky_top[1] + (sky_horiz[1] - sky_top[1]) * t)
        b = int(sky_top[2] + (sky_horiz[2] - sky_top[2]) * t)
        draw.line([(0, y), (width_px - 1, y)], fill=(r, g, b, 255))

    ac_elev_m  = get_elevations_m([(ac_lat, ac_lon)])[0]
    ac_elev_ft = ac_elev_m * FEET_PER_METER
    # Aircraft altitude AGL → used as eye height above terrain
    eye_agl_ft  = max(alt_ft - ac_elev_ft, 300.0)
    eye_agl_m   = eye_agl_ft * 0.3048

    # Vertical FOV: we'll display ±vfov_deg above/below horizontal
    vfov_deg   = 15.0
    # pixel-to-angle mapping: centre row = 0°, top = +vfov_deg, bottom = -vfov_deg
    # Positive angle = above horizon
    def _angle_to_y(angle_deg: float) -> int:
        """Angle above horizon → pixel row (0=top, height_px-1=bottom)."""
        frac = 0.5 - angle_deg / (2.0 * vfov_deg)   # 0=top, 1=bottom
        return int(round(frac * (height_px - 1)))

    horizon_y = _angle_to_y(0.0)

    # ---- ray-march each column ----
    # step sizes: logarithmic — dense near aircraft, sparse far away
    # Build distance samples 0.05 NM → max_range_nm
    distances_nm = []
    d = 0.05
    while d <= max_range_nm:
        distances_nm.append(d)
        # grow step gradually: fine near, coarser far
        d += max(0.05, d * 0.03)

    half_fov   = fov_deg / 2.0
    sky_profile = [height_px] * width_px  # y where terrain starts for each column

    for col in range(width_px):
        # Bearing for this column: bearing_deg ± half_fov
        col_frac   = col / (width_px - 1)          # 0=left, 1=right
        col_bear   = (bearing_deg - half_fov + col_frac * fov_deg + 360) % 360
        max_angle  = -90.0   # highest terrain angle found so far for this ray

        for dist_nm in distances_nm:
            pt_lat, pt_lon = offset_point(ac_lat, ac_lon, col_bear, dist_nm)
            try:
                terr_m = get_elevations_m([(pt_lat, pt_lon)])[0]
            except Exception:
                continue
            dist_m = dist_nm * 1852.0
            # Angle above horizontal from eye to terrain surface
            dh_m   = terr_m - (ac_elev_m + eye_agl_m)
            angle  = math.degrees(math.atan2(dh_m, dist_m))
            if angle > max_angle:
                max_angle = angle

        # Convert highest angle to pixel row
        top_y = _angle_to_y(max_angle)
        top_y = max(0, min(height_px, top_y))
        sky_profile[col] = top_y

    # ---- fill terrain ----
    # terrain colour: rocky grey-brown — lighter at ridgeline, darker/warmer towards base
    for col in range(width_px):
        top_y = sky_profile[col]
        for y in range(top_y, height_px):
            depth = (y - top_y) / max(1, height_px - top_y)  # 0=ridge, 1=base
            # ridge: cool grey (110,100,90); base: warm dark brown (55,45,35)
            r = int(110 - depth * 55)
            g = int(100 - depth * 55)
            b = int(90  - depth * 55)
            draw.point((col, y), fill=(r, g, b, 255))

    # Slightly brighten the ridge line for contrast
    for col in range(width_px):
        ty = sky_profile[col]
        for dy in range(3):
            y = ty + dy
            if 0 <= y < height_px:
                old = img.getpixel((col, y))
                bright = tuple(min(255, v + 50) for v in old[:3]) + (255,)
                img.putpixel((col, y), bright)

    # ---- overlays ----
    draw = ImageDraw.Draw(img)   # re-create after putpixel calls

    # Horizon dashed line
    dash_col = (255, 255, 255, 100)
    for x in range(0, width_px, 8):
        if x % 16 < 8:
            draw.line([(x, horizon_y), (min(x + 7, width_px - 1), horizon_y)],
                      fill=dash_col, width=1)

    # Centre bearing tick at bottom
    cx = width_px // 2
    draw.line([(cx, height_px - 1), (cx, height_px - 12)],
              fill=(255, 220, 0, 220), width=2)
    draw.line([(cx - 15, height_px - 6), (cx + 15, height_px - 6)],
              fill=(255, 220, 0, 180), width=1)

    # Bearing label at bottom centre
    try:
        _font = ImageFont.truetype("arial.ttf", 11)
    except Exception:
        _font = ImageFont.load_default()
    bear_lbl = f"{int(round(bearing_deg))}°"
    try:
        bb = _font.getbbox(bear_lbl)
        lw = bb[2] - bb[0]
    except Exception:
        lw = len(bear_lbl) * 7
    draw.text((cx - lw // 2, height_px - 26), bear_lbl,
              fill=(255, 220, 0, 230), font=_font)

    # Left/right bearing ticks
    for side, sign in (("L", -1), ("R", 1)):
        sx = cx + sign * (width_px // 2 - 6)
        edge_bear = (bearing_deg + sign * half_fov + 360) % 360
        elbl = f"{int(round(edge_bear))}°"
        try:
            bb2 = _font.getbbox(elbl); ew = bb2[2] - bb2[0]
        except Exception:
            ew = len(elbl) * 7
        ex = max(2, min(sx - ew // 2, width_px - ew - 2))
        draw.text((ex, height_px - 26), elbl, fill=(200, 220, 255, 180), font=_font)

    # ---- peak annotations: visible summit arrows ----
    try:
        peak_radius_m = int(max_range_nm * 1852)
        all_cands = _query_overpass(ac_lat, ac_lon, radius_m=peak_radius_m)
        peaks_in_fov = []
        for cand in all_cands:
            tags = cand.get("tags", {})
            if tags.get("natural") != "peak":
                continue
            pk_lat = float(cand["lat"])
            pk_lon = float(cand["lon"])
            dist_nm = gc_distance_nm(ac_lat, ac_lon, pk_lat, pk_lon)
            if dist_nm < 0.3 or dist_nm > max_range_nm:
                continue
            bear_to_pk = bearing_to_destination(ac_lat, ac_lon, pk_lat, pk_lon)
            # Signed angular offset from centre bearing: negative=left, positive=right
            rel = (bear_to_pk - bearing_deg + 360) % 360
            if rel > 180:
                rel -= 360
            if abs(rel) > half_fov:
                continue
            # Screen column (left edge = -half_fov, right edge = +half_fov)
            col = int(round((rel + half_fov) / fov_deg * (width_px - 1)))
            col = max(0, min(width_px - 1, col))
            # Elevation angle from aircraft eye to peak summit
            pk_elev_m = get_elevations_m([(pk_lat, pk_lon)])[0]
            dist_m = dist_nm * 1852.0
            dh_m = pk_elev_m - (ac_elev_m + eye_agl_m)
            pk_angle = math.degrees(math.atan2(dh_m, dist_m))
            # Ridge angle at this column (inverse of _angle_to_y)
            ridge_y = sky_profile[col]
            ridge_angle = vfov_deg * (0.5 - ridge_y / max(1, height_px - 1)) * 2
            # Only annotate if peak is not hidden behind the closer terrain
            # (its elevation angle must be within 1.5° below the local ridgeline)
            if pk_angle < ridge_angle - 1.5:
                continue
            pk_name = cand.get("name", "")
            # Elevation in feet: prefer OSM ele tag, fall back to DEM height
            ele_tag = tags.get("ele")
            try:
                ele_m = float(ele_tag) if ele_tag else pk_elev_m
                ele_ft = int(round(ele_m * 3.28084))
            except Exception:
                ele_ft = None
            peaks_in_fov.append((col, ridge_y, pk_name, dist_nm, pk_angle, ele_ft))

        # Sort by distance so closer peaks paint last (on top)
        peaks_in_fov.sort(key=lambda t: -t[3])

        # Simple de-overlap: suppress peaks whose screen column is too close to
        # an already-drawn peak (avoid pile-up of labels in narrow ridgelines)
        used_cols: list[int] = []
        min_col_gap = 50   # pixels

        for col, ridge_y, pk_name, dist_nm, pk_ang, ele_ft in peaks_in_fov:
            if any(abs(col - uc) < min_col_gap for uc in used_cols):
                continue
            used_cols.append(col)

            # Upward-pointing filled triangle just above the ridgeline
            arr_base_y = max(2, ridge_y - 3)
            arr_tip_y  = max(0, arr_base_y - 14)
            draw.polygon(
                [(col, arr_tip_y), (col - 5, arr_base_y), (col + 5, arr_base_y)],
                fill=(255, 215, 0, 230),
            )

            # Two-line label: name (line 1) + elevation in ft (line 2)
            name_lbl = pk_name if len(pk_name) <= 16 else pk_name[:15] + "…"
            ele_lbl  = f"{ele_ft:,} ft".replace(",", "\u202f") if ele_ft is not None else ""
            try:
                bb_n = _font.getbbox(name_lbl)
                nw, nh = bb_n[2] - bb_n[0], bb_n[3] - bb_n[1]
            except Exception:
                nw, nh = len(name_lbl) * 6, 10
            try:
                bb_e = _font.getbbox(ele_lbl)
                ew2, eh = bb_e[2] - bb_e[0], bb_e[3] - bb_e[1]
            except Exception:
                ew2, eh = len(ele_lbl) * 6, 10

            box_w   = max(nw, ew2) + 6
            box_h   = nh + eh + 6
            # Position label box above triangle tip, stem gap = 4px
            stem_gap = 4
            box_bot = arr_tip_y - stem_gap
            box_top = box_bot - box_h
            box_top = max(1, box_top)
            box_bot = box_top + box_h
            bx = max(2, min(col - box_w // 2, width_px - box_w - 2))

            # Vertical stem line from box bottom to arrow tip
            draw.line([(col, box_bot), (col, arr_tip_y)],
                      fill=(255, 215, 0, 180), width=1)

            # Semi-transparent dark background box
            from PIL import Image as _PILImg2
            box_ov = _PILImg2.new("RGBA", (box_w, box_h), (10, 10, 10, 170))
            img.paste(box_ov, (bx, box_top), box_ov)
            draw = ImageDraw.Draw(img)  # refresh draw after paste

            # Name text (white, centred in box)
            nx = bx + (box_w - nw) // 2
            draw.text((nx, box_top + 2), name_lbl, fill=(255, 255, 255, 240), font=_font)
            # Elevation text (yellow, centred below)
            ex2 = bx + (box_w - ew2) // 2
            draw.text((ex2, box_top + 2 + nh + 1), ele_lbl, fill=(255, 215, 80, 230), font=_font)
    except Exception:
        pass   # never let peak annotation crash the silhouette render

    return img.convert("RGB")


def draw_silhouette_page(c: "canvas.Canvas", legs: list, data: "FlightData",
                         page_w: float, page_h: float) -> None:
    """
    Draw a new PDF page of forward-view terrain silhouettes, one per leg tile.
    Layout mirrors the OSM/satellite tile pages: 4 cols × 3 rows.
    """
    try:
        from PIL import Image
        import io as _io3
        from reportlab.lib.utils import ImageReader
    except ImportError:
        return

    cols, rows_per_page = 4, 3
    MARGIN_X = 5 * mm
    MARGIN_Y = 8 * mm
    GAP_X    = 2 * mm
    GAP_Y    = 3 * mm
    tile_w   = (page_w - 2 * MARGIN_X - (cols - 1) * GAP_X) / cols
    tile_h   = (page_h - 2 * MARGIN_Y - (rows_per_page - 1) * GAP_Y) / rows_per_page

    real_legs = [l for l in legs
                 if not l.get("is_waypoint") and not l.get("is_descent")]

    # Same dest-tile logic as draw_leg_tiles_page
    if real_legs:
        last_leg = real_legs[-1]
        dest     = data.destination
        dest_lat = dest["lat"]
        dest_lon = dest["lon"]
        last_lat = last_leg.get("lat", dest_lat)
        last_lon = last_leg.get("lon", dest_lon)
        look_ahead_nm = TILE_DISPLAY_NM * (1.0 - TILE_CHKPT_Y_FRAC)
        dist_to_dest  = gc_distance_nm(last_lat, last_lon, dest_lat, dest_lon)
        dest_tile = {
            "lat": dest_lat, "lon": dest_lon,
            "segment_track_true": last_leg.get("segment_track_true", 0),
            "landmark": dest.get("name") or dest.get("icao", ""),
            "leg_num": last_leg.get("leg_num", 0),
            "_cumulative_min": last_leg.get("_cumulative_min", 0),
            "elapsed_min":     last_leg.get("elapsed_min", 0),
            "is_dest": True,
            "min_alt_ft": last_leg.get("min_alt_ft", 5000),
        }
        if dist_to_dest >= 0.05:
            if dist_to_dest <= look_ahead_nm:
                real_legs = real_legs[:-1] + [dest_tile]
            else:
                real_legs = real_legs + [dest_tile]

    total_tiles  = len(real_legs)
    pos_in_page  = 0
    page_started = False

    for tile_idx, leg in enumerate(real_legs):
        if pos_in_page == 0:
            if page_started:
                c.showPage()
            page_started = True
            c.saveState()
            c.setFont("Helvetica-Bold", 9)
            route_str = (f"{data.origin.get('icao','')} → "
                         f"{data.destination.get('icao','')}  |  "
                         f"{data.cruise_speed_ias:.0f} kt  |  "
                         f"{data.fuel_consumption_gph:.1f} GPH")
            c.drawString(MARGIN_X, page_h - 5 * mm, "Vista frontal (silueta de terreno)")
            c.drawRightString(page_w - MARGIN_X, page_h - 5 * mm, route_str)
            c.restoreState()

        col = pos_in_page % cols
        row = pos_in_page // cols
        tile_x = MARGIN_X + col * (tile_w + GAP_X)
        tile_y = page_h - MARGIN_Y - 7 * mm - (row + 1) * tile_h - row * GAP_Y

        leg_lat  = leg.get("lat")
        leg_lon  = leg.get("lon")
        bearing  = leg.get("segment_track_true", 0)
        leg_num  = leg.get("leg_num", tile_idx + 1)
        cum_min  = int(round(leg.get("_cumulative_min", leg.get("elapsed_min", 0))))
        alt_ft   = leg.get("min_alt_ft", 5000)
        is_dest  = leg.get("is_dest", False)

        if leg_lat is None or leg_lon is None:
            pos_in_page = (pos_in_page + 1) % (cols * rows_per_page)
            continue

        label = "DEST" if is_dest else f"T+{cum_min}min"
        print(f"  [silhouette {tile_idx+1}/{total_tiles}] ({leg_lat:.3f},{leg_lon:.3f}) "
              f"brg={bearing:.0f}° → {label}…", end="", flush=True)

        sil = _build_silhouette_image(leg_lat, leg_lon, bearing, alt_ft)
        if sil is None:
            print(" sin imagen", flush=True)
            c.setStrokeColor(colors.lightgrey)
            c.rect(tile_x, tile_y, tile_w, tile_h)
        else:
            # Header bar drawn directly on the PIL image
            try:
                from PIL import ImageDraw as _ID2, ImageFont as _IF2
                hdr_h = 22
                hdr_img = Image.new("RGBA", sil.size, (0, 0, 0, 0))
                sil = sil.convert("RGBA")
                hdr_ov = Image.new("RGBA", (sil.width, hdr_h),
                                   (0, 100, 0, 190) if is_dest else (30, 30, 30, 180))
                sil.paste(hdr_ov, (0, 0), hdr_ov)
                _d = _ID2.Draw(sil)
                try:
                    _f = _IF2.truetype("arial.ttf", 11)
                except Exception:
                    _f = _IF2.load_default()
                _d.text((6, 5), label, fill=(255, 255, 255, 240), font=_f)
                if not is_dest:
                    _d.text((sil.width - 32, 5), f"#{leg_num}",
                            fill=(255, 220, 0, 240), font=_f)
                sil = sil.convert("RGB")
            except Exception:
                pass

            bio = _io3.BytesIO()
            sil.save(bio, format="PNG")
            bio.seek(0)
            c.drawImage(ImageReader(bio), tile_x, tile_y,
                        width=tile_w, height=tile_h, preserveAspectRatio=False)
            c.saveState()
            c.setStrokeColor(colors.HexColor("#888888"))
            c.setLineWidth(0.4)
            c.rect(tile_x, tile_y, tile_w, tile_h, stroke=1, fill=0)
            c.restoreState()
            print(" OK", flush=True)

        pos_in_page = (pos_in_page + 1) % (cols * rows_per_page)


def draw_leg_tiles_page(c: canvas.Canvas, legs: list, data: "FlightData",
                        page_w: float, page_h: float,
                        page_label: str = "Fichas de tramo",
                        bg_mode: str = "osm") -> None:
    """
    Draw A4-landscape page(s) of leg minimap tiles, 4 columns × 3 rows per page.
    Each tile is ~70mm × 67mm representing 10 NM at 1:250,000 scale, track-up.
    bg_mode: "osm" for standard chart tiles; "satellite" for ESRI imagery.
    """
    try:
        from PIL import Image
        import io as _io2
        from reportlab.lib.utils import ImageReader
    except ImportError:
        print("  [warn] Pillow not installed – skipping leg tile pages.", file=sys.stderr)
        return

    cols, rows_per_page = 4, 3
    MARGIN_X = 5 * mm
    MARGIN_Y = 8 * mm
    GAP_X    = 2 * mm
    GAP_Y    = 3 * mm
    tile_w   = (page_w - 2 * MARGIN_X - (cols - 1) * GAP_X) / cols
    tile_h   = (page_h - 2 * MARGIN_Y - (rows_per_page - 1) * GAP_Y) / rows_per_page

    # Filter to real navigation legs (no waypoint markers, no descent rows)
    real_legs = [l for l in legs
                 if not l.get("is_waypoint") and not l.get("is_descent")]

    # Ensure the last tile is the actual destination.
    #
    # The last computed leg is at int(seg_min/LEG_MINUTES) × LEG_MINUTES minutes
    # which is typically ~6-15 % short of the destination.  Two strategies:
    #
    #  A) REPLACE the last leg with the destination when it is already within
    #     the tile's look-ahead window (≤ TILE_DISPLAY_NM×(1−TILE_CHKPT_Y_FRAC)).
    #     This avoids a near-duplicate tile showing virtually the same area twice.
    #
    #  B) APPEND a fresh destination tile when the last leg is farther away
    #     (destination would be off the top of the tile or barely visible).
    if real_legs:
        last_leg  = real_legs[-1]
        dest      = data.destination
        dest_lat  = dest["lat"]
        dest_lon  = dest["lon"]
        last_lat  = last_leg.get("lat", dest_lat)
        last_lon  = last_leg.get("lon", dest_lon)

        # How many NM ahead of the checkpoint are visible in the tile
        look_ahead_nm = TILE_DISPLAY_NM * (1.0 - TILE_CHKPT_Y_FRAC)
        dist_to_dest  = gc_distance_nm(last_lat, last_lon, dest_lat, dest_lon)

        dest_tile = {
            "lat":                dest_lat,
            "lon":                dest_lon,
            "segment_track_true": last_leg.get("segment_track_true", 0),
            "lm_lat":             dest_lat,
            "lm_lon":             dest_lon,
            "landmark":           dest.get("name") or dest.get("icao", ""),
            "leg_num":            last_leg.get("leg_num", 0),
            "_cumulative_min":    last_leg.get("_cumulative_min", 0),
            "elapsed_min":        last_leg.get("elapsed_min", 0),
            "is_dest":            True,
        }

        if dist_to_dest < 0.05:
            # Already at destination (e.g. last leg == dest exactly)
            pass
        elif dist_to_dest <= look_ahead_nm:
            # Strategy A: destination is within look-ahead → replace last leg
            real_legs = real_legs[:-1] + [dest_tile]
        else:
            # Strategy B: destination is beyond look-ahead → append extra tile
            dest_tile["leg_num"] = len(real_legs) + 1
            real_legs = real_legs + [dest_tile]

    tiles_per_page = cols * rows_per_page
    total_tiles = len(real_legs)

    for tile_idx, leg in enumerate(real_legs):
        page_idx = tile_idx // tiles_per_page
        pos_in_page = tile_idx % tiles_per_page

        if pos_in_page == 0:
            if tile_idx > 0:
                c.showPage()
            c.saveState()
            c.setFont("Helvetica-Bold", 7)
            c.setFillColor(colors.grey)
            route_str = (f"{data.origin['icao']} \u2192 {data.destination['icao']}  "
                         f"\u2013 {page_label}")
            c.drawString(MARGIN_X, page_h - 5 * mm, route_str)
            page_num = page_idx + 1
            c.drawRightString(page_w - MARGIN_X, page_h - 5 * mm,
                              f"p\u00e1g. {page_num}")
            c.restoreState()

        col = pos_in_page % cols
        row = pos_in_page // cols

        # ReportLab origin is bottom-left; y = 0 at bottom
        tile_x = MARGIN_X + col * (tile_w + GAP_X)
        tile_y = page_h - MARGIN_Y - 7 * mm - (row + 1) * tile_h - row * GAP_Y

        leg_lat  = leg.get("lat")
        leg_lon  = leg.get("lon")
        bearing  = leg.get("segment_track_true", 0)
        lm_lat   = leg.get("lm_lat", leg_lat)
        lm_lon   = leg.get("lm_lon", leg_lon)
        lm_label = leg.get("landmark", "")
        leg_num  = leg.get("leg_num", tile_idx + 1)
        cum_min  = int(round(leg.get("_cumulative_min", leg.get("elapsed_min", 0))))

        if leg_lat is None or leg_lon is None:
            continue

        is_dest  = leg.get("is_dest", False)
        print(f"  [tile {tile_idx+1}/{total_tiles}] ({leg_lat:.3f},{leg_lon:.3f}) "
              f"brg={bearing:.0f}° → {'DEST' if is_dest else f'T+{cum_min}min'}…",
              end="", flush=True)
        leg_alt_ft = leg.get("min_alt_ft", 5000)
        img = _build_tile_image(leg_lat, leg_lon, bearing, lm_lat, lm_lon,
                     lm_label, leg.get("lm_type", "poi"),
                     leg_num, cum_min, is_dest=is_dest,
                     bg_mode=bg_mode, alt_ft=leg_alt_ft,
                     extra_places=leg.get("lm_places", []))
        if img is None:
            print(" sin imagen", flush=True)
            c.setStrokeColor(colors.lightgrey)
            c.rect(tile_x, tile_y, tile_w, tile_h)
            c.setFont("Helvetica", 6)
            c.setFillColor(colors.grey)
            c.drawCentredString(tile_x + tile_w / 2, tile_y + tile_h / 2, "sin imagen")
            continue

        bio = _io2.BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        c.drawImage(ImageReader(bio), tile_x, tile_y, width=tile_w, height=tile_h,
                    preserveAspectRatio=False)

        # Thin border around tile
        c.saveState()
        c.setStrokeColor(colors.HexColor("#888888"))
        c.setLineWidth(0.4)
        c.rect(tile_x, tile_y, tile_w, tile_h, stroke=1, fill=0)
        c.restoreState()

        print(" OK", flush=True)




def generate_pdf(output_path: str,
                 data: "FlightData",
                 one_face: bool = False) -> None:
    """
    Genera un PDF duplex de dos paginas en A4 apaisado:
      Pagina 1 – Frontal: Hoja de Vuelo VFR en el panel A5 izquierdo
      Pagina 2 – Dorso  : Panel de Frecuencias (rotado 180 grados) en el panel A5 derecho
                          (queda a la izquierda al girar la hoja y doblarla)
    """
    origin      = data.origin
    destination = data.destination
    legs        = data.legs

    # Deduplicar alternativas en ruta por ICAO (omitir filas de waypoint)
    seen: set[str] = set()
    enroute: list[dict] = []
    for leg in legs:
        if leg.get("is_waypoint"):
            continue
        icao = leg.get("alt_icao", "\u2014")
        if icao not in seen and icao not in ("\u2014", origin["icao"], destination["icao"]):
            seen.add(icao)
            enroute.append(leg)

    c = canvas.Canvas(output_path, pagesize=landscape(A4))
    c.setTitle(f"Hoja de Vuelo VFR \u2013 {origin['icao']} a {destination['icao']}")
    c.setAuthor("VFROnePager")
    c.setSubject("Planificaci\u00f3n de Vuelo VFR")

    if one_face:
        # Draw both panels side-by-side on a single A4 landscape page
        wind_deferred, overflow = draw_front_panel(c, data, x_offset=0)
        draw_back_panel(c, data, enroute, x_offset=PANEL_W, rotate=False)
        draw_fold_guide(c)
        c.showPage()
        if overflow or (wind_deferred and data.wind_effect):
            c.saveState()
            y3 = PAGE_H - MARGIN
            for part in overflow:
                _pw, ph = part.wrapOn(c, INNER_W, y3 - MARGIN)
                part.drawOn(c, MARGIN, y3 - ph)
                y3 -= ph + 2 * mm
            if wind_deferred and data.wind_effect:
                _draw_wind_box(c, data.wind_effect, data.tc, data.mh,
                               box_x=MARGIN, box_y=max(MARGIN, y3 - 18 * mm))
            c.restoreState()
            c.showPage()
    else:
        wind_deferred, overflow = draw_front_panel(c, data, x_offset=0)
        draw_fold_guide(c)
        c.showPage()

        draw_back_panel(c, data, enroute, x_offset=PANEL_W, rotate=False)
        draw_fold_guide(c)
        c.showPage()

        if overflow or (wind_deferred and data.wind_effect):
            c.saveState()
            y3 = PAGE_H - MARGIN
            for part in overflow:
                _pw, ph = part.wrapOn(c, INNER_W, y3 - MARGIN)
                part.drawOn(c, MARGIN, y3 - ph)
                y3 -= ph + 2 * mm
            if wind_deferred and data.wind_effect:
                _draw_wind_box(c, data.wind_effect, data.tc, data.mh,
                               box_x=MARGIN, box_y=max(MARGIN, y3 - 18 * mm))
            c.restoreState()
            c.showPage()

    # Leg minimap tiles page(s) — OSM chart
    print("[+] Generando fichas de tramo (tiles OSM)…")
    draw_leg_tiles_page(c, data.legs, data, PAGE_W, PAGE_H)

    # Satellite imagery page(s) — ESRI World Imagery
    c.showPage()
    print("[+] Generando fichas de satélite (ESRI World Imagery)…")
    draw_leg_tiles_page(c, data.legs, data, PAGE_W, PAGE_H,
                        page_label="Imágenes de satélite",
                        bg_mode="satellite")

    # Terrain silhouette page(s) — forward-view from each checkpoint
    c.showPage()
    print("[+] Generando siluetas de terreno (vista frontal)…")
    draw_silhouette_page(c, data.legs, data, PAGE_W, PAGE_H)

    c.save()
    print(f"\n  PDF guardado en: {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_via(via_str: str) -> dict:
    """
    Parse a single --via argument string into a waypoint dict.
    Accepted formats (case-insensitive):
      NAME,lat,lon        e.g.  LONDON,51.514,-0.115
      [NAME,lat,lon]      brackets tolerated
    Returns {'name': str, 'lat': float, 'lon': float}.
    Raises ValueError on bad input.
    """
    raw = via_str.strip().strip("[]")
    parts = raw.split(",")
    if len(parts) != 3:
        raise ValueError(
            f"--via waypoint must be NAME,lat,lon  (got: {via_str!r})"
        )
    name, lat_s, lon_s = parts
    try:
        return {"name": name.strip().upper(), "lat": float(lat_s), "lon": float(lon_s),
                "elevation_ft": 0.0}
    except ValueError:
        raise ValueError(
            f"--via lat/lon must be numeric  (got: {via_str!r})"
        )


def main() -> None:
    """Entry point – parse CLI arguments, fetch data, compute, generate PDF."""
    parser = argparse.ArgumentParser(
        prog="vfr_onepager",
        description="Generate a VFR trip one-pager as a duplex A4-landscape PDF.",
    )
    parser.add_argument("origin_icao",      nargs='?', help="ICAO code of the departure airport")
    parser.add_argument("destination_icao", nargs='?', help="ICAO code of the destination airport")
    parser.add_argument(
        "--pairs", action="append", default=None,
        help=(
            "Comma-separated list (or repeat) of ORIG:DEST pairs to process in batch,\n"
            "e.g. --pairs LEPP:LERJ,LEPP:LESO or --pairs LEPP:LERJ --pairs LEBL:LEMG.\n"
            "When provided the script will generate one PDF per pair and exit."
        ),
    )
    parser.add_argument("cruise_speed_ias", type=float,
                        help="Cruise speed in knots IAS")
    parser.add_argument("fuel_consumption_gph", type=float,
                        help="Fuel consumption in US gallons per hour")
    parser.add_argument("-o", "--output",   default=None,
                        help="Output PDF path (default: <ORIG>_<DEST>_vfr.pdf)")
    parser.add_argument(
        "--via", metavar="NAME,LAT,LON", action="append", default=[],
        help=(
            "Intermediate waypoint in format NAME,lat,lon  "
            "(e.g. LONDON,51.514,-0.115).  Repeat for multiple waypoints "
            "in order: departure → WP1 → WP2 → … → destination."
        ),
    )
    parser.add_argument(
        "--one-face", action="store_true", default=False,
        help=("Render both front and back panels side-by-side on a single A4 face "
              "(useful for single-sided printing / preview)."),
    )
    parser.add_argument(
        "--wind", metavar="SPEED/DIR", default=None,
        help=(
            "Average en-route wind for projection, e.g. '15/270' meaning 15 kt from 270°. "
            "Adds a projected wind-effect box at the bottom of the trip page. "
            "Marked as estimated – real wind can change at any time."
        ),
    )
    parser.add_argument(
        "--leg-minutes", type=float, default=None,
        help=("Leg interval in minutes for route segmentation. If omitted, uses the default "
              "value defined in the script (5). Use non-integer values for finer control."),
    )
    parser.add_argument(
        "--terrain-buffer", type=float, default=300.0, metavar="FT",
        help=("Terrain clearance buffer in feet used to compute safe altitudes. "
              "Default: %(default)s ft (use 300 for recommended minimum)."),
    )
    parser.add_argument(
        "--climb-rate", type=float, default=500.0,
        help=("Climb rate in ft/min used to compute first-leg climb time to cruise altitude. "
              "Default: %(default)s ft/min."),
    )
    parser.add_argument(
        "--departure-runway", type=float, default=None, metavar="HDG",
        help=("Departure runway heading in degrees magnetic (e.g. 330). "
              "When given, adds a 2-minute straight-out climb row at the top of the trip "
              "table (runway heading, until 1000 ft AGL) before turning to cruise track."),
    )
    args = parser.parse_args()

    # If --pairs supplied, spawn a separate run for each ORIG:DEST pair using the
    # same CLI options (except --pairs). This avoids refactoring the whole main
    # flow while adding batch capability.
    if args.pairs:
        import subprocess, sys, os

        def _build_base_cmd(pair_orig: str, pair_dest: str) -> list:
            cmd = [sys.executable, os.path.abspath(__file__), pair_orig, pair_dest,
                   str(args.cruise_speed_ias), str(args.fuel_consumption_gph)]
            if args.output:
                # avoid clobbering one output for multiple pairs; let user specify
                # per-pair output by omitting --output or by providing template
                cmd += ["-o", args.output]
            if args.via:
                for v in args.via:
                    cmd += ["--via", v]
            if args.one_face:
                cmd.append("--one-face")
            if args.wind:
                cmd += ["--wind", args.wind]
            if args.leg_minutes is not None:
                cmd += ["--leg-minutes", str(args.leg_minutes)]
            if args.terrain_buffer is not None:
                cmd += ["--terrain-buffer", str(args.terrain_buffer)]
            if args.climb_rate is not None:
                cmd += ["--climb-rate", str(args.climb_rate)]
            if args.departure_runway is not None:
                cmd += ["--departure-runway", str(args.departure_runway)]
            return cmd

        # Expand any comma-separated entries and validate ORIG:DEST format
        pairs_expanded: list[tuple[str, str]] = []
        for chunk in args.pairs:
            for item in chunk.split(','):
                item = item.strip()
                if not item:
                    continue
                if ':' not in item:
                    parser.error(f"Invalid pair '{item}'; expected ORIG:DEST")
                o, d = item.split(':', 1)
                pairs_expanded.append((o.upper().strip(), d.upper().strip()))

        # Launch a subprocess per pair (forward) and its automatic return (reverse).
        for orig, dest in pairs_expanded:
            print(f"[batch] Generating for {orig}→{dest} …")
            cmd_fwd = _build_base_cmd(orig, dest)
            res = subprocess.run(cmd_fwd)
            if res.returncode != 0:
                print(f"[batch] {orig}→{dest} failed (exit {res.returncode})", file=sys.stderr)
            else:
                print(f"[batch] {orig}→{dest} done")

            # Now generate the return route dest->orig. If the user supplied a
            # specific --output filename, synthesize a separate filename for the
            # return by appending "_return" before the extension. Otherwise let
            # the child process choose its default name.
            print(f"[batch] Generating return for {dest}→{orig} …")
            if args.output:
                base, ext = os.path.splitext(args.output)
                ret_out = f"{base}_return{ext}"
                cmd_rev = _build_base_cmd(dest, orig)
                cmd_rev += ["-o", ret_out]
            else:
                cmd_rev = _build_base_cmd(dest, orig)

            res2 = subprocess.run(cmd_rev)
            if res2.returncode != 0:
                print(f"[batch] {dest}→{orig} failed (exit {res2.returncode})", file=sys.stderr)
            else:
                print(f"[batch] {dest}→{orig} done")
        return

    if not args.origin_icao or not args.destination_icao:
        parser.error("origin and destination ICAO required when --pairs is not used")

    origin_icao  = args.origin_icao.upper().strip()
    dest_icao    = args.destination_icao.upper().strip()
    cruise_kts   = args.cruise_speed_ias
    fuel_gph     = args.fuel_consumption_gph
    output_path  = args.output or f"{origin_icao}_{dest_icao}_vfr.pdf"

    # Allow runtime override of the default leg interval defined at module scope
    if args.leg_minutes is not None:
        global LEG_MINUTES
        LEG_MINUTES = float(args.leg_minutes)
        print(f"  Leg interval: {LEG_MINUTES} min")

    # Allow runtime override of the terrain buffer (default 300 ft)
    if hasattr(args, 'terrain_buffer'):
        global TERRAIN_BUFFER_FT
        TERRAIN_BUFFER_FT = float(args.terrain_buffer)
        print(f"  Terrain buffer: {TERRAIN_BUFFER_FT:.0f} ft")

    # Parse optional manual wind override (--wind SPEED/DIR)
    wind_override_speed: Optional[float] = None
    wind_override_dir:   Optional[float] = None
    if args.wind:
        try:
            ws_s, wd_s = args.wind.split("/")
            wind_override_speed = float(ws_s.strip())
            wind_override_dir   = float(wd_s.strip()) % 360
        except ValueError:
            parser.error("--wind must be SPEED/DIR, e.g. 15/270")

    # Parse intermediate waypoints
    waypoints: list[dict] = []
    for via_raw in args.via:
        waypoints.append(_parse_via(via_raw))

    via_str = "  →  ".join(wp["name"] for wp in waypoints) if waypoints else "(direct)"
    print(f"\n{'='*60}")
    print(f"  Generador de Hoja de Vuelo VFR")
    print(f"  {origin_icao} \u2192 {dest_icao}  |  {cruise_kts} kt  |  {fuel_gph} GPH")
    if waypoints:
        print(f"  Vía: {via_str}")
    print(f"{'='*60}\n")

    # 1. Datos de aeropuertos --------------------------------------------------
    print("[1/5] Buscando aeropuertos...")
    origin      = lookup_airport(origin_icao)
    destination = lookup_airport(dest_icao)
    print(f"  Origen:   {origin['name']}  ({origin['lat']:.4f}, {origin['lon']:.4f})  "
          f"Elev: {origin['elevation_ft']:.0f} ft")
    if waypoints:
        for wp in waypoints:
            print(f"  Vía:      {wp['name']}  ({wp['lat']:.4f}, {wp['lon']:.4f})")
    print(f"  Destino:  {destination['name']}  ({destination['lat']:.4f}, "
          f"{destination['lon']:.4f})  Elev: {destination['elevation_ft']:.0f} ft")

    # 2. Frecuencias -----------------------------------------------------------
    print("[2/5] Obteniendo frecuencias...")
    origin_freqs = get_airport_frequencies(origin["id"], origin_icao)
    dest_freqs   = get_airport_frequencies(destination["id"], dest_icao)

    # 3. Calculo de navegacion -------------------------------------------------
    print("[3/5] Calculando navegacion...")
    # Total route distance: sum of all segments
    route_stops_nav = [origin] + waypoints + [destination]
    total_nm = sum(
        gc_distance_nm(route_stops_nav[k]["lat"], route_stops_nav[k]["lon"],
                       route_stops_nav[k+1]["lat"], route_stops_nav[k+1]["lon"])
        for k in range(len(route_stops_nav) - 1)
    )
    # Initial bearing and magnetic heading from origin
    tc      = bearing_to_destination(origin["lat"], origin["lon"],
                                     route_stops_nav[1]["lat"], route_stops_nav[1]["lon"])
    mag_var = magnetic_variation(origin["lat"], origin["lon"])
    mh      = (tc - mag_var + 360) % 360
    ete_min = (total_nm / cruise_kts) * 60
    fuel_req = fuel_gph * (ete_min / 60)

    print(f"  Distancia total : {total_nm:.1f} NM")
    print(f"  Rumbo inicial   : {tc:.1f}\u00b0V  \u2192  {mh:.1f}\u00b0M")
    print(f"  Var. magnetica  : {mag_var:+.1f}\u00b0")
    print(f"  TEE total       : {ete_min:.0f} min")
    print(f"  Combustible req : {fuel_req:.1f} gal")

    # 4. Tramos de ruta --------------------------------------------------------
    print("[4/5] Construyendo tramos (terreno + referencias – puede tardar unos minutos)...")
    legs = build_legs(origin, destination, cruise_kts, fuel_gph, mag_var,
                      waypoints=waypoints)

    # 4b. Altitud de crucero recomendada --------------------------------------
    cruise_alt = recommended_cruise_altitude(legs)
    real_legs  = [l for l in legs if not l.get("is_waypoint")]
    max_terr   = max((l["max_terrain_ft"] for l in real_legs), default=0)
    print(f"  Altitud de crucero recomendada: {cruise_alt} ft  "
          f"(terreno max: {max_terr:.0f} ft)")

    # Adjust first leg duration to represent time to reach cruise altitude.
    # If --departure-runway is given, prepend 2 min for straight-out climb to
    # 1000 ft AGL, then the remaining climb time is added to the first nav leg.
    DEPARTURE_STRAIGHT_MIN = 2.0
    dep_rwy_hdg = getattr(args, "departure_runway", None)

    try:
        climb_rate_fpm = float(args.climb_rate)
    except Exception:
        climb_rate_fpm = 500.0
    origin_elev = origin.get("elevation_ft", 0)
    if cruise_alt and climb_rate_fpm and real_legs:
        alt_to_gain = max(0.0, cruise_alt - origin_elev)
        climb_time_min = alt_to_gain / float(climb_rate_fpm) if climb_rate_fpm > 0 else 0.0

        # Use the first-leg duration heuristic (LEG_MINUTES * CLIMB_SPEED_FACTOR)
        first_leg_min = round(LEG_MINUTES * CLIMB_SPEED_FACTOR, 1)
        # Ensure the first-leg elapsed reflects climb needs (but otherwise keep regular segmentation)
        total_overhead_min = max(climb_time_min, first_leg_min)

        if total_overhead_min > 0:
            # find first real leg index
            first_idx = next((i for i, l in enumerate(legs) if not l.get("is_waypoint")), None)
            if first_idx is not None:
                first_leg = legs[first_idx]
                old_elapsed = float(first_leg.get("elapsed_min", 0.0))
                delta = total_overhead_min - old_elapsed
                # update first leg elapsed and fuel burned (first-leg models climb)
                first_leg["elapsed_min"] = total_overhead_min
                first_leg["fuel_burned_gal"] = round(fuel_gph * (total_overhead_min / 60.0), 1)
                # update cumulative mins for all subsequent legs/rows
                for j in range(first_idx, len(legs)):
                    if "_cumulative_min" in legs[j]:
                        legs[j]["_cumulative_min"] = legs[j].get("_cumulative_min", 0.0) + delta
                # recompute ete_min and fuel_req
                real_legs_after = [l for l in legs if not l.get("is_waypoint")]
                ete_min = max(float(l.get("_cumulative_min", 0.0)) for l in real_legs_after)
                fuel_req = fuel_gph * (ete_min / 60.0)
                print(f"  Ajustado primer tramo (subida): {total_overhead_min:.1f} min  "
                      f"(delta {delta:+.1f} min). Nuevo TEE: {ete_min:.1f} min")

    # 4c. Tramo de descenso ---------------------------------------------------
    # Algorithm:
    #   1. Compute when to START descending (500 ft/min, dest elev + 1000 ft AGL, 2-min buffer).
    #   2. Walk through legs in the descent zone in time order.
    #   3. Floor at each leg = FORWARD terrain (next segment) + 300 ft, because the
    #      leg's own max_terrain_ft is the highest point already crossed at cruise altitude.
    #      What matters for descent is what's STILL AHEAD.
    #   4. If forward floor > descent profile → level-off + warn.
    #   5. When terrain clears → insert "▼ Retoma descenso" row.
    route_stops_full = [origin] + waypoints + [destination]
    dest_target_ft   = float(destination.get("elevation_ft", 0)) + 1000.0
    alt_to_lose      = max(0.0, float(cruise_alt) - dest_target_ft)
    desc_dur_min     = alt_to_lose / 500.0               # minutes at 500 ft/min
    desc_start_min   = ete_min - desc_dur_min - 2.0      # 2-min circuit entry buffer
    if desc_start_min <= 0:
        desc_start_min = ete_min * 0.5

    # Locate lat/lon for the ▼ INICIO DESCENSO marker via route interpolation
    _speed_npm = cruise_kts * KNOTS_TO_NM_PER_MIN
    _stop_et   = [0.0]
    for k in range(1, len(route_stops_full)):
        _seg_nm = gc_distance_nm(
            route_stops_full[k-1]["lat"], route_stops_full[k-1]["lon"],
            route_stops_full[k]["lat"],   route_stops_full[k]["lon"])
        _stop_et.append(_stop_et[-1] + _seg_nm / _speed_npm)
    _lat_d, _lon_d = route_stops_full[-1]["lat"], route_stops_full[-1]["lon"]
    for k in range(1, len(route_stops_full)):
        if _stop_et[k] >= desc_start_min:
            _sf, _st = route_stops_full[k-1], route_stops_full[k]
            _sd = _stop_et[k] - _stop_et[k-1]
            _fr = min((desc_start_min - _stop_et[k-1]) / _sd, 0.99) if _sd > 0 else 0.99
            _lat_d, _lon_d = intermediate_point(
                _sf["lat"], _sf["lon"], _st["lat"], _st["lon"], _fr)
            break

    descent_leg = {
        "is_waypoint":      False,
        "is_departure":     False,
        "is_descent":       True,
        "leg_num":          0,
        "elapsed_min":      round(desc_start_min),
        "_cumulative_min":  desc_start_min,
        "lat":              _lat_d,
        "lon":              _lon_d,
        "landmark":         "\u25bc INICIO DESCENSO",
        "max_terrain_ft":   0,
        "min_alt_ft":       int(cruise_alt),
        "fuel_burned_gal":  round(fuel_gph * (desc_start_min / 60.0), 1),
        "alt_icao":         destination["icao"],
        "alt_name":         destination["name"],
        "alt_dist_nm":      0,
        "alt_freq":         "",
        "segment_track_mag": None,
    }
    print(f"  Inicio descenso: min {round(desc_start_min)}  "
          f"(desde {cruise_alt} ft → {int(dest_target_ft)} ft, {desc_dur_min:.1f} min)")

    # Walk through legs in the descent zone
    _desc_legs = sorted(
        [l for l in legs
         if not l.get("is_waypoint")
         and float(l.get("_cumulative_min", l.get("elapsed_min", 0))) > desc_start_min],
        key=lambda l: float(l.get("_cumulative_min", l.get("elapsed_min", 0))),
    )

    # Precompute forward terrain (terrain from each leg position to the next) + terrain buffer floor.
    # Each leg's own max_terrain_ft was computed for the segment BEHIND it (already crossed
    # at cruise altitude), so it is irrelevant for descent.  What blocks descent is the
    # terrain AHEAD — the next leg's backward terrain, or terrain to destination for the last.
    _fwd_floors: list[float] = []
    for _i, _leg in enumerate(_desc_legs):
        if _i + 1 < len(_desc_legs):
            # terrain of the NEXT segment (next leg's backward scan covers from here to there)
            _next_terr = float(_desc_legs[_i + 1].get("max_terrain_ft") or 0)
        else:
            # Last leg: query terrain from here to destination
            _next_terr = max_terrain_elevation_ft(
                _leg["lat"], _leg["lon"],
                destination["lat"], destination["lon"],
            )
        _fwd_floors.append(max(_next_terr + TERRAIN_BUFFER_FT, dest_target_ft))

    _cur_alt    = float(cruise_alt)
    _prev_cum   = desc_start_min
    _prev_const = False   # was the previous leg terrain-constrained?
    _extra      = []      # (ref_leg, resume_leg) pairs to splice in before ref

    for _idx, _leg in enumerate(_desc_legs):
        _cum         = float(_leg.get("_cumulative_min", _leg.get("elapsed_min", 0)))
        _dt          = _cum - _prev_cum
        _profile_alt = max(_cur_alt - 500.0 * _dt, dest_target_ft)
        _floor       = _fwd_floors[_idx]

        if _floor > _profile_alt:
            # Terrain / obstacle ahead forces level-off
            _leg["min_alt_ft"] = int(round(_floor / 100) * 100)
            _cur_alt    = _floor
            _prev_const = True
            print(f"  [!] T+{_cum:.0f}min terreno adelante bloquea descenso: "
                  f"perfil {_profile_alt:.0f}ft < mín {_floor:.0f}ft")
        else:
            if _prev_const:
                # Terrain just cleared – insert "resume descent" marker before this leg
                _resume = {
                    "is_waypoint":      False,
                    "is_descent":       True,
                    "leg_num":          0,
                    "elapsed_min":      round(_cum),
                    "_cumulative_min":  _cum,
                    "lat":              _leg["lat"],
                    "lon":              _leg["lon"],
                    "landmark":         "\u25bc Retoma descenso",
                    "max_terrain_ft":   0,
                    "min_alt_ft":       int(round(_cur_alt / 100) * 100),
                    "fuel_burned_gal":  round(fuel_gph * (_cum / 60.0), 1),
                    "segment_track_mag": _leg.get("segment_track_mag"),
                    "alt_icao":         destination["icao"],
                    "alt_name":         destination["name"],
                    "alt_dist_nm":      0,
                    "alt_freq":         "",
                }
                _extra.append((_leg, _resume))
                print(f"  Retoma descenso T+{round(_cum)}min desde {int(round(_cur_alt))}ft")
            _leg["min_alt_ft"] = int(round(_profile_alt / 100) * 100)
            _cur_alt    = _profile_alt
            _prev_const = False
        _prev_cum = _cum

    # Splice resume-descent rows into legs list (before their reference leg)
    for _ref, _new in _extra:
        legs.insert(legs.index(_ref), _new)

    # 4d. Viento en ruta -------------------------------------------------------
    wind_speed_kts: Optional[float] = None
    wind_from_deg:  Optional[float] = None
    wind_source = ""
    wind_level  = ""

    if wind_override_speed is not None:
        # Manual override via --wind flag
        wind_speed_kts = wind_override_speed
        wind_from_deg  = wind_override_dir
        wind_source    = "Manual"
        print(f"  Viento manual: {wind_speed_kts:.0f} kt / {wind_from_deg:.0f}\u00b0V")
    else:
        # Auto-fetch: (a) per-leg winds for the table column,
        #             (b) midpoint wind for the summary effect box
        print("[4d] Consultando viento por tramo (Open-Meteo)...", flush=True)
        try:
            fetch_winds_for_legs(legs, cruise_alt)
            # Use the midpoint leg's wind (or average) for the summary box
            real_winds = [
                (l["wind_speed_kt"], l["wind_from_deg"])
                for l in legs
                if not l.get("is_waypoint")
                and "wind_speed_kt" in l and "wind_from_deg" in l
            ]
            if real_winds:
                # Compute vector (u,v) average to correctly average wind directions
                # Convert "from" direction to "to" vector by adding 180°.
                sum_u = 0.0
                sum_v = 0.0
                for s, d in real_winds:
                    theta_to = math.radians((d + 180) % 360)
                    sum_u += s * math.cos(theta_to)
                    sum_v += s * math.sin(theta_to)
                n = len(real_winds)
                mean_u = sum_u / n
                mean_v = sum_v / n
                # Resultant mean vector (to); convert back to a "from" direction
                mean_speed = math.hypot(mean_u, mean_v)
                dir_to_deg = (math.degrees(math.atan2(mean_v, mean_u))) % 360
                wind_speed_kts = mean_speed
                wind_from_deg = (dir_to_deg + 180) % 360
                wind_level     = _alt_to_pressure_level(cruise_alt)
                wind_source    = "Open-Meteo"
                print(f"  Viento medio por tramos ({wind_level} hPa): "
                      f"{wind_speed_kts:.0f} kt desde {wind_from_deg:.0f}\u00b0V "
                      f"(n={n})")
        except Exception as exc:
            print(f"  [warn] Open-Meteo no disponible: {exc}", file=sys.stderr)

    # 4e. Pista de despegue (desde CLI o auto-seleccionada por viento) ----------
    # No se inserta fila separada — se anota el primer tramo con la pista y el
    # tiempo real de vuelo hasta ese punto geográfico (más largo por la subida).
    if dep_rwy_hdg is None and wind_from_deg is not None:
        dep_rwy_hdg = best_departure_runway_mag(
            origin["id"], origin["icao"], wind_from_deg, mag_var
        )
        if dep_rwy_hdg is not None:
            rwy_num = f"{(round(dep_rwy_hdg / 10) % 36 or 36):02d}"
            print(f"  Pista de despegue auto (viento {wind_from_deg:.0f}\u00b0V): "
                  f"pista {rwy_num}")

    if dep_rwy_hdg is not None:
        rwy_num = f"{(round(dep_rwy_hdg / 10) % 36 or 36):02d}"
        _first_idx = next((i for i, l in enumerate(legs) if not l.get("is_waypoint")), None)
        if _first_idx is not None:
            _fl = legs[_first_idx]
            elapsed = _fl.get("elapsed_min", round(LEG_MINUTES * CLIMB_SPEED_FACTOR, 1))
            _fl["is_departure"] = True
            print(f"  Despegue pista {rwy_num}: primer tramo {elapsed:.1f} min "
                  f"(same map point as {LEG_MINUTES} min at cruise speed)")

    # 5. Generar PDF -----------------------------------------------------------
    print("[5/5] Generando PDF...")
    wind_effect = None
    if wind_speed_kts is not None and wind_from_deg is not None:
        # Convert IAS -> TAS for wind calculations (simple approximation)
        tas_kts = ias_to_tas(cruise_kts, cruise_alt or 0)
        print(f"  Usando TAS aprox {tas_kts:.1f} kt (desde IAS {cruise_kts} kt) para efecto viento")
        wind_effect = compute_wind_effect(
            tc, tas_kts, wind_from_deg, wind_speed_kts, total_nm, ete_min
        )
        wind_effect["source"]       = wind_source
        wind_effect["pressure_hpa"] = wind_level
        print(f"  Efecto viento: GS {wind_effect['gs']:.0f} kt  "
              f"TEE ajustado {int(round(wind_effect['new_ete_min']))} min "
              f"({'+' if wind_effect['delta_min'] >= 0 else ''}"
              f"{wind_effect['delta_min']:.0f} min)")

    flight_data = FlightData(
        origin=origin,
        destination=destination,
        tc=tc,
        mag_var=mag_var,
        mh=mh,
        total_nm=total_nm,
        ete_min=ete_min,
        fuel_required_gal=fuel_req,
        origin_freqs=origin_freqs,
        dest_freqs=dest_freqs,
        legs=legs,
        descent_leg=descent_leg,
        cruise_alt_ft=cruise_alt,
        wind_effect=wind_effect,
        cruise_speed_ias=cruise_kts,
        fuel_consumption_gph=fuel_gph,
    )
    generate_pdf(output_path, flight_data, one_face=args.one_face)

    print(f"\nListo. Abrir '{output_path}' e imprimir duplex (voltear por borde largo).\n")

    print(f"\nListo. Abrir '{output_path}' e imprimir duplex (voltear por borde largo).\n")


if __name__ == "__main__":
    main()
