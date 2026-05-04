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

    def test_bds_modes_surface_separately_no_intent_event(self):
        """BDS 4,0 mode flags must populate `autopilot_modes_bds` for
        UI display, but they must NOT generate intent_change ap_mode
        events (TC=29 is the sole source of mode-flap events to avoid
        the cross-source disagreement spam)."""
        from skywatch.decoder.synthetic import (
            make_bds40, make_df11_squitter,
        )
        from skywatch.decoder.beast import encode_beast
        engine, parser = self._setup_engine()
        # Roster the aircraft via a DF11 squitter.
        for f in parser.feed(encode_beast(make_df11_squitter("ABC123"), 0.0)):
            engine.feed(f)
        # First BDS 4,0 with mode flags set.  Should populate
        # autopilot_modes_bds, leave autopilot_modes empty (TC=29 is
        # the only writer of that), and not log a mode-flag intent
        # event.  A first-ever sel_alt event IS expected (BDS40 is
        # authoritative for that).
        for f in parser.feed(encode_beast(
            make_bds40("ABC123", 24000, 24000, 1013.2, alt_ft=20000,
                       vnav_mode=True, alt_hold_mode=False,
                       approach_mode=True),
            1.0)):
            engine.feed(f)
        ac = engine.aircraft["ABC123"]
        self.assertEqual(
            ac.autopilot_modes_bds,
            {"vnav": True, "alt_hold": False, "approach": True},
        )
        self.assertEqual(ac.autopilot_modes, {},
            "TC=29 has not been seen yet, so autopilot_modes must stay empty")
        # Flip the BDS flags on a subsequent frame; still no mode-flag
        # intent events should be generated.
        for f in parser.feed(encode_beast(
            make_bds40("ABC123", 24000, 24000, 1013.2, alt_ft=20000,
                       vnav_mode=False, alt_hold_mode=True,
                       approach_mode=False),
            2.0)):
            engine.feed(f)
        for f in parser.feed(encode_beast(
            make_bds40("ABC123", 24000, 24000, 1013.2, alt_ft=20000,
                       vnav_mode=False, alt_hold_mode=True,
                       approach_mode=False),
            3.0)):
            engine.feed(f)
        ap_mode_events = [e for e in engine.events
                          if e.get("type") == "intent_change"
                          and e.get("subtype") == "ap_mode"]
        self.assertEqual(ap_mode_events, [],
            f"BDS-flag flips must not fire ap_mode events; got: "
            f"{ap_mode_events}")
        # And the latest BDS view is reflected in to_dict for the UI.
        d = ac.to_dict()
        self.assertEqual(
            d["autopilot_modes_bds"],
            {"vnav": False, "alt_hold": True, "approach": False},
        )


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


# ─────────────────────────────────────────────────────────────────────
# Multi-receiver ingestion: receiver attribution, same-RX CPR pairing,
# multi-RX plausibility.
# ─────────────────────────────────────────────────────────────────────

class TestMultiReceiver(unittest.TestCase):
    """Verify the multi-receiver invariants:
       - frames carry their receiver_id end-to-end into Aircraft.by_receiver
       - CPR global decode only pairs same-receiver halves
       - plausibility passes if any registered receiver is in range
    """

    def _engine(self, receivers):
        """Build a bare engine seeded with the given receivers."""
        from skywatch.state import StateEngine
        eng = StateEngine()
        for spec in receivers:
            eng.receivers.upsert(
                spec["id"],
                name=spec.get("name", spec["id"]),
                lat=spec.get("lat"),
                lon=spec.get("lon"),
                max_range_nm=spec.get("max_range_nm", 280.0),
            )
        return eng

    def _push(self, engine, parser, raw_hex, ts):
        """Helper: encode + parse + feed."""
        for f in parser.feed(encode_beast(raw_hex, ts_seconds=ts)):
            engine.feed(f)

    def test_per_receiver_attribution(self):
        """Frames from two receivers update both the merged top-level
        counters AND the per-receiver buckets."""
        from skywatch.decoder.synthetic import make_df11_squitter
        eng = self._engine([{"id": "rx1"}, {"id": "rx2"}])
        p1 = BeastParser(receiver_id="rx1")
        p2 = BeastParser(receiver_id="rx2")
        # 3 frames from rx1, 2 from rx2
        for i in range(3):
            self._push(eng, p1, make_df11_squitter("ABC123"), float(i))
        for i in range(2):
            self._push(eng, p2, make_df11_squitter("ABC123"), float(10 + i))

        ac = eng.aircraft["ABC123"]
        self.assertIn("rx1", ac.by_receiver)
        self.assertIn("rx2", ac.by_receiver)
        self.assertEqual(ac.by_receiver["rx1"].rssi_samples, 3)
        self.assertEqual(ac.by_receiver["rx2"].rssi_samples, 2)
        # Merged view counts every frame.
        self.assertEqual(ac.rssi_samples, 5)
        # Engine-level receiver counters reflect per-RX traffic.
        self.assertEqual(eng.receivers.get("rx1").frames_total, 3)
        self.assertEqual(eng.receivers.get("rx2").frames_total, 2)

    def test_cpr_global_decode_requires_same_receiver(self):
        """An even from rx1 and odd from rx2 within the 10 s pair window
        must NOT produce a global position decode — only the local
        decode path (which needs a prior fix) is allowed across
        receivers."""
        from skywatch.decoder.synthetic import (
            make_df11_squitter, make_airborne_position,
        )
        # Use a real-world example from Sun ch 5: airborne position pair.
        # synthetic.make_airborne_position generates encoded CPR halves
        # for a given lat/lon.
        # Receivers placed near London so plausibility is satisfied.
        eng = self._engine([
            {"id": "rx1", "lat": 51.4775, "lon": -0.4614},
            {"id": "rx2", "lat": 51.5, "lon": -0.5},
        ])
        p1 = BeastParser(receiver_id="rx1")
        p2 = BeastParser(receiver_id="rx2")

        # Roster the aircraft via DF11 from rx1.
        self._push(eng, p1, make_df11_squitter("ABC123"), 0.0)

        # rx1 sees the EVEN half...
        msg_even = make_airborne_position("ABC123", 51.0, -0.5, 35000,
                                          even=True)
        # ...and rx2 sees the ODD half a second later.
        msg_odd = make_airborne_position("ABC123", 51.0, -0.5, 35000,
                                         even=False)
        self._push(eng, p1, msg_even, 1.0)
        self._push(eng, p2, msg_odd, 2.0)

        ac = eng.aircraft["ABC123"]
        # No same-receiver pair exists, so no global decode happened.
        # And no prior fix → no local decode either.
        self.assertIsNone(ac.lat, "cross-receiver CPR pair must NOT decode")
        self.assertIsNone(ac.lon)
        # The two halves are tracked under their respective receivers.
        self.assertIn("rx1", ac._cpr_even)
        self.assertIn("rx2", ac._cpr_odd)
        self.assertNotIn("rx2", ac._cpr_even)
        self.assertNotIn("rx1", ac._cpr_odd)

        # If rx1 also sees the ODD half, the same-receiver pair should
        # decode successfully.
        self._push(eng, p1, msg_odd, 3.0)
        self.assertIsNotNone(
            ac.lat, "same-receiver pair on rx1 must decode globally")

    def test_plausibility_any_receiver_in_range(self):
        """A candidate position is plausible as long as ONE receiver
        is within range; out-of-range for one but in-range for another
        must still pass."""
        # rx1 is at (51, 0), rx2 is at (40, -74) — 5000 km apart.
        # max_range_nm is 280 NM (~520 km) on each.
        eng = self._engine([
            {"id": "rx1", "lat": 51.0, "lon": 0.0, "max_range_nm": 280},
            {"id": "rx2", "lat": 40.0, "lon": -74.0, "max_range_nm": 280},
        ])
        from skywatch.state.aircraft import Aircraft
        ac = Aircraft(icao="ABC123")

        # Within rx1 range (~50 km from rx1) but ~5000 km from rx2 → pass
        self.assertTrue(eng._is_plausible(ac, 51.5, 0.5, 0.0))
        # Within rx2 range but far from rx1 → pass
        self.assertTrue(eng._is_plausible(ac, 40.2, -74.2, 0.0))
        # Out of both — at the equator, mid-atlantic
        self.assertFalse(eng._is_plausible(ac, 0.0, -30.0, 0.0))
        # Bad latitude bounds always fail
        self.assertFalse(eng._is_plausible(ac, 95.0, 0.0, 0.0))

    def test_snapshot_includes_receivers_list(self):
        """The snapshot WS payload exposes the full receiver registry
        so the multi-RX UI can render per-RX range rings and a
        connected/total counter."""
        eng = self._engine([
            {"id": "home",   "lat": 51.0, "lon": 0.0},
            {"id": "office", "lat": 53.0, "lon": -2.0},
        ])
        snap = eng.snapshot()
        self.assertIn("receivers", snap)
        ids = sorted(r["id"] for r in snap["receivers"])
        self.assertEqual(ids, ["home", "office"])
        # Legacy `receiver` block remains, filled with the primary RX.
        self.assertEqual(snap["receiver"]["lat"], 51.0)


# ─────────────────────────────────────────────────────────────────────
# MongoStore: persistence wiring.  Skipped when pymongo isn't installed.
# These tests verify the store API surface (no real MongoDB needed) by
# stubbing the client; an integration test against a live MongoDB is
# out-of-scope for the unit suite.
# ─────────────────────────────────────────────────────────────────────

class TestMongoStoreOptionalImport(unittest.TestCase):
    """The store import guard must not fail when pymongo is absent."""

    def test_optional_import(self):
        from skywatch.store import HAS_MONGO, MongoStore
        # MongoStore is None when pymongo is not installed.
        if not HAS_MONGO:
            self.assertIsNone(MongoStore)
        else:
            self.assertIsNotNone(MongoStore)

    def test_engine_accepts_no_store(self):
        """Engine works fine with store=None (default)."""
        from skywatch.state import StateEngine
        eng = StateEngine()
        self.assertIsNone(eng.store)
        # Frame ingestion path must not blow up when no store is wired.
        # (Use the synthetic generator's helpers as inputs.)
        from skywatch.decoder.synthetic import make_df11_squitter
        p = BeastParser(receiver_id="test")
        for f in p.feed(encode_beast(make_df11_squitter("ABC123"), 0.0)):
            eng.feed(f)
        self.assertIn("ABC123", eng.aircraft)


# ─────────────────────────────────────────────────────────────────────
# Edge ↔ central transport: spool, ABC, WS round-trip, central merger.
# Mongo-mode tests are covered by the integration suite (need a live RS).
# ─────────────────────────────────────────────────────────────────────

class TestEdgeSpool(unittest.TestCase):
    """SQLite-backed FIFO with size cap.  These all run in tmpdirs so
    nothing leaks between tests."""

    def setUp(self):
        self._spools_to_cleanup: list = []

    def tearDown(self):
        for sp, tmp in self._spools_to_cleanup:
            try:
                sp.close()
            except Exception:
                pass
            try:
                tmp.cleanup()
            except Exception:
                pass

    def _spool(self, max_rows=10):
        import tempfile, os
        from skywatch.edge.spool import Spool
        tmp = tempfile.TemporaryDirectory()
        sp = Spool(os.path.join(tmp.name, "s.sqlite"), max_rows=max_rows)
        self._spools_to_cleanup.append((sp, tmp))
        return sp

    def test_fifo_eviction_on_overflow(self):
        sp = self._spool(max_rows=5)
        for i in range(8):
            sp.enqueue({"gen": i, "type": "aircraft",
                        "receiver_id": "rx1", "ts": 0.0, "payload": {}})
        self.assertEqual(sp.count(), 5)
        self.assertEqual(sp.dropped, 3)
        rows = sp.peek_batch(10)
        self.assertEqual([r[1]["gen"] for r in rows], [3, 4, 5, 6, 7])
        sp.close()

    def test_pop_drains_in_order(self):
        sp = self._spool(max_rows=10)
        for i in range(5):
            sp.enqueue({"gen": i, "type": "aircraft",
                        "receiver_id": "rx1", "ts": 0.0, "payload": {}})
        rows = sp.peek_batch(3)
        sp.pop_to(rows[-1][0])
        self.assertEqual(sp.count(), 2)
        rows = sp.peek_batch(10)
        self.assertEqual([r[1]["gen"] for r in rows], [3, 4])
        sp.close()

    def test_corrupt_row_dropped_silently(self):
        # Direct sqlite insert of a row that isn't valid JSON; peek
        # should drop it and continue.
        import tempfile, os, sqlite3
        from skywatch.edge.spool import Spool
        with tempfile.TemporaryDirectory() as d:
            sp = Spool(os.path.join(d, "s.sqlite"), max_rows=10)
            sp.enqueue({"gen": 1, "type": "aircraft",
                        "receiver_id": "rx", "ts": 0.0, "payload": {}})
            # Inject a garbage row by hand.
            sp._conn.execute(
                "INSERT INTO deltas(payload) VALUES ('not-json')",
            )
            sp.enqueue({"gen": 2, "type": "aircraft",
                        "receiver_id": "rx", "ts": 0.0, "payload": {}})
            rows = sp.peek_batch(10)
            # Corrupt one is silently dropped; valid ones remain in order.
            self.assertEqual([r[1]["gen"] for r in rows], [1, 2])
            sp.close()


class TestTransportContract(unittest.TestCase):
    """Both transport implementations must conform to the same Transport
    ABC surface (start/stop/send/subscribe)."""

    def test_abc_methods_present(self):
        from skywatch.transport import Transport
        # Concrete implementations subclass Transport and implement all
        # abstract methods (would error on instantiation otherwise).
        from skywatch.transport.websocket_push import WebSocketPushTransport
        self.assertTrue(issubclass(WebSocketPushTransport, Transport))
        # Mongo subclass is gated on pymongo availability.
        try:
            import pymongo  # noqa: F401
            from skywatch.transport.mongo_changestream import (
                MongoChangeStreamTransport,
            )
            self.assertTrue(issubclass(MongoChangeStreamTransport, Transport))
        except ImportError:
            self.skipTest("pymongo not installed")

    def test_websocket_round_trip(self):
        """Real edge → central round-trip on localhost.  Uses a random
        free port to avoid clashing with anything else."""
        import socket, threading, time
        from skywatch.transport import Delta
        from skywatch.transport.websocket_push import WebSocketPushTransport

        # Find a free port
        s = socket.socket(); s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]; s.close()

        received: list[Delta] = []
        central = WebSocketPushTransport(
            bind=f"127.0.0.1:{port}", path="/ingest", token="test-secret",
        )
        central.start()
        central.subscribe(received.append)

        edge = WebSocketPushTransport(
            central_url=f"ws://127.0.0.1:{port}/ingest",
            token="test-secret",
        )
        edge.start()

        # Wait for the edge to handshake.  Polling beats sleep().
        deadline = time.time() + 3.0
        while edge.sent_total == 0 and time.time() < deadline:
            edge.send(Delta("aircraft", "rx-test", 1, {"icao": "ABCDEF"}))
            time.sleep(0.05)
        # And wait for the central to receive.
        deadline = time.time() + 3.0
        while not received and time.time() < deadline:
            time.sleep(0.05)
        edge.stop(); central.stop()

        self.assertGreaterEqual(len(received), 1)
        self.assertEqual(received[0].receiver_id, "rx-test")
        self.assertEqual(received[0].payload.get("icao"), "ABCDEF")


class TestCentralMerger(unittest.TestCase):
    """Verify the merger's gen-tracking and aircraft-merge behaviour."""

    def _bind_merger(self):
        from skywatch.central.merger import CentralMerger
        from skywatch.state import StateEngine
        eng = StateEngine()
        return CentralMerger(eng), eng

    def test_aircraft_delta_creates_and_updates(self):
        from skywatch.transport import Delta, DELTA_TYPE_AIRCRAFT
        m, eng = self._bind_merger()
        d = Delta(DELTA_TYPE_AIRCRAFT, "rx1", 1, {
            "icao": "ABCDEF", "callsign": "TEST1",
            "lat": 51.5, "lon": -0.5, "alt_baro_ft": 35000,
            "last_seen": 100.0, "first_seen": 90.0,
            "by_receiver": {"rx1": {
                "rssi": -42.0, "rssi_samples": 7,
                "msg_counts": {"17": 5, "11": 2},
                "first_seen": 90.0, "last_seen": 100.0,
                "gen": 7,
            }},
        })
        m.apply_delta(d)
        self.assertIn("ABCDEF", eng.aircraft)
        ac = eng.aircraft["ABCDEF"]
        self.assertEqual(ac.callsign, "TEST1")
        self.assertEqual(ac.lat, 51.5)
        self.assertIn("rx1", ac.by_receiver)
        self.assertEqual(ac.by_receiver["rx1"].rssi_avg, -42.0)
        self.assertEqual(ac.by_receiver["rx1"].rssi_samples, 7)

    def test_gen_gap_increments_counter(self):
        from skywatch.transport import Delta, DELTA_TYPE_AIRCRAFT
        m, _ = self._bind_merger()
        for gen in (1, 2, 5):  # gap of 2 between 2→5
            m.apply_delta(Delta(DELTA_TYPE_AIRCRAFT, "rx1", gen,
                                {"icao": "ABCDEF"}))
        self.assertEqual(m.gen_gaps, 2)

    def test_gen_reset_treated_as_edge_restart(self):
        from skywatch.transport import Delta, DELTA_TYPE_AIRCRAFT
        m, _ = self._bind_merger()
        # Big run then a reset to 1 — should not be a gap.
        for gen in (1, 2, 3, 4, 200, 1, 2):
            m.apply_delta(Delta(DELTA_TYPE_AIRCRAFT, "rx1", gen,
                                {"icao": "ABCDEF"}))
        self.assertEqual(m.gen_resets, 1)

    def test_two_receivers_merge_into_one_aircraft(self):
        from skywatch.transport import Delta, DELTA_TYPE_AIRCRAFT
        m, eng = self._bind_merger()
        m.apply_delta(Delta(DELTA_TYPE_AIRCRAFT, "rx1", 1, {
            "icao": "ABCDEF",
            "by_receiver": {"rx1": {"rssi": -42.0, "rssi_samples": 1,
                                    "msg_counts": {}, "first_seen": 1.0,
                                    "last_seen": 1.0, "gen": 1}},
        }))
        m.apply_delta(Delta(DELTA_TYPE_AIRCRAFT, "rx2", 1, {
            "icao": "ABCDEF",
            "by_receiver": {"rx2": {"rssi": -28.0, "rssi_samples": 1,
                                    "msg_counts": {}, "first_seen": 2.0,
                                    "last_seen": 2.0, "gen": 1}},
        }))
        ac = eng.aircraft["ABCDEF"]
        self.assertEqual(set(ac.by_receiver), {"rx1", "rx2"})
        self.assertEqual(ac.by_receiver["rx1"].rssi_avg, -42.0)
        self.assertEqual(ac.by_receiver["rx2"].rssi_avg, -28.0)


class TestEdgeRunner(unittest.TestCase):
    """End-to-end edge: feed it via the synthetic scenario and assert
    the transport receives properly-shaped deltas."""

    def test_edge_emits_aircraft_deltas(self):
        # In-memory transport stub — implements the ABC and just records.
        from skywatch.transport import Transport, Delta, DELTA_TYPE_AIRCRAFT

        class CaptureTransport(Transport):
            def __init__(self):
                self.deltas: list[Delta] = []
            def start(self): pass
            def stop(self): pass
            def send(self, d):
                self.deltas.append(d); return True
            def subscribe(self, cb): pass

        from skywatch.edge.runner import EdgeRunner
        from skywatch.decoder.synthetic import (
            make_df11_squitter, make_airborne_position,
        )

        cap = CaptureTransport()
        # We don't actually run the BEAST socket loop here; we drive the
        # engine directly via a parser to keep the test fast and
        # offline.  The runner's transport-bridging path is what matters.
        runner = EdgeRunner(
            receiver_id="rx-edge", beast_host="unused", beast_port=0,
            transport=cap, receiver_lat=51.4775, receiver_lon=-0.4614,
        )
        # The runner subscribes to the engine's listeners on construction;
        # do not call start() so we skip the BEAST thread.
        p = BeastParser(receiver_id="rx-edge")
        for raw in (
            make_df11_squitter("ABC123"),
            make_airborne_position("ABC123", 51.5, -0.5, 35000, even=True),
            make_airborne_position("ABC123", 51.5, -0.5, 35000, even=False),
        ):
            for f in p.feed(encode_beast(raw, ts_seconds=1.0)):
                runner.engine.feed(f)
        # The first delta is the receiver registration
        # ('_push_receiver_state' invoked from runner.__init__ — no, it's in
        # start()).  Since we skipped start(), aircraft deltas are all we have.
        ac_deltas = [d for d in cap.deltas if d.type == DELTA_TYPE_AIRCRAFT]
        self.assertGreater(len(ac_deltas), 0,
            "edge runner should ship at least one aircraft delta")
        # Every delta must be tagged with our receiver_id and have
        # monotonic gen counters.
        gens = [d.gen for d in cap.deltas]
        self.assertEqual(gens, sorted(gens))
        for d in cap.deltas:
            self.assertEqual(d.receiver_id, "rx-edge")


if __name__ == "__main__":
    unittest.main(verbosity=2)
