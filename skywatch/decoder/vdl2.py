"""dumpvdl2 JSON ingest — VDL Mode 2 / ACARS / CPDLC frame parsing.

CPDLC (Controller-Pilot Data Link Communications) is operationally
carried over VDL Mode 2 (~136 MHz VHF), HFDL, or SATCOM — never on
1090 MHz.  To capture controller-pilot exchanges, skywatch ingests
the JSON output of the external `dumpvdl2` daemon (one process per
VDL2 receiver), running it as a sibling to dump1090.

Wire format (verified against dumpvdl2 source — fmtr-json.c, avlc.c,
and libacars/{acars,cpdlc}.c):

```
{
  "vdl2": {
    "app": {...},
    "t": {"sec": ..., "usec": ...},
    "station": "...",
    "freq": ...,
    "sig_level": ...,
    "burst_len_octets": ...,
    "avlc": {
      "src": {"addr": "<6-hex ICAO or 7-hex ground id>",
              "type": "Aircraft" | "Ground station" | ...,
              "status": "..."},
      "dst": {"addr": "...", "type": "...", "status": "..."},
      "cr":  "Command" | "Response",
      "frame_type": "...",
      "cmd": "I" | "RR" | "UI" | ...,
      "acars": {
        "err": false, "crc_ok": true, "more": false,
        "reg": "<tail>", "mode": "...", "label": "<2-char>",
        "blk_id": "...", "ack": "...", "flight": "...",
        "msg_num": "...", "msg_num_seq": "...",
        "sublabel": "...", "mfi": "...", "msg_text": "..."
      },
      "cpdlc": {
        "err": false,
        ...ASN.1-derived sub-tree with message data / atc-uplink-msg-elem-id...
      }
    }
  }
}
```

Both `acars` and `cpdlc` may appear at `avlc` level.  A CPDLC payload
may also ride INSIDE an ACARS message (FANS-1/A) — in that case
`cpdlc` lives inside `acars.arinc622` (per libacars).  The parser
walks the tree defensively: every layer is optional; missing fields
are tolerated; malformed lines are dropped silently with a counter.

Direction (uplink = ground→aircraft, downlink = aircraft→ground) is
classified from the AVLC src.type / dst.type fields rather than
cmd/response, since those flags vary across frame types.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("skywatch.decoder.vdl2")


# Default tag when the parser is built without an explicit receiver_id.
DEFAULT_RECEIVER_ID = "default"


# Diagnostic: when SKYWATCH_TRACE_VDL2 is set, dump raw + classified
# data for matching frames to stderr.  Use to debug "why is this
# message empty?" — capture a fixture and I'll improve the parser.
#
# Filter syntax: comma-separated key=value pairs (any/all match).
# Recognised keys: `kind` (e.g. other,cpdlc), `icao` (6-hex address),
# `min_kind=other` (special: trace anything classified as kind OR
# "looser" — handy for debugging unrecognised frames).  An empty
# value (just `SKYWATCH_TRACE_VDL2=`) traces every frame.
#
# Examples:
#   SKYWATCH_TRACE_VDL2=             # everything (very chatty)
#   SKYWATCH_TRACE_VDL2=kind=other   # only unclassified frames
#   SKYWATCH_TRACE_VDL2=icao=407C0B  # one specific aircraft
def _parse_trace_filter(s: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        out.setdefault(k.strip(), set()).add(v.strip().upper())
    return out


_TRACE_RAW = os.environ.get("SKYWATCH_TRACE_VDL2")
_TRACE_FILTER = _parse_trace_filter(_TRACE_RAW) if _TRACE_RAW else None
_TRACE_ENABLED = _TRACE_RAW is not None


def _trace_match(frame) -> bool:
    """Return True if this frame matches the trace filter."""
    if not _TRACE_ENABLED:
        return False
    if not _TRACE_FILTER:
        return True
    f = _TRACE_FILTER
    if "kind" in f and frame.kind.upper() not in f["kind"]:
        return False
    if "icao" in f:
        haystack = {a.upper() for a in (frame.aircraft_icao,
                                        frame.src_icao,
                                        frame.dst_icao) if a}
        if not (haystack & f["icao"]):
            return False
    return True


def _emit_trace(frame) -> None:
    print(
        f"[VDL2 TRACE] kind={frame.kind} dir={frame.direction} "
        f"src={frame.src_icao} dst={frame.dst_icao} "
        f"label={frame.label!r} text={frame.text!r}\n"
        f"             raw={frame.raw}",
        file=sys.stderr, flush=True,
    )


# Frame "kind" classification — drives which UI bucket the message
# lands in (ticker pill, detail-pane row colour, persistence schema).
KIND_CPDLC = "cpdlc"        # ATN B1/B2 OR FANS-1/A CPDLC content
KIND_ACARS = "acars"        # Plain ACARS (no CPDLC payload)
KIND_ATN_CM = "atn_cm"      # ATN context management (logon/logoff)
KIND_LINK_MGMT = "link_mgmt"  # X.25 / VDL link establishment
KIND_OTHER = "other"

# Direction relative to the aircraft side of the link.
DIR_UPLINK = "uplink"       # ground → aircraft
DIR_DOWNLINK = "downlink"   # aircraft → ground
DIR_PEER = "peer"           # neither / both / unknown


@dataclass
class VdlFrame:
    """One decoded VDL2 frame.  Mirrors `BeastFrame` shape so the
    engine can treat the two ingest paths uniformly."""
    raw: str                       # original JSON line for archive/debug
    receiver_id: str               # which dumpvdl2 instance produced it
    ts: float                      # wall-clock seconds (engine receipt time)
    frame_ts: float | None         # message timestamp from dumpvdl2 (sec.usec)
    src_icao: str | None           # 6-hex ICAO when src is an aircraft
    dst_icao: str | None           # 6-hex ICAO when dst is an aircraft
    aircraft_icao: str | None      # whichever endpoint is the aircraft
    direction: str                 # DIR_*
    kind: str                      # KIND_*
    label: str | None              # ACARS label or CPDLC msg id, for display
    text: str | None               # human-readable summary line for the UI
    flight: str | None             # ACARS flight no (callsign), if present
    reg: str | None                # tail registration if ACARS reported one
    sig_level: float | None
    payload: dict[str, Any] = field(default_factory=dict)


def parse_vdl2_line(line: str, receiver_id: str = DEFAULT_RECEIVER_ID) -> VdlFrame | None:
    """Parse one newline-delimited dumpvdl2 JSON line into a VdlFrame.

    Returns None for malformed JSON or frames without an AVLC layer
    we can identify.  Never raises — the BEAST parser sets the
    precedent: drop bad input, keep the engine up."""
    line = line.strip()
    if not line:
        return None
    try:
        doc = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    vdl2 = doc.get("vdl2") if isinstance(doc, dict) else None
    if not isinstance(vdl2, dict):
        return None

    # Top-level metadata
    sig = _f(vdl2.get("sig_level"))
    t_obj = vdl2.get("t") or {}
    if isinstance(t_obj, dict):
        sec = _f(t_obj.get("sec"))
        usec = _f(t_obj.get("usec"))
        frame_ts = (sec or 0.0) + (usec or 0.0) / 1e6 if sec else None
    else:
        frame_ts = None

    avlc = vdl2.get("avlc")
    if not isinstance(avlc, dict):
        # No link-layer info — frame is unidentifiable; skip.
        return None

    # Endpoint addresses + classification.
    src_addr, src_is_aircraft = _addr_of(avlc.get("src"))
    dst_addr, dst_is_aircraft = _addr_of(avlc.get("dst"))

    src_icao = src_addr if src_is_aircraft else None
    dst_icao = dst_addr if dst_is_aircraft else None
    aircraft_icao = src_icao or dst_icao

    if src_is_aircraft and not dst_is_aircraft:
        direction = DIR_DOWNLINK
    elif dst_is_aircraft and not src_is_aircraft:
        direction = DIR_UPLINK
    else:
        direction = DIR_PEER

    # Walk the layers in priority order: highest-value information
    # wins for the kind/label/text classification.  CPDLC may live
    # at avlc.cpdlc (ATN) OR nested inside acars.arinc622.cpdlc
    # (FANS-1/A).
    acars = avlc.get("acars") if isinstance(avlc.get("acars"), dict) else None
    cpdlc = _find_cpdlc(avlc)

    kind = KIND_OTHER
    label: str | None = None
    text: str | None = None
    flight: str | None = None
    reg: str | None = None

    if cpdlc is not None:
        kind = KIND_CPDLC
        label, text = _cpdlc_summary(cpdlc)
    elif acars is not None:
        kind = KIND_ACARS
        label = (acars.get("label") or "").strip() or None
        # `msg_text` is the human-readable body; some ACARS labels
        # are control-only (e.g. "Q0" empty acks) and have no text
        # body.  Fall back to a label-derived summary so the row
        # isn't blank.
        text = (acars.get("msg_text") or "").strip() or None
        if not text:
            text = _acars_label_summary(label, acars)
        flight = (acars.get("flight") or "").strip() or None
        reg = (acars.get("reg") or "").strip() or None
    elif _has_atn_cm(avlc):
        kind = KIND_ATN_CM
        label = "CM"
        text = _atn_cm_summary(avlc)
    elif _has_link_mgmt(avlc):
        kind = KIND_LINK_MGMT
        label = (avlc.get("cmd") or avlc.get("frame_type") or "").strip() or None
        text = _link_mgmt_summary(avlc)
    else:
        # Final fallback: at least tell the operator WHAT the AVLC
        # layer carried so the COMMS row isn't a bare arrow.  Includes
        # the frame type ("I"/"S"/"U"), command name, and the keys
        # present at the AVLC level so unknown-protocol frames are
        # diagnosable from the UI alone.
        text = _other_summary(avlc)
        # Use the first useful label-ish value as the row label.
        label = (avlc.get("cmd") or avlc.get("frame_type") or "").strip() or None

    frame = VdlFrame(
        raw=line,
        receiver_id=receiver_id,
        ts=time.time(),
        frame_ts=frame_ts,
        src_icao=src_icao,
        dst_icao=dst_icao,
        aircraft_icao=aircraft_icao,
        direction=direction,
        kind=kind,
        label=label,
        text=text,
        flight=flight,
        reg=reg,
        sig_level=sig,
        payload=avlc,
    )
    if _trace_match(frame):
        _emit_trace(frame)
    return frame


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _f(v) -> float | None:
    """Best-effort float coercion."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_HEX = set("0123456789abcdefABCDEF")


def _addr_of(node) -> tuple[str | None, bool]:
    """Pull (addr_uppercase, is_aircraft) from an `src` or `dst` sub-object."""
    if not isinstance(node, dict):
        return None, False
    addr = node.get("addr")
    if not isinstance(addr, str):
        return None, False
    addr = addr.strip().upper()
    if not addr or any(c not in _HEX for c in addr):
        return None, False
    # dumpvdl2 uses "Aircraft" / "Ground station" (case may vary) —
    # match prefix-insensitively so we tolerate firmware variants.
    type_str = (node.get("type") or "").strip().lower()
    is_aircraft = type_str.startswith("aircraft")
    return addr, is_aircraft


def _find_nested(node, key: str, max_depth: int = 6) -> dict | None:
    """Depth-limited DFS for the first sub-dict named `key`.

    libacars nests protocol layers (CPDLC over ATN: AVLC → X.25 →
    CLNP → ICAO APDU → ATN dialogue → CPDLC; FANS-1/A: AVLC → ACARS
    → ARINC 622 → CPDLC).  Rather than enumerate every possible
    nesting path, we walk the tree.  Bounded depth so a malicious /
    pathological payload can't blow the stack.
    """
    if not isinstance(node, dict) or max_depth <= 0:
        return None
    direct = node.get(key)
    if isinstance(direct, dict):
        return direct
    for v in node.values():
        if isinstance(v, dict):
            found = _find_nested(v, key, max_depth - 1)
            if found is not None:
                return found
    return None


def _find_cpdlc(avlc: dict) -> dict | None:
    """Locate a CPDLC payload anywhere in the AVLC sub-tree.

    Two common nesting paths:
      avlc.cpdlc                       (ATN B1/B2)
      avlc.acars.arinc622.cpdlc        (FANS-1/A over ACARS)

    Plus arbitrary-depth nesting via X.25 / CLNP / ICAO-APDU / etc
    when ATN is involved.  Recursive walk handles all of these.
    """
    return _find_nested(avlc, "cpdlc")


def _cpdlc_summary(cpdlc: dict) -> tuple[str | None, str]:
    """Roll the CPDLC ASN.1 tree up into a short (label, text) pair
    for the UI.  CPDLC content is verbose; we extract whatever's
    most readable.

    Strategy: hunt for an `atc_uplink_message_data` /
    `atc_downlink_message_data` sub-tree (libacars naming), or a
    `message` / `messageData` / `MsgData` field, and stringify the
    first message-element we find.  Falls back to a generic
    "CPDLC <N elements>" summary when structure is opaque.
    """
    if cpdlc.get("err"):
        return ("CPDLC", "CPDLC (decode error)")
    # libacars commonly emits one of these top-level keys:
    for key in ("atc_uplink_msg", "atc_downlink_msg",
                "atc_uplink_message", "atc_downlink_message",
                "atc_uplink_message_data", "atc_downlink_message_data",
                "message", "messageData", "MsgData"):
        sub = cpdlc.get(key)
        if isinstance(sub, dict):
            text = _stringify_msg_data(sub)
            if text:
                return ("CPDLC", text)
    # Fall back: pretty-print the top-level dict, truncated.
    snippet = json.dumps(cpdlc, separators=(",", ":"))[:160]
    return ("CPDLC", snippet)


def _stringify_msg_data(sub: dict) -> str | None:
    """Pull a human-readable line out of a CPDLC message-data sub-tree.

    libacars formats vary; we try a few common shapes:
      {"header": {...}, "message_data": [{"id":"...", ...}, ...]}
      {"elements": [{"text":"CLIMB TO FL360", ...}]}
      {"id": "...", "text": "..."}  (single-element)
    """
    # Single element with text
    if "text" in sub and isinstance(sub["text"], str):
        return sub["text"]
    # Look for an elements/list field.
    for key in ("elements", "message_data", "msg_data", "list", "sequence"):
        elements = sub.get(key)
        if not isinstance(elements, list):
            continue
        parts: list[str] = []
        for el in elements[:4]:    # cap to keep summary short
            if isinstance(el, dict):
                if isinstance(el.get("text"), str):
                    parts.append(el["text"])
                elif isinstance(el.get("id"), str):
                    parts.append(el["id"])
        if parts:
            return " · ".join(parts)
    return None


def _has_link_mgmt(avlc: dict) -> bool:
    """AVLC frames that carry link-establishment or link flow-control
    only — no upper-layer payload to display.  Covers:

      * U-frames (unnumbered): SABM, UA, DM, DISC, FRMR, XID, TEST
      * S-frames (supervisory): RR, RNR, REJ, SREJ — link-layer ACKs
      * Frame type 'S' regardless of cmd (catch-all for supervisory)

    These are noisy on the wire; classifying them under a single
    KIND_LINK_MGMT lets the UI dim them as background traffic rather
    than letting them clutter the COMMS list as KIND_OTHER.
    """
    cmd = (avlc.get("cmd") or "").strip().upper()
    if cmd in ("SABM", "UA", "DM", "DISC", "FRMR", "XID", "TEST",
               "RR", "RNR", "REJ", "SREJ"):
        return True
    ft = (avlc.get("frame_type") or "").strip().upper()
    return ft == "S"


def _has_atn_cm(avlc: dict) -> bool:
    """ATN context-management traffic — logon/logoff handshakes
    initiating/terminating CPDLC sessions."""
    if "cm" in avlc and isinstance(avlc["cm"], dict):
        return True
    # libacars sometimes nests under x25 or clnp.
    for k in ("x25", "clnp", "es_is", "icao_apdu"):
        if isinstance(avlc.get(k), dict):
            return True
    return False


def _link_mgmt_summary(avlc: dict) -> str:
    cmd = (avlc.get("cmd") or "?").strip()
    ft = (avlc.get("frame_type") or "").strip()
    pf = avlc.get("pf")
    rseq = avlc.get("rseq")
    sseq = avlc.get("sseq")
    parts = [f"VDL2 {cmd}"]
    if ft and ft != cmd:
        parts.append(f"({ft})")
    if rseq is not None:
        parts.append(f"r={rseq}")
    if sseq is not None:
        parts.append(f"s={sseq}")
    if pf:
        parts.append("P/F")
    return " ".join(parts)


def _atn_cm_summary(avlc: dict) -> str:
    """Roll the CM / X.25 / CLNP layers up into a one-line summary."""
    cm = avlc.get("cm")
    if isinstance(cm, dict):
        # cm.cm_logon_request / cm.cm_logon_response are the typical
        # libacars keys.  If we recognise one, surface it; otherwise
        # list the cm sub-keys we did see.
        for k in ("cm_logon_request", "cm_logon_response",
                  "cm_contact_request", "cm_contact_response",
                  "cm_forward_request", "cm_forward_response",
                  "cm_update", "cm_end_request", "cm_end_response"):
            if k in cm:
                return f"ATN CM · {k}"
        keys = ", ".join(k for k in cm.keys() if isinstance(k, str))
        if keys:
            return f"ATN CM · {keys}"
    # X.25 / CLNP / ES-IS without an inner CM message — call out the
    # transport so it's not just opaque "ATN".
    for layer in ("x25", "clnp", "es_is", "icao_apdu"):
        if isinstance(avlc.get(layer), dict):
            return f"ATN {layer.upper()} (no CM payload)"
    return "ATN context-management"


def _acars_label_summary(label: str | None, acars: dict) -> str | None:
    """ACARS labels with no `msg_text` are typically control-only
    (Q-series acks, _∂ link tests, _ɸ squitters).  Build a short
    descriptor from what we DO have so the COMMS row isn't blank."""
    if not label:
        return None
    parts = [f"ACARS {label}"]
    msgno = (acars.get("msg_num") or "").strip()
    sublabel = (acars.get("sublabel") or "").strip()
    if msgno:
        parts.append(f"msg {msgno}")
    if sublabel:
        parts.append(f"sub {sublabel}")
    if acars.get("ack") and acars.get("ack") != "NAK":
        parts.append(f"ack {acars['ack']}")
    return " · ".join(parts)


def _other_summary(avlc: dict) -> str:
    """Fallback summary for AVLC frames whose payload we don't decode.
    Surfaces the frame type, cmd, and the AVLC sub-keys we DID see —
    enough for the operator to spot the protocol layer they're
    looking at and drop a fixture in for parser improvement."""
    ft = (avlc.get("frame_type") or "?").strip()
    cmd = (avlc.get("cmd") or "?").strip()
    interesting_keys = [
        k for k in avlc.keys()
        if k not in ("src", "dst", "cr", "frame_type", "cmd", "pf",
                     "rseq", "sseq", "poll")
        and isinstance(avlc.get(k), (dict, list))
    ]
    parts = [f"VDL2 {ft}/{cmd}"]
    if interesting_keys:
        parts.append("[" + ", ".join(interesting_keys) + "]")
    return " ".join(parts)
