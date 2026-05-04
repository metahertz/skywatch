"""Generate a small bundled airports seed (no runways).

This is the fallback the UI uses when the user hasn't yet run
`python -m skywatch.airports.fetch`.  Covers ~60 of the world's
busiest commercial airports plus a handful of UK regional fields
matching the synthetic scenario region.

The schema is a strict subset of OurAirports' airports.csv: same
column names so the runtime parser is identical.  Runway data is NOT
included in the seed (would balloon the bundled size and need precise
threshold coordinates per airport).  The frontend renders airport
markers from the seed; runways only appear once the full dataset has
been fetched.

Run as: python -m skywatch.airports.seed
"""
from __future__ import annotations

import csv
import gzip
import io
from pathlib import Path

from . import AIRPORTS_SEED_PATH, RUNWAYS_SEED_PATH

# Schema: (id, ident, type, name, latitude_deg, longitude_deg,
#          elevation_ft, continent, iso_country, iso_region,
#          municipality, scheduled_service, gps_code, iata_code, …rest blank)
#
# We only fill the columns the frontend reads; the rest are left blank
# strings so the CSV parses as the same dialect as the canonical data.
#
# Coordinates and elevations are from Wikipedia / each airport's
# operator pages; accurate to ~3 dp which is plenty for a marker icon.
_SEED_AIRPORTS = [
    # --- UK + Ireland (matches the receiver location range) ---
    ("EGLL", "large_airport",  "London Heathrow Airport",            51.4706,  -0.4619,    83, "GB", "London"),
    ("EGKK", "large_airport",  "London Gatwick Airport",             51.1481,  -0.1903,   202, "GB", "Crawley"),
    ("EGSS", "large_airport",  "London Stansted Airport",            51.8849,   0.2350,   348, "GB", "London"),
    ("EGLC", "medium_airport", "London City Airport",                51.5053,   0.0553,    19, "GB", "London"),
    ("EGGW", "large_airport",  "London Luton Airport",               51.8747,  -0.3683,   526, "GB", "Luton"),
    ("EGCC", "large_airport",  "Manchester Airport",                 53.3537,  -2.2750,   257, "GB", "Manchester"),
    ("EGPH", "large_airport",  "Edinburgh Airport",                  55.9500,  -3.3725,   135, "GB", "Edinburgh"),
    ("EGPF", "large_airport",  "Glasgow Airport",                    55.8719,  -4.4331,    26, "GB", "Glasgow"),
    ("EGBB", "large_airport",  "Birmingham Airport",                 52.4539,  -1.7480,   327, "GB", "Birmingham"),
    ("EGGP", "medium_airport", "Liverpool John Lennon Airport",      53.3336,  -2.8497,    80, "GB", "Liverpool"),
    ("EGCN", "medium_airport", "Doncaster Sheffield Airport",        53.4747,  -1.0067,    55, "GB", "Doncaster"),
    ("EIDW", "large_airport",  "Dublin Airport",                     53.4214,  -6.2701,   242, "IE", "Dublin"),
    ("EICK", "medium_airport", "Cork Airport",                       51.8413,  -8.4911,   502, "IE", "Cork"),
    ("EIDL", "medium_airport", "Donegal Airport",                    55.0442,  -8.3411,    30, "IE", "Carrickfin"),
    # --- Continental Europe ---
    ("EHAM", "large_airport",  "Amsterdam Schiphol Airport",         52.3086,   4.7639,   -11, "NL", "Amsterdam"),
    ("LFPG", "large_airport",  "Paris Charles de Gaulle Airport",    49.0097,   2.5479,   392, "FR", "Paris"),
    ("LFPO", "large_airport",  "Paris Orly Airport",                 48.7233,   2.3794,   292, "FR", "Paris"),
    ("LFPB", "medium_airport", "Paris Le Bourget Airport",           48.9694,   2.4414,   218, "FR", "Paris"),
    ("EDDF", "large_airport",  "Frankfurt am Main Airport",          50.0379,   8.5622,   364, "DE", "Frankfurt"),
    ("EDDM", "large_airport",  "Munich Airport",                     48.3538,  11.7861,  1487, "DE", "Munich"),
    ("EDDB", "large_airport",  "Berlin Brandenburg Airport",         52.3667,  13.5033,   157, "DE", "Berlin"),
    ("EDDH", "large_airport",  "Hamburg Airport",                    53.6304,   9.9882,    53, "DE", "Hamburg"),
    ("LSZH", "large_airport",  "Zurich Airport",                     47.4647,   8.5492,  1416, "CH", "Zurich"),
    ("LSGG", "large_airport",  "Geneva Airport",                     46.2381,   6.1090,  1411, "CH", "Geneva"),
    ("LOWW", "large_airport",  "Vienna International Airport",       48.1103,  16.5697,   600, "AT", "Vienna"),
    ("LEMD", "large_airport",  "Adolfo Suárez Madrid–Barajas",       40.4936,  -3.5668,  2001, "ES", "Madrid"),
    ("LEBL", "large_airport",  "Barcelona–El Prat Airport",          41.2974,   2.0833,    12, "ES", "Barcelona"),
    ("LIRF", "large_airport",  "Rome Fiumicino Airport",             41.8003,  12.2389,    13, "IT", "Rome"),
    ("LIMC", "large_airport",  "Milan Malpensa Airport",             45.6306,   8.7281,   768, "IT", "Milan"),
    ("EKCH", "large_airport",  "Copenhagen Airport",                 55.6181,  12.6561,    17, "DK", "Copenhagen"),
    ("ESSA", "large_airport",  "Stockholm Arlanda Airport",          59.6519,  17.9186,   137, "SE", "Stockholm"),
    ("ENGM", "large_airport",  "Oslo Gardermoen Airport",            60.1939,  11.1004,   681, "NO", "Oslo"),
    ("EFHK", "large_airport",  "Helsinki-Vantaa Airport",            60.3172,  24.9633,   179, "FI", "Helsinki"),
    ("LTFM", "large_airport",  "Istanbul Airport",                   41.2753,  28.7519,   325, "TR", "Istanbul"),
    ("LGAV", "large_airport",  "Athens International Airport",       37.9364,  23.9445,   308, "GR", "Athens"),
    # --- Middle East ---
    ("OMDB", "large_airport",  "Dubai International Airport",        25.2528,  55.3644,    62, "AE", "Dubai"),
    ("OMAA", "large_airport",  "Abu Dhabi International Airport",    24.4330,  54.6511,    88, "AE", "Abu Dhabi"),
    ("OTHH", "large_airport",  "Hamad International Airport",        25.2731,  51.6080,    13, "QA", "Doha"),
    # --- Asia ---
    ("WSSS", "large_airport",  "Singapore Changi Airport",            1.3592, 103.9894,    22, "SG", "Singapore"),
    ("VHHH", "large_airport",  "Hong Kong International Airport",    22.3080, 113.9185,    28, "HK", "Hong Kong"),
    ("RJTT", "large_airport",  "Tokyo Haneda Airport",               35.5494, 139.7798,    35, "JP", "Tokyo"),
    ("RJAA", "large_airport",  "Narita International Airport",       35.7647, 140.3864,   135, "JP", "Tokyo"),
    ("ZBAA", "large_airport",  "Beijing Capital International",      40.0801, 116.5846,   116, "CN", "Beijing"),
    ("ZSPD", "large_airport",  "Shanghai Pudong International",      31.1443, 121.8083,    13, "CN", "Shanghai"),
    ("RKSI", "large_airport",  "Incheon International Airport",      37.4691, 126.4505,    23, "KR", "Seoul"),
    # --- Australia ---
    ("YSSY", "large_airport",  "Sydney Kingsford Smith Airport",    -33.9461, 151.1772,    21, "AU", "Sydney"),
    ("YMML", "large_airport",  "Melbourne Airport",                 -37.6733, 144.8433,   434, "AU", "Melbourne"),
    # --- North America ---
    ("KJFK", "large_airport",  "John F. Kennedy International",      40.6398, -73.7789,    13, "US", "New York"),
    ("KLGA", "large_airport",  "LaGuardia Airport",                  40.7773, -73.8726,    21, "US", "New York"),
    ("KEWR", "large_airport",  "Newark Liberty International",       40.6925, -74.1687,    18, "US", "Newark"),
    ("KBOS", "large_airport",  "Boston Logan International",         42.3656, -71.0096,    20, "US", "Boston"),
    ("KIAD", "large_airport",  "Washington Dulles International",    38.9445, -77.4558,   313, "US", "Washington"),
    ("KATL", "large_airport",  "Hartsfield–Jackson Atlanta",         33.6367, -84.4281,  1026, "US", "Atlanta"),
    ("KORD", "large_airport",  "Chicago O'Hare International",       41.9786, -87.9048,   672, "US", "Chicago"),
    ("KDFW", "large_airport",  "Dallas/Fort Worth International",    32.8998, -97.0403,   607, "US", "Dallas"),
    ("KDEN", "large_airport",  "Denver International Airport",       39.8617, -104.6731, 5431, "US", "Denver"),
    ("KLAX", "large_airport",  "Los Angeles International",          33.9425, -118.4081,  125, "US", "Los Angeles"),
    ("KSFO", "large_airport",  "San Francisco International",        37.6189, -122.3750,   13, "US", "San Francisco"),
    ("KSEA", "large_airport",  "Seattle-Tacoma International",       47.4502, -122.3088,  433, "US", "Seattle"),
    ("CYYZ", "large_airport",  "Toronto Pearson International",      43.6772, -79.6306,   569, "CA", "Toronto"),
    ("CYUL", "large_airport",  "Montréal–Trudeau International",     45.4706, -73.7408,   118, "CA", "Montréal"),
    # --- Latin America ---
    ("MMMX", "large_airport",  "Mexico City International",          19.4361, -99.0719,  7316, "MX", "Mexico City"),
    ("SBGR", "large_airport",  "São Paulo–Guarulhos International",  -23.4322, -46.4695,  2459, "BR", "São Paulo"),
    # --- Africa ---
    ("FAOR", "large_airport",  "OR Tambo International Airport",     -26.1392, 28.2460,  5558, "ZA", "Johannesburg"),
    ("HECA", "large_airport",  "Cairo International Airport",         30.1219, 31.4056,   382, "EG", "Cairo"),
]


def _airport_row(idx: int, ident: str, kind: str, name: str,
                 lat: float, lon: float, elev_ft: int,
                 country: str, municipality: str) -> list[str]:
    # Match OurAirports' column order so the runtime parser doesn't need
    # to special-case the seed.
    return [
        str(idx),                # id
        ident,                   # ident (ICAO)
        kind,                    # type
        name,                    # name
        f"{lat:.6f}",            # latitude_deg
        f"{lon:.6f}",            # longitude_deg
        str(elev_ft),            # elevation_ft
        "",                      # continent (left blank — frontend doesn't read)
        country,                 # iso_country
        "",                      # iso_region
        municipality,            # municipality
        "yes" if kind in ("large_airport", "medium_airport") else "no",  # scheduled_service
        ident,                   # gps_code
        ident[1:] if len(ident) == 4 else "",  # iata_code (rough; only used as label fallback)
        "", "", "", "",          # local_code, home_link, wiki_link, keywords
    ]


_AIRPORTS_HEADER = [
    "id", "ident", "type", "name", "latitude_deg", "longitude_deg",
    "elevation_ft", "continent", "iso_country", "iso_region",
    "municipality", "scheduled_service", "gps_code", "iata_code",
    "local_code", "home_link", "wikipedia_link", "keywords",
]

_RUNWAYS_HEADER = [
    "id", "airport_ref", "airport_ident", "length_ft", "width_ft",
    "surface", "lighted", "closed", "le_ident", "le_latitude_deg",
    "le_longitude_deg", "le_elevation_ft", "le_heading_degT",
    "le_displaced_threshold_ft", "he_ident", "he_latitude_deg",
    "he_longitude_deg", "he_elevation_ft", "he_heading_degT",
    "he_displaced_threshold_ft",
]


def generate(out_airports: Path = AIRPORTS_SEED_PATH,
             out_runways: Path = RUNWAYS_SEED_PATH) -> None:
    out_airports.parent.mkdir(parents=True, exist_ok=True)
    # Airports
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_AIRPORTS_HEADER)
    for i, row in enumerate(_SEED_AIRPORTS, start=1):
        w.writerow(_airport_row(i, *row))
    out_airports.write_bytes(gzip.compress(buf.getvalue().encode("utf-8")))
    # Runways: empty file with header.  See module docstring for why
    # the seed deliberately has no runway rows.
    rbuf = io.StringIO()
    csv.writer(rbuf).writerow(_RUNWAYS_HEADER)
    out_runways.write_bytes(gzip.compress(rbuf.getvalue().encode("utf-8")))


def main() -> int:
    generate()
    print(f"wrote {AIRPORTS_SEED_PATH} ({len(_SEED_AIRPORTS)} airports)")
    print(f"wrote {RUNWAYS_SEED_PATH} (header-only — run "
          "`python -m skywatch.airports.fetch` for runway data)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
