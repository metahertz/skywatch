# Skywatch

A 1090 MHz aircraft surveillance visualiser. Listens to ADS-B, Mode S, and
TCAS RA traffic via an RTL-SDR (or any BEAST-format source), decodes
everything decodable, and renders it in a browser as an ATC-style live
console.

> Receive-only. Skywatch never transmits on 1030 or 1090 MHz. Doing so is
> illegal in every ICAO state and dangerous to operating aircraft.

```
   ┌────────────────────────────────────────────────────┐
   │  RTL-SDR ──► dump1090-fa ──► BEAST :30005          │
   │                                  │                 │
   │                                  ▼                 │
   │                          skywatch (Python)         │
   │                          ├ BEAST parser            │
   │                          ├ ADS-B / Mode S decoder  │
   │                          ├ BDS 4,0/5,0/6,0/4,4     │
   │                          ├ TCAS RA correlator      │
   │                          ├ Offline aircraft DB     │
   │                          └ WebSocket :8765         │
   │                                  │                 │
   │                                  ▼                 │
   │                          Browser UI (HTTP :8080)   │
   │                          ├ Map with live tracks    │
   │                          ├ Per-aircraft inspector  │
   │                          ├ TCAS RA timeline        │
   │                          └ Receiver health         │
   └────────────────────────────────────────────────────┘
```

## What you can see

Skywatch decodes the full set of civil 1090 MHz downlink formats, so the
information richness depends on which messages your receiver picks up:

| Source                                 | What you get                                                    |
| -------------------------------------- | --------------------------------------------------------------- |
| ADS-B position (DF17 TC 9–18)          | lat/lon/altitude for every ADS-B-equipped aircraft              |
| ADS-B velocity (DF17 TC 19)            | ground speed, track, vertical rate, plus heading + IAS/TAS      |
| ADS-B identification (DF17 TC 1–4)     | callsign and wake-vortex category                               |
| ADS-B target state (DF17 TC 29)        | autopilot selected altitude, QNH, mode flags (v2 only)          |
| ADS-B ops status (DF17 TC 31)          | ADS-B version, NIC/NAC/SIL accuracy & integrity                 |
| **ADS-B TCAS RA broadcast** (TC 28 ST 2) | Active resolution advisory + threat aircraft ICAO              |
| Mode S surveillance (DF4/5)            | altitude / squawk for non-ADS-B targets near a Mode S radar     |
| **Comm-B (DF20/21)**                   | BDS 4,0 (selected alt, QNH, AP modes), BDS 5,0 (roll, track rate, GS, TAS), BDS 6,0 (heading, IAS, Mach, IRS vrate), BDS 4,4 (winds aloft, SAT) |
| **TCAS coordination (DF16)**           | RA active, sense, increased rate, reversal, crossing            |

Comm-B traffic only arrives when a Mode S **ground radar** is interrogating
nearby aircraft — coverage varies dramatically by location. Receivers near
major airports see hundreds of Comm-B messages per second; rural receivers
see almost none. Skywatch shows you what it's getting in the per-aircraft
inspector ("BDS REGISTERS OBSERVED" and "MESSAGE COUNTS BY DF" sections).

## Installation

Skywatch is **pure Python 3.10+ with zero runtime dependencies** beyond the
standard library. Frontend uses Leaflet via CDN at runtime.

```bash
git clone https://example/skywatch.git
cd skywatch
python3 --version    # 3.10 or newer required
```

That's it. There is nothing to install.

### One-off: download the offline aircraft database

Skywatch ships with a 15-row seed database so it works out of the box, but
to identify real-world aircraft you'll want the full Mictronics database
(~5 MB, covers ~500,000 aircraft). Run this once on a machine with internet:

```bash
python3 -m skywatch.db.fetch
```

This downloads three files into `data/`:

- `aircraft.csv.gz` — ICAO 24-bit → registration / type / operator
- `operators.json` — extended airline operator codes
- `types.json` — full ICAO Doc 8643 type catalogue

After this, skywatch runs fully offline forever. To update the database
later (Mictronics publishes new versions monthly), re-run the same command.

If your runtime machine has no internet at all, run `python3 -m skywatch.db.fetch --list-sources` on a connected machine to print the URLs, fetch the files manually, and copy them into `data/`.

## Running

### Without an SDR (synthetic feed for evaluation)

```bash
python3 -m skywatch
```

This starts the synthetic generator with five aircraft over London,
including a scheduled TCAS RA event between two of them at t≈45 s. Then
open <http://127.0.0.1:8080/> in a browser.

### With an RTL-SDR

You'll need [dump1090-fa](https://github.com/flightaware/dump1090) running
locally as the demodulator. Skywatch consumes its BEAST output:

```bash
# In one terminal: start dump1090
dump1090-fa --net --net-bo-port 30005 --mlat --quiet --device-index 0

# In another: start skywatch, pointing at dump1090
python3 -m skywatch --beast localhost:30005 --lat 51.4775 --lon -0.4614
```

`--lat` / `--lon` are your antenna location (decimal degrees). They're
optional but enable the receiver-range plausibility filter and the range
ring on the map.

For a remote dump1090 (e.g. on a Raspberry Pi feeding FlightAware), give
the host:

```bash
python3 -m skywatch --beast pi.local:30005
```

### Connecting to a busy public BEAST source

Many ADS-B aggregators publish a public BEAST feed. As an example using a
local feeder:

```bash
python3 -m skywatch --beast feed.example.org:30005
```

### CLI options

```
python3 -m skywatch --help

  --beast HOST:PORT    Connect to a dump1090 BEAST source
                       (default: synthetic generator)
  --http  HOST:PORT    HTTP bind for the web UI (default: 127.0.0.1:8080)
  --ws    HOST:PORT    WebSocket bind for live state (default: 127.0.0.1:8765)
  --lat / --lon        Receiver position (decimal degrees)
  --max-range-nm       Plausibility filter radius (default: 280 NM)
  --db PATH            Path to aircraft.csv.gz (default: data/aircraft.csv.gz)
  --time-scale N       Synthetic feed: N>1 simulates faster than real time
  -v / --verbose       DEBUG logging
```

### Exposing the UI to your LAN

By default skywatch binds to `127.0.0.1` only. To make it visible on your
local network:

```bash
python3 -m skywatch --http 0.0.0.0:8080 --ws 0.0.0.0:8765 \
                    --beast localhost:30005 --lat $LAT --lon $LON
```

Then any device on your network can open `http://your-host:8080/`.

## What's in the UI

- **Map** — live aircraft positions with heading vectors, 60-second trails,
  altitude colouring (white→amber→cyan→magenta as you climb), and red
  pulsing pairs when a TCAS RA is in progress.
- **Traffic list** — sortable table of every aircraft heard recently;
  RAs sort to the top.
- **Inspector** — click any aircraft for the full state: position,
  altitude (baro and GNSS), velocities (GS/TAS/IAS/Mach), autopilot
  intent, IRS roll/track-rate, observed BDS registers, message counts
  by DF, RSSI.
- **Event ticker** — new aircraft, RAs starting/ending, emergencies.
- **TCAS RA timeline** — permanent log of every RA observed this session,
  with summary, threat ICAO, source (TC=28 broadcast vs DF16 reply),
  and duration.
- **Top bar** — uptime, total frames, current frame rate (msgs/s), drop
  count, active aircraft count, link status.

## Testing

```bash
python3 -m unittest discover tests/ -v
```

24 tests covering CRC, address-parity ICAO recovery, ADS-B identification,
CPR position decoding (against textbook reference values), velocity
decoding, BDS 4,0/5,0/6,0 register decoders (verified against Junzi Sun's
*1090 MHz Riddle*), BDS inference uniqueness, BEAST protocol roundtrip
and frame splitting, FAA N-number algorithm (against canonical reference
table), country lookup, lookup cascade (DB → algorithmic → country), and
end-to-end engine integration with the synthetic scenario.

## Project layout

```
skywatch/
├── skywatch/
│   ├── __main__.py            CLI entry point
│   ├── decoder/
│   │   ├── common.py          CRC, ICAO recovery, bit utilities
│   │   ├── adsb.py            DF17/18 (callsign, position, velocity, TCAS RA, ...)
│   │   ├── modes.py           DF0/4/5/16/20/21 (altitude, squawk, BDS, TCAS coord)
│   │   ├── beast.py           BEAST binary protocol parser/encoder
│   │   └── synthetic.py       Synthetic message generator (scenarios)
│   ├── state/
│   │   ├── aircraft.py        Aircraft state object
│   │   └── engine.py          Dispatcher, CPR pair manager, TCAS correlator
│   ├── db/
│   │   ├── icao_ranges.py     ICAO 24-bit country allocations (embedded)
│   │   ├── algorithmic.py     N-number, D-, G-, C-F/C-G recovery
│   │   ├── operators.py       ~110 embedded airline operator codes
│   │   ├── types.py           ~150 embedded ICAO Doc 8643 type codes
│   │   ├── mictronics.py      Mictronics CSV loader
│   │   ├── lookup.py          Unified lookup (DB → algorithmic → country)
│   │   ├── seed.py            Tiny seed DB for offline-first demos
│   │   └── fetch.py           One-off downloader for the Mictronics DB
│   └── server/
│       ├── websocket.py       Pure-stdlib WebSocket server (RFC 6455)
│       ├── http_server.py     Static file server for the UI
│       └── app.py             Glues engine to WS broadcaster, BEAST client
├── web/
│   ├── index.html             Browser UI shell
│   ├── style.css              ATC-console aesthetic
│   └── app.js                 WebSocket client + map + inspector
├── data/                      Downloaded aircraft databases (gitignored)
├── tests/
│   └── test_skywatch.py       Test suite
└── README.md
```

## Operational notes & limitations

- **Coverage is location-dependent.** Comm-B (DF20/21) only arrives when a
  Mode S ground radar is interrogating local aircraft. TCAS coordination
  (DF16) only appears during actual RA events between aircraft within
  ~30 NM of you. Both can be sparse outside major airport areas.

- **Address-parity ICAO recovery is fallible.** For DF0/4/5/16/20/21
  messages the CRC is XORed with the ICAO address, so a CRC failure
  produces a fake ICAO that happens to look valid. Skywatch validates by
  requiring the recovered ICAO to match one we've seen via squitter
  (DF11/17/18) within the last 60 s. Unverified messages are dropped.

- **CPR position decoding can occasionally produce wrong-but-CRC-valid
  fixes** when two unrelated even/odd messages get paired. Skywatch
  applies a receiver-range filter (`--max-range-nm`) and a previous-fix
  velocity check (max 1500 kt between fixes) to catch these. Messages
  that fail either check are silently rejected.

- **ADS-B is unauthenticated.** Anyone with a transmitter can fabricate
  ADS-B traffic. The Inspector pane shows per-aircraft message-count and
  BDS-source breakdown so you can judge the signal's plausibility yourself.

- **PIA aircraft.** The FAA Privacy ICAO Address programme cycles
  participating aircraft through addresses in `ADF7C8`–`AFFFFF`. Skywatch
  flags these in the Inspector. The aircraft you're looking at probably
  isn't the one whose registration would normally correspond to that hex.

## References

- Sun, J. (2021). *The 1090 Megahertz Riddle: A Guide to Decoding Mode S
  and ADS-B Signals* (2nd ed.). TU Delft. <https://mode-s.org/1090mhz/>
- ICAO Annex 10 Vol IV — Surveillance and Collision Avoidance Systems
- ICAO Doc 9871 — Technical Provisions for Mode S Services and Extended
  Squitter
- RTCA DO-260B — MOPS for 1090 MHz Extended Squitter ADS-B and TIS-B
- FAA reference for the N-number algorithm:
  <https://github.com/guillaumemichel/icao-nnumber_converter>
- Mictronics aircraft database: <https://www.mictronics.de/aircraft-database/>
  - `aircraft.csv.gz` — <https://github.com/wiedehopf/tar1090-db> (csv branch)
  - `operators.json`, `types.json` — <https://github.com/Mictronics/readsb-protobuf>
    (`webapp/src/db/`)
- dump1090-fa: <https://github.com/flightaware/dump1090>
