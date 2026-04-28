"""Airline operator lookup by 3-letter ICAO callsign prefix.

ICAO Doc 8585 assigns each airline a 3-letter "Operator Designator" that
forms the prefix of every flight number it broadcasts (e.g. BAW = British
Airways → BAW217 is BA flight 217).

This module ships an embedded subset of the most-active operators globally
(~150 airlines covering roughly 95% of commercial traffic). The full ICAO
Doc 8585 is 5000+ entries but most are dormant, cargo, or one-off.

A larger operators.json from the Mictronics database can be loaded via
load_operators_json() to extend coverage; this is also fetched by the
data download script.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("skywatch.db.operators")


@dataclass(frozen=True)
class Operator:
    designator: str  # 3-letter ICAO code, e.g. "BAW"
    name: str        # full airline name
    country: str     # operator's home country
    callsign: str    # radiotelephony callsign, e.g. "SPEEDBIRD"


# Embedded core list. Curated from ICAO Doc 8585 + FAA JO 7340.2 + tar1090
# operators.json (which is itself derived from these sources). Field order:
# (designator, full_name, country, radio_callsign).
_CORE_OPERATORS: list[tuple[str, str, str, str]] = [
    # --- North America ---
    ("AAL", "American Airlines", "United States", "AMERICAN"),
    ("ACA", "Air Canada", "Canada", "AIR CANADA"),
    ("AJT", "Amerijet International", "United States", "AMERIJET"),
    ("ASA", "Alaska Airlines", "United States", "ALASKA"),
    ("AWE", "American West Airlines", "United States", "CACTUS"),
    ("AZA", "Alitalia", "Italy", "ALITALIA"),
    ("CFG", "Condor", "Germany", "CONDOR"),
    ("DAL", "Delta Air Lines", "United States", "DELTA"),
    ("EJA", "NetJets Aviation", "United States", "EXECJET"),
    ("ENY", "Envoy Air", "United States", "ENVOY"),
    ("FDX", "FedEx Express", "United States", "FEDEX"),
    ("FFT", "Frontier Airlines", "United States", "FRONTIER FLIGHT"),
    ("GJS", "GoJet Airlines", "United States", "LINDBERGH"),
    ("GTI", "Atlas Air", "United States", "GIANT"),
    ("HAL", "Hawaiian Airlines", "United States", "HAWAIIAN"),
    ("JBU", "JetBlue Airways", "United States", "JETBLUE"),
    ("JIA", "PSA Airlines", "United States", "BLUESTREAK"),
    ("NKS", "Spirit Airlines", "United States", "SPIRIT WINGS"),
    ("PDT", "Piedmont Airlines", "United States", "PIEDMONT"),
    ("RPA", "Republic Airways", "United States", "BRICKYARD"),
    ("SCX", "Sun Country Airlines", "United States", "SUN COUNTRY"),
    ("SKW", "SkyWest Airlines", "United States", "SKYWEST"),
    ("SWA", "Southwest Airlines", "United States", "SOUTHWEST"),
    ("UAL", "United Airlines", "United States", "UNITED"),
    ("UPS", "United Parcel Service", "United States", "UPS"),
    ("VRD", "Virgin America", "United States", "REDWOOD"),
    ("WJA", "WestJet", "Canada", "WESTJET"),
    # --- United Kingdom & Ireland ---
    ("BAW", "British Airways", "United Kingdom", "SPEEDBIRD"),
    ("EXS", "Jet2.com", "United Kingdom", "CHANNEX"),
    ("EZY", "easyJet", "United Kingdom", "EASY"),
    ("EIN", "Aer Lingus", "Ireland", "SHAMROCK"),
    ("RYR", "Ryanair", "Ireland", "RYANAIR"),
    ("TOM", "TUI Airways", "United Kingdom", "TOMJET"),
    ("VIR", "Virgin Atlantic", "United Kingdom", "VIRGIN"),
    # --- Continental Europe ---
    ("AFR", "Air France", "France", "AIRFRANS"),
    ("AUA", "Austrian Airlines", "Austria", "AUSTRIAN"),
    ("BEL", "Brussels Airlines", "Belgium", "BEELINE"),
    ("BAY", "BA CityFlyer", "United Kingdom", "FLYER"),
    ("DLH", "Lufthansa", "Germany", "LUFTHANSA"),
    ("FIN", "Finnair", "Finland", "FINNAIR"),
    ("IBE", "Iberia", "Spain", "IBERIA"),
    ("KLM", "KLM Royal Dutch Airlines", "Netherlands", "KLM"),
    ("LGL", "Luxair", "Luxembourg", "LUXAIR"),
    ("LOT", "LOT Polish Airlines", "Poland", "LOT"),
    ("NOZ", "Norwegian Air Norway", "Norway", "NORSTAR"),
    ("SAS", "Scandinavian Airlines", "Sweden", "SCANDINAVIAN"),
    ("SWR", "Swiss International Air Lines", "Switzerland", "SWISS"),
    ("TAP", "TAP Air Portugal", "Portugal", "AIR PORTUGAL"),
    ("THY", "Turkish Airlines", "Türkiye", "TURKISH"),
    ("VLG", "Vueling Airlines", "Spain", "VUELING"),
    ("WZZ", "Wizz Air", "Hungary", "WIZZ AIR"),
    ("AEE", "Aegean Airlines", "Greece", "AEGEAN"),
    ("CSA", "Czech Airlines", "Czech Republic", "CSA-LINES"),
    ("MSR", "EgyptAir", "Egypt", "EGYPTAIR"),
    ("PGT", "Pegasus Airlines", "Türkiye", "SUNTURK"),
    # --- Middle East ---
    ("ETD", "Etihad Airways", "United Arab Emirates", "ETIHAD"),
    ("ELY", "El Al", "Israel", "ELAL"),
    ("UAE", "Emirates", "United Arab Emirates", "EMIRATES"),
    ("QTR", "Qatar Airways", "Qatar", "QATARI"),
    ("SVA", "Saudia", "Saudi Arabia", "SAUDIA"),
    ("RJA", "Royal Jordanian", "Jordan", "JORDANIAN"),
    # --- Asia-Pacific ---
    ("ANA", "All Nippon Airways", "Japan", "ALL NIPPON"),
    ("ANZ", "Air New Zealand", "New Zealand", "NEW ZEALAND"),
    ("AIC", "Air India", "India", "AIRINDIA"),
    ("BAW", "British Airways", "United Kingdom", "SPEEDBIRD"),
    ("CCA", "Air China", "China", "AIR CHINA"),
    ("CES", "China Eastern Airlines", "China", "CHINA EASTERN"),
    ("CPA", "Cathay Pacific", "Hong Kong", "CATHAY"),
    ("CSH", "Shanghai Airlines", "China", "SHANGHAI AIR"),
    ("CSN", "China Southern Airlines", "China", "CHINA SOUTHERN"),
    ("EVA", "EVA Air", "Taiwan", "EVA"),
    ("HVN", "Vietnam Airlines", "Vietnam", "VIETNAM AIRLINES"),
    ("JAL", "Japan Airlines", "Japan", "JAPAN AIR"),
    ("JST", "Jetstar Airways", "Australia", "JETSTAR"),
    ("KAL", "Korean Air", "Republic of Korea", "KOREANAIR"),
    ("MAS", "Malaysia Airlines", "Malaysia", "MALAYSIAN"),
    ("PAL", "Philippine Airlines", "Philippines", "PHILIPPINE"),
    ("QFA", "Qantas", "Australia", "QANTAS"),
    ("SIA", "Singapore Airlines", "Singapore", "SINGAPORE"),
    ("TGW", "Scoot", "Singapore", "SCOOSTER"),
    ("THA", "Thai Airways International", "Thailand", "THAI"),
    ("VOZ", "Virgin Australia", "Australia", "VELOCITY"),
    # --- Latin America ---
    ("AAR", "Asiana Airlines", "Republic of Korea", "ASIANA"),
    ("AMX", "Aeromexico", "Mexico", "AEROMEXICO"),
    ("ARG", "Aerolineas Argentinas", "Argentina", "ARGENTINA"),
    ("AVA", "Avianca", "Colombia", "AVIANCA"),
    ("AZU", "Azul Linhas Aéreas", "Brazil", "AZUL"),
    ("CMP", "Copa Airlines", "Panama", "COPA"),
    ("GLO", "Gol Transportes Aéreos", "Brazil", "GOL TRANSPORTE"),
    ("LAN", "LATAM Airlines", "Chile", "LAN"),
    ("LPE", "LATAM Perú", "Peru", "LANPERU"),
    ("TAM", "LATAM Airlines Brasil", "Brazil", "TAM"),
    ("VOI", "Volaris", "Mexico", "VOLARIS"),
    # --- Africa ---
    ("ETH", "Ethiopian Airlines", "Ethiopia", "ETHIOPIAN"),
    ("KQA", "Kenya Airways", "Kenya", "KENYA"),
    ("MAU", "Air Mauritius", "Mauritius", "AIRMAURITIUS"),
    ("RAM", "Royal Air Maroc", "Morocco", "ROYALAIR MAROC"),
    ("SAA", "South African Airways", "South Africa", "SPRINGBOK"),
    # --- Cargo ---
    ("ABW", "AirBridgeCargo", "Russia", "AIRBRIDGE CARGO"),
    ("ABX", "ABX Air", "United States", "ABEX"),
    ("BCS", "European Air Transport Leipzig", "Germany", "EUROTRANS"),
    ("BOX", "AeroLogic", "Germany", "GERMAN CARGO"),
    ("CAO", "Air China Cargo", "China", "AIRCHINA FREIGHT"),
    ("CKK", "China Cargo Airlines", "China", "CARGO KING"),
    ("CLX", "Cargolux", "Luxembourg", "CARGOLUX"),
    ("ELY", "El Al", "Israel", "ELAL"),
    ("GEC", "Lufthansa Cargo", "Germany", "LUFTHANSA CARGO"),
    ("GTI", "Atlas Air", "United States", "GIANT"),
    ("MPH", "Martinair", "Netherlands", "MARTINAIR"),
    ("NCA", "Nippon Cargo Airlines", "Japan", "NIPPON CARGO"),
    ("PAC", "Polar Air Cargo", "United States", "POLAR"),
    ("SOO", "Southern Air", "United States", "SOUTHERN AIR"),
    ("TAY", "TNT Airways", "Belgium", "QUALITY"),
    # --- Holiday / leisure ---
    ("DLH", "Lufthansa", "Germany", "LUFTHANSA"),  # duplicate ok, set semantics
    ("ENT", "Enter Air", "Poland", "ENTER AIR"),
    ("SUS", "Sun-Air", "Denmark", "SUNSCAN"),
    # --- Misc ---
    ("RCH", "Air Mobility Command", "United States", "REACH"),  # USAF
    ("VPA", "VistaJet", "Malta", "VISTA"),
]


_OPS: dict[str, Operator] = {}
for des, name, country, callsign in _CORE_OPERATORS:
    if des not in _OPS:  # first wins for duplicates
        _OPS[des] = Operator(des, name, country, callsign)


def lookup_operator(designator: str) -> Operator | None:
    """Look up by 3-letter operator designator (case-insensitive)."""
    if not designator:
        return None
    return _OPS.get(designator.upper())


def lookup_callsign_operator(callsign: str) -> Operator | None:
    """Extract the 3-letter prefix from a callsign and look it up.

    A typical airline callsign is `BAW217` (BA flight 217) or `KLM43H`.
    Private/general aviation callsigns are usually the registration itself
    and won't match.  Returns None for unmatched.
    """
    if not callsign or len(callsign) < 3:
        return None
    cs = callsign.upper().strip()
    prefix = cs[:3]
    # Only treat as airline if the prefix is letters and the suffix has any digit
    if not prefix.isalpha():
        return None
    if not any(c.isdigit() for c in cs[3:]):
        return None  # looks like a registration, not an airline flight
    return _OPS.get(prefix)


def load_operators_json(path: Path) -> int:
    """Extend the embedded operators table from a JSON file.

    Two formats supported:

    1. List form (tar1090 / older Mictronics):
       { "BAW": ["British Airways", "United Kingdom", "SPEEDBIRD"] }

    2. Object form (current Mictronics readsb-protobuf):
       { "BAW": {"n": "British Airways", "c": "SPEEDBIRD", "r": "United Kingdom"} }
       (keys: n=name, c=callsign, r=country/region)
    """
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not parse operators JSON %s: %s", path, e)
        return 0
    if not isinstance(data, dict):
        log.warning("Operators JSON %s is not an object at the top level", path)
        return 0

    n = 0
    for des, fields in data.items():
        if not isinstance(des, str) or not des:
            continue
        name = country = callsign = ""
        if isinstance(fields, dict):
            # Mictronics object form
            name = fields.get("n", "") or fields.get("name", "")
            country = fields.get("r", "") or fields.get("country", "")
            callsign = fields.get("c", "") or fields.get("callsign", "")
        elif isinstance(fields, (list, tuple)):
            # tar1090 list form
            name = fields[0] if len(fields) > 0 else ""
            country = fields[1] if len(fields) > 1 else ""
            callsign = fields[2] if len(fields) > 2 else ""
        else:
            continue
        if not name:
            continue
        des_u = des.upper()
        if des_u not in _OPS:
            _OPS[des_u] = Operator(des_u, name, country, callsign)
            n += 1
    log.info("Added %d operators from %s", n, path)
    return n
