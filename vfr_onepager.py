#!/usr/bin/env python3
"""
vfr_onepager.py
===============
Generates a VFR trip one-pager as a duplex A4-landscape PDF.

  Page 1 (front): A5 VFR Trip Log
  Page 2 (back) : A5 Frequency Panel, rotated 180° for double-sided fold

Data sources (all free, no API key required):
  - Airport/frequency data : OurAirports CSV  (https://ourairports.com/data/)
  - Terrain elevation       : Open-Elevation API (https://api.open-elevation.com/)
  - Reverse geocoding       : Nominatim / OpenStreetMap
  - Magnetic variation      : NOAA WMM via the 'geomag' library (local)
  - Great-circle math       : geopy

Dependencies (install with pip):
    pip install reportlab geopy requests geomag
"""

import argparse
import csv
import datetime
import io
import math
import sys
import time
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
# Terrain elevation (Open-Elevation API with Open-Topo-Data fallback)
# ---------------------------------------------------------------------------

def _get_elevations_open_elevation(locations: list[dict]) -> list[float]:
    """Try Open-Elevation. Returns elevations in metres or raises on failure."""
    resp = requests.post(
        OPEN_ELEVATION_URL,
        json={"locations": locations},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data["results"]
    if len(results) != len(locations):
        raise ValueError(f"Open-Elevation returned {len(results)} results for {len(locations)} points")
    return [r["elevation"] for r in results]


def _get_elevations_opentopodata(locations: list[dict]) -> list[float]:
    """Try Open-Topo-Data (SRTM 30m). Returns elevations in metres or raises on failure.
    API expects locations as a pipe-separated string: 'lat,lon|lat,lon|...' (max 100 per request).
    """
    # Build pipe-separated string as required by the Open-Topo-Data API
    loc_str = "|".join(f"{loc['latitude']},{loc['longitude']}" for loc in locations)
    resp = requests.post(
        OPEN_TOPO_DATA_URL,
        json={"locations": loc_str},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "OK":
        raise ValueError(f"Open-Topo-Data status: {data.get('status')}")
    results = data["results"]
    if len(results) != len(locations):
        raise ValueError(f"Open-Topo-Data returned {len(results)} results for {len(locations)} points")
    return [r["elevation"] or 0.0 for r in results]


def get_elevations_m(points: list[tuple[float, float]]) -> list[float]:
    """
    Query terrain elevation for a list of (lat, lon) tuples.
    Tries Open-Elevation first; falls back to Open-Topo-Data (SRTM 30m).
    Returns 0 m for each point if both sources fail.
    """
    if not points:
        return []
    locations = [{"latitude": lat, "longitude": lon} for lat, lon in points]
    try:
        return _get_elevations_open_elevation(locations)
    except Exception as exc:
        print(f"    [warn] Open-Elevation failed ({exc}); retrying with Open-Topo-Data...",
              file=sys.stderr)
    try:
        return _get_elevations_opentopodata(locations)
    except Exception as exc2:
        print(f"    [warn] Open-Topo-Data also failed ({exc2}); using 0 m fallback.",
              file=sys.stderr)
        return [0.0] * len(points)


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

def get_landmark(lat: float, lon: float, zoom: int = 10) -> str:
    """
    Use Nominatim reverse geocoding to return a short landmark description
    near (lat, lon).  Falls back to a lat/lon string on error.
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
        # Keep only the first two comma-separated components for brevity
        parts = [p.strip() for p in display.split(",")]
        short = ", ".join(parts[:2]) if len(parts) >= 2 else display
        return short[:40]   # cap at 40 chars to fit table
    except Exception as exc:
        print(f"    [warn] Nominatim query failed: {exc}", file=sys.stderr)
        return f"{lat:.3f}°N {lon:.3f}°E"


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

            # Referencia a la izquierda del avion (~45 grados, ~8 NM)
            track_deg    = bearing_to_destination(prev_lat, prev_lon, lat, lon)
            left_bearing = (track_deg - 45 + 360) % 360
            lm_lat, lm_lon = offset_point(lat, lon, left_bearing, 8.0)
            landmark = get_landmark(lm_lat, lm_lon, zoom=12)

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
    min_safe = max(max_terrain + 300, max_leg_min)
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
    tas_kts        : True airspeed (knots) – we treat IAS ≈ TAS here
    wind_from_deg  : Wind direction FROM (degrees true), e.g. 270 = westerly
    wind_speed_kts : Wind speed (knots)
    total_nm       : Total route distance (NM)
    ete_no_wind_min: ETE without any wind (minutes)

    Returns a dict with all values needed for the display box.
    """
    # Angle between wind-from and true course
    # Headwind component: positive = headwind, negative = tailwind
    angle_rad = math.radians(wind_from_deg - tc_true)
    hw  = wind_speed_kts * math.cos(angle_rad)   # + headwind / - tailwind
    xw  = wind_speed_kts * math.sin(angle_rad)   # + from right / - from left

    # Wind Correction Angle (WCA) to maintain track
    wca_rad = math.asin(max(-1.0, min(1.0, xw / tas_kts)))
    wca_deg = math.degrees(wca_rad)              # negative = correct left

    # Ground speed after correcting for crosswind
    gs = tas_kts * math.cos(wca_rad) - hw
    gs = max(1.0, gs)                            # safety floor

    # Adjusted ETE
    new_ete_min = (total_nm / gs) * 60.0
    delta_min   = new_ete_min - ete_no_wind_min  # + = slower, - = faster

    # Cross-track drift per 5 min if NO correction is applied
    drift_nm_per_5 = xw * (5.0 / 60.0)          # NM sideways in 5 min

    # Determine qualitative descriptions
    if abs(hw) < 1.0:
        hw_label = "Sin componente frontal/trasero"
    elif hw > 0:
        hw_label = f"Viento en cara  {hw:.0f} kt  → más lento"
    else:
        hw_label = f"Viento de cola  {abs(hw):.0f} kt  → más rápido"

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
                        fontSize=5.5, fontName="Helvetica", leading=6)

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


def draw_front_panel(c: canvas.Canvas,
                     origin: dict, destination: dict,
                     tc: float, mag_var: float, mh: float,
                     total_nm: float, ete_min: float, fuel_gal: float,
                     origin_freqs: list[dict], dest_freqs: list[dict],
                     legs: list[dict],
                     descent_leg: Optional[dict],
                     cruise_alt_ft: int = 0,
                     x_offset: float = 0.0,
                     wind_effect: Optional[dict] = None) -> None:
    """
    Dibuja la Hoja de Vuelo VFR en el panel A5 izquierdo (en español).
    Incluye columnas de registro manual (H.Real, C.Real) y fila de descenso.
    """
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
        c.drawString(x_off, yy, "QNH: _____")
        c.drawString(x_off + 32 * mm, yy, "Squawk: _____")

    y -= apt_info_h + 2 * mm

    # ── Tabla de tramos ───────────────────────────────────────────────────────
    # Columns: T.Plan | Track | Referencia | Alt.Mín | C.plan | Viento | Alternativo | H.Real | C.Real
    col_widths = [9 * mm, 9 * mm, 28 * mm, 13 * mm, 10 * mm, 14 * mm, 26 * mm, 11 * mm, 6 * mm]
    deficit = INNER_W - sum(col_widths)
    if deficit > 0:
        col_widths[2] += deficit  # extra espacio a Referencia

    headers = [
        "T.Plan\n(min)", "Track\n(\u00b0M)", "Referencia", "Alt.M\u00edn\n(ft)",
        "C.plan\n(gal)", "Viento\n(kt/\u00b0V)", "Alternativo\n(ICAO/Frec)",
        "T", "C",
    ]

    COL_WRITEIN = colors.white    # sin relleno amarillo
    COL_DESCENT = colors.white    # sin relleno naranja
    COL_D_TEXT  = colors.black    # texto descenso en negro

    # Combinar tramos + tramo de descenso ordenados por tiempo acumulado.
    # Los waypoint-markers ya vienen embebidos en `legs`, también ordenados.
    all_entries: list[dict] = []
    for leg in legs:
        all_entries.append({
            "is_descent":  False,
            "is_waypoint": leg.get("is_waypoint", False),
            "data":        leg,
        })
    if descent_leg:
        d_cum = descent_leg.get("_cumulative_min", descent_leg["elapsed_min"])
        ins = len(all_entries)
        for i, e in enumerate(all_entries):
            if not e["is_waypoint"]:
                e_cum = e["data"].get("_cumulative_min", e["data"]["elapsed_min"])
                if e_cum >= d_cum:
                    ins = i
                    break
        all_entries.insert(ins, {"is_descent": True, "is_waypoint": False, "data": descent_leg})

    # Append final destination marker row (shows total ETE and distance)
    try:
        dest_marker = {
            "is_waypoint": True,
            "is_descent": False,
            "data": {
                "leg_num": 0,
                "elapsed_min": int(round(ete_min)),
                "_cumulative_min": ete_min,
                "_cumulative_dist": total_nm,
                "cum_dist_nm": round(total_nm, 1),
                "lat": destination.get("lat"),
                "lon": destination.get("lon"),
                "landmark": f"\u25ba DEST: {destination.get('icao', '')}",
            }
        }
        all_entries.append(dest_marker)
    except Exception:
        pass

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
                wind_cell = f"{int(round(ws))}/{int(round(wd)) :03d}"
            except Exception:
                wind_cell = f"{ws}/{wd}"
        else:
            wind_cell = ""

        rows.append([
            t_plan_str,
            track_str,
            lm,
            f"{leg['min_alt_ft']:,}" if leg["min_alt_ft"] else "",
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
        ("FONTSIZE",      (0, 0), (-1, 0), 6),
        ("FONTSIZE",      (0, 1), (-1, -1), 5.5),
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
    if h > available_h:
        frame = KeepInFrame(INNER_W, available_h, [tbl], mode="shrink")
        w, h = frame.wrapOn(c, INNER_W, available_h)
        frame.drawOn(c, MARGIN, y - h)
    else:
        tbl.drawOn(c, MARGIN, y - h)

    # ── Viento proyectado ───────────────────────────────────────────────────
    if wind_effect:
        we = wind_effect
        wind_box_h = 18 * mm
        wy = MARGIN + 6 * mm + wind_box_h   # top of the wind box

        # Thin border box
        c.setStrokeColor(COL_BORDER)
        c.setLineWidth(0.4)
        c.rect(MARGIN, MARGIN + 6 * mm, INNER_W, wind_box_h)

        # Header
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 6)
        src_tag  = we.get("source", "")
        lvl_tag  = we.get("pressure_hpa", "")
        src_str  = f" [{src_tag} {lvl_tag} hPa]" if lvl_tag else (f" [{src_tag}]" if src_tag else "")
        c.drawString(MARGIN + 2 * mm, wy - 3 * mm,
                     f"VIENTO EN RUTA{src_str}  \u2014  "
                     f"{we['wind_speed']:.0f} kt desde {we['wind_from']:.0f}\u00b0V  "
                     f"(RC verdadero: {tc:.0f}\u00b0V)")

        # Disclaimer
        c.setFont("Helvetica-Oblique", 5)
        c.setFillColor(colors.HexColor("#555555"))
        disclaimer = ("\u26a0 Tiempo real Open-Meteo \u2013 verificar antes del vuelo"
                      if src_tag == "Open-Meteo"
                      else "\u26a0 MANUAL \u2013 el viento real puede cambiar en cualquier momento")
        c.drawRightString(MARGIN + INNER_W - 2 * mm, wy - 3 * mm, disclaimer)

        # Separator line under header
        c.setStrokeColor(colors.HexColor("#AAAAAA"))
        c.setLineWidth(0.3)
        c.line(MARGIN, wy - 4.5 * mm, MARGIN + INNER_W, wy - 4.5 * mm)

        # Three columns of data
        c.setFont("Helvetica", 5.5)
        c.setFillColor(colors.black)
        col3 = INNER_W / 3
        cx1 = MARGIN + 2 * mm
        cx2 = MARGIN + col3 + 2 * mm
        cx3 = MARGIN + 2 * col3 + 2 * mm
        line_h = 3.4 * mm
        ly = wy - 7 * mm

        # Col 1: headwind / tailwind and ground speed
        c.drawString(cx1, ly, we["hw_label"])
        ly -= line_h
        c.drawString(cx1, ly, f"Vel. sobre tierra (GS): {we['gs']:.0f} kt")
        ly -= line_h
        delta_sign = "+" if we["delta_min"] >= 0 else ""
        c.drawString(cx1, ly,
                     f"TEE ajustado: {int(round(we['new_ete_min']))} min "
                     f"({delta_sign}{we['delta_min']:.0f} min)")

        # Col 2: crosswind / correction
        ly = wy - 7 * mm
        c.drawString(cx2, ly, we["xw_label"])
        ly -= line_h
        c.drawString(cx2, ly, we["wca_label"])
        ly -= line_h
        c.drawString(cx2, ly,
                     f"Sin correcc.: deriva {we['drift_nm_per_5']:.1f} NM/5 min "
                     f"a la {we['drift_dir']}")

        # Col 3: magnetic headings
        ly = wy - 7 * mm
        wca_mag = we["wca_deg"]
        new_mh = (mh + wca_mag + 360) % 360
        c.drawString(cx3, ly, f"RM sin viento: {mh % 360:.0f}\u00b0M")
        ly -= line_h
        c.drawString(cx3, ly, f"RM corregido:  {new_mh:.0f}\u00b0M")
        ly -= line_h
        c.drawString(cx3, ly,
                     f"WCA: {abs(wca_mag):.1f}\u00b0 a la "
                     + ("der." if wca_mag > 0 else "izq."))


    # ── Pie de p\u00e1gina ─────────────────────────────────────────────────────────
    c.setFont("Helvetica", 5)
    c.setFillColor(colors.grey)
    c.drawCentredString(PANEL_W / 2, 3 * mm,
                        "Datos: OurAirports \u00b7 Open-Elevation \u00b7 Nominatim/OSM  "
                        "| Solo para planificaci\u00f3n/simulaci\u00f3n – verificar datos antes del vuelo")

    c.restoreState()


def draw_back_panel(c: canvas.Canvas,
                    origin: dict, destination: dict,
                    origin_freqs: list[dict], dest_freqs: list[dict],
                    enroute_airports: list[dict],
                    x_offset: float = 0.0,
                    rotate: bool = True) -> None:
    """
    Dibuja el Panel de Frecuencias, rotado 180 grados para impresion duplex
    con pliegue vertical.  Todo el texto en espanol.
    """
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

    # ── Cabecera: linea + texto negro, sin relleno ──────────────────────
    hdr_h = 18 * mm
    c.setStrokeColor(colors.black)
    c.setLineWidth(1)
    c.line(0, PAGE_H - hdr_h, PANEL_W, PAGE_H - hdr_h)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(PANEL_W / 2, PAGE_H - 7 * mm,
                        f"PANEL DE FRECUENCIAS  \u00b7  {origin['icao']} \u2192 {destination['icao']}")
    c.setFont("Helvetica", 6)
    c.setFillColor(colors.black)
    c.drawCentredString(PANEL_W / 2, PAGE_H - 13 * mm,
                        "Verificar todas las frecuencias en publicaciones vigentes antes del vuelo")
    # Linea separadora bajo cabecera
    c.setStrokeColor(COL_BORDER)
    c.setLineWidth(0.4)
    c.line(MARGIN, PAGE_H - hdr_h, PANEL_W - MARGIN, PAGE_H - hdr_h)

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
        c.drawString(MARGIN, y, "QNH: _____")
        c.drawString(MARGIN + 32 * mm, y, "Squawk: _____")
        y -= 5 * mm

        col_w = [18 * mm, INNER_W - 18 * mm - 24 * mm, 24 * mm]
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
        w, h = tbl.wrapOn(c, INNER_W, y - MARGIN)
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

        col_w2 = [14 * mm, INNER_W - 14 * mm - 18 * mm - 34 * mm, 18 * mm, 34 * mm]
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
        w, h = tbl2.wrapOn(c, INNER_W, y - MARGIN)
        tbl2.drawOn(c, MARGIN, y - h)

    # ── Pie de p\u00e1gina ─────────────────────────────────────────────────────────
    c.setFont("Helvetica", 5)
    c.setFillColor(colors.grey)
    c.drawCentredString(PANEL_W / 2, 3 * mm,
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


def generate_pdf(output_path: str,
                 origin: dict, destination: dict,
                 cruise_speed_ias: float,
                 fuel_consumption_gph: float,
                 legs: list[dict],
                 descent_leg: Optional[dict],
                 origin_freqs: list[dict],
                 dest_freqs: list[dict],
                 tc: float, mag_var: float, mh: float,
                 total_nm: float, ete_min: float,
                 fuel_required_gal: float,
                 cruise_alt_ft: int = 0,
                 one_face: bool = False,
                 wind_effect: Optional[dict] = None) -> None:
    """
    Genera un PDF duplex de dos paginas en A4 apaisado:
      Pagina 1 – Frontal: Hoja de Vuelo VFR en el panel A5 izquierdo
      Pagina 2 – Dorso  : Panel de Frecuencias (rotado 180 grados) en el panel A5 derecho
                          (queda a la izquierda al girar la hoja y doblarla)
    """
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
        draw_front_panel(
            c, origin, destination,
            tc, mag_var, mh,
            total_nm, ete_min, fuel_required_gal,
            origin_freqs, dest_freqs, legs, descent_leg,
            cruise_alt_ft=cruise_alt_ft,
            x_offset=0,
            wind_effect=wind_effect,
        )
        # Back panel unrotated on the right half
        draw_back_panel(
            c, origin, destination,
            origin_freqs, dest_freqs, enroute,
            x_offset=PANEL_W,
            rotate=False,
        )
        draw_fold_guide(c)
        c.showPage()
    else:
        # ── PAGINA 1 (FRONTAL) ───────────────────────────────────────────────────
        draw_front_panel(
            c, origin, destination,
            tc, mag_var, mh,
            total_nm, ete_min, fuel_required_gal,
            origin_freqs, dest_freqs, legs, descent_leg,
            cruise_alt_ft=cruise_alt_ft,
            x_offset=0,
            wind_effect=wind_effect,
        )
        draw_fold_guide(c)
        c.showPage()

        # ── PAGINA 2 (DORSO) ─────────────────────────────────────────────────────
        # Impresion duplex con giro por el borde largo (izquierdo):
        #   El dorso del panel IZQUIERDO de la pag.1 se imprime en el DERECHO de la pag.2.
        #   Rotamos 180 grados para que se lea correctamente al doblar verticalmente.
        draw_back_panel(
            c, origin, destination,
            origin_freqs, dest_freqs, enroute,
            x_offset=PANEL_W,   # mitad derecha de la hoja A4 apaisada
            rotate=False,
        )
        draw_fold_guide(c)
        c.showPage()

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
    parser.add_argument("origin_icao",      help="ICAO code of the departure airport")
    parser.add_argument("destination_icao", help="ICAO code of the destination airport")
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
    args = parser.parse_args()

    origin_icao  = args.origin_icao.upper().strip()
    dest_icao    = args.destination_icao.upper().strip()
    cruise_kts   = args.cruise_speed_ias
    fuel_gph     = args.fuel_consumption_gph
    output_path  = args.output or f"{origin_icao}_{dest_icao}_vfr.pdf"

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

    # 4c. Tramo de descenso ---------------------------------------------------
    route_stops_full = [origin] + waypoints + [destination]
    descent_leg = compute_descent_leg(
        legs, route_stops_full, cruise_kts, fuel_gph, ete_min,
        cruise_altitude_ft=cruise_alt,
    )
    if descent_leg:
        print(f"  Inicio descenso: min {descent_leg['elapsed_min']} "
              f"(alt crucero {descent_leg['min_alt_ft']} ft)")

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

    # 5. Generar PDF -----------------------------------------------------------
    print("[5/5] Generando PDF...")
    wind_effect = None
    if wind_speed_kts is not None and wind_from_deg is not None:
        wind_effect = compute_wind_effect(
            tc, cruise_kts, wind_from_deg, wind_speed_kts, total_nm, ete_min
        )
        wind_effect["source"]       = wind_source
        wind_effect["pressure_hpa"] = wind_level
        print(f"  Efecto viento: GS {wind_effect['gs']:.0f} kt  "
              f"TEE ajustado {int(round(wind_effect['new_ete_min']))} min "
              f"({'+' if wind_effect['delta_min'] >= 0 else ''}"
              f"{wind_effect['delta_min']:.0f} min)")
    generate_pdf(
        output_path,
        origin, destination,
        cruise_kts, fuel_gph,
        legs, descent_leg,
        origin_freqs, dest_freqs,
        tc, mag_var, mh,
        total_nm, ete_min, fuel_req,
        cruise_alt_ft=cruise_alt,
        one_face=args.one_face,
        wind_effect=wind_effect,
    )

    print(f"\nListo. Abrir '{output_path}' e imprimir duplex (voltear por borde largo).\n")


if __name__ == "__main__":
    main()
