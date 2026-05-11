# Building a baked pi-feeder image

For shipping a `.img.xz` people can flash directly to an SD card,
build with [pi-gen](https://github.com/RPi-Distro/pi-gen) — the same
tool the official Raspberry Pi Foundation uses to produce stock Pi
OS Lite.

This is **out of scope for the pi-feeder runtime** (the runtime is
just Docker Compose + a Flask manager).  This doc captures the
recipe so it can be revived when someone wants to ship images.

## Prerequisites

* An x86_64 Linux build host with binfmt arm64 emulation.  Ubuntu
  22.04+ or Debian 12 are the well-trodden paths.  pi-gen on macOS
  does NOT work (it needs `apt-get` + binfmt).
* ~12 GB free disk for the pi-gen workspace.
* The pi-gen repo cloned somewhere:
  ```bash
  git clone https://github.com/RPi-Distro/pi-gen.git
  cd pi-gen
  ```

## Adding a `02-pi-feeder` stage

pi-gen runs a sequence of numbered stages.  We add a custom stage
between stock Pi OS Lite (stage 2) and the desktop variant (stage 3
onwards, which we skip):

```
pi-gen/
├── stage0/        # bootstrap
├── stage1/        # core OS
├── stage2/        # Pi OS Lite ← we want this as the base
├── stage3/        # (skipped — desktop)
├── stage4/        # (skipped)
├── stage5/        # (skipped)
└── stage-pi-feeder/    ← new stage WE add
```

Layout of the new stage:

```
stage-pi-feeder/
├── prerun.sh                   # symlink to ../stage2/prerun.sh
├── EXPORT_IMAGE                # marker file: "yes, build a .img"
├── EXPORT_NOOBS                # absent: skip NOOBS variant
├── 00-pi-feeder/
│   ├── 00-run.sh               # the actual install, run inside chroot
│   └── files/                  # any static files to drop into /etc
└── prerun.sh
```

`stage-pi-feeder/EXPORT_IMAGE` (any non-empty content):
```
pi-feeder-lite-arm64
```

`stage-pi-feeder/00-pi-feeder/00-run.sh` is where the heavy lifting
happens.  Run inside the image's chroot, with networking via the
build host's resolver:

```bash
#!/bin/bash -e
on_chroot << 'CHROOT'
set -e
# 1. Repos / docker
apt-get update
apt-get install -y --no-install-recommends \
    curl ca-certificates git python3 python3-pip \
    rtl-sdr librtlsdr-dev usbutils
curl -fsSL https://get.docker.com | sh
systemctl enable docker

# 2. Clone the skywatch repo into /opt
git clone --depth 1 https://github.com/<your-fork>/skywatch.git /opt/skywatch
chown -R pi:pi /opt/skywatch

# 3. Pre-pull the upstream feeder images so the SD card boots
#    "ready to go" — saves a 5-minute first-run pull on the
#    user's home Wi-Fi.
docker pull ghcr.io/sdr-enthusiasts/docker-adsb-ultrafeeder:latest
docker pull ghcr.io/sdr-enthusiasts/docker-piaware:latest
docker pull ghcr.io/sdr-enthusiasts/docker-flightradar24:latest
docker pull ghcr.io/sdr-enthusiasts/docker-adsbexchange:latest
docker pull ghcr.io/sdr-enthusiasts/docker-planefinder:latest
docker pull ghcr.io/sdr-enthusiasts/docker-radarbox:latest
docker pull ghcr.io/sdr-enthusiasts/docker-opensky-network:latest
docker pull ghcr.io/sdr-enthusiasts/docker-airframes:latest
docker pull ghcr.io/sdr-enthusiasts/acars_router:latest

# 4. Build local images (manager, skywatch, dumpvdl2) so the user
#    doesn't pay for a `docker compose build` on first boot.
cd /opt/skywatch/pi-feeder
docker compose build manager
docker compose --profile skywatch build skywatch
docker compose --profile vdl2 build vdl2

# 5. Install the systemd unit so the stack starts on first boot
cp /opt/skywatch/pi-feeder/scripts/pi-feeder.service \
   /etc/systemd/system/pi-feeder.service
systemctl enable pi-feeder.service

# 6. Drop in the udev blacklist for kernel-mode RTL-SDR drivers
cp /opt/skywatch/pi-feeder/scripts/blacklist-rtlsdr.conf \
   /etc/modprobe.d/blacklist-rtlsdr.conf

# 7. First-boot hint for the user
cat > /etc/motd <<'MOTD'
================================================================
 pi-feeder — ADS-B / VDL2 feeder hub
 Manager UI: http://<this-host>:8090/
 tar1090:    http://<this-host>/
 First boot: open the manager and configure feeders.
================================================================
MOTD
CHROOT
```

## Building

```bash
cd pi-gen
echo 'IMG_NAME=pi-feeder' > config
echo 'TARGET_HOSTNAME=pi-feeder' >> config
echo 'STAGE_LIST="stage0 stage1 stage2 stage-pi-feeder"' >> config
sudo ./build-docker.sh
```

The output `.img.xz` lands in `deploy/`.  Flash with Raspberry Pi
Imager (or `xzcat ... | sudo dd of=/dev/sdX bs=4M`).

## Update strategy for shipped images

* Manager UI gives the user a "git pull && docker compose up" button
  to update in place — keeps shipping point-in-time images cheap.
* OR: rebuild the image quarterly, push to a public artefact host,
  let users re-flash.  No clever in-place upgrade machinery.

## Why not OctoPrint-style installer

OctoPi-style "PrintOS" tools layer ~100 MB of custom Python on top
of stock Pi OS.  We don't need that — every running piece of
pi-feeder is a Docker container, so the host stays tiny (~2.5 GB
post-install) and the upgrade story is "rebuild and reflash, OR
git pull on the host".  Trying to be clever beyond that is
maintenance debt for one operator.
