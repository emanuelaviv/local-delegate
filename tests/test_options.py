"""The options table and the recommendation under each priority preset (no model runs)."""

import time
import unittest

from helpers import Env, fake_profile, future_night_window, set_config
from local_delegate import logbook, server
from test_e2e import big_report


class Options(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        fake_profile(self.env)          # fast reading, slow writing, like the reference machine
        self.svc = server.Service()
        set_config(self.env, night_window=future_night_window())   # results don't depend on the hour
        self.report = self.env.write("report.md", big_report())

    def tearDown(self):
        self.env.close()

    def est(self, **kw):
        return self.svc.local_estimate("digest", {"paths": [str(self.report)]}, **kw)

    def test_each_preset_recommends_what_it_promises(self):
        rush = self.est(priority="rush")
        self.assertEqual(rush["action"], "do_here", rush)
        self.assertEqual(rush["recommended"], "claude")
        bal = self.est(priority="balanced")
        self.assertEqual(bal["action"], "ask")
        self.assertTrue(bal["recommended"].endswith("_now"), bal["recommended"])
        saver = self.est(priority="saver")
        self.assertIn(saver["recommended"].split("_")[0], ("careful", "deep"))
        night = self.est(priority="night")
        self.assertTrue(night["recommended"].endswith("_tonight"), night["recommended"])
        self.assertTrue(night["recommended"].startswith("deep"))

    def test_options_carry_time_tokens_and_quality(self):
        e = self.est()
        self.assertLessEqual(len(e["options"]), 4)
        self.assertEqual(e["options"][0]["recommended"], True)
        locals_ = [o for o in e["options"] if o["id"] not in ("claude", "script")]
        for o in locals_:
            self.assertRegex(o["description"], r"(Ready|Starts|Runs now)")
            self.assertIn("tokens", o["description"])
            self.assertIn("redo risk", o["description"])
            lo, hi = o["minutes"]
            self.assertLessEqual(lo, hi)
        claude = next(o for o in e["options"] if o["id"] == "claude")
        self.assertIn("Claude tokens", claude["description"])
        self.assertLessEqual(len(e["ask_text"].splitlines()), 5)
        # deeper reasoning takes longer
        runs = self.svc.estimates[e["estimate_id"]]["est"]["_runs"]
        self.assertLess(runs["fast_now"]["p50_s"], runs["careful_now"]["p50_s"])
        self.assertLess(runs["careful_now"]["p50_s"], runs["deep_now"]["p50_s"])
        self.assertGreater(runs["deep_tonight"]["think_budget"], runs["deep_now"]["think_budget"])

    def test_need_by_drops_late_options(self):
        e = self.est(need_by="3 min", priority="saver")
        ids = [o["id"] for o in e["options"]]
        self.assertFalse(any(i.endswith("_tonight") for i in ids), ids)
        late = {x["id"]: x["why"] for x in e["not_possible"]}
        self.assertIn("after you need it", late.get("deep_now", ""))

    def test_redo_risk_learns_from_feedback(self):
        before = self.est()
        fast_before = next(o for o in before["options"] + (before.get("more_options") or [])
                           if o["id"] == "fast_now")
        self.assertIn("starting guess", fast_before["description"])
        for i in range(6):
            jid = f"j{i}"
            logbook.append({"type": "job", "job_id": jid, "task": "digest", "mode": "fast", "status": "done"})
            logbook.feedback(jid, "redone")
        after = self.est()
        rates = logbook.outcome_rates("digest", "fast")
        self.assertGreater(rates["redo"], 0.4)
        o = next(o for o in after["options"] + (after.get("more_options") or []) if o["id"] == "fast_now")
        self.assertNotIn("starting guess", o["description"])
        self.assertTrue(after["recommended"] != "fast_now")

    def test_tonight_inside_the_window_runs_now(self):
        t = time.localtime()
        start = f"{(t.tm_hour - 1) % 24:02d}:00"
        end = f"{(t.tm_hour + 2) % 24:02d}:00"
        set_config(self.env, night_window={"start": start, "end": end})
        e = self.est(priority="night")
        o = next(o for o in e["options"] if o["id"].endswith("_tonight"))
        self.assertIn("Runs now through the night", o["description"])

    def test_privacy_first_never_recommends_claude(self):
        e = self.est(prefer_local=True, priority="rush")
        self.assertNotEqual(e["recommended"], "claude")
        self.assertEqual(e["options"][-1]["id"], "claude")


if __name__ == "__main__":
    unittest.main()
