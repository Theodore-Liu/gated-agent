"""Unit tests for options_mapper. Pure logic, no network. Run: python test_options_mapper.py"""
from __future__ import annotations

import unittest
from datetime import date

from gated_agent.options_mapper import MapperConfig, map_signal


TODAY = date(2026, 8, 28)
SPOT = 500.0


def C(sym, typ, strike, exp, bid, ask, oi=500):
    return {"symbol": sym, "type": typ, "strike_price": strike,
            "expiration_date": exp, "open_interest": oi, "bid": bid, "ask": ask}


def make_chain():
    """Synthetic SPY-ish chain: one good expiry (7 DTE), strikes every 5."""
    exp = "2026-09-04"
    chain = []
    for k in range(480, 525, 5):
        # intrinsic + decaying time value: ATM rich, OTM visibly cheaper
        tv = max(8.0 - 0.4 * abs(SPOT - k), 0.5)
        m = max(SPOT - k, 0) + tv
        chain.append(C(f"CALL{k}", "call", float(k), exp, m - 0.05, m + 0.05))
        p = max(k - SPOT, 0) + tv
        chain.append(C(f"PUT{k}", "put", float(k), exp, p - 0.05, p + 0.05))
    return chain


class TestTiers(unittest.TestCase):
    def test_neutral_and_weak_stand_aside(self):
        chain = make_chain()
        self.assertEqual(map_signal({"symbol": "SPY", "direction": "neutral",
                                     "strength": 0.9, "spot": SPOT}, chain, TODAY), [])
        self.assertEqual(map_signal({"symbol": "SPY", "direction": "long",
                                     "strength": 0.1, "spot": SPOT}, chain, TODAY), [])

    def test_strong_long_is_call_debit_spread(self):
        legs = map_signal({"symbol": "SPY", "direction": "long",
                           "strength": 0.9, "spot": SPOT}, make_chain(), TODAY)
        self.assertEqual(len(legs), 2)
        buy, sell = legs
        self.assertEqual(buy["side"], "buy")
        self.assertEqual(sell["side"], "sell")
        self.assertIn("CALL", buy["occ_symbol"])
        # buy leg ATM (=500), sell leg above spot
        self.assertEqual(buy["occ_symbol"], "CALL500")
        self.assertGreater(float(sell["occ_symbol"][4:]), 500)
        self.assertEqual(buy["qty"], sell["qty"])

    def test_mild_long_is_cash_secured_put(self):
        legs = map_signal({"symbol": "SPY", "direction": "long",
                           "strength": 0.5, "spot": SPOT}, make_chain(), TODAY)
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["side"], "sell")
        self.assertIn("PUT", legs[0]["occ_symbol"])
        self.assertLess(float(legs[0]["occ_symbol"][3:]), SPOT)  # OTM

    def test_strong_short_is_put_debit_spread(self):
        legs = map_signal({"symbol": "SPY", "direction": "short",
                           "strength": 0.8, "spot": SPOT}, make_chain(), TODAY)
        self.assertEqual(len(legs), 2)
        self.assertIn("PUT", legs[0]["occ_symbol"])

    def test_mild_short_is_credit_call_spread(self):
        legs = map_signal({"symbol": "SPY", "direction": "short",
                           "strength": 0.5, "spot": SPOT}, make_chain(), TODAY)
        self.assertEqual(len(legs), 2)
        sell, buy = legs
        self.assertEqual(sell["side"], "sell")
        self.assertEqual(buy["side"], "buy")
        # both OTM calls, buy further out than sell
        self.assertGreater(float(buy["occ_symbol"][4:]), float(sell["occ_symbol"][4:]))


class TestGates(unittest.TestCase):
    def test_wide_spread_rejected(self):
        chain = make_chain()
        for c in chain:  # blow out every quote
            c["bid"], c["ask"] = 1.0, 2.0   # 66% spread
        legs = map_signal({"symbol": "SPY", "direction": "long",
                           "strength": 0.9, "spot": SPOT}, chain, TODAY)
        self.assertEqual(legs, [])

    def test_low_open_interest_rejected(self):
        chain = make_chain()
        for c in chain:
            c["open_interest"] = 3
        legs = map_signal({"symbol": "SPY", "direction": "long",
                           "strength": 0.9, "spot": SPOT}, chain, TODAY)
        self.assertEqual(legs, [])

    def test_unknown_oi_passes(self):
        chain = make_chain()
        for c in chain:
            c["open_interest"] = None    # indicative feed often lacks OI
        legs = map_signal({"symbol": "SPY", "direction": "long",
                           "strength": 0.9, "spot": SPOT}, chain, TODAY)
        self.assertEqual(len(legs), 2)

    def test_expiry_window(self):
        chain = make_chain()
        for c in chain:
            c["expiration_date"] = "2026-10-30"   # 63 DTE, outside 5..16
        legs = map_signal({"symbol": "SPY", "direction": "long",
                           "strength": 0.9, "spot": SPOT}, chain, TODAY)
        self.assertEqual(legs, [])

    def test_risk_cap_limits_qty(self):
        cfg = MapperConfig(equity=10_000.0)   # 1% = $100 max loss
        legs = map_signal({"symbol": "SPY", "direction": "long",
                           "strength": 0.9, "spot": SPOT}, make_chain(), TODAY, cfg)
        # spread debit ~ a few dollars * 100 -> qty 0 -> stand aside, never oversize
        self.assertEqual(legs, [])

    def test_deterministic(self):
        sig = {"symbol": "SPY", "direction": "long", "strength": 0.9, "spot": SPOT}
        a = map_signal(sig, make_chain(), TODAY)
        b = map_signal(sig, make_chain(), TODAY)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
