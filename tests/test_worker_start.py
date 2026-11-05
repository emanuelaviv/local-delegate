"""How the worker is started on Windows (simulated: which ways are tried, in what order), the job
object layout it reads, whether it outlives Claude's app, and handing over to a newer version."""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import unittest

from helpers import Env, fake_profile
from local_delegate import config, jobs, server, sysinfo

BREAKAWAY = 0x01000000


class FakePopen:
    def __init__(self, refuse_breakaway=False, fail_all=False):
        self.calls = []
        self.refuse_breakaway = refuse_breakaway
        self.fail_all = fail_all

    def __call__(self, cmd, **kw):
        self.calls.append({"cmd": cmd, "flags": kw.get("creationflags", 0)})
        if self.fail_all or (self.refuse_breakaway and kw.get("creationflags", 0) & BREAKAWAY):
            raise PermissionError(5, "Access is denied")
        return object()


class SpawnOnWindows(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.saved = (config.IS_WINDOWS, sysinfo.job_object, sysinfo.run, subprocess.Popen)
        config.IS_WINDOWS = True
        self.wmi_calls = []
        self.wmi_result = (0, "\r\n0\r\n", "")

        def fake_run(cmd, timeout=10.0):
            self.wmi_calls.append(cmd)
            return self.wmi_result
        sysinfo.run = fake_run

    def tearDown(self):
        config.IS_WINDOWS, sysinfo.job_object, sysinfo.run, subprocess.Popen = self.saved
        self.env.close()

    def spawn(self, job, popen):
        sysinfo.job_object = lambda: job
        subprocess.Popen = popen
        return jobs.spawn_worker()

    def test_outside_any_job_breakaway_is_requested_anyway(self):
        p = FakePopen()
        self.assertEqual(self.spawn({"in_job": False}, p), "detached")
        self.assertTrue(p.calls[0]["flags"] & BREAKAWAY)
        self.assertEqual(p.calls[0]["cmd"][-1], "launch-worker")

    def test_detection_failing_does_not_matter(self):
        p = FakePopen()
        self.assertEqual(self.spawn(None, p), "detached")
        self.assertTrue(p.calls[0]["flags"] & BREAKAWAY)

    def test_in_a_job_that_allows_leaving(self):
        p = FakePopen()
        job = {"in_job": True, "kill_on_close": True, "breakaway_ok": True, "silent_breakaway": True}
        self.assertEqual(self.spawn(job, p), "breakaway")
        self.assertEqual(len(p.calls), 1)

    def test_job_refuses_breakaway_but_lets_children_go_silently(self):
        p = FakePopen(refuse_breakaway=True)
        job = {"in_job": True, "kill_on_close": True, "breakaway_ok": False, "silent_breakaway": True}
        self.assertEqual(self.spawn(job, p), "detached")
        self.assertEqual(len(p.calls), 2)
        self.assertFalse(p.calls[1]["flags"] & BREAKAWAY)
        self.assertEqual(self.wmi_calls, [])

    def test_job_refuses_both_so_windows_creates_it(self):
        p = FakePopen(refuse_breakaway=True)
        job = {"in_job": True, "kill_on_close": True, "breakaway_ok": False, "silent_breakaway": False}
        self.assertEqual(self.spawn(job, p), "wmi")
        self.assertEqual(len(p.calls), 1)                      # no plain start inside the job
        self.assertIn("Win32_Process", self.wmi_calls[0][-1])
        self.assertIn("worker", self.wmi_calls[0][-1])

    def test_unknown_job_refusing_breakaway_goes_to_wmi(self):
        p = FakePopen(refuse_breakaway=True)
        self.assertEqual(self.spawn(None, p), "wmi")

    def test_wmi_error_codes_are_not_success(self):
        p = FakePopen(refuse_breakaway=True)
        self.wmi_result = (0, "10\r\n", "")       # not a real code, but must not read as 0
        job = {"in_job": True, "kill_on_close": True, "breakaway_ok": False, "silent_breakaway": False}
        self.assertEqual(self.spawn(job, p), "child")
        self.assertFalse(p.calls[-1]["flags"] & BREAKAWAY)


class FakeFn:
    def __init__(self, impl):
        self.impl, self.argtypes, self.restype = impl, None, None

    def __call__(self, *a):
        return self.impl(*a)


class FakeKernel32:
    """Stands in for kernel32: checks that the pseudo-handle of the current process arrives intact
    (the 0.2.0 bug cut it to 32 bits) and fills in what Windows would."""
    PSEUDO = 0xFFFFFFFFFFFFFFFF

    def __init__(self, in_job=False, flags=0, fail=False):
        def is_in_job(proc, job, out):
            if fail or proc != self.PSEUDO or job is not None:
                return 0
            out._obj.value = 1 if in_job else 0
            return 1

        def query(job, cls, info, size, ret):
            if job is not None or cls != 9 or size != ctypes.sizeof(sysinfo._ExtendedLimits):
                return 0
            info._obj.BasicLimitInformation.LimitFlags = flags
            return 1
        self.GetCurrentProcess = FakeFn(lambda: self.PSEUDO)
        self.IsProcessInJob = FakeFn(is_in_job)
        self.QueryInformationJobObject = FakeFn(query)
        self.OpenProcess = FakeFn(lambda *a: None)
        self.GetExitCodeProcess = FakeFn(lambda *a: 0)
        self.CloseHandle = FakeFn(lambda *a: 1)


class on_windows:
    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        self.saved = (sysinfo.IS_WINDOWS, list(sysinfo._K32), getattr(ctypes, "WinDLL", None))
        sysinfo.IS_WINDOWS = True
        sysinfo._K32.clear()
        ctypes.WinDLL = lambda name, use_last_error=False: self.fake

    def __exit__(self, *exc):
        sysinfo.IS_WINDOWS = self.saved[0]
        sysinfo._K32[:] = self.saved[1]
        if self.saved[2] is None:
            del ctypes.WinDLL
        else:
            ctypes.WinDLL = self.saved[2]


class JobObjectFacts(unittest.TestCase):
    def test_job_object_through_kernel32(self):
        fake = FakeKernel32(in_job=True, flags=0x2000 | 0x1000)
        with on_windows(fake):
            self.assertEqual(sysinfo.job_object(), {"in_job": True, "kill_on_close": True, "breakaway_ok": False,
                                                    "silent_breakaway": True})
            self.assertTrue(sysinfo.in_job_object())
        self.assertIs(fake.GetCurrentProcess.restype, ctypes.c_void_p)    # handles declared pointer-sized
        self.assertIs(fake.IsProcessInJob.argtypes[0], ctypes.c_void_p)
        with on_windows(FakeKernel32(in_job=False)):
            self.assertEqual(sysinfo.job_object(), {"in_job": False})
            self.assertFalse(sysinfo.in_job_object())
        with on_windows(FakeKernel32(fail=True)):
            self.assertIsNone(sysinfo.job_object())
            self.assertFalse(sysinfo.pid_alive(1234))

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit layout")
    def test_layout_matches_the_windows_sdk(self):
        self.assertEqual(ctypes.sizeof(sysinfo._ExtendedLimits), 144)
        self.assertEqual(sysinfo._BasicLimits.LimitFlags.offset, 16)
        self.assertEqual(sysinfo._ExtendedLimits.IoInfo.offset, 64)

    def test_flag_bits(self):
        self.assertEqual(sysinfo.job_flags(0x2000 | 0x0800),
                         {"kill_on_close": True, "breakaway_ok": True, "silent_breakaway": False})
        self.assertEqual(sysinfo.job_flags(0x1000),
                         {"kill_on_close": False, "breakaway_ok": False, "silent_breakaway": True})

    def test_not_windows_reports_unknown(self):
        if not config.IS_WINDOWS:
            self.assertIsNone(sysinfo.job_object())
            self.assertIsNone(sysinfo.in_job_object())

    def test_outlives_claude_from_what_the_worker_reports(self):
        saved = config.IS_WINDOWS
        config.IS_WINDOWS = True
        try:
            o = jobs.outlives_claude
            self.assertTrue(o({"how": "breakaway", "job": {"in_job": False}}))
            self.assertFalse(o({"how": "child", "job": {"in_job": True, "kill_on_close": True}}))
            self.assertTrue(o({"how": "detached", "job": {"in_job": True, "kill_on_close": False}}))
            self.assertIsNone(o({"how": "detached", "job": {"in_job": True}}))
            self.assertIsNone(o({"how": "detached", "in_job_object": None}))   # a 0.2.0 worker
            self.assertFalse(o({"how": "in-process"}))
        finally:
            config.IS_WINDOWS = saved


class LateJob(jobs.Worker):
    """Finds nothing to do, leaves, and sees a job appear just as it lets go of the queue."""

    def __init__(self):
        super().__init__(idle_exit_s=0, poll_s=0.01, hand_over=True)
        self.looks = 0

    def tick(self):
        return False

    def _open_jobs(self):
        self.looks += 1
        return [] if self.looks == 1 else [{"job_id": "x", "status": "queued"}]


class Handover(unittest.TestCase):
    def test_a_job_arriving_as_the_worker_leaves_gets_a_fresh_worker(self):
        env = Env()
        saved = jobs.launch_successor
        started = []
        jobs.launch_successor = lambda note: started.append(note) or True
        try:
            self.assertTrue(LateJob().run_forever().startswith("handed over"))
            self.assertEqual(started[0]["server_dir"], str(config.SERVER_DIR))
        finally:
            jobs.launch_successor = saved
            env.close()

    def test_note_and_version_order(self):
        env = Env()
        try:
            server.Service()                              # notes its version on start
            note = json.loads((env.tmp / "home" / "server.json").read_text())
            self.assertEqual(note["version"], config.VERSION)
            self.assertIsNone(jobs.newer_code())          # same code: nothing to hand over to
            self.assertEqual(jobs.code_version(config.SERVER_DIR), config.VERSION)
            self.assertGreater(jobs._version_tuple("0.10.0"), jobs._version_tuple("0.9.9"))
        finally:
            env.close()

    def test_a_running_worker_hands_over_to_a_newer_install(self):
        os.environ.pop("LOCAL_DELEGATE_INPROCESS_WORKER", None)
        os.environ["LOCAL_DELEGATE_WORKER_IDLE_S"] = "2"
        os.environ["LOCAL_DELEGATE_VERSION_CHECK_S"] = "0.3"
        env = Env(config_extra={"ram_check": False})
        os.environ.pop("LOCAL_DELEGATE_INPROCESS_WORKER", None)
        newer = env.tmp / "newer" / "server"
        try:
            fake_profile(env)
            shutil.copytree(config.SERVER_DIR, newer, ignore=shutil.ignore_patterns("__pycache__"))
            init = newer / "local_delegate" / "__init__.py"
            init.write_text(init.read_text().replace(f'"{config.VERSION}"', '"9.9.9"'))
            info_path = env.tmp / "home" / "worker.json"
            jobs.ensure_worker()
            first = self._wait_for(info_path, lambda w: w.get("version") == config.VERSION)
            self.assertNotEqual(first["pid"], os.getpid())
            # the newer connector starts (as after a plugin update) and notes itself
            (env.tmp / "home" / "server.json").write_text(json.dumps(
                {"version": "9.9.9", "server_dir": str(newer), "python": sys.executable}))
            second = self._wait_for(info_path, lambda w: w.get("version") == "9.9.9")
            self.assertNotEqual(second["pid"], first["pid"])
            self.assertIn("job", second)
            end = time.time() + 15
            while jobs.worker_alive() and time.time() < end:  # the new worker idles out
                time.sleep(0.2)
            self.assertFalse(jobs.worker_alive())
        finally:
            env.close()
            os.environ["LOCAL_DELEGATE_INPROCESS_WORKER"] = "1"
            os.environ.pop("LOCAL_DELEGATE_WORKER_IDLE_S", None)
            os.environ.pop("LOCAL_DELEGATE_VERSION_CHECK_S", None)

    def _wait_for(self, path, ok, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            try:
                w = json.loads(path.read_text())
                if ok(w):
                    return w
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        self.fail(f"worker.json never matched: {path.read_text() if path.exists() else 'missing'}")


class WorkerCheck(unittest.TestCase):
    def test_check_starts_the_worker_without_a_job(self):
        os.environ.pop("LOCAL_DELEGATE_INPROCESS_WORKER", None)
        os.environ["LOCAL_DELEGATE_WORKER_IDLE_S"] = "1.5"
        env = Env(config_extra={"ram_check": False})
        os.environ.pop("LOCAL_DELEGATE_INPROCESS_WORKER", None)
        try:
            svc = server.Service()
            out = svc.local_admin("worker_check")
            self.assertTrue(out["running"], out)
            self.assertTrue(out["started_now"], out)
            self.assertIn("job", out)
            self.assertTrue(out["outlives_claude"], out)      # own session on this OS
            self.assertIn("keep running", out["verdict"])
            self.assertEqual(svc.local_admin("jobs")["open_jobs"], 0)
            end = time.time() + 15
            while jobs.worker_alive() and time.time() < end:
                time.sleep(0.2)
            self.assertFalse(jobs.worker_alive())
        finally:
            env.close()
            os.environ["LOCAL_DELEGATE_INPROCESS_WORKER"] = "1"
            os.environ.pop("LOCAL_DELEGATE_WORKER_IDLE_S", None)


if __name__ == "__main__":
    unittest.main()
