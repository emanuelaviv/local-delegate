"""End to end against the fake Ollama: estimate -> option chosen -> background worker -> checked result."""

import json
import re
import subprocess
import time
import unittest
from pathlib import Path

from helpers import GRADLE_LOG, PYTEST_LOG, SAMPLE_REPORT, Env, fake_profile, future_night_window, set_config
from local_delegate import calibrate, config, logbook, server


def big_report(n=300):
    parts = [SAMPLE_REPORT]
    for i in range(n):
        parts.append(f"\n## Step {i}\nWorked on module_{i}. The checks for unit {i} ran and the output was recorded in "
                     f"`app/src/main/java/app/example/step{i}/Unit{i}.kt`. Nothing else changed in this step, and the "
                     f"next step continues from the same branch without new dependencies.\n")
    return "".join(parts)


def doc_lines(req):
    text = "\n".join(m["content"] for m in req["messages"] if m["role"] == "user")
    return {int(m.group(1)): m.group(2) for m in re.finditer(r"^D1:(\d+)\| (.*)$", text, re.M)}


def digest_answer(lines, bad_note=True):
    def find(word):
        return next(n for n, t in lines.items() if word in t)

    def cite(n, k=5):
        return {"doc": "D1", "line": n, "quote": " ".join(lines[n].split()[:k])}

    s, a, todo = find("Status:"), find("APPROVAL NEEDED"), find("TODO")
    out = {"status": {"text": "Done; waiting for approval", "cite": cite(s)},
           "files": [{"path": "backend/migrations/20261020_inventory_theme.sql", "action": "added",
                      "cite": cite(find("migrations"))}],
           "tests": [{"what": "gradlew testDebugUnitTest", "result": "142 passed", "cite": cite(find("gradlew"))}],
           "open_items": [{"text": "confirm French labels", "cite": cite(todo)}],
           "approval_lines": [cite(a, 6)], "decisions": [], "notes": []}
    if bad_note:
        out["notes"] = [{"text": "invented", "cite": {"doc": "D1", "line": 3, "quote": "this sentence is not in the report"}}]
    return out


def digest_responder(req):
    return digest_answer(doc_lines(req))


def chats(env):
    return [r for r in env.fake.requests if r["path"] == "/api/chat"]


class E2E(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        fake_profile(self.env)
        self.svc = server.Service()

    def tearDown(self):
        self.env.close()

    def test_small_report_is_done_here(self):
        p = self.env.write("report.md", SAMPLE_REPORT)
        est = self.svc.local_estimate("digest", {"paths": [str(p)]})
        self.assertEqual(est["action"], "do_here", est)
        self.assertIn("local alternative", est["next"])

    def test_digest_ask_and_run_fast(self):
        p = self.env.write("report.md", big_report())
        self.env.fake.responder = digest_responder
        est = self.svc.local_estimate("digest", {"paths": [str(p)]})
        self.assertEqual(est["action"], "ask", est)
        ids = [o["id"] for o in est["options"]]
        self.assertIn("claude", ids)
        self.assertLessEqual(len(ids), 4)
        self.assertTrue(any(o.get("recommended") for o in est["options"]))
        self.assertIn("How should it run?", est["ask_text"])
        out = self.svc.local_run(est["estimate_id"], option="fast_now")
        self.assertIn("approval needed", out["error"])
        out = self.svc.local_run(est["estimate_id"], option="fast_now", user_approved=True, wait_s=15)
        self.assertEqual(out["status"], "done", out)
        res = out["result"]
        self.assertEqual(res["citation_check"]["unverified_quotes"][0]["quote"], "this sentence is not in the report")
        chat = chats(self.env)[-1]
        self.assertEqual(chat["think"], "none")                 # reasoning off at the fast level
        self.assertEqual(chat["options"]["temperature"], 0.0)
        self.assertIsInstance(chat["format"], dict)
        self.assertGreaterEqual(chat["options"]["num_ctx"], 16384)
        self.assertEqual(self.svc.local_feedback(out["job_id"], "accepted"), {"recorded": True})
        summ = logbook.summary()
        self.assertEqual(summ["per_task_and_level"]["digest / fast"]["accepted"], 1)

    def test_careful_uses_low_reasoning_and_model_card_sampling(self):
        p = self.env.write("report.md", big_report())
        self.env.fake.responder = digest_responder
        est = self.svc.local_estimate("digest", {"paths": [str(p)]})
        out = self.svc.local_run(est["estimate_id"], option="careful_now", user_approved=True, wait_s=15)
        self.assertEqual(out["status"], "done", out)
        chat = chats(self.env)[-1]
        self.assertEqual(chat["think"], "low")
        self.assertEqual(chat["options"]["temperature"], 1.0)
        self.assertEqual(chat["options"]["top_k"], 20)
        self.assertGreaterEqual(chat["options"]["num_predict"], 3000)   # answer cap + reasoning budget
        self.assertEqual(out["timing"]["reasoning_tokens"], 40)

    def test_deep_runs_a_fixup_pass_on_flagged_problems(self):
        p = self.env.write("report.md", big_report())

        def responder(req):
            fixing = "Automatic checks found these problems" in req["messages"][-1]["content"]
            return digest_answer(doc_lines(req), bad_note=not fixing)

        self.env.fake.responder = responder
        est = self.svc.local_estimate("digest", {"paths": [str(p)]})
        out = self.svc.local_run(est["estimate_id"], option="deep_now", user_approved=True, wait_s=20)
        self.assertEqual(out["status"], "done", out)
        reqs = chats(self.env)
        self.assertEqual(reqs[-2]["think"], "medium")
        self.assertEqual(reqs[-1]["think"], "low")                     # the fix-up pass reasons briefly
        self.assertEqual(reqs[-1]["messages"][-2]["role"], "assistant")
        self.assertIn("is not in the text", reqs[-1]["messages"][-1]["content"])
        res = out["result"]
        self.assertEqual(res["quality"]["fixups"][0]["kept"], "fixed version")
        self.assertEqual(res["citation_check"]["unverified_quotes"], [])
        self.assertGreater(out["timing"]["cached_in"], 0)              # the fix-up reused the cached prompt

    def test_reasoning_loop_falls_back_to_a_direct_answer(self):
        p = self.env.write("report.md", big_report())
        self.env.fake.responder = digest_responder
        self.env.fake.loop_thinking = True
        est = self.svc.local_estimate("digest", {"paths": [str(p)]})
        out = self.svc.local_run(est["estimate_id"], option="careful_now", user_approved=True, wait_s=20)
        self.assertEqual(out["status"], "done", out)
        self.assertIn("went in circles", out["result"]["quality"]["answered_without_reasoning"][0])
        self.assertEqual(chats(self.env)[-1]["think"], "none")

    def test_reasoning_budget_is_enforced(self):
        modes = json.loads(json.dumps(config.load()["modes"]))
        modes["careful"]["think_budget"] = 10
        set_config(self.env, modes=modes)
        p = self.env.write("report.md", big_report())
        self.env.fake.responder = digest_responder
        est = self.svc.local_estimate("digest", {"paths": [str(p)]})
        out = self.svc.local_run(est["estimate_id"], option="careful_now", user_approved=True, wait_s=20)
        self.assertEqual(out["status"], "done", out)
        self.assertIn("budget used up", out["result"]["quality"]["answered_without_reasoning"][0])

    def test_test_logs_script_result_first(self):
        log = self.env.write("build.log", GRADLE_LOG + "\n" + PYTEST_LOG)
        est = self.svc.local_estimate("test_log_triage", {"paths": [str(log)]})
        self.assertEqual(est["action"], "script", est)
        facts = est["script_result"]["facts"]
        self.assertEqual(facts["totals"]["failed"], 3)
        self.assertIn(":app:testDebugUnitTest", facts["tests_not_executed_this_run"])
        out = self.svc.local_run(est["estimate_id"], option="script")
        self.assertEqual(out["result"]["facts"]["totals"]["failed"], 3)

    def test_budget_watchdog(self):
        self.env.fake.time_scale = 1.0
        self.env.fake.decode_tps = 10
        self.env.fake.responder = lambda req: "word " * 200
        est = self.svc.local_estimate("freeform", {"instruction": "say a lot", "max_words": 400})
        t0 = time.time()
        out = self.svc.local_run(est["estimate_id"], option="fast_now", user_approved=True, max_minutes=0.03, wait_s=20)
        self.assertEqual(out["status"], "aborted", out)
        self.assertIn("time budget", out["error"])
        self.assertLess(time.time() - t0, 10)

    def test_cancel(self):
        self.env.fake.time_scale = 1.0
        self.env.fake.decode_tps = 5
        self.env.fake.responder = lambda req: "word " * 200
        est = self.svc.local_estimate("freeform", {"instruction": "write", "max_words": 400})
        out = self.svc.local_run(est["estimate_id"], option="fast_now", user_approved=True, wait_s=0)
        self.assertIn(out["status"], ("queued", "running"))
        self.svc.local_admin("cancel", job_id=out["job_id"])
        final = self.svc.local_result(out["job_id"], wait_s=10)
        self.assertEqual(final["status"], "cancelled", final)

    def test_translate_stays_in_a_local_file(self):
        p = self.env.write("lettre.txt", "Bonjour,\n\nLe rendez-vous est confirmé pour le 3 novembre 2026.\n")
        self.env.fake.responder = lambda req: "Hello,\n\nThe appointment is confirmed for November 3, 2026."
        est = self.svc.local_estimate("translate", {"paths": [str(p)], "target": "en"}, prefer_local=True)
        self.assertNotEqual(est["recommended"], "claude")
        self.assertEqual(est["options"][-1]["id"], "claude")
        self.assertIn("sends the text to Claude", est["options"][-1]["description"])
        out = self.svc.local_run(est["estimate_id"], option="fast_now", user_approved=True, wait_s=15)
        self.assertEqual(out["status"], "done", out)
        saved = Path(out["result"]["saved_to"])
        self.assertIn("November 3", saved.read_text(encoding="utf-8"))
        self.assertNotIn("November", json.dumps(out["result"]))

    def test_context_pack_returns_verbatim_lines(self):
        self.env.write("docs/a.md", "\n".join(f"line {i} about rest timers" for i in range(1, 60)))
        self.env.write("docs/b.md", "\n".join(["intro"] * 10 + ["The theme is dictated by the app.",
                                                                 "Users cannot switch theme on their own."] + ["x"] * 10))

        def pick(req):
            m = re.search(r"\[C(\d+)\] (\S+b\.md) lines (\d+)-(\d+)", req["messages"][1]["content"])
            return {"picks": [{"chunk": f"C{m.group(1)}", "start": 11, "end": 12, "why": "theme rule"}]}

        self.env.fake.responder = pick
        est = self.svc.local_estimate("context_pack", {"question": "Who decides the screen theme?",
                                                       "paths": [str(self.env.work / "docs")]})
        self.assertTrue(est["script_result"]["passages"])
        out = self.svc.local_run(est["estimate_id"], option="fast_now", user_approved=True, wait_s=15)
        self.assertEqual(out["status"], "done", out)
        p = out["result"]["passages"][0]
        self.assertEqual(p["text"], "The theme is dictated by the app.\nUsers cannot switch theme on their own.")
        self.assertEqual((p["start"], p["end"]), (11, 12))

    def test_rules_check_script_finds_forbidden_terms(self):
        packs_dir = self.env.tmp / "home" / "packs"
        packs_dir.mkdir(exist_ok=True)
        (packs_dir / "jobtest.md").write_text("# rules\n```rules\nforbid: OldProductName\nforbid_near: Languages | Esperanto\n"
                                              "require_near: French | B2\nforbid@website: FormerEmployer\n```\nBe concise.\n")
        draft = self.env.write("cv.txt", "Worked with OldProductName data.\nLanguages: English, French (C1), Esperanto\nFormerEmployer 2023\n")
        est = self.svc.local_estimate("rules_check", {"paths": [str(draft)], "packs": ["jobtest"], "target": "cv"})
        hits = est["script_result"]["script_findings"]["D1"]["script_findings"]
        self.assertEqual(sorted(h["rule"] for h in hits),
                         ["forbid: OldProductName", "forbid_near: Languages | Esperanto", "require_near: French | B2"])

    def test_tiny_question_is_done_here(self):
        est = self.svc.local_estimate("freeform", {"instruction": "Define 'hardware' in one sentence.", "max_words": 30})
        self.assertEqual(est["action"], "do_here", est)

    def test_low_memory_now_still_offers_tonight(self):
        import local_delegate.sysinfo as si
        p = self.env.write("report.md", big_report())
        set_config(self.env, night_window=future_night_window())   # tonight's window is hours away
        si.memory = lambda: {"total": 34e9, "available": 6e9}   # e.g. Android emulator + Studio open
        est = self.svc.local_estimate("digest", {"paths": [str(p)]})
        why = {x["id"]: x["why"] for x in est["not_possible"]}
        self.assertIn("GB free now", why["fast_now"])
        self.assertTrue(est["recommended"].endswith("_tonight"), est)
        self.assertEqual(est["action"], "ask")


class GitTasks(unittest.TestCase):
    def setUp(self):
        self.env = Env(config_extra={"protected_paths": ["backend/migrations/**"],
                                     "areas": {"app/src": "Android app", "backend/migrations": "SQL migrations"}})
        fake_profile(self.env)
        self.svc = server.Service()
        self.repo = self.env.work / "repo"
        self.repo.mkdir()

        def git(*a):
            subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (self.repo / "README.md").write_text("hi\n")
        git("add", ".")
        git("commit", "-qm", "init")
        (self.repo / "app/src").mkdir(parents=True)
        (self.repo / "app/src/Plan.kt").write_text("fun theme() = \"light\"\n")
        (self.repo / "backend/migrations").mkdir(parents=True)
        (self.repo / "backend/migrations/001.sql").write_text("alter table plan add column theme text;\n")
        (self.repo / "local.properties").write_text("sdk.dir=C:/Android\napi_key=abcd1234efgh5678\n")
        git("add", ".")
        git("commit", "-qm", "theme")

    def tearDown(self):
        self.env.close()

    def test_diff_summary_facts(self):
        est = self.svc.local_estimate("diff_summary", {"repo": str(self.repo), "rev": "HEAD"}, include_script_result=True)
        facts = est["script_result"]["facts"]
        paths = {f["path"]: f for f in facts["files"]}
        self.assertEqual(set(paths), {"app/src/Plan.kt", "backend/migrations/001.sql", "local.properties"})
        self.assertEqual(facts["protected_hits"], ["backend/migrations/001.sql"])
        self.assertEqual(paths["app/src/Plan.kt"]["area"], "Android app")
        self.assertIn("local.properties", facts["not_shown_to_model"])
        self.env.fake.responder = lambda req: {"areas": [], "risks": [], "questions": []}
        out = self.svc.local_run(est["estimate_id"], option="fast_now", user_approved=True, wait_s=15)
        self.assertEqual(out["status"], "done", out)
        chat = chats(self.env)[-1]
        self.assertNotIn("abcd1234", chat["messages"][-1]["content"])
        self.assertIn("alter table plan", chat["messages"][-1]["content"])

    def test_filelist_compare_against_commit(self):
        report = self.env.write("report.md", "Changed `app/src/Plan.kt` and `docs/NOTES.md`.\n")
        est = self.svc.local_estimate("filelist_compare", {"sources": [
            {"label": "report", "path": str(report)}, {"label": "commit", "git": "HEAD", "repo": str(self.repo)}]})
        self.assertEqual(est["action"], "script")
        pair = est["script_result"]["pairs"][0]
        self.assertEqual(pair["only_in_a"], ["docs/NOTES.md"])
        self.assertEqual(sorted(pair["only_in_b"]), ["backend/migrations/001.sql", "local.properties"])

    def test_coordinator_log_since_keeps_line_numbers(self):
        log = self.env.write("COORDINATOR_LOG.md", "# Log\n## 2026-10-01\nold decision\n## 2026-10-20\n"
                                                   "Decision: theme dictated by app (approved by Dana)\n")
        seen = {}

        def resp(req):
            seen["user"] = req["messages"][-1]["content"]
            return {"status": None, "files": [], "tests": [], "open_items": [], "approval_lines": [
                {"doc": "D1", "line": 5, "quote": "theme dictated by app"}], "decisions": [], "notes": []}

        self.env.fake.responder = resp
        out = self.svc.local_run(task="digest", params={"paths": [str(log)], "template": "coordinator_log",
                                                         "since": "2026-10-15"}, option="fast_now",
                                 user_approved=True, wait_s=15)
        self.assertEqual(out["status"], "done", out)
        self.assertNotIn("old decision", seen["user"])
        self.assertIn("D1:5| Decision: theme dictated", seen["user"])
        self.assertEqual(out["result"]["citation_check"]["confirmed"], 1)


class Calibration(unittest.TestCase):
    def test_calibration_measures_the_fake(self):
        env = Env(prefill_tps=40, decode_tps=4, load_s=20, reload_s=8, time_scale=0.0003)
        try:
            summary = calibrate.Calibrator(config.load(), budget_s=1000).run()
            self.assertTrue(summary["calibrated"])
            prof = calibrate.load_profile()["models"][env.fake.model]
            self.assertAlmostEqual(prof["prefill"]["tps_4k"], 40, delta=4)
            self.assertAlmostEqual(prof["decode"]["tps_short"], 4, delta=0.4)
            self.assertAlmostEqual(prof["memory"]["kv_bytes_per_token"], 65536, delta=2000)
            self.assertAlmostEqual(prof["load_s"], 20, delta=1)
            self.assertGreater(prof["prefix_cache_ratio"], 0.8)
            demo = prof["think"]["one_line_question"]
            self.assertGreater(demo["with_default_reasoning"]["output_tokens"], demo["with_reasoning_off"]["output_tokens"])
            est = server.Service().local_estimate("digest", {"paths": [str(env.write("r.md", big_report()))]})
            self.assertNotIn("not calibrated", json.dumps(est.get("notes")))
        finally:
            env.close()


class Admin(unittest.TestCase):
    def test_setup_asks_first_then_writes_server_json(self):
        env = Env(cloud_log=None)
        try:
            svc = server.Service()
            sj = env.tmp / "ollama" / "server.json"
            sj.write_text(json.dumps({"other_setting": 1}))
            plan = svc.local_admin("setup_local_only")
            self.assertTrue(plan["needs_approval"])
            self.assertEqual(json.loads(sj.read_text()), {"other_setting": 1})
            done = svc.local_admin("setup_local_only", user_approved=True)
            self.assertEqual(json.loads(sj.read_text()), {"other_setting": 1, "disable_ollama_cloud": True})
            by = {c["name"]: c["ok"] for c in done["local_only_checks"]["checks"]}
            self.assertTrue(by["cloud_disabled_config"])
            self.assertTrue(svc.local_admin("calibrate")["needs_approval"])
            self.assertTrue(svc.local_admin("allow_root", path="D:/data")["needs_approval"])
            self.assertIn("cloud model", svc.local_admin("set_model", model="gpt-oss:120b-cloud", user_approved=True)["error"])
        finally:
            env.close()

    def test_calibrate_runs_in_the_worker(self):
        env = Env(time_scale=0.0002)
        try:
            svc = server.Service()
            out = svc.local_admin("calibrate", user_approved=True, budget_minutes=20)
            res = svc.local_result(out["job_id"], wait_s=40)
            self.assertEqual(res["status"], "done", res)
            self.assertTrue(res["result"]["calibrated"])
            st = svc.local_status()
            self.assertTrue(st["ready"], st)
            self.assertEqual(st["reasoning_levels"]["used"], {"fast": "none", "careful": "low", "deep": "medium"})
        finally:
            env.close()

    def test_priority_and_night_window(self):
        env = Env()
        try:
            svc = server.Service()
            out = svc.local_admin("set_priority", priority="saver")
            self.assertEqual((out["active"], out["default"]), ("saver", "saver"))
            out = svc.local_admin("set_priority", priority="rush", until="23:59")
            self.assertEqual(out["active"], "rush")
            self.assertIn("until", out["why"])
            out = svc.local_admin("set_priority", schedule_rules=[{"from": "00:00", "to": "23:59", "priority": "night"}],
                                  priority="balanced")
            self.assertEqual(out["active"], "night")      # the schedule beats the default
            self.assertIn("error", svc.local_admin("set_priority", priority="turbo"))
            out = svc.local_admin("night_window", start="22:30", end="06:30")
            self.assertEqual(out["night_window"], {"start": "22:30", "end": "06:30"})
            self.assertIn("error", svc.local_admin("night_window", start="25:00"))
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
