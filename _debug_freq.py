import io, csv, requests

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
FREQS_URL    = "https://davidmegginson.github.io/ourairports-data/airport-frequencies.csv"

def fetch(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))

print("Loading..."); airports = fetch(AIRPORTS_URL); freqs = fetch(FREQS_URL)

def find_freqs(airport_id, icao):
    out = []
    for row in freqs:
        if row.get("airport_ident","").upper() == icao.upper() or row.get("airport_ref","") == airport_id:
            out.append(row)
    return out

test_icaos = ["LEZG", "LEHC", "LESO", "LERS", "LEAB"]
for icao in test_icaos:
    for row in airports:
        if row.get("gps_code","").upper() == icao or row.get("ident","").upper() == icao:
            apt_id = row.get("id","")
            gps    = row.get("gps_code","")
            ident  = row.get("ident","")
            derived = (gps or ident or "").upper()
            f = find_freqs(apt_id, derived)
            print(f"{icao}: id={apt_id!r} gps={gps!r} ident={ident!r} derived={derived!r} freqs={len(f)}")
            if f:
                print(f"  sample: ident={f[0].get('airport_ident')!r} ref={f[0].get('airport_ref')!r} type={f[0].get('type')!r} freq={f[0].get('frequency_mhz')!r}")
            break
    else:
        print(f"{icao}: not found")
