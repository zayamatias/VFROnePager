"""Quick diagnostic: show alternate airport + frequency for each leg on LEPP->LEZG"""
import sys
sys.path.insert(0, ".")

# Patch to avoid printing progress noise
import builtins
_print = builtins.print

# Just import the functions we need
from vfr_onepager import (
    lookup_airport, build_legs, magnetic_variation,
    get_airport_frequencies, best_freq, closest_airport
)

origin = lookup_airport("LEPP")
dest   = lookup_airport("LEZG")
mag_var = magnetic_variation(origin["lat"], origin["lon"])

from vfr_onepager import gc_distance_nm, bearing_to_destination, intermediate_point, KNOTS_TO_NM_PER_MIN, LEG_MINUTES, CLIMB_SPEED_FACTOR

total_nm = gc_distance_nm(origin["lat"], origin["lon"], dest["lat"], dest["lon"])
speed_nm_per_min = 100 * KNOTS_TO_NM_PER_MIN
total_min = total_nm / speed_nm_per_min
n_legs = max(1, int(total_min / LEG_MINUTES))

print(f"Route: LEPP->LEZG, {total_nm:.1f} NM, {total_min:.0f} min, {n_legs} legs")
print(f"{'Leg':>4}  {'Alt ICAO':>10}  {'freq_str':>20}  freqs_count")

for i in range(1, n_legs + 1):
    fraction = min(i * LEG_MINUTES / total_min, 1.0)
    lat, lon = intermediate_point(origin["lat"], origin["lon"], dest["lat"], dest["lon"], fraction)
    alt = closest_airport(lat, lon, exclude_icaos=(origin["icao"], dest["icao"]), cruise_speed_kts=100)
    icao = alt["icao"]
    apt_id = ""
    # find apt_id
    import io, csv, requests
    from vfr_onepager import get_airports
    for row in get_airports():
        if (row.get("gps_code","") or row.get("ident","")).upper() == icao:
            apt_id = row.get("id","")
            break
    freqs = get_airport_frequencies(apt_id, icao)
    freq_str = alt["freq"]
    print(f"{i:>4}  {icao:>10}  {freq_str:>20}  {len(freqs)}")
