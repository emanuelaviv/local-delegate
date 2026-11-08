from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))
sys.path.insert(0, str(HERE))

from fake_ollama import FakeOllama  # noqa: E402
from local_delegate import config, guard, jobs, sysinfo  # noqa: E402

_REAL_MEMORY = sysinfo.memory
_REAL_KEEP_AWAKE = sysinfo.keep_awake
_REAL_IDLE = sysinfo.idle_seconds
_REAL_USER_ENV = sysinfo.user_env
_REAL_ENV_MTIME = guard._env_key_mtime


class Env:
    """Temporary data dir, Ollama home/log and a fake Ollama server."""

    def __init__(self, cloud_log: str | None = "true", config_extra: dict | None = None,
                 ram_free_gb: float = 26.0, **fake_kw):
        # Pretend to be a 32 GB machine with the model not yet loaded.
        sysinfo.memory = lambda: {"total": int(32e9), "available": int(ram_free_gb * 1e9)}
        # Ignore this machine's real user environment (on Windows, the registry).
        self.user_env: dict = {}
        sysinfo.user_env = lambda name: self.user_env.get(name)
        guard._env_key_mtime = lambda: None
        self.tmp = Path(tempfile.mkdtemp(prefix="ldtest-"))
        (self.tmp / "home").mkdir()
        (self.tmp / "ollama").mkdir()
        (self.tmp / "work").mkdir()
        self.old_env = {k: os.environ.get(k) for k in ("LOCAL_DELEGATE_HOME", "LOCAL_DELEGATE_OLLAMA_HOME",
                                                         "LOCAL_DELEGATE_OLLAMA_LOG")}
        os.environ["LOCAL_DELEGATE_HOME"] = str(self.tmp / "home")
        os.environ.setdefault("LOCAL_DELEGATE_INPROCESS_WORKER", "1")
        self.awake_calls: list = []
        sysinfo.keep_awake = lambda on: self.awake_calls.append(on) or False
        sysinfo.idle_seconds = lambda: self.idle_s
        self.idle_s = None
        os.environ["LOCAL_DELEGATE_OLLAMA_HOME"] = str(self.tmp / "ollama")
        log = self.tmp / "ollama" / "server.log"
        lines = ["time=2026-10-22T08:00:00 level=INFO source=routes.go msg=\"server config\" env=\"map[]\""]
        if cloud_log is not None:
            lines.append(f"time=2026-10-22T08:00:01 level=INFO msg=\"Ollama cloud disabled: {cloud_log}\"")
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ["LOCAL_DELEGATE_OLLAMA_LOG"] = str(log)
        self.fake = FakeOllama(**fake_kw)
        cfg = {"ollama_url": self.fake.url, "allowed_roots": [str(self.tmp)], "model": self.fake.model}
        cfg.update(config_extra or {})
        (self.tmp / "home" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        config.load(refresh=True)
        guard.clear_cache()

    @property
    def work(self) -> Path:
        return self.tmp / "work"

    def write(self, name: str, text: str) -> Path:
        p = self.work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def close(self) -> None:
        jobs.stop_inprocess_worker()
        sysinfo.keep_awake = _REAL_KEEP_AWAKE
        sysinfo.idle_seconds = _REAL_IDLE
        sysinfo.memory = _REAL_MEMORY
        sysinfo.user_env = _REAL_USER_ENV
        guard._env_key_mtime = _REAL_ENV_MTIME
        self.fake.close()
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.load(refresh=True)
        guard.clear_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)


SAMPLE_REPORT = """# Agent report: screen inventory sync (task T-142)

Status: DONE, waiting for Dana's approval before merge.

## Files
- Changed `app/src/main/java/app/example/inventory/InventoryRepository.kt` to read the new CSV columns.
- Added `backend/migrations/20261020_inventory_theme.sql` with the theme column.
- Updated `docs/NEXT_STEPS.md`.

## Tests
- `./gradlew testDebugUnitTest`: 142 passed, 0 failed.
- `deno test backend/functions`: 18 passed.

## Open items
- TODO: confirm French labels for Dark theme.
- The emulator run was skipped because the AVD would not start.

APPROVAL NEEDED: Dana to approve the migration before it is applied to the remote database.
"""

PYTEST_LOG = """============================= test session starts ==============================
collected 13 items

tests/test_engine.py ..F.......s.F                                        [100%]

=================================== FAILURES ===================================
_____________________________ test_theme_calibration _____________________________

    def test_theme_calibration():
        plan = build_plan(level=2)
>       assert plan.theme == "light"
E       AssertionError: assert 'dark' == 'light'

tests/test_engine.py:41: AssertionError
=========================== short test summary info ============================
FAILED tests/test_engine.py::test_theme_calibration - AssertionError: assert 'dark' == 'light'
FAILED tests/test_engine.py::test_rest_timer - KeyError: 'rest'
=================== 2 failed, 10 passed, 1 skipped in 3.21s ====================
"""

GRADLE_LOG = """> Task :app:compileDebugKotlin UP-TO-DATE
> Task :app:testDebugUnitTest FROM-CACHE
> Task :core:testDebugUnitTest

com.example.core.PlanBuilderTest > buildsSummary FAILED
    java.lang.AssertionError: expected:<3> but was:<2>
        at org.junit.Assert.fail(Assert.java:89)
        at com.example.core.PlanBuilderTest.buildsSummary(PlanBuilderTest.kt:57)

5 tests completed, 1 failed

FAILURE: Build failed with an exception.

BUILD FAILED in 12s
21 actionable tasks: 4 executed, 15 up-to-date, 2 from cache
"""


def fake_profile(env: "Env", prefill_tps: float = 300.0, decode_tps: float = 4.2, load_s: float = 38.0,
                 reload_s: float = 22.0) -> None:
    """A calibration profile like the reference machine (RTX 5060 + 32 GB): fast reading, slow writing."""
    from local_delegate import calibrate

    calibrate.save_profile({"token_ratio": {"prose": 3.7, "code": 3.2, "fr": 3.4, "he": 2.3},
                            "models": {env.fake.model: {
                                "load_s": load_s, "reload_s": reload_s,
                                "prefill": {"a": 1 / prefill_tps, "b": 0.0, "tps_4k": prefill_tps},
                                "decode": {"g": 1 / decode_tps, "h": 0.0, "tps_short": decode_tps},
                                "memory": {"base_bytes": 18.6e9, "kv_bytes_per_token": 66000, "vram_fraction": 0.29},
                                "prefix_cache_ratio": 0.92, "think": {"off_value": "none"},
                                "limits": {"model_max_ctx": 262144, "default_ctx": 16384}}}})


def set_config(env: "Env", **kv) -> None:
    p = env.tmp / "home" / "config.json"
    cfg = json.loads(p.read_text())
    cfg.update(kv)
    p.write_text(json.dumps(cfg))
    config.load(refresh=True)


def future_night_window(hours_ahead: int = 3, hours_long: int = 8) -> dict:
    """A night window that opens a few hours from now, so a test doesn't depend on the time of day."""
    from datetime import datetime, timedelta
    start = datetime.now() + timedelta(hours=hours_ahead)
    end = start + timedelta(hours=hours_long)
    return {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
