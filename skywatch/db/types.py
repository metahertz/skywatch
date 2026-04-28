"""ICAO Doc 8643 aircraft type designators.

Doc 8643 assigns each aircraft model a 4-character code (e.g. B738 for
the 737-800). We embed the common types covering the bulk of commercial
and general aviation traffic.

The full Doc 8643 list (~9000 entries including obscure historical types)
can be loaded from the Mictronics types.json by `load_types_json()`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("skywatch.db.types")


# The "WTC" (wake turbulence category) field uses ICAO RECAT EU values:
#   L = Light, M = Medium, H = Heavy, J = Super Heavy (A380)
# The description format from Doc 8643 is something like "L2J" =
# Land plane, 2 engines, Jet.  We give a shorter human-friendly display.

@dataclass(frozen=True)
class AircraftType:
    code: str          # 4-char ICAO type designator
    manufacturer: str  # e.g. "BOEING", "AIRBUS"
    model: str         # e.g. "737-800", "A320"
    wtc: str           # L / M / H / J
    engine_type: str   # "jet" / "turboprop" / "piston" / "electric"
    engine_count: int


_TYPES_RAW: list[tuple[str, str, str, str, str, int]] = [
    # === BOEING ===
    ("B712", "Boeing", "717-200", "M", "jet", 2),
    ("B721", "Boeing", "727-100", "M", "jet", 3),
    ("B722", "Boeing", "727-200", "M", "jet", 3),
    ("B732", "Boeing", "737-200", "M", "jet", 2),
    ("B733", "Boeing", "737-300", "M", "jet", 2),
    ("B734", "Boeing", "737-400", "M", "jet", 2),
    ("B735", "Boeing", "737-500", "M", "jet", 2),
    ("B736", "Boeing", "737-600", "M", "jet", 2),
    ("B737", "Boeing", "737-700", "M", "jet", 2),
    ("B738", "Boeing", "737-800", "M", "jet", 2),
    ("B739", "Boeing", "737-900", "M", "jet", 2),
    ("B37M", "Boeing", "737 MAX 7", "M", "jet", 2),
    ("B38M", "Boeing", "737 MAX 8", "M", "jet", 2),
    ("B39M", "Boeing", "737 MAX 9", "M", "jet", 2),
    ("B3XM", "Boeing", "737 MAX 10", "M", "jet", 2),
    ("B741", "Boeing", "747-100", "H", "jet", 4),
    ("B742", "Boeing", "747-200", "H", "jet", 4),
    ("B743", "Boeing", "747-300", "H", "jet", 4),
    ("B744", "Boeing", "747-400", "H", "jet", 4),
    ("B748", "Boeing", "747-8", "H", "jet", 4),
    ("B752", "Boeing", "757-200", "M", "jet", 2),
    ("B753", "Boeing", "757-300", "M", "jet", 2),
    ("B762", "Boeing", "767-200", "H", "jet", 2),
    ("B763", "Boeing", "767-300", "H", "jet", 2),
    ("B764", "Boeing", "767-400", "H", "jet", 2),
    ("B772", "Boeing", "777-200", "H", "jet", 2),
    ("B77L", "Boeing", "777-200LR", "H", "jet", 2),
    ("B773", "Boeing", "777-300", "H", "jet", 2),
    ("B77W", "Boeing", "777-300ER", "H", "jet", 2),
    ("B778", "Boeing", "777-8", "H", "jet", 2),
    ("B779", "Boeing", "777-9", "H", "jet", 2),
    ("B788", "Boeing", "787-8", "H", "jet", 2),
    ("B789", "Boeing", "787-9", "H", "jet", 2),
    ("B78X", "Boeing", "787-10", "H", "jet", 2),
    # === AIRBUS ===
    ("A306", "Airbus", "A300-600", "H", "jet", 2),
    ("A30B", "Airbus", "A300B2/B4/C4", "H", "jet", 2),
    ("A310", "Airbus", "A310", "H", "jet", 2),
    ("A318", "Airbus", "A318", "M", "jet", 2),
    ("A319", "Airbus", "A319", "M", "jet", 2),
    ("A320", "Airbus", "A320", "M", "jet", 2),
    ("A321", "Airbus", "A321", "M", "jet", 2),
    ("A19N", "Airbus", "A319neo", "M", "jet", 2),
    ("A20N", "Airbus", "A320neo", "M", "jet", 2),
    ("A21N", "Airbus", "A321neo", "M", "jet", 2),
    ("A332", "Airbus", "A330-200", "H", "jet", 2),
    ("A333", "Airbus", "A330-300", "H", "jet", 2),
    ("A337", "Airbus", "A330-700 Beluga XL", "H", "jet", 2),
    ("A338", "Airbus", "A330-800neo", "H", "jet", 2),
    ("A339", "Airbus", "A330-900neo", "H", "jet", 2),
    ("A342", "Airbus", "A340-200", "H", "jet", 4),
    ("A343", "Airbus", "A340-300", "H", "jet", 4),
    ("A345", "Airbus", "A340-500", "H", "jet", 4),
    ("A346", "Airbus", "A340-600", "H", "jet", 4),
    ("A359", "Airbus", "A350-900", "H", "jet", 2),
    ("A35K", "Airbus", "A350-1000", "H", "jet", 2),
    ("A388", "Airbus", "A380-800", "J", "jet", 4),
    # === EMBRAER ===
    ("E135", "Embraer", "ERJ-135", "M", "jet", 2),
    ("E145", "Embraer", "ERJ-145", "M", "jet", 2),
    ("E170", "Embraer", "E-170", "M", "jet", 2),
    ("E175", "Embraer", "E-175", "M", "jet", 2),
    ("E190", "Embraer", "E-190", "M", "jet", 2),
    ("E195", "Embraer", "E-195", "M", "jet", 2),
    ("E290", "Embraer", "E-190-E2", "M", "jet", 2),
    ("E295", "Embraer", "E-195-E2", "M", "jet", 2),
    ("E50P", "Embraer", "Phenom 100", "L", "jet", 2),
    ("E55P", "Embraer", "Phenom 300", "L", "jet", 2),
    # === BOMBARDIER / DE HAVILLAND CANADA ===
    ("CRJ1", "Bombardier", "CRJ-100", "M", "jet", 2),
    ("CRJ2", "Bombardier", "CRJ-200", "M", "jet", 2),
    ("CRJ7", "Bombardier", "CRJ-700", "M", "jet", 2),
    ("CRJ9", "Bombardier", "CRJ-900", "M", "jet", 2),
    ("CRJX", "Bombardier", "CRJ-1000", "M", "jet", 2),
    ("CL30", "Bombardier", "Challenger 300", "M", "jet", 2),
    ("CL35", "Bombardier", "Challenger 350", "M", "jet", 2),
    ("CL60", "Bombardier", "Challenger 600/601/604/605", "M", "jet", 2),
    ("GLF4", "Gulfstream", "G-IV", "M", "jet", 2),
    ("GLF5", "Gulfstream", "G-V", "M", "jet", 2),
    ("GLF6", "Gulfstream", "G650", "M", "jet", 2),
    ("DH8A", "De Havilland Canada", "DHC-8-100", "M", "turboprop", 2),
    ("DH8B", "De Havilland Canada", "DHC-8-200", "M", "turboprop", 2),
    ("DH8C", "De Havilland Canada", "DHC-8-300", "M", "turboprop", 2),
    ("DH8D", "De Havilland Canada", "DHC-8-400", "M", "turboprop", 2),
    ("DHC6", "De Havilland Canada", "DHC-6 Twin Otter", "L", "turboprop", 2),
    # === ATR ===
    ("AT43", "ATR", "ATR 42-300/320", "M", "turboprop", 2),
    ("AT45", "ATR", "ATR 42-500", "M", "turboprop", 2),
    ("AT46", "ATR", "ATR 42-600", "M", "turboprop", 2),
    ("AT72", "ATR", "ATR 72", "M", "turboprop", 2),
    ("AT75", "ATR", "ATR 72-500", "M", "turboprop", 2),
    ("AT76", "ATR", "ATR 72-600", "M", "turboprop", 2),
    # === MCDONNELL DOUGLAS ===
    ("DC10", "McDonnell Douglas", "DC-10", "H", "jet", 3),
    ("MD11", "McDonnell Douglas", "MD-11", "H", "jet", 3),
    ("MD80", "McDonnell Douglas", "MD-80 series", "M", "jet", 2),
    ("MD81", "McDonnell Douglas", "MD-81", "M", "jet", 2),
    ("MD82", "McDonnell Douglas", "MD-82", "M", "jet", 2),
    ("MD83", "McDonnell Douglas", "MD-83", "M", "jet", 2),
    ("MD87", "McDonnell Douglas", "MD-87", "M", "jet", 2),
    ("MD88", "McDonnell Douglas", "MD-88", "M", "jet", 2),
    ("MD90", "McDonnell Douglas", "MD-90", "M", "jet", 2),
    # === GENERAL AVIATION (light singles & twins) ===
    ("C152", "Cessna", "152", "L", "piston", 1),
    ("C172", "Cessna", "172", "L", "piston", 1),
    ("C182", "Cessna", "182", "L", "piston", 1),
    ("C208", "Cessna", "208 Caravan", "L", "turboprop", 1),
    ("C25A", "Cessna", "Citation CJ2", "L", "jet", 2),
    ("C25B", "Cessna", "Citation CJ3", "L", "jet", 2),
    ("C25C", "Cessna", "Citation CJ4", "L", "jet", 2),
    ("C56X", "Cessna", "Citation Excel/XLS", "L", "jet", 2),
    ("C680", "Cessna", "Citation Sovereign", "L", "jet", 2),
    ("C750", "Cessna", "Citation X", "M", "jet", 2),
    ("PA28", "Piper", "PA-28 Cherokee", "L", "piston", 1),
    ("PA32", "Piper", "PA-32 Saratoga", "L", "piston", 1),
    ("PA46", "Piper", "PA-46 Malibu/Meridian", "L", "piston", 1),
    ("BE36", "Beechcraft", "Bonanza 36", "L", "piston", 1),
    ("BE58", "Beechcraft", "Baron 58", "L", "piston", 2),
    ("BE9L", "Beechcraft", "King Air 90", "L", "turboprop", 2),
    ("BE20", "Beechcraft", "King Air 200", "L", "turboprop", 2),
    ("DA40", "Diamond", "DA40", "L", "piston", 1),
    ("DA42", "Diamond", "DA42", "L", "piston", 2),
    ("DA62", "Diamond", "DA62", "L", "piston", 2),
    ("PC12", "Pilatus", "PC-12", "L", "turboprop", 1),
    ("PC24", "Pilatus", "PC-24", "L", "jet", 2),
    ("TBM7", "Daher", "TBM 700", "L", "turboprop", 1),
    ("TBM8", "Daher", "TBM 850", "L", "turboprop", 1),
    ("TBM9", "Daher", "TBM 900/910/930/940", "L", "turboprop", 1),
    # === HELICOPTERS ===
    ("R22", "Robinson", "R22", "L", "piston", 1),
    ("R44", "Robinson", "R44", "L", "piston", 1),
    ("R66", "Robinson", "R66", "L", "turboprop", 1),
    ("EC20", "Eurocopter", "EC120", "L", "turboprop", 1),
    ("EC25", "Eurocopter", "EC225", "M", "turboprop", 2),
    ("EC30", "Eurocopter", "EC130", "L", "turboprop", 1),
    ("EC35", "Eurocopter", "EC135", "L", "turboprop", 2),
    ("EC45", "Eurocopter", "EC145", "M", "turboprop", 2),
    ("AS50", "Eurocopter", "AS350 Écureuil", "L", "turboprop", 1),
    ("AS65", "Eurocopter", "AS365 Dauphin", "M", "turboprop", 2),
    ("S76", "Sikorsky", "S-76", "M", "turboprop", 2),
    # === MILITARY (limited – often sanitised/encoded ICAOs) ===
    ("F16", "Lockheed Martin", "F-16 Fighting Falcon", "M", "jet", 1),
    ("F18", "Boeing", "F/A-18 Hornet", "M", "jet", 2),
    ("F35", "Lockheed Martin", "F-35 Lightning II", "M", "jet", 1),
    ("E3", "Boeing", "E-3 Sentry", "H", "jet", 4),
    ("C17", "Boeing", "C-17 Globemaster III", "H", "jet", 4),
    ("C130", "Lockheed", "C-130 Hercules", "M", "turboprop", 4),
    ("C5M", "Lockheed", "C-5M Super Galaxy", "H", "jet", 4),
    ("KC10", "McDonnell Douglas", "KC-10 Extender", "H", "jet", 3),
    ("KC30", "Airbus", "KC-30 (A330 MRTT)", "H", "jet", 2),
    ("KC35", "Boeing", "KC-135 Stratotanker", "H", "jet", 4),
    ("KC46", "Boeing", "KC-46 Pegasus", "H", "jet", 2),
    ("P8", "Boeing", "P-8 Poseidon", "H", "jet", 2),
    # === TUPOLEV / ILYUSHIN / SUKHOI / IRKUT (Russia/CIS) ===
    ("T154", "Tupolev", "Tu-154", "M", "jet", 3),
    ("T204", "Tupolev", "Tu-204", "M", "jet", 2),
    ("IL76", "Ilyushin", "Il-76", "H", "jet", 4),
    ("IL96", "Ilyushin", "Il-96", "H", "jet", 4),
    ("SU95", "Sukhoi", "Superjet 100", "M", "jet", 2),
    ("MS21", "Irkut", "MC-21", "M", "jet", 2),
    # === COMAC ===
    ("C909", "COMAC", "ARJ21", "M", "jet", 2),
    ("C919", "COMAC", "C919", "M", "jet", 2),
    # === MISC ===
    ("BCS3", "Airbus", "A220-300 (CSeries CS300)", "M", "jet", 2),
    ("BCS1", "Airbus", "A220-100 (CSeries CS100)", "M", "jet", 2),
    ("F100", "Fokker", "100", "M", "jet", 2),
    ("F70", "Fokker", "70", "M", "jet", 2),
]


_TYPES: dict[str, AircraftType] = {}
for code, mfr, model, wtc, eng, count in _TYPES_RAW:
    _TYPES[code.upper()] = AircraftType(code.upper(), mfr, model, wtc, eng, count)


def lookup_type(code: str) -> AircraftType | None:
    """Look up by ICAO Doc 8643 type code (case-insensitive)."""
    if not code:
        return None
    return _TYPES.get(code.upper().strip())


def load_types_json(path: Path) -> int:
    """Extend the embedded types from a Mictronics types.json file.

    Two formats supported:

    1. List form (older Mictronics / tar1090):
       { "B738": ["BOEING 737-800", "L2J"] }
       where L2J = Land plane, 2 engines, Jet.

    2. Object form (current Mictronics readsb-protobuf):
       { "B738": {"desc": "BOEING 737-800", "wtc": "M"} }
       Or alternatively keys "t"/"d"/"w".  We accept any combination.
    """
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not parse types JSON %s: %s", path, e)
        return 0
    if not isinstance(data, dict):
        log.warning("Types JSON %s is not an object at the top level", path)
        return 0

    n = 0
    for code, fields in data.items():
        if not isinstance(code, str) or not code:
            continue
        code_u = code.upper()
        if code_u in _TYPES:
            continue
        descr = wtc_descr = ""
        if isinstance(fields, dict):
            descr = (
                fields.get("desc") or fields.get("d") or fields.get("t") or
                fields.get("description") or ""
            )
            wtc_descr = (
                fields.get("wtc") or fields.get("w") or fields.get("category") or ""
            )
        elif isinstance(fields, (list, tuple)):
            descr = fields[0] if len(fields) > 0 else ""
            wtc_descr = fields[1] if len(fields) > 1 else ""
        else:
            continue
        if not descr:
            continue
        # Parse Mictronics WTC descriptor like "L2J" (Land/2 engines/Jet) or
        # a plain WTC code like "M" / "H".
        ec = 0
        engine_type = "unknown"
        if len(wtc_descr) >= 3 and wtc_descr[2] in "JTPE":
            try:
                ec = int(wtc_descr[1])
            except ValueError:
                ec = 0
            engine_type = {
                "J": "jet", "T": "turboprop", "P": "piston", "E": "electric",
            }.get(wtc_descr[2], "unknown")
        # Split manufacturer/model heuristically
        parts = descr.split(" ", 1)
        mfr = parts[0] if parts else ""
        model = parts[1] if len(parts) > 1 else ""
        _TYPES[code_u] = AircraftType(code_u, mfr, model, "M", engine_type, ec)
        n += 1
    log.info("Added %d aircraft types from %s", n, path)
    return n
