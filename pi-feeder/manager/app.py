"""pi-feeder manager — Flask web UI for configuring ADS-B/VDL2 feeders.

Single-process Flask app, served on :8090, that:

  * reads/writes the sibling `pi-feeder/.env` file
  * shows the current state of each docker compose service
  * re-runs `docker compose up -d --remove-orphans` after every save

Trust model: this binds to all interfaces by default and assumes the
Pi is on a trusted LAN.  See README.md for the recommended remote-access
path (Tailscale / WireGuard).  No auth is built in v1.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

# Bind-mounted by docker-compose.  Falls back to the parent dir for
# native (non-container) invocation during development.
PI_FEEDER_DIR = Path(os.environ.get("PI_FEEDER_DIR", Path(__file__).resolve().parent.parent))
ENV_PATH = PI_FEEDER_DIR / ".env"
ENV_EXAMPLE = PI_FEEDER_DIR / ".env.example"
COMPOSE_FILE = PI_FEEDER_DIR / "docker-compose.yml"


# ───────────────────────────────────────────────────────────────────
# Feeder catalogue.  Each entry maps to one compose `profiles:` value
# and carries metadata the form needs to render its config block.
# ───────────────────────────────────────────────────────────────────

FEEDERS: list[dict] = [
    {
        "id": "skywatch", "label": "Skywatch — monolithic (UI on this host)",
        "fields": [],
        "help": "Decode + UI in one process on this Pi.  Pick this when "
                "the radio host is also where you read the map.  "
                "Mutually exclusive with 'Skywatch — edge mode'.",
        "stats_url": None,
    },
    {
        "id": "skywatch-edge", "label": "Skywatch — edge mode (sender to a remote central)",
        "fields": [
            {"key": "SKYWATCH_EDGE_NAME", "label": "Receiver name (site id)",
             "hint": "Stable identifier the central uses to attribute "
                     "frames to this site.  Pick something distinct per Pi "
                     "(e.g. hostname)."},
            {"key": "SKYWATCH_CENTRAL_URL", "label": "Central ingest URL",
             "hint": "e.g. ws://central.lan:8767/ingest"},
            {"key": "SKYWATCH_INGEST_TOKEN", "label": "Ingest bearer token",
             "secret": True,
             "hint": "Issued by the operator running the central node."},
        ],
        "help": "Pi only does receive/decode; UI, API and Mongo archive "
                "live on a remote central.  No local web UI.  Mutually "
                "exclusive with 'Skywatch — monolithic'.",
        "stats_url": None,
    },
    {
        "id": "vdl2", "label": "VDL Mode 2 (CPDLC / ACARS)",
        "fields": [
            {"key": "SDR_VDL2_GAIN", "label": "VDL2 SDR gain (0..49.6)",
             "default": "44"},
        ],
        "help": "Decodes 136 MHz controller-pilot data link.  Requires a "
                "second RTL-SDR; configure SDR_VDL2_SERIAL above.",
        "stats_url": None,
    },
    {
        "id": "piaware", "label": "FlightAware (PiAware)",
        "fields": [
            {"key": "PIAWARE_FEEDER_ID", "label": "Feeder ID",
             "secret": False,
             "hint": "Run once unconfigured, then claim at "
                     "flightaware.com/adsb/piaware/claim and paste the ID here."},
        ],
        "help": "Free, MLAT-enabled, well supported.",
        "stats_url": "https://flightaware.com/adsb/stats/user/",
    },
    {
        "id": "fr24", "label": "Flightradar24",
        "fields": [
            {"key": "FR24_KEY", "label": "Sharing key", "secret": True,
             "hint": "Obtain by running the upstream signup script — "
                     "see README."},
        ],
        "help": "Free for personal feeders; obtains a Premium account.",
        "stats_url": "https://www.flightradar24.com/account/data-sharing",
    },
    {
        "id": "adsbexchange", "label": "ADSBExchange",
        "fields": [
            {"key": "ADSBX_UUID", "label": "UUID (auto-generated)",
             "secret": False,
             "hint": "Leave blank on first run; the container generates "
                     "one and registers it.  Persisted in this .env."},
        ],
        "help": "Unfiltered hobbyist aggregator; free MLAT.",
        "stats_url": "https://www.adsbexchange.com/myip/",
    },
    {
        "id": "planefinder", "label": "PlaneFinder",
        "fields": [
            {"key": "PLANEFINDER_SHARECODE", "label": "Share code", "secret": True},
        ],
        "help": "",
        "stats_url": "https://planefinder.net/sharing/data",
    },
    {
        "id": "radarbox", "label": "RadarBox",
        "fields": [
            {"key": "RBFEEDER_KEY", "label": "Sharing key", "secret": True},
        ],
        "help": "",
        "stats_url": "https://www.radarbox.com/stations",
    },
    {
        "id": "opensky", "label": "OpenSky Network (academic)",
        "fields": [
            {"key": "OPENSKY_USER", "label": "Username"},
            {"key": "OPENSKY_SERIAL", "label": "Serial",
             "hint": "Generated when you register a feeder at opensky-network.org."},
        ],
        "help": "Public / research aggregator.  No premium perks but "
                "your data goes into open science.",
        "stats_url": "https://opensky-network.org/my-opensky",
    },
    {
        "id": "airframes-1090", "label": "airframes.io (1090 MHz)",
        "fields": [
            {"key": "AIRFRAMES_STATION_ID", "label": "Station ID"},
            {"key": "AIRFRAMES_FEEDER_KEY", "label": "Feeder key", "secret": True},
        ],
        "help": "Non-aircraft-tracking aggregator focused on Mode S, "
                "VDL2, ACARS, HFDL.",
        "stats_url": "https://app.airframes.io/stations",
    },
    {
        "id": "airframes-vdl2", "label": "airframes.io (VDL2)",
        "fields": [],
        "help": "Forwards dumpvdl2's JSON output to airframes.io.  "
                "Requires the VDL2 receiver to be enabled too.",
        "stats_url": "https://app.airframes.io/stations",
    },
]


# ───────────────────────────────────────────────────────────────────
# .env read/write
# ───────────────────────────────────────────────────────────────────

_ENV_LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def load_env() -> dict[str, str]:
    """Read the .env file (or seed from .env.example on first run)."""
    if not ENV_PATH.exists() and ENV_EXAMPLE.exists():
        ENV_PATH.write_text(ENV_EXAMPLE.read_text())
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        return out
    for raw in ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def save_env(values: dict[str, str]) -> None:
    """Round-trip rewrite preserving the .env.example comments and
    section ordering — we walk the example file line by line and
    substitute KEY=... lines.  Keys missing from the form get left
    at whatever was in .env (or .env.example if .env didn't exist).
    """
    template = ENV_EXAMPLE.read_text() if ENV_EXAMPLE.exists() else ""
    out: list[str] = []
    seen: set[str] = set()
    for raw in template.splitlines():
        m = _ENV_LINE_RE.match(raw.strip())
        if m:
            k = m.group(1)
            if k in values:
                out.append(f"{k}={values[k]}")
                seen.add(k)
                continue
        out.append(raw)
    # Tail: any keys not in the template (shouldn't happen, but keep
    # round-trip safe).
    for k, v in values.items():
        if k not in seen:
            out.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(out) + "\n")


# ───────────────────────────────────────────────────────────────────
# Compose interaction
# ───────────────────────────────────────────────────────────────────

def compose(*args: str) -> tuple[int, str, str]:
    """Run `docker compose` with COMPOSE_PROFILES from the .env."""
    env = os.environ.copy()
    env_vars = load_env()
    env.update(env_vars)
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE)] + list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                          cwd=PI_FEEDER_DIR)
    return proc.returncode, proc.stdout, proc.stderr


def container_status() -> dict[str, str]:
    """Return {service_name: state} for every compose service."""
    rc, out, err = compose("ps", "--format", "json")
    if rc != 0:
        return {}
    statuses: dict[str, str] = {}
    # `docker compose ps --format json` emits one JSON object per
    # line (NDJSON) — parse robustly.
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, list):
            for d in doc:
                statuses[d.get("Service", "")] = d.get("State", "")
        elif isinstance(doc, dict):
            statuses[doc.get("Service", "")] = doc.get("State", "")
    return statuses


# ───────────────────────────────────────────────────────────────────
# Flask routes
# ───────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/")
def index():
    env = load_env()
    enabled = set((env.get("COMPOSE_PROFILES") or "").split(","))
    enabled.discard("")
    statuses = container_status()
    return render_template(
        "index.html",
        env=env,
        feeders=FEEDERS,
        enabled=enabled,
        statuses=statuses,
    )


@app.route("/save", methods=["POST"])
def save():
    env = load_env()
    form = request.form

    # Site-wide config.
    for k in ("FEEDER_LAT", "FEEDER_LON", "FEEDER_ALT_M", "FEEDER_TZ",
              "SDR_1090_SERIAL", "SDR_VDL2_SERIAL",
              "SDR_1090_GAIN", "SDR_VDL2_GAIN",
              "VDL2_CENTERFREQ"):
        if k in form:
            env[k] = form[k].strip()

    # Per-feeder fields (only the ones declared in FEEDERS).
    for f in FEEDERS:
        for fld in f.get("fields", []):
            k = fld["key"]
            if k in form:
                env[k] = form[k].strip()

    # Profile selection.
    enabled = ["ultrafeeder"]   # always-on core
    for f in FEEDERS:
        if form.get(f"enable_{f['id']}") == "on":
            enabled.append(f["id"])
    env["COMPOSE_PROFILES"] = ",".join(enabled)

    save_env(env)

    # Apply: bring up enabled services, take down disabled ones.
    rc, out, err = compose("up", "-d", "--remove-orphans")
    if rc != 0:
        # Show stderr to the operator if compose blew up.
        return f"<pre>compose up failed:\n{err}</pre>", 500

    return redirect(url_for("index"))


@app.route("/status")
def status():
    return jsonify(container_status())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090)
