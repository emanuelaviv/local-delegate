"""The background worker: scheduled starts, keeping the computer awake, deadlines with saved
parts, continuing a stopped job, a quick job overtaking a long one, idle starts, and a real
detached worker process."""

import json
import os
import re
import time
import unittest
from datetime import datetime, timedelta

from helpers import Env, fake_profile, set_config
from local_delegate import jobs, server


def chats(env):
    return [r for r in env.fake.requests if r["path"] == "/api/chat"]


def letter(paragraphs=4, words=120):
    return "\n\n".join(f"Paragraphe {p}: " + " ".join(f"mot{p}_{i}" for i in range(words)) for p in range(paragraphs))


def echo_translation(req):
    src = req["messages"][-1]["content"]
    return src.replace("Paragraphe", "Paragraph")


def at(seconds):
    return "at:" + (datetime.now() + timedelta(seconds=seconds)).strftime("%H:%M:%S")


class Worker(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        fake_profile(self.env)
        self.svc = server.Service()

    def tearDown(self):
        self.env.close()

    def realtime(self, decode_tps):
        f = self.env.fake
        f.time_scale, f.prefill_tps, f.decode_tps, f.load_s, f.reload_s = 1.0, 100000, decode_tps, 0.05, 0.05

    def translate_job(self, chunks=4, **run):
        src = self.env.write("lettre.txt", "\n\n".join(f"Partie {c}. " + letter(1, 700) for c in range(chunks)))
        self.env.fake.responder = echo_translation
        est = self.svc.local_estimate("translate", {"paths": [str(src)], "target": "en"}, prefer_local=True)
        return self.svc.local_run(est["estimate_id"], user_approved=True, **run)

    def test_scheduled_start_keeps_the_computer_awake_then_lets_it_sleep(self):
        est = self.svc.local_estimate("freeform", {"instruction": "say hi", "max_words": 20})
        out = self.svc.local_run(est["estimate_id"], option="fast_now", when=at(2), user_approved=True)
        self.assertEqual(out["status"], "scheduled", out)
        self.assertIn("starts_at", out)
        time.sleep(0.6)
        self.assertIn(True, self.env.awake_calls)            # stays awake while a job waits
        self.assertEqual(len(chats(self.env)), 0)
        done = self.svc.local_result(out["job_id"], wait_s=15)
        self.assertEqual(done["status"], "done", done)
        time.sleep(0.8)
        self.assertEqual(self.env.awake_calls[-1], False)    # queue empty: sleep allowed again

    def test_tonight_inside_the_window_starts_now_with_the_window_as_deadline(self):
        t = time.localtime()
        set_config(self.env, night_window={"start": f"{(t.tm_hour - 1) % 24:02d}:00",
                                           "end": f"{(t.tm_hour + 2) % 24:02d}:00"})
        out = self.translate_job(chunks=2, option="deep_tonight")
        self.assertIn(out["status"], ("queued", "running", "done"), out)
        job = jobs.read(out["job_id"])
        self.assertIsNotNone(job["deadline"])
        done = self.svc.local_result(out["job_id"], wait_s=20)
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(chats(self.env)[0]["think"], "medium")
        self.assertEqual(chats(self.env)[0]["options"]["num_predict"] > 16000, True)  # bigger night budget

    def test_deadline_keeps_finished_parts_and_the_job_can_continue(self):
        self.realtime(decode_tps=2000)       # about a second per part
        out = self.translate_job(chunks=4, option="fast_now", max_minutes=0.025, wait_s=20)
        self.assertEqual(out["status"], "partial", out)
        self.assertIn("resumable", out)
        m = re.search(r"returned the first (\d+) of 4 parts", out["error"])
        self.assertIsNotNone(m, out["error"])
        kept = int(m.group(1))
        self.assertTrue(1 <= kept < 4, kept)
        text = open(out["result"]["saved_to"], encoding="utf-8").read()
        self.assertEqual(text.count("Paragraph 0:"), kept)   # the saved text holds just the finished parts
        first = len(chats(self.env))
        # A part still running when the budget runs out is stopped, then redone when the job
        # continues. Whether one is cut off like that depends on the machine's speed.
        self.assertIn(first - kept, (0, 1), (first, kept))
        again = self.svc.local_run(job_id=out["job_id"], when="now", user_approved=True)
        done = self.svc.local_result(again["job_id"], wait_s=30)
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(len(chats(self.env)) - first, 4 - kept)   # finished parts were not redone
        self.assertEqual(open(done["result"]["saved_to"], encoding="utf-8").read().count("Paragraph"), 4)

    def test_a_now_job_overtakes_an_overnight_job_between_parts(self):
        t = time.localtime()
        set_config(self.env, night_window={"start": f"{(t.tm_hour - 1) % 24:02d}:00",
                                           "end": f"{(t.tm_hour + 2) % 24:02d}:00"})
        self.realtime(decode_tps=1500)
        night = self.translate_job(chunks=4, option="fast_tonight")
        deadline = time.time() + 10
        while len(chats(self.env)) < 1 and time.time() < deadline:
            time.sleep(0.05)
        est = self.svc.local_estimate("freeform", {"instruction": "QUICK one", "max_words": 20})
        quick = self.svc.local_run(est["estimate_id"], option="fast_now", user_approved=True, wait_s=20)
        self.assertEqual(quick["status"], "done", quick)
        night_done = self.svc.local_result(night["job_id"], wait_s=30)
        self.assertEqual(night_done["status"], "done", night_done)
        order = ["QUICK" in r["messages"][-1]["content"] for r in chats(self.env)]
        self.assertIn(True, order[1:4], order)               # ran before the last overnight part

    def test_idle_start_waits_for_the_user_to_step_away(self):
        self.env.idle_s = 5.0
        est = self.svc.local_estimate("freeform", {"instruction": "say hi", "max_words": 20})
        out = self.svc.local_run(est["estimate_id"], option="fast_now", when="idle", user_approved=True)
        time.sleep(1.0)
        job = jobs.read(out["job_id"])
        self.assertIn("idle", job["phase"])
        self.assertEqual(len(chats(self.env)), 0)
        self.env.idle_s = 3600.0
        done = self.svc.local_result(out["job_id"], wait_s=15)
        self.assertEqual(done["status"], "done", done)

    def test_cancel_a_scheduled_job(self):
        est = self.svc.local_estimate("freeform", {"instruction": "say hi", "max_words": 20})
        out = self.svc.local_run(est["estimate_id"], option="fast_tonight", user_approved=True)
        self.assertIn(out["status"], ("scheduled", "queued"))
        c = self.svc.local_admin("cancel", job_id=out["job_id"])
        self.assertEqual(c["status"], "cancelled", c)
        q = self.svc.local_admin("jobs")
        self.assertEqual(q["open_jobs"], 0)


class DetachedWorker(unittest.TestCase):
    """A real worker process, started the way the connector starts it."""

    def test_worker_process_runs_a_job_and_exits_when_idle(self):
        os.environ.pop("LOCAL_DELEGATE_INPROCESS_WORKER", None)
        os.environ["LOCAL_DELEGATE_WORKER_IDLE_S"] = "1.5"
        env = Env(config_extra={"ram_check": False})
        os.environ.pop("LOCAL_DELEGATE_INPROCESS_WORKER", None)
        try:
            fake_profile(env)
            svc = server.Service()
            est = svc.local_estimate("freeform", {"instruction": "say hi", "max_words": 20})
            out = svc.local_run(est["estimate_id"], option="fast_now", user_approved=True, wait_s=20)
            self.assertEqual(out["status"], "done", out)
            info = json.loads((env.tmp / "home" / "worker.json").read_text()) if (env.tmp / "home" / "worker.json").exists() else {}
            self.assertNotEqual(info.get("pid"), os.getpid())
            deadline = time.time() + 15
            while jobs.worker_alive() and time.time() < deadline:
                time.sleep(0.2)
            self.assertFalse(jobs.worker_alive())             # exited once the queue was empty
        finally:
            env.close()
            os.environ["LOCAL_DELEGATE_INPROCESS_WORKER"] = "1"
            os.environ.pop("LOCAL_DELEGATE_WORKER_IDLE_S", None)


if __name__ == "__main__":
    unittest.main()
