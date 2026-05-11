# dumpvdl2 — VDL Mode 2 / CPDLC / ACARS decoder, built from source.
#
# Two SDR input paths are exposed:
#   * --rtlsdr (direct USB)    — used by the `usb` compose profile on
#                                 Linux hosts.
#   * --iq-file - (stdin IQ)   — used by the `rtltcp` compose profile
#                                 on macOS / Windows.  The default CMD
#                                 connects with `nc` to an external
#                                 `rtl_tcp` server (running natively on
#                                 the Docker host) and pipes the
#                                 raw 8-bit IQ stream into dumpvdl2,
#                                 skipping the 12-byte rtl_tcp header.
#
# SoapySDR is deliberately NOT built in: the rtl_tcp SoapySDR plugin
# isn't packaged for Debian, and the iq-file pipe path achieves the
# same goal without an extra dependency.

FROM debian:bookworm-slim AS build

ARG DUMPVDL2_REF=master
ARG LIBACARS_REF=master

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git pkg-config ca-certificates \
        libglib2.0-dev libxml2-dev libjansson-dev libzmq3-dev \
        librtlsdr-dev libusb-1.0-0-dev \
    && rm -rf /var/lib/apt/lists/*

# libacars (handles ACARS reassembly + ATN/FANS-1/A CPDLC decode).
RUN git clone --depth 1 --branch ${LIBACARS_REF} \
        https://github.com/szpajder/libacars.git /src/libacars \
    && cmake -S /src/libacars -B /build/libacars \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
    && cmake --build /build/libacars --parallel \
    && cmake --install /build/libacars

# dumpvdl2 itself.
RUN git clone --depth 1 --branch ${DUMPVDL2_REF} \
        https://github.com/szpajder/dumpvdl2.git /src/dumpvdl2 \
    && cmake -S /src/dumpvdl2 -B /build/dumpvdl2 \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DRTLSDR=ON -DSOAPYSDR=OFF \
    && cmake --build /build/dumpvdl2 --parallel \
    && cmake --install /build/dumpvdl2

# ─── Runtime image ──────────────────────────────────────────────────
FROM debian:bookworm-slim
# `ncat` (nmap's netcat) is needed for two things:
#   * connecting outbound to the host's rtl_tcp (any nc would do)
#   * exposing dumpvdl2's stdout JSON as a long-lived TCP server with
#     --keep-open so skywatch can reconnect.  The plain
#     `netcat-openbsd` doesn't have -k / --keep-open.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libxml2 libjansson4 libzmq5 \
        librtlsdr0 libusb-1.0-0 \
        ncat \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/bin/dumpvdl2 /usr/local/bin/
COPY --from=build /usr/local/lib/libacars-2.so* /usr/local/lib/
RUN ldconfig

EXPOSE 5555

# Defaults match what the macOS recipe in ../README.md expects.
# The host's `rtl_tcp` must be started with matching --centerfreq and
# sample rate (1.05 MS/s ≈ 105k * oversample 10).  Override here to
# work with a different host config.
ENV RTL_TCP_HOST=host.docker.internal
ENV RTL_TCP_PORT=1234
ENV VDL2_CENTERFREQ=136850000
# Five operational European VDL Mode 2 channels (covers London FIR
# and the rest of EUROCONTROL airspace).  All five fit inside the
# 1.05 MS/s capture bandwidth centred on 136.85 MHz:
#   136.725  ARINC
#   136.775  SITA
#   136.825  ARINC
#   136.875  SITA
#   136.975  Common Signalling Channel (CSC, both providers worldwide)
ENV VDL2_FREQS="136725000 136775000 136825000 136875000 136975000"
ENV VDL2_OUT_PORT=5555

# Default CMD: connect to host rtl_tcp, skip its 12-byte header, pipe
# raw 8-bit IQ into dumpvdl2, then republish dumpvdl2's stdout JSON
# as a long-lived TCP server via `ncat -lk` so skywatch can connect,
# disconnect, and reconnect freely.  dumpvdl2's only network-output
# options are UDP and ZeroMQ — neither matches our existing TCP
# ingest path — hence the ncat bridge.
#
# The Linux `usb` compose profile overrides this with a direct
# `--rtlsdr 0` invocation.
CMD ["sh", "-c", "exec ncat ${RTL_TCP_HOST} ${RTL_TCP_PORT} \
    | tail -c +13 \
    | dumpvdl2 \
        --iq-file - \
        --sample-format U8 \
        --centerfreq ${VDL2_CENTERFREQ} \
        --output decoded:json:file:path=/dev/stdout \
        ${VDL2_FREQS} \
    | ncat -l -k -p ${VDL2_OUT_PORT} --send-only"]
