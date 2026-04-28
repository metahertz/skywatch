"""Test suite for skywatch.

These tests verify the decoders against published reference values
(from Sun's "1090 MHz Riddle" and the FAA N-number specification).
Run with:
    python -m unittest discover tests/
"""
import unittest

from skywatch.decoder import adsb, common
from skywatch.decoder import modes as ms
from skywatch.decoder.beast import BeastParser, encode_beast
from skywatch.decoder.synthetic import (
    default_scenario, encode_cpr, make_airborne_position,
    make_identification, make_velocity,
)
from skywatch.db import InfoLookup, MictronicsDB
from skywatch.db.algorithmic import _icao_to_n, algo_registration
from skywatch.db.icao_ranges import country_for_icao, is_pia
from skywatch.db.seed import SEED_PATH


# ─────────────────────────────────────────────────────────────────────
# Mode S CRC and ICAO recovery
# ─────────────────────────────────────────────────────────────────────

class TestCommon(unittest.TestCase):
    """Verify Mode S CRC and ICAO recovery against textbook values."""

    def test_crc_address_parity(self):
        # Sun ch 11: A0001838CA380031440000F24177 → ICAO 3C6DD0
        msg = "A0001838CA380031440000F24177"
        self.assertEqual(common.recover_icao(msg), "3C6DD0")

    def test_crc_pure_parity(self):
        # DF17 should validate to zero residual
        msg = "8D40621D58C382D690C8AC2863A7"
        self.assertTrue(common.crc_check(msg))

    def test_df_decode(self):
        # DF17, DF20 should be recognised
        self.assertEqual(common.df("8D40621D58C382D690C8AC2863A7"), 17)
        self.assertEqual(common.df("A0001838CA380031440000F24177"), 20)


# ─────────────────────────────────────────────────────────────────────
# ADS-B decoders
# ─────────────────────────────────────────────────────────────────────

class TestAdsb(unittest.TestCase):
    """Reference values from Sun's '1090 MHz Riddle' chapters 4-7."""

    def test_callsign(self):
        # Sun ch 4: KLM1023
        msg = "8D4840D6202CC371C32CE0576098"
        self.assertEqual(adsb.callsign(msg), "KLM1023")
        self.assertEqual(adsb.typecode(msg), 4)

    def test_position_global(self):
        # Sun ch 5
        e = "8D40621D58C382D690C8AC2863A7"
        o = "8D40621D58C386435CC412692AD6"
        pos = adsb.position_global(e, o, 1457996402, 1457996400)
        self.assertIsNotNone(pos)
        lat, lon = pos
        self.assertAlmostEqual(lat, 52.2572, places=3)
        self.assertAlmostEqual(lon, 3.91937, places=4)

    def test_altitude(self):
        # Sun ch 5: 38,000 ft
        msg = "8D40621D58C382D690C8AC2863A7"
        self.assertEqual(adsb.altitude(msg), 38000)

    def test_velocity_ground(self):
        # Sun ch 7 sub-type 1 (ground vector)
        msg = "8D485020994409940838175B284F"
        v = adsb.velocity(msg)
        self.assertAlmostEqual(v.speed, 159.20, places=1)
        self.assertAlmostEqual(v.track, 182.88, places=1)
        self.assertEqual(v.vrate, -832)
        self.assertEqual(v.speed_type, "GS")

    def test_velocity_airspeed(self):
        # Sun ch 7 sub-type 3 (airspeed/heading)
        msg = "8DA05F219B06B6AF189400CBC33F"
        v = adsb.velocity(msg)
        self.assertEqual(v.speed, 375)
        self.assertAlmostEqual(v.heading, 243.98, places=1)
        self.assertEqual(v.vrate, -2304)
        self.assertEqual(v.speed_type, "TAS")


# ─────────────────────────────────────────────────────────────────────
# Mode S BDS register decoders
# ─────────────────────────────────────────────────────────────────────

class TestBds(unittest.TestCase):
    """Reference values from Sun ch 17 (BDS 4,0 / 5,0 / 6,0)."""

    def test_bds40(self):
        msg = "A8001EBCAEE57730A80106DE1344"
        b = ms.decode_bds_40(msg)
        self.assertIsNotNone(b)
        self.assertEqual(b.mcp_alt_ft, 24000)
        self.assertEqual(b.fms_alt_ft, 24000)
        self.assertAlmostEqual(b.qnh_mb, 1013.2, places=1)

    def test_bds50(self):
        msg = "A80006ACF9363D3BBF9CE98F1E1D"
        b = ms.decode_bds_50(msg)
        self.assertIsNotNone(b)
        self.assertAlmostEqual(b.roll_deg, -9.7, places=1)
        self.assertAlmostEqual(b.track_deg, 140.273, places=2)
        self.assertEqual(b.gs_kt, 476)
        self.assertAlmostEqual(b.track_rate_dps, -0.406, places=2)
        self.assertEqual(b.tas_kt, 466)

    def test_bds60(self):
        msg = "A80004AAA74A072BFDEFC1D5CB4F"
        b = ms.decode_bds_60(msg)
        self.assertIsNotNone(b)
        self.assertAlmostEqual(b.heading_deg, 110.391, places=2)
        self.assertEqual(b.ias_kt, 259)
        self.assertAlmostEqual(b.mach, 0.7, places=2)
        self.assertEqual(b.vrate_baro_fpm, -2144)
        self.assertEqual(b.vrate_ins_fpm, -2016)

    def test_bds_inference_uniqueness(self):
        """Inference should pick the unique correct BDS for each test message."""
        cases = [
            ("A8001EBCAEE57730A80106DE1344", "4,0"),
            ("A80006ACF9363D3BBF9CE98F1E1D", "5,0"),
            ("A80004AAA74A072BFDEFC1D5CB4F", "6,0"),
        ]
        for msg, expected in cases:
            cands = ms.infer_bds(msg)
            codes = {c.bds_code for c in cands}
            self.assertIn(expected, codes,
                          f"{expected} should be in candidates for {msg}")
            # Should be the only candidate (or the highest-confidence one)
            self.assertEqual(len(cands), 1,
                             f"{msg}: expected unique inference, got {codes}")


# ─────────────────────────────────────────────────────────────────────
# BEAST protocol
# ─────────────────────────────────────────────────────────────────────

class TestBeast(unittest.TestCase):

    def test_roundtrip(self):
        msg = "8D40621D58C382D690C8AC2863A7"
        beast = encode_beast(msg, ts_seconds=12.345, signal=180)
        p = BeastParser()
        frames = p.feed(beast)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].raw_hex, msg)
        self.assertEqual(frames[0].df, 17)

    def test_split_buffer(self):
        msg = "8D40621D58C382D690C8AC2863A7"
        beast = encode_beast(msg, ts_seconds=0.0, signal=180)
        p = BeastParser()
        # Split mid-frame; first half should give 0 frames, second half gives 1
        mid = len(beast) // 2
        frames1 = p.feed(beast[:mid])
        frames2 = p.feed(beast[mid:])
        self.assertEqual(len(frames1), 0)
        self.assertEqual(len(frames2), 1)
        self.assertEqual(frames2[0].raw_hex, msg)

    def test_escaped_esc_byte(self):
        """An ESC byte (0x1A) inside the payload must be doubled and
        un-escaped on decode."""
        # signal=0x1A would put an ESC in the payload
        msg = "8D40621D58C382D690C8AC2863A7"
        beast = encode_beast(msg, ts_seconds=0.0, signal=0x1A)
        p = BeastParser()
        frames = p.feed(beast)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].raw_hex, msg)


# ─────────────────────────────────────────────────────────────────────
# CPR encoder/decoder roundtrip
# ─────────────────────────────────────────────────────────────────────

class TestCpr(unittest.TestCase):

    def test_roundtrip(self):
        for lat, lon in [(51.5, 0.1), (-33.8, 151.2), (40.7, -74.0), (0.0, 0.0)]:
            msg_e = make_airborne_position("ABC123", lat, lon, 35000, even=True)
            msg_o = make_airborne_position("ABC123", lat, lon, 35000, even=False)
            pos = adsb.position_global(msg_e, msg_o, 1.0, 2.0)
            self.assertIsNotNone(pos, f"global decode failed at {lat},{lon}")
            self.assertAlmostEqual(pos[0], lat, places=2,
                                    msg=f"lat mismatch at {lat}")
            self.assertAlmostEqual(pos[1], lon, places=2,
                                    msg=f"lon mismatch at {lon}")


# ─────────────────────────────────────────────────────────────────────
# N-number algorithm
# ─────────────────────────────────────────────────────────────────────

class TestNNumber(unittest.TestCase):
    """Reference cases from guillaumemichel/icao-nnumber_converter."""

    REFERENCE_CASES = [
        (0xa00001, "N1"), (0xa00002, "N1A"), (0xa00003, "N1AA"),
        (0xa0001a, "N1AZ"), (0xa0001b, "N1B"), (0xa00259, "N1ZZ"),
        (0xa0025a, "N10"), (0xa0025b, "N10A"), (0xa0025c, "N10AA"),
        (0xa0070b, "N100ZZ"), (0xa0070c, "N1000"), (0xa00725, "N10000"),
        (0xa05157, "N11999"), (0xa05158, "N12"), (0xa18d50, "N2"),
        (0xa31a9f, "N3"), (0xac6a79, "N9"), (0xadf7c7, "N99999"),
        (0xa061d9, "N12345"), (0xabcdef, "N86QU"),
    ]

    def test_canonical_cases(self):
        for icao_int, expected in self.REFERENCE_CASES:
            with self.subTest(icao=f"{icao_int:06X}"):
                self.assertEqual(_icao_to_n(icao_int), expected)


# ─────────────────────────────────────────────────────────────────────
# Country lookup
# ─────────────────────────────────────────────────────────────────────

class TestCountry(unittest.TestCase):

    def test_known_blocks(self):
        cases = [
            ("4840D6", "NL"),    # KLM
            ("406B90", "GB"),    # British Airways
            ("3C6750", "DE"),    # Lufthansa
            ("A12345", "US"),    # USA
            ("C075DC", "CA"),    # Canada
            ("780A1B", "CN"),    # China
            ("840001", "JP"),    # Japan
        ]
        for icao, expected in cases:
            with self.subTest(icao=icao):
                res = country_for_icao(icao)
                self.assertIsNotNone(res, f"{icao} should resolve to a country")
                self.assertEqual(res[0], expected)

    def test_pia(self):
        self.assertTrue(is_pia("ADF7C8"))
        self.assertTrue(is_pia("AFFFFF"))
        self.assertFalse(is_pia("A12345"))
        self.assertFalse(is_pia("4840D6"))


# ─────────────────────────────────────────────────────────────────────
# Lookup cascade (DB → algorithmic → country)
# ─────────────────────────────────────────────────────────────────────

class TestLookupCascade(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not SEED_PATH.exists():
            from skywatch.db.seed import generate
            generate()
        cls.db = MictronicsDB(SEED_PATH)
        cls.db.load()
        cls.lookup = InfoLookup(mictronics_db=cls.db)

    def test_db_hit(self):
        info = self.lookup.lookup("406B90")
        self.assertEqual(info.registration, "G-EUYG")
        self.assertEqual(info.registration_source, "database")
        self.assertEqual(info.country_code, "GB")

    def test_algorithmic_fallback(self):
        # Not in seed DB, US block → algorithmic N-number recovery
        info = self.lookup.lookup("ABCDEF")
        self.assertEqual(info.registration, "N86QU")
        self.assertEqual(info.registration_source, "algorithmic")
        self.assertEqual(info.country_code, "US")

    def test_country_only(self):
        # Not in DB, not in any algorithmic range → country only
        info = self.lookup.lookup("780A1B")
        self.assertIsNone(info.registration)
        self.assertEqual(info.country_code, "CN")

    def test_operator_lookup(self):
        info = self.lookup.lookup("406B90", callsign="BAW217")
        self.assertIsNotNone(info.operator)
        self.assertEqual(info.operator.designator, "BAW")
        self.assertEqual(info.operator.name, "British Airways")


# ─────────────────────────────────────────────────────────────────────
# End-to-end engine integration with synthetic feed
# ─────────────────────────────────────────────────────────────────────

class TestIntentChangeEvents(unittest.TestCase):
    """Autopilot-intent change events should fire on selected-alt / QNH /
    mode flips, with hysteresis to defend against single noisy frames."""

    def _setup_engine(self):
        from skywatch.state import StateEngine
        if not SEED_PATH.exists():
            from skywatch.db.seed import generate
            generate()
        db = MictronicsDB(SEED_PATH)
        db.load()
        lookup = InfoLookup(mictronics_db=db)
        return StateEngine(receiver_lat=51.4775, receiver_lon=-0.4614,
                           info_lookup=lookup), BeastParser()

    def test_first_sel_alt_emits_immediately(self):
        from skywatch.decoder.synthetic import (
            make_bds40, make_df11_squitter,
        )
        from skywatch.decoder.beast import encode_beast
        engine, parser = self._setup_engine()
        # Establish the ICAO in the squitter roster first
        for f in parser.feed(encode_beast(make_df11_squitter("ABC123"), 0.0)):
            engine.feed(f)
        # First BDS 4,0 — should produce an immediate event
        for f in parser.feed(encode_beast(
            make_bds40("ABC123", 24000, 24000, 1013.2, alt_ft=20000), 1.0)):
            engine.feed(f)
        intent_evs = [e for e in engine.events if e.get("type") == "intent_change"]
        self.assertGreater(len(intent_evs), 0, "first SEL ALT should emit immediately")
        # Should mention 24,000 ft
        self.assertTrue(any("24,000" in e.get("summary", "") for e in intent_evs))

    def test_changed_sel_alt_with_hysteresis(self):
        """A change in selected altitude requires two confirming frames
        before the event is emitted."""
        from skywatch.decoder.synthetic import (
            make_bds40, make_df11_squitter,
        )
        from skywatch.decoder.beast import encode_beast
        engine, parser = self._setup_engine()
        # Roster
        for f in parser.feed(encode_beast(make_df11_squitter("ABC123"), 0.0)):
            engine.feed(f)
        # Establish initial value (immediate emit)
        for f in parser.feed(encode_beast(
            make_bds40("ABC123", 24000, 24000, 1013.2, alt_ft=20000), 1.0)):
            engine.feed(f)
        # Clear events from setup
        baseline = len([e for e in engine.events if e.get("type") == "intent_change"])
        # First frame with NEW value — should NOT emit yet
        for f in parser.feed(encode_beast(
            make_bds40("ABC123", 18000, 18000, 1013.2, alt_ft=20000), 2.0)):
            engine.feed(f)
        new_count = len([e for e in engine.events
                         if e.get("type") == "intent_change"])
        self.assertEqual(new_count, baseline,
            "single change frame should NOT emit (hysteresis)")
        # Second frame with same NEW value — should emit
        for f in parser.feed(encode_beast(
            make_bds40("ABC123", 18000, 18000, 1013.2, alt_ft=20000), 3.0)):
            engine.feed(f)
        confirmed = len([e for e in engine.events
                         if e.get("type") == "intent_change"])
        self.assertGreater(confirmed, baseline,
            "second confirming frame should emit the change event")
        # The new event should mention the change
        latest = [e for e in engine.events
                  if e.get("type") == "intent_change"][-1]
        self.assertIn("18,000", latest.get("summary", ""))

    def test_scenario_intent_changes(self):
        """The default scenario should produce all scripted intent events."""
        from skywatch.decoder.synthetic import default_scenario
        from skywatch.decoder.beast import encode_beast
        engine, parser = self._setup_engine()
        scn = default_scenario()
        for tick in range(95):
            for t, msg in scn.step(1.0):
                for f in parser.feed(encode_beast(msg, ts_seconds=t)):
                    engine.feed(f)
        # Count intent_change events by subtype
        intent = [e for e in engine.events if e.get("type") == "intent_change"]
        sel_alt_changes = [e for e in intent
                           if e.get("subtype") == "selected_altitude"
                           and e.get("old") is not None]
        qnh_changes = [e for e in intent
                       if e.get("subtype") == "qnh"
                       and e.get("old") is not None]
        # Scenario schedules 4 selected-altitude transitions with hysteresis
        # (DAL58 to 3000, KLM43H to 12000, EIN98K to 37000, KLM43H to 8000)
        # Each gives both MCP and FMS events because synthetic generator
        # sets both fields equal — so 8 transitions in total.
        self.assertGreaterEqual(len(sel_alt_changes), 6,
            f"expected ≥6 selected-alt transitions, got {len(sel_alt_changes)}")
        # Scenario schedules 1 QNH transition (DAL58 1013.2 → 1011.5)
        self.assertGreaterEqual(len(qnh_changes), 1,
            f"expected ≥1 QNH transition, got {len(qnh_changes)}")


# ─────────────────────────────────────────────────────────────────────
# Full pipeline integration
# ─────────────────────────────────────────────────────────────────────

class TestIntegration(unittest.TestCase):
    """Run the full pipeline against the synthetic scenario."""

    def test_synthetic_scenario(self):
        from skywatch.state import StateEngine

        if not SEED_PATH.exists():
            from skywatch.db.seed import generate
            generate()
        db = MictronicsDB(SEED_PATH)
        db.load()
        lookup = InfoLookup(mictronics_db=db)

        scn = default_scenario()
        engine = StateEngine(
            receiver_lat=scn.receiver_lat, receiver_lon=scn.receiver_lon,
            info_lookup=lookup,
        )
        parser = BeastParser()
        for tick in range(60):
            for t, msg in scn.step(1.0):
                beast = encode_beast(msg, ts_seconds=t, signal=180)
                for f in parser.feed(beast):
                    engine.feed(f)

        # All 5 scenario aircraft should be tracked
        self.assertEqual(len(engine.aircraft), 5)
        self.assertEqual(engine.frames_dropped, 0)

        # Each should have a position decoded
        for icao, ac in engine.aircraft.items():
            self.assertIsNotNone(ac.lat, f"{icao} has no lat")
            self.assertIsNotNone(ac.lon, f"{icao} has no lon")
            self.assertIsNotNone(ac.callsign, f"{icao} has no callsign")
            self.assertIsNotNone(ac.alt_baro_ft, f"{icao} has no altitude")

        # TCAS event should have been logged
        self.assertGreaterEqual(len(engine.tcas_event_log), 2,
                                "TCAS RA event between two aircraft expected")

        # BAW217 should have its DB info resolved
        baw = engine.aircraft["406B90"]
        self.assertEqual(baw.db_info.registration, "G-EUYG")
        self.assertEqual(baw.db_info.operator.name, "British Airways")


if __name__ == "__main__":
    unittest.main(verbosity=2)
