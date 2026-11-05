"""Jobs: a queue of files on disk, worked through by a separate worker process.

The connector Claude talks to only writes job files and reads them back. The worker is its own
process, started outside Claude's app, so a job keeps going if the app restarts or closes, and
can wait for tonight. The worker:
  * runs one model call at a time, quickest-needed first: a job wanted now overtakes an overnight
    job between two of its calls;
  * starts scheduled jobs at their time, or once nobody has touched the keyboard for a while;
  * stops each job at its deadline or approved time budget, keeping the parts already finished,
    so a long job can carry on another night;
  * keeps the computer from sleeping while jobs wait or run (on mains power only), and lets it
    sleep again once the queue is empty;
  * re-checks the local-only guarantees and free memory before starting each job;
  * exits a few minutes after the queue empties.

Files: <data dir>/jobs/<id>.json (status, progress, finished parts, result); <id>.cancel asks the
worker to stop a job; worker.lock is held by the running worker.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import calibrate, config, estimate, guard, logbook, schedule, sysinfo, textio
from .ollama import Ollama, OllamaError, think_off_value, think_value
from .tasks import TASKS, Prepared, TaskInputError, repair_messages
from .verify import validate

log = logging.getLogger("local_delegate.jobs")

SPEC = 2
FINAL = ("done", "partial", "failed", "aborted", "cancelled", "missed")
OPEN = ("queued", "scheduled", "waiting", "running")
ORDER = {"now": 0, "idle": 1, "at": 2, "tonight": 3}
TEXT_PARAMS = ("text", "notes", "bullets", "context_text", "cv_text", "posting_text")


# ===================================================================== file store

def jobs_dir() -> Path:
    return config.sub_dir("jobs")


def _safe(job_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z-]", "", job_id or "")


def job_path(job_id: str) -> Path:
    return jobs_dir() / f"{_safe(job_id)}.json"


def cancel_path(job_id: str) -> Path:
    return jobs_dir() / f"{_safe(job_id)}.cancel"


def new_id() -> str:
    return time.strftime("%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


def read(job_id: str) -> dict[str, Any] | None:
    p = job_path(job_id)
    for _ in range(20):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if data.get("spec") == SPEC else None
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)  # the worker is replacing the file
    return None


def write(job: dict[str, Any]) -> None:
    p = job_path(job["job_id"])
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, default=str), encoding="utf-8")
    for attempt in range(60):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:  # Windows: a reader has the file open for a moment
            time.sleep(0.05)
    os.replace(tmp, p)


def all_jobs() -> list[dict[str, Any]]:
    out = []
    for f in jobs_dir().glob("*.json"):
        j = read(f.stem)
        if j:
            out.append(j)
    return out


class FileLock:
    """An exclusive lock on a file, held until release() or until the process ends."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def try_acquire(self) -> bool:
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def release(self) -> None:
        if not self.fh:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        self.fh = None


# ===================================================================== worker process management

_inproc: dict[str, Any] = {"worker": None, "thread": None}
_last_spawn = {"t": 0.0}


def worker_lock_path() -> Path:
    return config.data_dir() / "worker.lock"


def worker_alive() -> bool:
    w = _inproc.get("worker")
    if w is not None and _inproc["thread"] is not None and _inproc["thread"].is_alive():
        return True
    lk = FileLock(worker_lock_path())
    if lk.try_acquire():
        lk.release()
        return False
    return True


def _read_json(p: Path) -> dict[str, Any]:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def outlives_claude(info: dict[str, Any]) -> bool | None:
    """Whether the running worker keeps going when Claude's app closes, judged from the job object
    the worker itself reports (None when unknown, e.g. a worker started by an older version)."""
    if info.get("how") == "in-process":
        return False
    if not config.IS_WINDOWS:
        return True  # started in its own session
    job = info.get("job")
    if not isinstance(job, dict):
        return None
    if not job.get("in_job"):
        return True
    return None if job.get("kill_on_close") is None else not job["kill_on_close"]


def worker_info() -> dict[str, Any]:
    """Is the worker running, how was it started ('breakaway', 'detached', 'wmi' or 'child'), and
    will it outlive Claude's app? The last comes from the job object the worker reports."""
    info: dict[str, Any] = {"running": worker_alive()}
    info.update(_read_json(config.data_dir() / "spawn.json"))
    if info["running"]:
        info.update(_read_json(config.data_dir() / "worker.json"))
    if _inproc.get("thread") is not None:
        info["how"] = "in-process"
    if info["running"]:
        info["outlives_claude"] = outlives_claude(info)
    return info


def check_worker() -> dict[str, Any]:
    """Start the worker if it is not running (no job, no model) and report on it: the job object it
    sits in and whether it will outlive Claude's app. It exits again a few minutes later."""
    started = ensure_worker()
    info = worker_info()
    end = time.time() + 8
    while (info.get("running") and info.get("how") != "in-process" and "job" not in info
           and time.time() < end):
        time.sleep(0.2)
        info = worker_info()
    info["started_now"] = started.get("how") not in (None, "in-process")
    info["connector_job"] = sysinfo.job_object()
    verdict = info.get("outlives_claude")
    info["verdict"] = ("the worker sits outside Claude's app: overnight jobs keep running if Claude closes"
                       if verdict else
                       "the worker would stop if Claude's app closes: keep Claude open while overnight jobs run"
                       if verdict is False else
                       "unknown: the worker could not report its job object" if info.get("running") else
                       "the worker did not start; see worker.log in the data folder")
    return info


def ensure_worker() -> dict[str, Any]:
    if os.environ.get("LOCAL_DELEGATE_INPROCESS_WORKER"):
        t = _inproc.get("thread")
        if t is None or not t.is_alive():
            w = Worker(idle_exit_s=float(os.environ.get("LOCAL_DELEGATE_WORKER_IDLE_S", "3600")), poll_s=0.2,
                       hand_over=False)
            t = threading.Thread(target=w.run_forever, name="local-delegate-worker", daemon=True)
            _inproc.update(worker=w, thread=t)
            t.start()
            w.started.wait(5)
        return {"running": True, "how": "in-process"}
    if worker_alive():
        return {"running": True}
    if time.time() - _last_spawn["t"] < 5:
        return {"running": False, "starting": True}
    _last_spawn["t"] = time.time()
    how = spawn_worker()
    try:
        (config.data_dir() / "spawn.json").write_text(json.dumps({"how": how, "at": schedule.fmt_clock(time.time())}),
                                                       encoding="utf-8")
    except OSError:
        pass
    for _ in range(40):
        if worker_alive():
            return {"running": True, "how": how}
        time.sleep(0.1)
    return {"running": worker_alive(), "how": how}


def stop_inprocess_worker() -> None:
    w, t = _inproc.get("worker"), _inproc.get("thread")
    if w is not None:
        w.stop.set()
    if t is not None:
        t.join(10)
    _inproc.update(worker=None, thread=None)


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _worker_cmd(gui: bool) -> list[str]:
    exe = Path(sys.executable)
    if gui and config.IS_WINDOWS and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    return [str(exe), "-E", "-X", "utf8", str(config.SERVER_DIR / "ld.py"), "worker"]


def spawn_worker() -> str:
    """Start the worker so that it outlives this process and Claude's app."""
    devnull = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    cwd = str(config.data_dir())
    if not config.IS_WINDOWS:
        subprocess.Popen(_worker_cmd(False), start_new_session=True, close_fds=True, cwd=cwd, **devnull)
        return "session"
    NO_WINDOW, NEW_GROUP, BREAKAWAY = 0x08000000, 0x00000200, 0x01000000
    launcher = [sys.executable, "-E", "-X", "utf8", str(config.SERVER_DIR / "ld.py"), "launch-worker"]
    job = sysinfo.job_object() or {}
    # 1) a short-lived middle process that starts the worker, so the worker is not this process's
    #    child, created outside the app's job object. Windows ignores the breakaway request when
    #    there is no job, and refuses it when the job forbids leaving (then the next ways are tried).
    try:
        subprocess.Popen(launcher, creationflags=NO_WINDOW | NEW_GROUP | BREAKAWAY, close_fds=True, cwd=cwd, **devnull)
        return "breakaway" if job.get("in_job") else "detached"
    except OSError:
        pass
    # 2) a job that lets its processes' children go anyway ("silent breakaway") needs no request.
    if job.get("silent_breakaway"):
        try:
            subprocess.Popen(launcher, creationflags=NO_WINDOW | NEW_GROUP, close_fds=True, cwd=cwd, **devnull)
            return "detached"
        except OSError:
            pass
    # 3) ask Windows (WMI) to create it: it then belongs to no app at all.
    cmd = subprocess.list2cmdline(_worker_cmd(True))
    ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine="
          + _ps_quote(cmd) + "; CurrentDirectory=" + _ps_quote(cwd) + "}; $r.ReturnValue")
    rc, out, _ = sysinfo.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=30)
    if rc == 0 and out.strip() and out.strip().splitlines()[-1].strip() == "0":  # ReturnValue 0: created
        return "wmi"
    # 4) last resort: an ordinary child (stops if Claude's app closes).
    subprocess.Popen(launcher, creationflags=NO_WINDOW | NEW_GROUP, close_fds=True, cwd=cwd, **devnull)
    return "child"


def launch_worker() -> None:
    """Body of `ld.py launch-worker`: start the real worker detached, then exit at once."""
    DETACHED, NEW_GROUP = 0x00000008, 0x00000200
    kw: dict[str, Any] = {"close_fds": True, "cwd": str(config.data_dir()), "stdin": subprocess.DEVNULL,
                          "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if config.IS_WINDOWS:
        kw["creationflags"] = DETACHED | NEW_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(_worker_cmd(True), **kw)


# ===================================================================== after an update: hand over

def _version_tuple(v: str | None) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4]) or (0,)


def code_version(server_dir: str | Path) -> str | None:
    """The connector version in a code folder, read from the file (not imported)."""
    try:
        text = (Path(server_dir) / "local_delegate" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", text)
    return m.group(1) if m else None


def note_server() -> None:
    """Record this connector's version and code folder in server.json. A worker started by an older
    version reads it and hands over to this version's worker once nothing is in progress. A note
    left by a newer version that is still installed is kept."""
    p = config.data_dir() / "server.json"
    cur = _read_json(p)
    other_dir = cur.get("server_dir")
    if other_dir and other_dir != str(config.SERVER_DIR):
        other = code_version(other_dir)
        if other and _version_tuple(other) > _version_tuple(config.VERSION):
            return
    try:
        p.write_text(json.dumps({"version": config.VERSION, "server_dir": str(config.SERVER_DIR),
                                 "python": sys.executable, "at": schedule.fmt_clock(time.time())}),
                     encoding="utf-8")
    except OSError:
        pass


def newer_code() -> dict[str, Any] | None:
    """The newer connector this worker should hand over to, if one is installed (see note_server)."""
    note = _read_json(config.data_dir() / "server.json")
    d = note.get("server_dir")
    if not d or d == str(config.SERVER_DIR) or not (Path(d) / "ld.py").exists():
        return None
    v = code_version(d)
    if not v or _version_tuple(v) <= _version_tuple(config.VERSION):
        return None
    return {**note, "version": v}


def launch_successor(note: dict[str, Any]) -> bool:
    """Start a worker from the code folder in `note` (through its launcher). Used by a worker on its
    way out: to hand over to a newer version, or when a job arrived just as it was leaving."""
    py = note.get("python") or ""
    if not py or not Path(py).exists():
        py = sys.executable
    cmd = [py, "-E", "-X", "utf8", str(Path(note["server_dir"]) / "ld.py"), "launch-worker"]
    kw: dict[str, Any] = {"close_fds": True, "cwd": str(config.data_dir()), "stdin": subprocess.DEVNULL,
                          "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if not config.IS_WINDOWS:
        kw["start_new_session"] = True
        tries = [kw]
    else:
        NO_WINDOW, NEW_GROUP, BREAKAWAY = 0x08000000, 0x00000200, 0x01000000
        tries = [{**kw, "creationflags": NO_WINDOW | NEW_GROUP | BREAKAWAY}, {**kw, "creationflags": NO_WINDOW | NEW_GROUP}]
    for t in tries:
        try:
            subprocess.Popen(cmd, **t)
            return True
        except OSError:
            continue
    return False


# ===================================================================== front: submit, wait, cancel

def submit(job: dict[str, Any]) -> dict[str, Any]:
    job.setdefault("spec", SPEC)
    job.setdefault("job_id", new_id())
    job.setdefault("created", time.time())
    job.setdefault("parts", {})
    job.setdefault("notes", [])
    job["status"] = "scheduled" if job.get("not_before") and job["not_before"] > time.time() + 0.5 else "queued"
    write(job)
    job["worker"] = ensure_worker()
    return job


def task_job(task: str, params: dict[str, Any], run: dict[str, Any], return_mode: str, budget_s: float | None,
             yield_to_user: bool, deadline: float | None, not_before: float | None, when: str,
             mode: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return submit({
        "kind": "task", "task": task, "params": params, "mode": mode or run["mode"], "when": when,
        "model": run["model"], "not_before": not_before, "deadline": deadline, "budget_s": budget_s,
        "return_mode": return_mode, "yield_to_user": yield_to_user, "estimate": run,
        "progress": {"done": 0, "total": None}, **(extra or {}),
    })


def calibration_job(model: str, budget_s: float) -> dict[str, Any]:
    return submit({"kind": "calibrate", "task": "calibrate", "params": {}, "mode": "fast", "when": "now",
                   "model": model, "budget_s": budget_s, "deadline": None, "not_before": None,
                   "estimate": {"p50_s": budget_s * 0.6}, "return_mode": "content", "yield_to_user": False})


def public(job: dict[str, Any] | None, now: float | None = None, with_result: bool = True) -> dict[str, Any] | None:
    if job is None:
        return None
    now = now or time.time()
    out: dict[str, Any] = {"job_id": job["job_id"], "task": job.get("task"), "level": job.get("mode"),
                           "when": job.get("when"), "status": job.get("status"), "phase": job.get("phase")}
    st = job.get("status")
    if st in ("queued", "scheduled", "waiting") and job.get("not_before") and job["not_before"] > now:
        out["starts_at"] = schedule.fmt_clock(job["not_before"], now)
    if st in OPEN:
        eta = _eta(job, now)
        if eta is not None:
            out["ready_about"] = schedule.fmt_clock(now + eta, now)
            out["eta_s"] = round(eta)
        if job.get("deadline"):
            out["stops_by"] = schedule.fmt_clock(job["deadline"], now)
    prog = job.get("progress") or {}
    if prog.get("total") and prog["total"] > 1:
        out["progress"] = f"{prog.get('done', 0)}/{prog['total']} parts" + (
            f", {prog['fixups']} fix-ups" if prog.get("fixups") else "")
    if job.get("started"):
        out["running_for_s"] = round((job.get("finished") or now) - job["started"])
    for k in ("error", "saved_to"):
        if job.get(k):
            out[k] = job[k]
    if job.get("timing"):
        t = job["timing"]
        out["timing"] = {k: t.get(k) for k in ("took_s", "estimated_s", "tokens_in", "cached_in", "tokens_out", "reasoning_tokens",
                                               "fixup_calls", "fallbacks") if t.get(k) is not None}
    if with_result and job.get("result") is not None:
        out["result"] = job["result"]
    if st in ("partial", "aborted", "cancelled", "failed", "missed") and job.get("parts"):
        out["resumable"] = f"{len(job['parts'])} finished part(s) kept; local_run(job_id=..., when=...) continues"
    return out


def _eta(job: dict[str, Any], now: float) -> float | None:
    if job.get("status") == "running" and job.get("eta_s") is not None and job.get("eta_at"):
        return max(5.0, float(job["eta_s"]) - (now - float(job["eta_at"])))
    est = (job.get("estimate") or {}).get("p50_s")
    if est is None:
        return None
    start = max(now, float(job.get("not_before") or now))
    return start - now + float(est)


def wait(job_id: str, timeout: float, cancel: threading.Event | None = None) -> dict[str, Any] | None:
    end = time.monotonic() + max(0.0, timeout)
    job = read(job_id)
    last_check = 0.0
    while job and job.get("status") in OPEN and time.monotonic() < end:
        if cancel is not None and cancel.is_set():
            break
        if job.get("status") in ("queued", "running") and time.monotonic() - last_check > 10:
            last_check = time.monotonic()
            if not worker_alive():
                ensure_worker()
        time.sleep(0.4)
        job = read(job_id)
    return job


def request_cancel(job_id: str) -> dict[str, Any] | None:
    job = read(job_id)
    if not job:
        return None
    if job.get("status") in FINAL:
        return job
    cancel_path(job_id).write_text("cancel", encoding="utf-8")
    ensure_worker()
    return wait(job_id, 15)


def resume(job_id: str, when_info: dict[str, Any], budget_s: float | None, deadline: float | None,
           mode: str | None = None) -> dict[str, Any] | None:
    job = read(job_id)
    if not job or job.get("status") not in ("partial", "aborted", "cancelled", "failed", "missed"):
        return job
    try:
        cancel_path(job_id).unlink()
    except FileNotFoundError:
        pass
    job.update({"when": when_info["when"], "not_before": when_info.get("not_before"),
                "deadline": deadline, "budget_s": budget_s, "result": None, "error": None, "finished": None,
                "started": None, "retry_at": None, "phase": None})
    if mode:
        job["mode"] = mode
    job["status"] = "scheduled" if job.get("not_before") and job["not_before"] > time.time() + 0.5 else "queued"
    write(job)
    ensure_worker()
    return job


def queue_state(now: float | None = None) -> dict[str, Any]:
    """What is queued: how long until a new 'now' job could start, and how much is booked tonight."""
    now = now or time.time()
    open_jobs = [j for j in all_jobs() if j.get("status") in OPEN]
    now_eta = 0.0
    tonight = 0.0
    for j in open_jobs:
        p50 = float((j.get("estimate") or {}).get("p50_s") or 0)
        if j.get("when") == "tonight":
            tonight += p50 if j.get("status") != "running" else (_eta(j, now) or 0)
        elif j.get("when") == "now":
            now_eta += (_eta(j, now) or p50) if j.get("status") == "running" else p50
        elif j.get("status") == "running":
            now_eta += min(_eta(j, now) or 60.0, 180.0)  # a 'now' job overtakes it at the next part
    return {"queue_eta_s": now_eta, "tonight_queue_s": tonight, "open": len(open_jobs)}


def recent(n: int = 10) -> list[dict[str, Any]]:
    js = sorted(all_jobs(), key=lambda j: -float(j.get("created") or 0))[:n]
    return [public(j, with_result=False) for j in js]


def cleanup(days: float) -> None:
    cutoff = time.time() - days * 86400
    for d in ("jobs", "outbox"):
        for f in config.sub_dir(d).glob("*"):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue


# ===================================================================== the worker

class RunState:
    """A job the worker has started: its prepared input, finished answers, and pending fix-ups."""

    def __init__(self, job: dict[str, Any], prep: Prepared, task: Any):
        self.prep, self.task = prep, task
        self.outs: dict[int, dict[str, Any]] = {}
        self.issues: dict[int, list[str]] = {}
        self.checked: set[int] = set()
        self.repairs: list[dict[str, Any]] = []
        self.fallbacks: list[str] = []
        self.totals = {"load_s": 0.0, "prefill_s": 0.0, "decode_s": 0.0, "prompt_tokens": 0, "cached_tokens": 0,
                       "output_tokens": 0, "think_tokens": 0, "busy_s": 0.0}
        self.mode = job.get("mode") or "fast"
        self.ctx = 8192
        self.model = job.get("model")
        self.think_on: Any = None
        self.think_off: Any = None
        self.repair_think: Any = None
        self.think_budget = 0
        self.repair_budget = 0
        self.with_format = True
        self.sampling: dict[str, Any] = {}
        self.repair_enabled = False

    def next_step(self) -> tuple[str, int] | None:
        for i in range(len(self.prep.calls)):
            if i not in self.outs:
                return "main", i
        for i in sorted(self.issues):
            if i not in self.checked:
                return "repair", i
        return None


class Worker:
    def __init__(self, idle_exit_s: float | None = None, poll_s: float = 1.0, hand_over: bool = True):
        self.idle_exit_s = float(idle_exit_s if idle_exit_s is not None
                                 else os.environ.get("LOCAL_DELEGATE_WORKER_IDLE_S", "180"))
        self.poll_s = poll_s
        self.hand_over = hand_over          # a separate process: may hand over to a newer version
        self.successor: dict[str, Any] | None = None
        self.version_check_s = float(os.environ.get("LOCAL_DELEGATE_VERSION_CHECK_S", "10"))
        self._next_version_check = 0.0
        self.stop = threading.Event()
        self.started = threading.Event()
        self.active: dict[str, RunState] = {}
        self.awake = False
        self.last_hash: str | None = None
        self.show_cache: dict[str, dict[str, Any]] = {}
        self.lock = FileLock(worker_lock_path())
        self._last_phase_write: dict[str, float] = {}
        self._final: dict[str, float] = {}

    # ------------------------------------------------------------ main loop
    def run_forever(self) -> str:
        # A worker on its way out (idle, or handing over) may still hold the lock for a moment.
        end = time.time() + (5 if self.hand_over else 0)
        while not self.lock.try_acquire():
            if time.time() >= end:
                self.started.set()
                return "another worker is already running"
            time.sleep(0.2)
        info_path = config.data_dir() / "worker.json"
        try:
            info_path.write_text(json.dumps({"pid": os.getpid(), "since": schedule.fmt_clock(time.time()),
                                             "version": config.VERSION, "python": sys.executable,
                                             "job": sysinfo.job_object()}), encoding="utf-8")
        except OSError:
            pass
        self.started.set()
        try:
            cleanup(float(config.load().get("retention_days", 14)))
        except Exception:
            pass
        last_busy = time.time()
        idle_exit = False
        try:
            while not self.stop.is_set():
                try:
                    busy = self.tick()
                except Exception:
                    log.exception("worker step failed")
                    busy = False
                    self.stop.wait(2)
                if busy:
                    last_busy = time.time()
                    continue
                if self.hand_over and not self.active and time.time() >= self._next_version_check:
                    self._next_version_check = time.time() + self.version_check_s
                    self.successor = newer_code()
                    if self.successor:   # nothing in progress: the newer version takes the queue
                        log.info("handing over to version %s", self.successor.get("version"))
                        break
                if not self._open_jobs() and time.time() - last_busy > self.idle_exit_s:
                    idle_exit = True
                    break
                self.stop.wait(self.poll_s)
        finally:
            self._set_awake(False)
            try:
                info_path.unlink()
            except OSError:
                pass
            self.lock.release()
        if self.hand_over and idle_exit and self.successor is None and self._open_jobs():
            # a job arrived while this worker was leaving: start a fresh one rather than strand it
            self.successor = {"server_dir": str(config.SERVER_DIR), "python": sys.executable}
        if self.successor:
            ok = launch_successor(self.successor)
            return f"handed over ({'started' if ok else 'could not start'} {self.successor.get('version') or 'a new worker'})"
        return "stopped"

    def _open_jobs(self) -> list[dict[str, Any]]:
        """Open jobs. Finished job files are remembered by modification time and not re-read every
        second while the worker waits (e.g. for tonight); a file that changes is read again."""
        out = []
        seen = set()
        for f in jobs_dir().glob("*.json"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            seen.add(f.stem)
            if self._final.get(f.stem) == mtime:
                continue
            j = read(f.stem)
            if not j:
                continue
            if j.get("status") in OPEN:
                out.append(j)
            else:
                self._final[f.stem] = mtime
        for gone in set(self._final) - seen:
            del self._final[gone]
        return out

    def _set_awake(self, on: bool) -> None:
        if on != self.awake:
            sysinfo.keep_awake(on)
            self.awake = on

    def tick(self) -> bool:
        now = time.time()
        jobs = self._open_jobs()
        if not jobs:
            self._set_awake(False)
            return False
        cfg = config.load(refresh=True)
        for j in jobs:
            if cancel_path(j["job_id"]).exists():
                self._end(j, "cancelled", "cancelled")
        jobs = [j for j in self._open_jobs()]
        for j in jobs:
            if j.get("deadline") and now >= float(j["deadline"]):
                self._end(j, "partial" if j.get("parts") else ("missed" if not j.get("started") else "aborted"),
                          "reached its deadline" if j.get("started") else "the window ended before it could start")
        jobs = [j for j in self._open_jobs()]
        self._set_awake(bool(jobs) and bool(cfg.get("keep_awake", True)) and sysinfo.on_mains_power())
        ready = []
        for j in jobs:
            ok, why = self._ready(j, now, cfg)
            if ok:
                ready.append(j)
            elif why and why != j.get("phase"):
                self._phase(j, why, force=True)
        if not ready:
            return False
        job = min(ready, key=lambda j: (ORDER.get(j.get("when"), 9), float(j.get("created") or 0)))
        for j in ready:
            if j is not job and j.get("status") == "running" and j.get("phase") != "paused for a quicker job":
                self._phase(j, "paused for a quicker job", force=True)
        self._step(job, cfg)
        return True

    def _ready(self, j: dict[str, Any], now: float, cfg: dict[str, Any]) -> tuple[bool, str | None]:
        if j.get("not_before") and now < float(j["not_before"]):
            return False, f"scheduled: starts at {schedule.fmt_clock(float(j['not_before']), now)}"
        if j.get("retry_at") and now < float(j["retry_at"]):
            return False, j.get("phase")
        idle = sysinfo.idle_seconds()
        if j.get("when") == "idle" and not j.get("started"):
            need = float(cfg.get("idle_minutes", 10)) * 60
            if idle is not None and idle < need:
                return False, f"waiting until the computer has been idle {int(need // 60)} min"
        if j.get("yield_to_user") and j.get("started") and idle is not None and idle < 60:
            return False, "paused while you use the computer"
        return True, None

    # ------------------------------------------------------------ one unit of work
    def _step(self, job: dict[str, Any], cfg: dict[str, Any]) -> None:
        if job.get("kind") == "calibrate":
            self._calibrate(job, cfg)
            return
        rs = self.active.get(job["job_id"])
        if rs is None:
            rs = self._begin(job, cfg)
            if rs is None:
                return
            self.active[job["job_id"]] = rs
        step = rs.next_step()
        if step is None:
            self._end(job, "done", None)
            return
        self._run(job, rs, step, cfg)

    def _client(self, cfg: dict[str, Any]) -> Ollama:
        return Ollama(cfg["ollama_url"])

    def _show(self, client: Ollama, model: str) -> dict[str, Any]:
        if model not in self.show_cache:
            self.show_cache[model] = client.show(model)
        return self.show_cache[model]

    def _wait_or_fail(self, job: dict[str, Any], why: str, retry_s: float, give_up_s: float) -> None:
        first = float(job.get("waiting_since") or time.time())
        job["waiting_since"] = first
        if job.get("when") == "now" and time.time() - first > give_up_s:
            self._end(job, "failed", why)
            return
        job["status"] = "waiting"
        job["retry_at"] = time.time() + retry_s
        self._phase(job, f"waiting: {why}", force=True)

    def _begin(self, job: dict[str, Any], cfg: dict[str, Any]) -> RunState | None:
        try:
            client = self._client(cfg)
            rep = guard.report(cfg, client, refresh=True)
        except OllamaError as exc:
            rep = {"ok": False, "problems": [str(exc)]}
        if not rep["ok"]:
            self._wait_or_fail(job, "local-only checks not passing (" + "; ".join(rep["problems"][:2]) + ")", 120, 60)
            return None
        task = TASKS.get(job.get("task") or "")
        if task is None:
            self._end(job, "failed", f"unknown task {job.get('task')}")
            return None
        profile = calibrate.load_profile()
        try:
            prep = task.prepare(job.get("params") or {}, cfg, profile)
        except (TaskInputError, textio.ReadError, OSError, ValueError) as exc:
            self._end(job, "failed", str(exc))
            return None
        rs = RunState(job, prep, task)
        if not prep.calls:
            self.active[job["job_id"]] = rs
            return rs
        model = job.get("model") or cfg["model"]
        rs.model = model
        try:
            show = self._show(client, model)
            loaded = client.loaded(model)
            version = client.version() or "0"
        except OllamaError as exc:
            self._wait_or_fail(job, str(exc), 120, 60)
            return None
        mc = cfg["modes"].get(rs.mode) or cfg["modes"]["fast"]
        thinking, budget = estimate.mode_budget(cfg, rs.mode, job.get("when") == "tonight")
        rs.think_off = think_off_value(show)
        rs.think_on = think_value(mc.get("think"), show) if thinking else rs.think_off
        if thinking and rs.think_on in (None, False, "none"):
            job["notes"].append("this model has no reasoning switch; ran without reasoning")
            thinking = False
        rs.think_budget = budget if thinking else 0
        _, rs.repair_budget = estimate.mode_budget(cfg, "careful", job.get("when") == "tonight")
        rs.repair_think = think_value((cfg["modes"]["careful"]).get("think"), show) if thinking else rs.think_off
        rs.repair_enabled = bool(mc.get("repair")) and task.issues is not None
        rs.with_format = _version_at_least(version, (0, 34, 4)) or not thinking
        rs.sampling = {k: mc[k] for k in ("temperature", "top_p", "top_k", "min_p") if k in mc} if thinking else {}
        r = estimate.rates_for(model, profile, cfg)
        lt = estimate.local_time(prep, task, cfg, r, rs.mode, job.get("when") == "tonight", loaded, self.last_hash,
                                 profile.get("token_ratio"))
        if lt["ctx"] is None:
            self._end(job, "failed", f"the input needs ~{lt['need_ctx']:,} tokens of context; split it")
            return None
        rs.ctx = lt["ctx"]
        if cfg.get("ram_check", True):
            need = estimate.memory_need(r, rs.ctx, loaded)
            avail = sysinfo.memory().get("available")
            headroom = float(cfg.get("ram_headroom_gb", 4.0)) * 1e9
            if avail is not None and avail - need < headroom:
                self._wait_or_fail(job, f"not enough free memory (needs ~{need / 1e9:.1f} GB more, {avail / 1e9:.1f} GB "
                                        "free); close heavy apps", 120, 600)
                return None
        for i, c in enumerate(prep.calls):  # parts finished in an earlier run
            part = (job.get("parts") or {}).get(c.key)
            if part:
                rs.outs[i] = part
                if part.get("checked") or not rs.repair_enabled:
                    rs.checked.add(i)
                elif task.issues:
                    iss = task.issues(prep, i, part)
                    if iss:
                        rs.issues[i] = iss
                    else:
                        rs.checked.add(i)
        job.update({"status": "running", "started": job.get("started") or time.time(), "retry_at": None,
                    "waiting_since": None, "progress": {"done": len(rs.outs), "total": len(prep.calls)}})
        if not loaded:
            job["phase"] = "loading the model"
        write(job)
        return rs

    def _remaining(self, job: dict[str, Any]) -> float:
        now = time.time()
        limits = []
        if job.get("deadline"):
            limits.append(float(job["deadline"]) - now)
        if job.get("budget_s") and job.get("started"):
            limits.append(float(job["started"]) + float(job["budget_s"]) - now)
        return min(limits) if limits else 6 * 3600.0

    def _run(self, job: dict[str, Any], rs: RunState, step: tuple[str, int], cfg: dict[str, Any]) -> None:
        kind, i = step
        prep, call = rs.prep, rs.prep.calls[step[1]]
        remaining = self._remaining(job)
        if remaining < 0.5:
            self._end(job, "partial" if rs.outs else "aborted", "stopped at the approved time budget")
            return
        thinking = rs.mode != "fast" and rs.think_budget > 0
        if kind == "main":
            messages, think, budget = call.messages, (rs.think_on if thinking else rs.think_off), rs.think_budget
        else:
            messages = repair_messages(call, rs.outs[i], rs.issues[i])
            think, budget = rs.repair_think, (rs.repair_budget if thinking else 0)
        label = "fixing flagged problems" if kind == "repair" else "reading and answering"
        n = len(prep.calls)
        self._phase(job, f"{label}{f' (part {i + 1}/{n})' if n > 1 else ''}", force=True)
        res, out = self._chat(job, rs, call, messages, think, budget if thinking else 0, remaining, cfg)
        if res.aborted and res.abort_reason in ("loop", "think_budget") and thinking:
            rs.fallbacks.append(f"part {i + 1}: {'reasoning went in circles' if res.abort_reason == 'loop' else 'reasoning budget used up'}; answered without reasoning")
            res, out = self._chat(job, rs, call, messages, rs.think_off, 0, self._remaining(job), cfg)
        elif (not res.aborted and thinking and call.schema is not None and out.get("json") is None
              and res.done_reason == "length"):
            rs.fallbacks.append(f"part {i + 1}: reasoning left no room for the answer; answered without reasoning")
            res, out = self._chat(job, rs, call, messages, rs.think_off, 0, self._remaining(job), cfg)
        if res.aborted:
            reason = res.abort_reason
            if reason == "cancelled":
                self._end(job, "cancelled", "cancelled")
            elif reason == "time_budget":
                self._end(job, "partial" if rs.outs else "aborted", "stopped at the approved time budget")
            else:
                self._end(job, "failed", {"no_output_before_deadline": "no output in time (model too slow or swapping)",
                                          "stalled": "the model stopped producing output",
                                          "loop": "the model kept repeating itself",
                                          "incomplete_response": "Ollama closed the connection early"}.get(reason, reason))
            return
        self.last_hash = prep.system_hash
        task = rs.task
        if kind == "main":
            out["checked"] = not rs.repair_enabled
            rs.outs[i] = out
            if rs.repair_enabled and task.issues:
                iss = task.issues(prep, i, out)
                if iss:
                    rs.issues[i] = iss
                else:
                    rs.checked.add(i)
                    out["checked"] = True
            else:
                rs.checked.add(i)
            job["parts"][call.key] = _slim(out)
        else:
            before = rs.issues[i]
            after = task.issues(prep, i, out) if task.issues else []
            better = len(after) < len(before) or (rs.outs[i].get("json") is None and out.get("json") is not None
                                                  and call.schema is not None)
            rs.checked.add(i)
            rs.repairs.append({"part": i + 1, "problems_before": len(before), "problems_after": len(after),
                               "kept": "fixed version" if better else "first version"})
            if better:
                out["repaired"] = True
                rs.outs[i] = out
            rs.outs[i]["checked"] = True
            job["parts"][call.key] = _slim(rs.outs[i])
        job["progress"] = {"done": len(rs.outs), "total": len(prep.calls), "fixups": len(rs.repairs)}
        nxt = rs.next_step()
        job["eta_s"], job["eta_at"] = self._eta_after(job, rs), time.time()
        write(job)
        if nxt is None:
            self._end(job, "done", None)

    def _eta_after(self, job: dict[str, Any], rs: RunState) -> float:
        est = job.get("estimate") or {}
        n = max(1, len(rs.prep.calls))
        left = n - len(rs.outs) + len([k for k in rs.issues if k not in rs.checked])
        per = float(est.get("p50_s") or 60) / n
        return max(5.0, left * per)

    def _chat(self, job: dict[str, Any], rs: RunState, call: Any, messages: list[dict[str, str]], think: Any,
              budget: int, remaining: float, cfg: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        client = self._client(cfg)
        thinking = budget > 0
        options: dict[str, Any] = {"num_ctx": rs.ctx, "num_predict": call.num_predict + budget, "seed": 7}
        if thinking:
            options.update(rs.sampling)
        else:
            options["temperature"] = call.temperature
        fmt = call.schema if (not thinking or rs.with_format) else None
        cancel = threading.Event()
        flag = cancel_path(job["job_id"])
        done = threading.Event()

        def watch() -> None:
            while not done.wait(0.5):
                if flag.exists():
                    cancel.set()
                    return

        threading.Thread(target=watch, daemon=True).start()
        est = job.get("estimate") or {}
        n = max(1, len(rs.prep.calls))
        exp_tokens = (float(est.get("output_tokens_p50") or 300) + float(est.get("think_tokens_p50") or 0)) / n
        later = (n - len(rs.outs) - 1) * float(est.get("p50_s") or 60) / n
        started = time.time()

        def progress(info: dict[str, Any]) -> None:
            elapsed = max(0.1, time.time() - started)
            toks = int(info.get("tokens_out") or 0)
            per_tok = elapsed / max(1, toks)
            left = max(0.0, exp_tokens - toks) * per_tok + max(0.0, later)
            what = "reasoning" if info.get("phase") == "thinking" else "writing"
            job["eta_s"], job["eta_at"] = max(5.0, left), time.time()
            self._phase(job, f"{what} ({int(info.get('thinking_tokens') or 0)} reasoning + "
                             f"{int(info.get('answer_tokens') or 0)} answer tokens)")

        try:
            first_timeout = min(remaining, max(180.0, float(est.get("load_s") or 40) * 2 +
                                               float(est.get("prefill_s") or 60) / n * 2.5 + 60))
            res = client.chat(rs.model, messages, options=options, fmt=fmt, think=think,
                              keep_alive=cfg.get("keep_alive", "10m"), budget_s=remaining,
                              first_token_timeout_s=first_timeout, stall_timeout_s=240, cancel=cancel,
                              loop_guard=True, max_thinking_tokens=budget if thinking else None, on_progress=progress)
        except OllamaError as exc:
            from .ollama import ChatResult

            res = ChatResult(aborted=True, abort_reason=f"Ollama error: {exc}")
        finally:
            done.set()
        st = res.stats or {}
        t = rs.totals
        t["load_s"] += (st.get("load_duration") or 0) / 1e9
        t["prefill_s"] += (st.get("prompt_eval_duration") or 0) / 1e9
        t["decode_s"] += (st.get("eval_duration") or 0) / 1e9
        t["prompt_tokens"] += st.get("prompt_eval_count") or 0
        t["cached_tokens"] += st.get("prompt_eval_cached_count") or 0
        t["output_tokens"] += st.get("eval_count") or res.tokens_streamed
        t["think_tokens"] += res.thinking_tokens
        t["busy_s"] += res.wall_s
        out: dict[str, Any] = {"text": res.content, "truncated": st.get("done_reason") == "length",
                               "think_tokens": res.thinking_tokens}
        if call.schema is not None:
            out["json"] = parse_json(res.content)
            if out["json"] is not None:
                errs = validate(out["json"], call.schema)
                if errs:
                    out["schema_errors"] = errs
        return res, out

    # ------------------------------------------------------------ finishing
    def _restore(self, job: dict[str, Any], cfg: dict[str, Any]) -> RunState | None:
        """Rebuild a job's state from its saved parts (to hand back what was finished)."""
        task = TASKS.get(job.get("task") or "")
        if task is None or not job.get("parts"):
            return None
        try:
            prep = task.prepare(job.get("params") or {}, cfg, calibrate.load_profile())
        except Exception:
            return None
        rs = RunState(job, prep, task)
        for i, c in enumerate(prep.calls):
            if c.key in job["parts"]:
                rs.outs[i] = job["parts"][c.key]
        return rs

    def _end(self, job: dict[str, Any], status: str, reason: str | None) -> None:
        job = read(job["job_id"]) or job
        rs = self.active.pop(job["job_id"], None)
        if rs is None and status in ("partial", "aborted") and job.get("kind") == "task":
            rs = self._restore(job, config.load())
        try:
            cancel_path(job["job_id"]).unlink()
        except FileNotFoundError:
            pass
        result = None
        if rs is not None and status != "cancelled":
            calls = len(rs.prep.calls)
            prefix: list[dict[str, Any]] = []
            for i in range(calls):
                if i not in rs.outs:
                    break
                prefix.append(rs.outs[i])
            outs = None
            if calls == 0 or len(prefix) == calls:
                outs = prefix
                if status != "done":   # everything was answered; only some fix-ups were cut short
                    reason, status = None, "done"
            elif prefix and rs.task.partial_ok and status in ("partial", "aborted"):
                outs, status = prefix, "partial"
                reason = f"{reason}; returned the first {len(prefix)} of {calls} parts"
            elif status == "partial":
                status = "aborted"
            if outs is not None:
                try:
                    res = rs.task.finalize(rs.prep, outs) if rs.task.finalize else {"outputs": [o.get("text") for o in outs]}
                    result = self._deliver(job, rs, res)
                except Exception as exc:  # a bug in assembling must not lose the finished parts
                    log.exception("finalize failed")
                    status, reason, result = "failed", f"could not assemble the result ({exc}); the parts are kept", None
        job.update({"status": status, "finished": time.time(), "error": None if status == "done" else reason,
                    "result": result, "phase": status, "eta_s": None})
        if rs is not None:
            t = rs.totals
            est = job.get("estimate") or {}
            job["timing"] = {"took_s": round(t["busy_s"], 1), "estimated_s": est.get("p50_s"),
                             "tokens_in": t["prompt_tokens"], "cached_in": t["cached_tokens"],
                             "tokens_out": t["output_tokens"], "reasoning_tokens": t["think_tokens"],
                             "fixup_calls": len(rs.repairs) or None, "fallbacks": rs.fallbacks or None}
            if t["busy_s"] or rs.repairs:
                self._log(job, rs)
        if status in ("done", "missed"):
            job["parts"] = {} if status == "done" else job.get("parts", {})
            params = job.get("params") or {}
            for k in TEXT_PARAMS:
                if k in params and status == "done":
                    params[k] = "(removed after the job finished)"
        write(job)

    def _deliver(self, job: dict[str, Any], rs: RunState, result: dict[str, Any]) -> dict[str, Any]:
        prep = rs.prep
        ext = result.pop("_file_ext", ".json")
        quality = {"truncated_parts": sum(1 for o in rs.outs.values() if o.get("truncated")),
                   "schema_errors": [e for o in rs.outs.values() for e in o.get("schema_errors", [])][:5],
                   "fixups": rs.repairs or None, "answered_without_reasoning": rs.fallbacks or None}
        meta = {"task": job.get("task"), "level": rs.mode,
                "kind": "DRAFT - review before use" if job.get("task") == "draft" else "local model output",
                "notes": (prep.notes + job.get("notes", [])) or None, "secrets_redacted": prep.redactions or None,
                "quality": {k: v for k, v in quality.items() if v}}
        meta = {k: v for k, v in meta.items() if v}
        if job.get("return_mode") != "file_only":
            return {**meta, **result}
        name = f"{time.strftime('%Y%m%d-%H%M%S')}_{job.get('task')}_{job['job_id'][-4:]}{ext}"
        out_path = Path(prep.params["out_path"]) if prep.params.get("out_path") else config.sub_dir("outbox") / name
        if ext == ".txt" and "translation" in result:
            body = result["translation"]
        elif ext == ".md" and ("draft" in result or "answer" in result):
            body = str(result.get("draft") or result.get("answer"))
        else:
            body = json.dumps(result, ensure_ascii=False, indent=2)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body, encoding="utf-8")
        job["saved_to"] = str(out_path)
        return {**meta, "saved_to": str(out_path), "chars": len(body),
                "note": "kept on this computer only; open the file to read it"}

    def _log(self, job: dict[str, Any], rs: RunState) -> None:
        t, est = rs.totals, job.get("estimate") or {}
        logbook.append({"type": "job", "job_id": job["job_id"], "task": job.get("task"), "model": rs.model,
                        "mode": rs.mode, "when": job.get("when"), "status": job.get("status"), "ctx": rs.ctx,
                        "calls": len(rs.prep.calls), "input_chars": rs.prep.raw_chars,
                        "prompt_tokens": t["prompt_tokens"], "cached_tokens": t["cached_tokens"],
                        "output_tokens": t["output_tokens"], "think_tokens": t["think_tokens"],
                        "pred_think_tokens": est.get("pred_think_tokens"),
                        "load_s": round(t["load_s"], 2), "prefill_s": round(t["prefill_s"], 2),
                        "decode_s": round(t["decode_s"], 2), "wall_s": round(t["busy_s"], 2),
                        "waited_s": round(float(job.get("started") or job["created"]) - float(job["created"]), 1),
                        "pred_prefill_s": est.get("prefill_s"), "pred_decode_s": est.get("decode_s"),
                        "est_p50_s": est.get("p50_s"), "est_p90_s": est.get("p90_s"),
                        "repair_calls": len(rs.repairs), "repairs_kept": sum(1 for r in rs.repairs if r["kept"] == "fixed version"),
                        "fallbacks": len(rs.fallbacks),
                        "claude_tokens_saved_est": est.get("claude_tokens_saved_est"),
                        "stop_reason": job.get("error")})

    def _phase(self, job: dict[str, Any], text: str, force: bool = False) -> None:
        job["phase"] = text
        now = time.time()
        if not force and now - self._last_phase_write.get(job["job_id"], 0) < 2.0:
            return
        self._last_phase_write[job["job_id"]] = now
        write(job)

    # ------------------------------------------------------------ calibration
    def _calibrate(self, job: dict[str, Any], cfg: dict[str, Any]) -> None:
        cancel = threading.Event()
        done = threading.Event()
        flag = cancel_path(job["job_id"])

        def watch() -> None:
            while not done.wait(0.5):
                if flag.exists():
                    cancel.set()
                    return

        threading.Thread(target=watch, daemon=True).start()
        job.update({"status": "running", "started": time.time()})
        write(job)

        def say(s: str) -> None:
            self._phase(job, f"calibrating: {s}", force=True)

        try:
            res = calibrate.Calibrator(cfg, job.get("model"), budget_s=float(job.get("budget_s") or 720),
                                       progress=say, cancel=cancel).run()
            job.update({"status": "done", "result": res, "error": None})
        except Exception as exc:
            job.update({"status": "cancelled" if cancel.is_set() else "failed", "error": str(exc)})
        finally:
            done.set()
        job.update({"finished": time.time(), "phase": job["status"]})
        try:
            flag.unlink()
        except FileNotFoundError:
            pass
        write(job)


def _slim(out: dict[str, Any]) -> dict[str, Any]:
    return {k: out[k] for k in ("text", "json", "truncated", "schema_errors", "think_tokens", "checked", "repaired")
            if k in out}


def _version_at_least(v: str, want: tuple[int, ...]) -> bool:
    nums = [int(x) for x in re.findall(r"\d+", v or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums))) >= want


def parse_json(text: str) -> Any:
    t = re.sub(r"(?s)<think>.*?</think>", "", text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    for candidate in (t, t[t.find("{"): t.rfind("}") + 1] if "{" in t else ""):
        if candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    return None
