# pi-feeder

A turnkey ADS-B / VDL2 feeder hub for Raspberry Pi 3 / 4 / 5.  Drives
one (or two) RTL-SDR dongles into the major aggregator services and
into the local Skywatch UI from a single small web console.

## What this is

A Docker Compose stack plus a tiny Flask web UI ("manager") that
runs on a fresh Raspberry Pi OS Lite install and:

* Pulls 1090 MHz ADS-B / Mode S off the SDR via dump1090 / readsb
  (packaged as the well-maintained
  [`sdr-enthusiasts/docker-adsb-ultrafeeder`](https://github.com/sdr-enthusiasts/docker-adsb-ultrafeeder)).
* Pulls 136 MHz VDL Mode 2 / CPDLC / ACARS off a second SDR via
  [`dumpvdl2`](https://github.com/szpajder/dumpvdl2) (re-using the
  Dockerfile from `docker/dumpvdl2/` in this repo).
* Fans the BEAST stream out to any/all of:
  * **FlightAware** (PiAware)
  * **FlightRadar24** (fr24feed)
  * **ADSBExchange**
  * **PlaneFinder**
  * **RadarBox**
  * **OpenSky Network**
  * **airframes.io** (1090 + VDL2 + ACARS)
  * **Skywatch — monolithic**, the local web UI from this repo, OR
  * **Skywatch — edge mode**, a sender that ships decoded state to a
    more powerful remote central running the UI / API / Mongo archive.
    Use this when the Pi is purely a radio site.
* Exposes a single web console on `:8090` to enable/disable each
  feeder and enter its sharing key.

## Why Docker

Each feeder ships a different binary with a different licence (some
proprietary, some open) and a different idea of where its config
file should live.  Using the upstream containers
([sdr-enthusiasts catalog](https://github.com/sdr-enthusiasts)) means:

* The host OS stays vanilla Raspberry Pi OS Lite — no PPAs, no
  package conflicts, easy to upgrade.
* Each feeder is isolated; one mis-behaving aggregator can't break
  the others.
* `docker compose up -d` brings the whole stack back after a power
  cycle.

## Hardware assumptions

| Pi | Tested | Notes |
|---|---|---|
| Pi 3B / 3B+ | Should work | One SDR comfortable; two SDRs at the limit. |
| Pi 4 (2 GB+) | Recommended | Comfortable with two SDRs and all feeders. |
| Pi 5 | Recommended | Comfortable with two SDRs and all feeders. |

* arm64 (64-bit Raspberry Pi OS Lite) only — every container in the
  feeder ecosystem ships arm64 builds.
* **Building images ON the Pi 3B is not recommended.**  1 GB RAM is
  tight for buildkitd; the dumpvdl2 image in particular compiles
  libacars and dumpvdl2 from C source and OOMs reliably (manifests as
  a `SIGBUS` / Go runtime fault during `docker compose build`).  Use
  one of:
   1. Build on an x86 machine with `docker buildx --platform linux/arm64`
      and push to a registry, or `docker save | ssh pi 'docker load'`.
   2. Use the pre-baked image flow in `docs/building-image.md`.
   3. (Last resort) enable zram on the Pi and build one image at a time.
  The runtime stack itself fits 1 GB if you stick to a couple of
  feeders; it's the *build step* that's the problem.
* USB 2.0 RTL-SDR dongles (RTL2832U + R820T2) are the de-facto
  hardware.  Two slightly different serial numbers (use `rtl_eeprom -d
  N -s name`) so you can address each independently — one for 1090,
  one for 136 MHz VDL2.

## Quick start (existing Pi OS Lite install)

```bash
# Fresh Pi OS Lite (Bookworm 64-bit), connected to network, ssh'd in
curl -fsSL https://raw.githubusercontent.com/<your-fork>/skywatch/main/pi-feeder/scripts/install.sh | bash
```

That's it.  The installer:

1. Updates the OS, installs Docker + Compose, RTL-SDR udev rules.
2. Clones this repo into `/opt/skywatch`.
3. Brings up only the core stack (ultrafeeder + manager UI).  No
   feeders are enabled yet — open the manager and turn on the
   ones you want.
4. Installs a systemd unit so the stack starts on boot.

When done, point a browser at:

* `http://<pi>:8090/` — manager UI (configure feeders, set lat/lon,
  enter sharing keys, see status).
* `http://<pi>:8080/` — Skywatch UI (if you've enabled the
  Skywatch feeder).
* `http://<pi>/` — ultrafeeder's tar1090 / graphs1090 (always on).

## Deployment topology

Two ways the Skywatch container can run on the Pi, picked via the
manager UI (mutually exclusive):

| Profile | Use when | Local ports | Remote dependency |
|---|---|---|---|
| `skywatch` (monolithic) | The Pi is also where you read the UI. | `:8080`, `:8765` | None |
| `skywatch-edge` (sender) | Multiple Pis, one central UI/archive host. | None | A `skywatch.central` reachable on the LAN |

Edge mode is much lighter on the Pi: no Mictronics DB load, no
web-asset serving, no Mongo writer.  It just decodes locally and
ships `Delta`s over WebSocket (with an on-disk spool to ride out a
transient central outage).  Pi 3B running edge-only + ultrafeeder +
dumpvdl2 fits comfortably in 1 GB RAM.

To run edge mode you need three .env values (the manager prompts
for them when you tick the box):

* `SKYWATCH_EDGE_NAME`    — stable site id (e.g. hostname).
* `SKYWATCH_CENTRAL_URL`  — `ws://central.lan:8767/ingest`.
* `SKYWATCH_INGEST_TOKEN` — bearer token; mint one with the helper
  below.

### Generating an ingest token

Tokens are just shared secrets — there is no central authority
issuing them.  Mint one on the central host:

```bash
python -m skywatch.central gen-token
# → e.g. nbqlj9zNxYM-rZw4uTHe9d8sjJ2cFkR-W3Z1lQc8YfA
```

Paste the same value into `SKYWATCH_INGEST_TOKEN` on the central
(its own env file) and on every edge (via the manager UI's secret
field).  How it's used: the edge sends `{"type":"hello","token":...}`
as its first WS frame after connect; the central checks it with
`hmac.compare_digest` and closes the socket on mismatch.  Tokens
travel in cleartext over the plain `ws://` channel — keep ingest on
a trusted LAN or terminate TLS at a reverse proxy in front of the
central's `--ingest-bind` port.

## Manager UI

A single Flask app served on `:8090`.  One section per feeder:

* **Site config** — lat/lon, altitude, MLAT enable/disable, timezone.
* **Per-feeder enable + key** — toggle, paste sharing key, optional
  extras (e.g. fr24's call sign field).
* **Status panel** — shows each container's state, last-seen
  message timestamp, and frame-rate counter.  Direct links to each
  aggregator's stats page.
* **Apply** — writes `pi-feeder/.env`, then runs
  `docker compose up -d --remove-orphans` and re-evaluates the
  active profile set.

## Path to a baked Pi image

For shipping a `.img.xz` people can flash directly to an SD card,
build with [pi-gen](https://github.com/RPi-Distro/pi-gen) using the
recipe in [`docs/building-image.md`](docs/building-image.md).
That's a separate one-shot process — `install.sh` runs inside the
chroot during the pi-gen `02-stage-feeder` step and the rest of the
image is just stock Pi OS Lite.

## Out of scope

* **HFDL / SATCOM** receivers.  Same shape as VDL2 but different
  hardware (HF SDR, IFF SDR) — easy to add as another container
  later.
* **MLAT server**.  ultrafeeder ships an MLAT *client* per feeder
  (PiAware MLAT, FR24 MLAT, etc.); running our own multilateration
  server is a separate, harder project.
* **TLS / HTTPS** for the manager UI.  The Pi sits on the LAN; if
  you want it remote, put a reverse proxy + cert in front of it.
* **User authentication** for the manager.  Same reasoning — LAN
  trust assumed.  Document and offer Tailscale / WireGuard as the
  recommended remote-access path.

## Layout

```
pi-feeder/
├── README.md                    ← you are here
├── docker-compose.yml           ← all services; feeders gated by profiles
├── .env.example                 ← config template the manager copies on first run
├── manager/                     ← Flask web console
│   ├── Dockerfile
│   ├── app.py
│   ├── requirements.txt
│   ├── templates/
│   │   └── index.html
│   └── static/
│       └── styles.css
├── scripts/
│   └── install.sh               ← one-shot bootstrapper for fresh Pi OS Lite
└── docs/
    └── building-image.md        ← path to a baked .img.xz via pi-gen
```
