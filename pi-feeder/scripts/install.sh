#!/usr/bin/env bash
# pi-feeder one-shot installer — turns a fresh Raspberry Pi OS Lite
# (Bookworm 64-bit) into a running ADS-B / VDL2 feeder hub.
#
# Idempotent: re-running on an already-installed Pi is safe and
# performs whatever step is missing.
#
# What it does:
#   1. apt update + install Docker + Compose + RTL-SDR udev rules.
#   2. Add the invoking user to the `docker` group so subsequent
#      `docker compose` commands don't need sudo.
#   3. Clone (or update) the skywatch repo into /opt/skywatch.
#   4. Seed pi-feeder/.env from .env.example if absent.
#   5. Bring up the always-on services (manager + ultrafeeder).
#   6. Install a systemd unit so the stack restarts on reboot.
#
# Usage (on a fresh Pi):
#   curl -fsSL <repo-raw>/pi-feeder/scripts/install.sh | bash
#
# or, if the repo is already cloned somewhere:
#   sudo bash /path/to/pi-feeder/scripts/install.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/metahertz/skywatch.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/skywatch}"
INSTALL_USER="${SUDO_USER:-${USER}}"

log() { printf '\n[\033[1;33mpi-feeder\033[0m] %s\n' "$*"; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        log "Re-running with sudo..."
        exec sudo -E "$0" "$@"
    fi
}

check_arch() {
    local arch
    arch=$(uname -m)
    if [[ "$arch" != "aarch64" ]]; then
        log "WARNING: detected $arch — pi-feeder targets arm64 (aarch64)."
        log "         Pi 3/4/5 with 64-bit Pi OS Lite is the supported config."
        log "         Continuing anyway — some upstream images may not exist for $arch."
    fi
}

apt_install() {
    log "apt update + base packages"
    apt-get update -y
    apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git \
        rtl-sdr librtlsdr-dev usbutils \
        python3 python3-venv
}

install_docker() {
    if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
        log "Docker + Compose already installed; skipping."
        return
    fi
    log "Installing Docker via the official convenience script"
    # The official get.docker.com script handles arm64 + Bookworm
    # cleanly and includes the compose plugin.
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
}

add_user_to_docker_group() {
    if id -nG "$INSTALL_USER" | grep -qw docker; then
        return
    fi
    log "Adding $INSTALL_USER to the docker group (effective on next login)"
    usermod -aG docker "$INSTALL_USER"
}

install_rtlsdr_udev() {
    # The rtl-sdr Debian package ships a udev rule but it's worth
    # double-checking the dvb_usb_rtl28xxu kernel driver isn't
    # claiming the device (a common Pi-OS gotcha).
    local blacklist=/etc/modprobe.d/blacklist-rtlsdr.conf
    if [[ ! -f $blacklist ]]; then
        log "Blacklisting kernel DVB drivers that grab RTL-SDR dongles"
        cat > "$blacklist" <<'EOF'
# pi-feeder: keep dvb_usb_rtl28xxu off so dump1090 / dumpvdl2 can
# claim the RTL-SDR USB devices via libusb.
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
        # Take effect immediately if possible; harmless if not loaded.
        rmmod dvb_usb_rtl28xxu 2>/dev/null || true
        rmmod rtl2832         2>/dev/null || true
    fi
}

clone_or_update_repo() {
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        log "Updating existing checkout at $INSTALL_DIR"
        git -C "$INSTALL_DIR" pull --ff-only || true
    elif [[ -d "$INSTALL_DIR" ]]; then
        log "$INSTALL_DIR exists but is not a git checkout; leaving alone"
    else
        log "Cloning $REPO_URL into $INSTALL_DIR"
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
    chown -R "$INSTALL_USER:$INSTALL_USER" "$INSTALL_DIR"
}

seed_env() {
    local feeder_dir="$INSTALL_DIR/pi-feeder"
    if [[ ! -f "$feeder_dir/.env" && -f "$feeder_dir/.env.example" ]]; then
        log "Seeding pi-feeder/.env from .env.example"
        cp "$feeder_dir/.env.example" "$feeder_dir/.env"
        chown "$INSTALL_USER:$INSTALL_USER" "$feeder_dir/.env"
    fi
}

bring_up_stack() {
    log "Bringing up the manager + ultrafeeder (sudo)"
    cd "$INSTALL_DIR/pi-feeder"
    # Build the local images (skywatch + manager); pull the others.
    docker compose pull --ignore-pull-failures || true
    docker compose up -d --build --remove-orphans
}

install_systemd_unit() {
    local unit=/etc/systemd/system/pi-feeder.service
    if [[ -f $unit ]]; then
        log "systemd unit already installed; skipping"
        return
    fi
    log "Installing systemd unit (pi-feeder.service)"
    cat > "$unit" <<EOF
[Unit]
Description=pi-feeder — ADS-B / VDL2 feeder hub
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$INSTALL_DIR/pi-feeder
ExecStart=/usr/bin/docker compose up -d --remove-orphans
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable pi-feeder.service
}

print_summary() {
    local ip
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    : "${ip:=<pi-ip>}"
    cat <<EOF

[pi-feeder] DONE.

Open the manager:  http://$ip:8090/
tar1090 (live):    http://$ip/
Skywatch UI:       http://$ip:8080/    (after enabling the 'skywatch' feeder)

Next steps:
  1. Open the manager URL above.
  2. Set lat/lon and enable the feeders you want.
  3. Paste sharing keys for each (links to obtain them are in the UI).
  4. Click "Apply & Restart".

If you have two SDR dongles, set their serial numbers FIRST so the
containers can address them by name:
   rtl_eeprom -d 0 -s rtlsdr-1090
   rtl_eeprom -d 1 -s rtlsdr-vdl2

EOF
}

main() {
    require_root "$@"
    check_arch
    apt_install
    install_docker
    add_user_to_docker_group
    install_rtlsdr_udev
    clone_or_update_repo
    seed_env
    bring_up_stack
    install_systemd_unit
    print_summary
}

main "$@"
