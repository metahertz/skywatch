# Dockerized secondary receiver — VDL Mode 2 / CPDLC

Skywatch's primary 1090 MHz feed already comes from `dump1090` running
either natively, on a Pi, or in some other container.  This directory
adds the **secondary receiver chain** — `dumpvdl2` listening on ~136 MHz
VHF for CPDLC / ACARS / VDL2 link traffic — packaged in Docker.

## The macOS situation (read this first)

**Docker Desktop on macOS cannot pass a USB device through to a
container.**  The host runs Docker Engine inside a hidden LinuxKit VM,
and that VM has no path to USB devices on the Mac.  This is a
well-known limitation, not a bug we can route around with `--device`.

So we use a **two-process pattern** on macOS:

```
   ┌──────────────────────────────┐
   │ Mac host                     │
   │                              │       ┌─────────────────────────┐
   │  RTL-SDR ─► rtl_tcp :1234 ───┼──TCP─►│ Docker container        │
   │  (host owns USB)             │       │  nc :1234 |             │
   │                              │       │  tail -c +13 |          │
   │                              │       │  dumpvdl2 --iq-file -   │
   │                              │       │  → JSON :5555           │
   └──────────────────────────────┘       └─────────────────────────┘
                                                      │
                                                      ▼
                                          skywatch (native or container)
                                          --vdl2 localhost:5555
```

`rtl_tcp` runs natively on the Mac (it owns the USB dongle) at a
fixed centre frequency and sample rate.  Inside the container, `nc`
connects to it, `tail -c +13` strips the 12-byte rtl_tcp protocol
header, and the raw 8-bit IQ stream is piped into
`dumpvdl2 --iq-file -`.  Skywatch consumes the resulting JSON output
on port 5555.

This avoids SoapySDR entirely (the rtl_tcp SoapySDR module isn't
packaged for Debian) and keeps the image small.

Linux hosts can skip `rtl_tcp` and use direct USB passthrough — see
the `usb` compose profile below.

---

## macOS recipe

### 1. Install host-side tools (one-off)

```bash
brew install librtlsdr
```

`librtlsdr` ships `rtl_tcp`, the only thing we run natively.

### 2. Plug the SDR in and start `rtl_tcp`

The container expects rtl_tcp pre-tuned to a centre frequency that
covers all three default VDL2 channels (136.725-136.975 MHz, so
136.85 MHz is the natural midpoint), at a 1.05 MS/s sample rate to
match dumpvdl2's default oversample.  Keep this terminal open:

```bash
# -a 0.0.0.0  bind on all interfaces so the Docker VM can reach it
# -p 1234     control + data port (matches RTL_TCP_PORT in compose)
# -f 136.85M  centre frequency (matches VDL2_CENTERFREQ)
# -s 1.05M    sample rate (matches dumpvdl2 default oversample=10)
# -g 40       tuner gain (~40 dB)  — tune to your antenna setup
rtl_tcp -a 0.0.0.0 -p 1234 -f 136850000 -s 1050000 -g 40
```

If you have two RTL-SDRs (one for 1090 dump1090, one for VDL2),
distinguish by `-d 0` / `-d 1`.

> **Why this matters.**  The container connects to rtl_tcp purely
> as a byte stream — it does not send back tuning commands.  rtl_tcp
> uses whatever frequency / sample rate it was started with, so they
> have to match the VDL2 channel plan up front.  If you change
> `VDL2_CENTERFREQ` in compose, change rtl_tcp's `-f` to match.

### 3. Build + start `dumpvdl2`

```bash
cd docker
docker compose --profile rtltcp up --build
```

First build is ~3 minutes (compiles `libacars` and `dumpvdl2` from
source).  Subsequent starts are instant.

### 4. Point skywatch at the JSON output

In another terminal:

```bash
python3 -m skywatch \
    --beast localhost:30005 \
    --vdl2  localhost:5555 --vdl2-name home \
    --lat $LAT --lon $LON
```

The container exposes `5555:5555` to the host, so a native skywatch
reaches `dumpvdl2` via `localhost:5555`.

### 5. Verify it's working

```bash
# JSON frames should be flowing
nc localhost 5555 | head -3

# Once skywatch is running, the topbar shows a `VDL2` counter that
# increments per frame, and CPDLC messages appear in the event ticker
# (cyan pill, `▲` for uplink / `▼` for downlink).
```

---

## Linux recipe (direct USB passthrough)

```bash
# 1. Confirm the SDR is visible to the host.
lsusb | grep -i rtl

# 2. Bring it up — `--profile usb` mounts /dev/bus/usb into the
#    container and runs dumpvdl2 against it directly.
cd docker
docker compose --profile usb up --build
```

Frequencies, gain, and output ports are configurable via the same
environment variables documented in the Dockerfile.

---

## Tuning

| Env var          | Default                                                                 | Notes                                                                                |
|------------------|-------------------------------------------------------------------------|--------------------------------------------------------------------------------------|
| `VDL2_FREQS`     | `136725000 136775000 136825000 136875000 136975000`                     | Five operational European VDL2 channels (London FIR / EUROCONTROL).  CSC last.       |
| `VDL2_CENTERFREQ`| `136850000`                                                             | Mid-point of the channel plan; rtl_tcp on the host must be tuned to match.           |
| `VDL2_OUT_PORT`  | `5555`                                                                  | TCP port for JSON output (host-mapped to same).                                      |
| `RTL_TCP_HOST`   | `host.docker.internal`                                                  | Resolved via `extra_hosts`. Override with the host's LAN IP if your network needs it.|
| `RTL_TCP_PORT`   | `1234`                                                                  | Match `rtl_tcp -p`.                                                                  |

**Channel plan reference** (per [Wikipedia VHF Data Link](https://en.wikipedia.org/wiki/VHF_Data_Link)):

| MHz       | Provider | Notes                                                             |
|-----------|----------|-------------------------------------------------------------------|
| 136.725   | ARINC    |                                                                   |
| 136.775   | SITA     |                                                                   |
| 136.825   | ARINC    |                                                                   |
| 136.875   | SITA     |                                                                   |
| 136.975   | both     | Common Signalling Channel (CSC) — mandatory worldwide.            |

For non-European regions, swap the channel list:
- **North America** typically uses 136.700, 136.750, 136.800, 136.850, 136.975.
- **Asia/Pacific** allocations vary by ANSP — check the local AIP.

Override per service in `docker-compose.yml` or with `-e VAR=...` on
`docker compose run`.

---

## Why not just run it natively on macOS?

You can — `dumpvdl2` builds cleanly on macOS via Homebrew if you'd
rather skip Docker.  The container exists for parity with deployments
where the rest of the stack is already containerised, and so we don't
have to chase macOS-vs-Linux build differences as the upstream
project moves.  If you're starting from scratch on a Mac and only
need this one piece, native is the lower-friction path.

---

## Stopping

```bash
docker compose --profile rtltcp down
# or:
docker compose --profile usb down
```

`rtl_tcp` on the host needs to be stopped separately (Ctrl-C).

---

## Troubleshooting

**`Ncat: Network is unreachable` from the container.**
The container couldn't route to whatever `host.docker.internal`
resolved to.  Two checks:

1. Confirm rtl_tcp on the host is actually bound to all interfaces:
   ```
   lsof -nP -iTCP -sTCP:LISTEN | grep 1234
   # should show *:1234 — not 127.0.0.1:1234
   ```
   If it's bound only to localhost, restart with `rtl_tcp -a 0.0.0.0`.

2. Confirm `host.docker.internal` resolves and is reachable from
   inside the container:
   ```
   docker run --rm --add-host=host.docker.internal:host-gateway \
       alpine sh -c 'getent hosts host.docker.internal && \
                     ping -c1 -W1 host.docker.internal'
   ```
   If it resolves but ping fails, fall back to passing the host's
   actual LAN IP through the env var:
   ```
   RTL_TCP_HOST=192.168.1.42 docker compose --profile rtltcp up
   ```

**`Connection refused` from dumpvdl2 to rtl_tcp.**
The host is reachable but no rtl_tcp is listening.  Start it
(see step 2 of the recipe above).

**`No such device` from dumpvdl2 (Linux USB profile).**
Confirm `/dev/bus/usb` exists on the host and that the user running
`docker` has access — on systemd-udev distros you may need an
`rtl-sdr` group membership or a matching udev rule.

**Skywatch shows `VDL2` counter at 0.**
Verify dumpvdl2 is decoding (look at its container logs — it prints a
line per decoded frame) and that the JSON socket is reachable.
`nc localhost 5555 | head` should produce JSON; if it produces
nothing, dumpvdl2 isn't seeing demodulator output (gain too low,
antenna disconnected, wrong frequencies for your region).
