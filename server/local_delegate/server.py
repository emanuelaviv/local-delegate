"""The tools Claude sees, and what they do."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from . import calibrate, config, estimate, guard, jobs, logbook, packs, schedule, setup_local, sysinfo
from .ollama import Ollama, OllamaError, think_levels, think_value
from .tasks import TASKS, Prepared, Task, TaskInputError, catalog

INSTRUCTIONS = """\
local-delegate runs routine reading, extraction and drafting on the user's own computer with a local Ollama model
(default qwen3.8:27b-q4_K_M). Inputs never leave the computer; only what these tools return reaches this chat.

Protocol:
1. local_estimate(task, params) first. Pass file paths, not file contents. It returns `action`, a short list of
   `options` (Claude now, the local model at fast / careful / deep reasoning, now or tonight), each with when it
   would be ready, Claude tokens saved and redo risk, and the recommended option under the user's priority preset.
2. action "script": use script_result (exact, instant; no question). "do_here": do it yourself; mention the best
   local alternative in one line. "blocked" / "too_large": do it yourself and say why.
   "ask": ask the user with the options (AskUserQuestion: header "Local model", the `question`, the options in order,
   recommended one first). Only after they pick a local option, call
   local_run(estimate_id, option=<id>, user_approved=true). They may also say "when I step away" (when="idle") or
   "at 14:00" (when="at:14:00").
3. Jobs run in a background worker outside Claude's app: quick ones return inline; others return a job_id. Keep
   working, then call local_result(job_id) once when needed (it waits up to ~45 s). Never poll in a loop.
   Tonight's jobs start in the night window and finish by morning; the computer is kept awake meanwhile.
4. Model output is a draft or an extraction. Numbers and file lists come from scripts; quotes are checked against
   the files (citation_check). Verdicts, approvals and anything sent in the user's name stay with Claude and the user.
5. Never pass secrets, other people's personal data or sensitive personal details. After using a result, record
   local_feedback(job_id, accepted|edited|redone): it is what the redo-risk figures learn from.
"""


class Service:
    def __init__(self) -> None:
        self.estimates: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        try:
            jobs.note_server()  # a worker left running by an older version hands over to this one
        except Exception:
            pass
        try:
            if any(j.get("status") in jobs.OPEN for j in jobs.all_jobs()):
                jobs.ensure_worker()  # pick up jobs left waiting (e.g. tonight's queue) after a restart
        except Exception:
            pass

    # ------------------------------------------------------------ helpers
    def _client(self, cfg: dict[str, Any]) -> Ollama:
        return Ollama(cfg["ollama_url"])

    def _profile(self, cfg: dict[str, Any]) -> dict[str, Any]:
        prof = calibrate.load_profile()
        for model, pm in (prof.get("models") or {}).items():
            adj = logbook.adjustments(model)
            if adj:
                pm["adjust"] = {"prefill": adj.get("prefill", 1.0), "decode": adj.get("decode", 1.0)}
        return prof

    def _state(self, cfg: dict[str, Any], client: Ollama | None) -> dict[str, Any]:
        q = jobs.queue_state()
        st: dict[str, Any] = {"memory": sysinfo.memory(), "battery": sysinfo.battery(),
                              "queue_eta_s": q["queue_eta_s"], "tonight_queue_s": q["tonight_queue_s"]}
        if client is not None:
            try:
                ps = client.ps()
                mine = [m for m in ps if m.get("name") == cfg["model"] or m.get("model") == cfg["model"]]
                if mine:
                    st["loaded"] = {"model": cfg["model"], "context_length": mine[0].get("context_length"),
                                    "size": mine[0].get("size"), "size_vram": mine[0].get("size_vram")}
            except OllamaError:
                pass
        return st

    def _prepare(self, name: str, params: dict[str, Any]) -> tuple[Task, Prepared, dict[str, Any], dict[str, Any]]:
        task = TASKS.get(name)
        if task is None:
            raise TaskInputError(f"unknown task '{name}'. Tasks: {', '.join(TASKS)}")
        cfg = config.load()
        profile = self._profile(cfg)
        prep = task.prepare(params or {}, cfg, profile)
        return task, prep, cfg, profile

    # ------------------------------------------------------------ status
    def local_status(self, refresh: bool = False) -> dict[str, Any]:
        cfg = config.load(refresh=refresh)
        now = time.time()
        out: dict[str, Any] = {"connector": f"local-delegate {config.VERSION}"}
        try:
            client = self._client(cfg)
        except OllamaError as exc:
            return {**out, "ready": False, "problems": [str(exc)]}
        rep = guard.report(cfg, client, refresh=refresh)
        state = self._state(cfg, client)
        prof = calibrate.load_profile()
        mem = state["memory"]
        loaded = state.get("loaded")
        cal = calibrate.summarize(prof, cfg["model"])
        levels: dict[str, Any] = {}
        try:
            show = client.show(cfg["model"])
            levels = {"model_offers": think_levels(show),
                      "used": {m: think_value(cfg["modes"][m].get("think"), show) for m in ("fast", "careful", "deep")},
                      "budgets": {m: cfg["modes"][m].get("think_budget") for m in ("careful", "deep")},
                      "deep_tonight_budget": cfg["modes"]["deep"].get("night_think_budget")}
        except OllamaError:
            pass
        setup_needed = []
        if not rep["ok"]:
            setup_needed.append("local-only setup: local_admin(action='setup_local_only') - shows the changes first")
        if not cal.get("calibrated"):
            setup_needed.append("calibration: local_admin(action='calibrate') - a few minutes on this computer")
        pname, pwhy = schedule.active_priority(cfg, now)
        nb = schedule.night_bounds(now, cfg.get("night_window") or {})
        q = jobs.queue_state(now)
        if q["open"] and not jobs.worker_alive():
            jobs.ensure_worker()
        out.update({
            "ready": rep["ok"] and bool(cal.get("calibrated")),
            "local_only": {"ok": rep["ok"], "ollama_version": rep.get("ollama_version"),
                           "passing": [c["name"] for c in rep["checks"] if c["ok"] is True],
                           "problems": rep["problems"]},
            "model": cfg["model"],
            "model_loaded": None if not loaded else {
                "ctx": loaded.get("context_length"), "ram_gb": round((loaded.get("size") or 0) / 1e9, 1),
                "on_gpu_share": round((loaded.get("size_vram") or 0) / max(1, loaded.get("size") or 1), 2)},
            "machine": {"ram_total_gb": round((mem.get("total") or 0) / 1e9, 1),
                        "ram_free_gb": round((mem.get("available") or 0) / 1e9, 1),
                        "mains_power": sysinfo.on_mains_power()},
            "calibration": cal,
            "reasoning_levels": levels or None,
            "priority": {"active": pname, "why": pwhy, "default": cfg.get("priority"),
                         "schedule": cfg.get("priority_schedule") or None,
                         "presets": {k: v.get("about") for k, v in cfg["priorities"].items()}},
            "night_window": {**(cfg.get("night_window") or {}),
                             "next": f"{schedule.fmt_clock(nb[0], now)} to {schedule.fmt_clock(nb[1], now)}",
                             "inside_now": nb[2]},
            "worker": jobs.worker_info(),
            "queue": {"open_jobs": q["open"], "now_wait_s": round(q["queue_eta_s"]),
                      "tonight_booked_min": round(q["tonight_queue_s"] / 60), "recent": jobs.recent(5)},
            "packs": packs.available(),
            "setup_needed": setup_needed,
        })
        if cfg.get("_config_error"):
            out["config_warning"] = cfg["_config_error"]
        return out

    # ------------------------------------------------------------ estimate
    def local_estimate(self, task: str, params: dict[str, Any] | None = None, claude_context_tokens: int | None = None,
                       return_mode: str | None = None, include_script_result: bool | None = None,
                       prefer_local: bool = False, need_by: str | None = None, priority: str | None = None) -> dict[str, Any]:
        t, prep, cfg, profile = self._prepare(task, params or {})
        try:
            client = self._client(cfg)
            rep = guard.report(cfg, client)
        except OllamaError as exc:
            client, rep = None, {"ok": False, "problems": [str(exc)]}
        state = self._state(cfg, client)
        if return_mode:
            state["file_only"] = return_mode == "file_only"
        if prefer_local and not return_mode and t.name in ("doc_summary", "translate", "draft", "freeform"):
            state["file_only"] = True
        now = time.time()
        try:
            need_ts = schedule.parse_deadline(need_by, now) if need_by else None
        except (ValueError, schedule.WhenError) as exc:
            return {"error": f"need_by: {exc}"}
        est = estimate.estimate(prep, t, cfg, profile, state, guard_ok=rep["ok"],
                                claude_context_tokens=claude_context_tokens, prefer_local=bool(prefer_local),
                                need_by=need_ts, priority=priority, now_ts=now)
        eid = uuid.uuid4().hex[:8]
        give_script = prep.script_result is not None and (
            est["action"] == "script" or bool(include_script_result) or
            (include_script_result is None and t.script_substitutes))
        with self._lock:
            self._expire()
            self.estimates[eid] = {"at": now, "task": t, "prep": prep, "est": est,
                                   "return_mode": return_mode or ("file_only" if (prep.file_only_default or
                                                                                  state.get("file_only")) else "content"),
                                   "script_delivered": give_script}
        out: dict[str, Any] = {"estimate_id": eid, "task": task}
        out.update({k: v for k, v in est.items() if v is not None and not k.startswith("_")})
        out["inputs"] = [{"path": d["path"], "chars": d["chars"]} for d in prep.doc_meta][:20]
        if prep.notes:
            out["input_notes"] = prep.notes[:10]
        if prep.redactions:
            out["secrets_redacted_before_model"] = prep.redactions
        if not rep["ok"]:
            out["local_only_problems"] = rep.get("problems")
        if give_script:
            out["script_result"] = prep.script_result
        a = est["action"]
        out["next"] = {
            "script": "Use script_result; no question needed.",
            "ask": "Ask the user with these options (recommended first); then local_run(estimate_id, option=<id>, "
                   "user_approved=true). If they pick 'claude', do it here.",
            "do_here": "Do it here. In one line, mention the local alternative: " + str(est.get("_best_local") or "none"),
            "blocked": "Do it here; the local-only checks are failing (see local_only_problems).",
            "too_large": "Do it here, or split the input and estimate the parts.",
        }.get(a)
        return out

    def _expire(self) -> None:
        cutoff = time.time() - 3600
        for k in [k for k, v in self.estimates.items() if v["at"] < cutoff]:
            del self.estimates[k]

    # ------------------------------------------------------------ run
    def local_run(self, estimate_id: str | None = None, option: str | None = None, mode: str | None = None,
                  when: str | None = None, deadline: str | None = None, job_id: str | None = None,
                  task: str | None = None, params: dict[str, Any] | None = None, user_approved: bool = False,
                  max_minutes: float | None = None, wait_s: float = 40, return_mode: str | None = None,
                  yield_to_user: bool | None = None) -> dict[str, Any]:
        cfg = config.load()
        now = time.time()
        if job_id:
            return self._resume(job_id, when, deadline, mode, max_minutes, user_approved, cfg)
        stored = self.estimates.get(estimate_id or "") if estimate_id else None
        if estimate_id and stored is None:
            return {"error": "estimate expired or unknown; call local_estimate again"}
        if stored is None:
            if not task:
                return {"error": "give estimate_id (from local_estimate), or task and params"}
            est_out = self.local_estimate(task, params, return_mode=return_mode)
            if "estimate_id" not in est_out:
                return est_out
            stored = self.estimates[est_out["estimate_id"]]
        t, prep, est = stored["task"], stored["prep"], stored["est"]
        if option == "script" or mode == "script":
            if prep.script_result is None:
                return {"error": f"task {t.name} has no script-only result"}
            return {"task": t.name, "option": "script", "result": prep.script_result}
        if option == "claude":
            return {"note": "The user chose Claude: do the task here."}
        if not prep.calls:
            return {"task": t.name, "option": "script", "result": prep.script_result, "note": "this task needs no model"}
        if est["action"] == "blocked":
            return {"error": "the local-only checks are failing", "problems": est.get("local_only_problems")}
        if option and "_" in option:
            o_mode, o_when = option.split("_", 1)
        else:
            o_mode, o_when = (est.get("recommended") or "fast_now").split("_", 1) if "_" in (est.get("recommended") or "") \
                else ("fast", "now")
        mode = mode or o_mode
        when = when or o_when
        if mode not in estimate.MODES:
            return {"error": f"mode must be one of {', '.join(estimate.MODES)}"}
        try:
            w = schedule.parse_when(when, now, cfg)
        except schedule.WhenError as exc:
            return {"error": str(exc)}
        run = est["_runs"].get(f"{mode}_{'tonight' if w['when'] == 'tonight' else 'now'}") or \
            est["_runs"].get(f"{mode}_now") or est["_runs"].get(f"{mode}_tonight")
        if run is None:
            return {"error": "this input is too large for any context size; split it", "not_possible": est.get("not_possible")}
        if not user_approved:
            return {"error": "approval needed: ask the user first with the estimate's options, then call local_run again "
                             "with user_approved=true", "question": est.get("question"),
                    "options": est.get("options")}
        try:
            dl = schedule.parse_deadline(deadline, now, w.get("not_before")) if deadline else None
        except (ValueError, schedule.WhenError) as exc:
            return {"error": f"deadline: {exc}"}
        if w["when"] == "tonight":
            dl = min(dl or w["window_end"], w["window_end"])
            budget = float(max_minutes) * 60 if max_minutes else None
        else:
            budget = float(max_minutes) * 60 if max_minutes else max(float(run["p90_s"]) * 1.5, float(run["p50_s"]) + 90)
        not_before = w.get("not_before")
        with self._lock:
            self.estimates.pop(estimate_id or "", None)
        job = jobs.task_job(t.name, prep.params, run, return_mode or stored["return_mode"], budget,
                            bool(yield_to_user if yield_to_user is not None else w["when"] == "idle"), dl,
                            not_before, w["when"], mode=mode,
                            extra={"script_delivered": bool(stored.get("script_delivered"))})
        if w["when"] != "now":
            return self._present(jobs.read(job["job_id"]) or job, scheduled=True)
        snap = jobs.wait(job["job_id"], min(max(0.0, float(wait_s)), 50.0))
        return self._present(snap or job)

    def _resume(self, job_id: str, when: str | None, deadline: str | None, mode: str | None,
                max_minutes: float | None, user_approved: bool, cfg: dict[str, Any]) -> dict[str, Any]:
        job = jobs.read(job_id)
        if not job:
            return {"error": f"no job {job_id}"}
        if job.get("status") not in ("partial", "aborted", "cancelled", "failed", "missed"):
            return {"error": f"job {job_id} is {job.get('status')}; only stopped jobs can continue"}
        if not user_approved:
            return {"error": "approval needed: ask the user before continuing this job"}
        now = time.time()
        try:
            w = schedule.parse_when(when or "now", now, cfg)
            dl = schedule.parse_deadline(deadline, now, w.get("not_before")) if deadline else None
        except (ValueError, schedule.WhenError) as exc:
            return {"error": str(exc)}
        est = job.get("estimate") or {}
        if w["when"] == "tonight":
            dl, budget = min(dl or w["window_end"], w["window_end"]), None
        else:
            budget = float(max_minutes) * 60 if max_minutes else max(float(est.get("p90_s") or 600) * 1.5, 300)
        j = jobs.resume(job_id, w, budget, dl, mode)
        return self._present(j, scheduled=w["when"] != "now")

    def _present(self, job: dict[str, Any] | None, scheduled: bool = False) -> dict[str, Any]:
        if job is None:
            return {"error": "job not found"}
        snap = jobs.public(job) or {}
        if job.get("script_delivered") and isinstance(snap.get("result"), dict) and "facts" in snap["result"]:
            snap["result"]["facts"] = "(same as script_result in the estimate)"
        st = snap.get("status")
        if st in ("queued", "scheduled", "waiting", "running"):
            if job.get("when") == "tonight":
                snap["next"] = ("Queued for tonight. The computer stays awake until the queue is done (the screen can "
                                "turn off); Claude's app may be closed. Collect it tomorrow with local_result(job_id) or "
                                "local_admin(action='jobs'), or schedule one check-in for the ready time.")
            elif scheduled:
                snap["next"] = "Scheduled. Call local_result(job_id) once after it should be ready."
            else:
                snap["next"] = ("Running in the background. Keep working on other parts; call local_result(job_id) once "
                                f"around {snap.get('ready_about', 'the expected time')}.")
        elif st == "done":
            snap["next"] = "Check citation_check and quality; then record local_feedback(job_id, outcome)."
        elif st in ("partial", "aborted", "failed", "missed", "cancelled") and snap.get("resumable"):
            snap["next"] = "Offer to continue it (local_run(job_id=..., when='tonight' or 'now', user_approved=true))."
        return snap

    def local_result(self, job_id: str, wait_s: float = 40, cancel: threading.Event | None = None) -> dict[str, Any]:
        job = jobs.wait(job_id, min(max(0.0, float(wait_s)), 50.0), cancel=cancel)
        if job is None:
            return {"error": f"no job {job_id}"}
        return self._present(job)

    def local_feedback(self, job_id: str, outcome: str, note: str | None = None) -> dict[str, Any]:
        if outcome not in ("accepted", "edited", "redone", "rejected"):
            return {"error": "outcome must be accepted, edited, redone or rejected"}
        logbook.feedback(job_id, outcome, note)
        return {"recorded": True}

    # ------------------------------------------------------------ admin
    def local_admin(self, action: str, user_approved: bool = False, budget_minutes: float | None = None,
                    context_length: int | None = None, path: str | None = None, job_id: str | None = None,
                    model: str | None = None, days: float = 30, priority: str | None = None,
                    until: str | None = None, schedule_rules: list[dict[str, str]] | None = None,
                    start: str | None = None, end: str | None = None) -> dict[str, Any]:
        cfg = config.load()
        now = time.time()
        if action == "tasks":
            return {"tasks": catalog(), "packs": packs.available()}
        if action == "log_summary":
            return logbook.summary(days)
        if action in ("jobs", "queue"):
            q = jobs.queue_state(now)
            return {"jobs": jobs.recent(12), "open_jobs": q["open"], "tonight_booked_min": round(q["tonight_queue_s"] / 60),
                    "worker": jobs.worker_info()}
        if action == "worker_check":
            return jobs.check_worker()
        if action == "cancel":
            j = jobs.request_cancel(job_id or "")
            return jobs.public(j, with_result=False) if j else {"error": "unknown job"}
        if action == "unload":
            try:
                self._client(cfg).unload(cfg["model"])
                return {"unloaded": cfg["model"], "ram_free_gb": round((sysinfo.memory().get("available") or 0) / 1e9, 1)}
            except OllamaError as exc:
                return {"error": str(exc)}
        if action == "setup_local_only":
            changes = setup_local.plan(cfg, context_length)
            if not changes:
                guard.clear_cache()
                return {"nothing_to_change": True, "local_only_checks": guard.report(cfg, refresh=True)}
            if not user_approved:
                return {"needs_approval": True, "changes": [c["what"] for c in changes],
                        "ask": "Show the user these changes; call again with user_approved=true if they agree."}
            return setup_local.apply(cfg, context_length)
        if action == "calibrate":
            m = model or cfg["model"]
            minutes = float(budget_minutes or 12)
            if not user_approved:
                return {"needs_approval": True, "about": f"Measures {m} on this computer: a few minutes, capped at "
                                                         f"{minutes:.0f} min. The model runs at full load meanwhile."}
            rep = guard.report(cfg, refresh=True)
            if not rep["ok"]:
                return {"error": "fix the local-only checks first", "problems": rep["problems"]}
            job = jobs.calibration_job(m, minutes * 60)
            return {"job_id": job["job_id"], "status": "started", "next": "call local_result(job_id) in a few minutes"}
        if action == "set_priority":
            presets = cfg["priorities"]
            if schedule_rules is not None:
                for r_ in schedule_rules:
                    if r_.get("priority") not in presets:
                        return {"error": f"unknown preset {r_.get('priority')!r}; presets: {', '.join(presets)}"}
                    try:
                        schedule.parse_hhmm(r_["from"]), schedule.parse_hhmm(r_["to"])
                    except (KeyError, schedule.WhenError) as exc:
                        return {"error": f"schedule entry {r_}: {exc}"}
                config.save_user_setting("priority_schedule", schedule_rules)
            if priority:
                if priority not in presets:
                    return {"error": f"unknown preset {priority!r}; presets: {', '.join(presets)}"}
                if until:
                    try:
                        ts = schedule.parse_deadline(until, now)
                    except (ValueError, schedule.WhenError) as exc:
                        return {"error": f"until: {exc}"}
                    config.save_user_setting("priority_override", {"name": priority, "until": ts})
                else:
                    config.save_user_setting("priority", priority)
                    config.save_user_setting("priority_override", None)
            cfg = config.load(refresh=True)
            name, why = schedule.active_priority(cfg, now)
            return {"active": name, "why": why, "about": cfg["priorities"][name].get("about"),
                    "default": cfg.get("priority"), "schedule": cfg.get("priority_schedule") or None}
        if action == "night_window":
            if start or end:
                try:
                    win = {"start": start or cfg["night_window"]["start"], "end": end or cfg["night_window"]["end"]}
                    schedule.parse_hhmm(win["start"]), schedule.parse_hhmm(win["end"])
                except schedule.WhenError as exc:
                    return {"error": str(exc)}
                config.save_user_setting("night_window", win)
                cfg = config.load(refresh=True)
            nb = schedule.night_bounds(now, cfg["night_window"])
            return {"night_window": cfg["night_window"],
                    "next": f"{schedule.fmt_clock(nb[0], now)} to {schedule.fmt_clock(nb[1], now)}"}
        if action == "allow_root":
            if not path:
                return {"error": "give path"}
            if not user_approved:
                return {"needs_approval": True, "change": f"allow the connector to read files under {path}"}
            roots = list(cfg.get("allowed_roots") or ["~"])
            if path not in roots:
                roots.append(path)
            config.save_user_setting("allowed_roots", roots)
            return {"allowed_roots": roots}
        if action == "set_model":
            if not model:
                return {"error": "give model"}
            if guard.model_is_cloud_name(model):
                return {"error": f"{model} is a cloud model; only local models are allowed"}
            if not user_approved:
                return {"needs_approval": True, "change": f"use {model} for local jobs (recalibrate afterwards)"}
            config.save_user_setting("model", model)
            guard.clear_cache()
            return {"model": model, "next": "local_admin(action='calibrate')"}
        return {"error": f"unknown action '{action}'"}


# ===================================================================== tool specs (MCP)

_PARAMS = {"type": "object", "description": "Task parameters; see local_admin(action='tasks') or the skill's task list."}
_WHEN = ("now | idle (start once nobody has used the computer for a while) | tonight (the night window) | "
         "at:HH:MM")

TOOLS: list[dict[str, Any]] = [
    {"name": "local_status",
     "description": "Readiness of the local model: local-only checks, RAM, calibration, reasoning levels, the active "
                    "priority preset, tonight's window and queue, and the background worker. Call once per session.",
     "inputSchema": {"type": "object", "properties": {"refresh": {"type": "boolean", "default": False}}},
     "annotations": {"title": "Local model status", "readOnlyHint": True, "openWorldHint": False}},
    {"name": "local_estimate",
     "description": "Prepare a task on this computer and price the ways to do it: Claude now, the exact script result, "
                    "and the local model at fast / careful / deep reasoning, now or tonight; each with ready time, "
                    "Claude tokens saved and redo risk. Returns `action` (script | ask | do_here | blocked | too_large), "
                    "the recommended option under the active priority preset, and up to 4 options to show. Script "
                    "results come back immediately. Pass paths, never file contents. Tasks: " + ", ".join(TASKS) + ".",
     "inputSchema": {"type": "object", "required": ["task"], "properties": {
         "task": {"type": "string", "enum": list(TASKS)},
         "params": _PARAMS,
         "claude_context_tokens": {"type": "integer", "description": "Rough size of this conversation so far; improves the savings estimate."},
         "return_mode": {"type": "string", "enum": ["content", "file_only"], "description": "file_only keeps the result in a local file; Claude sees only the path."},
         "include_script_result": {"type": "boolean"},
         "prefer_local": {"type": "boolean", "description": "The user wants this kept on their computer (privacy): local options only, even without token savings."},
         "need_by": {"type": "string", "description": "When the user needs the result: 'HH:MM' or '20 min'. Later options are dropped."},
         "priority": {"type": "string", "enum": ["rush", "balanced", "saver", "night"], "description": "Override the active preset for this job."}}},
     "annotations": {"title": "Estimate a local job", "readOnlyHint": True, "openWorldHint": False}},
    {"name": "local_run",
     "description": "Run an estimated job. option='script' returns the exact script result (no approval needed). Any "
                    "local option REQUIRES user_approved=true, set only after the user chose it in chat. option is an id from "
                    "the estimate (e.g. careful_now, deep_tonight); mode and when override it (when: " + _WHEN + "). "
                    "Quick jobs return inline (waits up to wait_s, max 50); others return a job_id. job_id + when "
                    "continues a stopped job from its finished parts.",
     "inputSchema": {"type": "object", "properties": {
         "estimate_id": {"type": "string"}, "option": {"type": "string"},
         "mode": {"type": "string", "enum": ["fast", "careful", "deep", "script"]},
         "when": {"type": "string", "description": _WHEN},
         "deadline": {"type": "string", "description": "Stop by this time ('HH:MM'); finished parts are kept."},
         "job_id": {"type": "string", "description": "Continue a stopped job."},
         "task": {"type": "string"}, "params": _PARAMS,
         "user_approved": {"type": "boolean", "default": False},
         "max_minutes": {"type": "number", "description": "Hard time budget for a job run now; default 1.5x the slow estimate."},
         "wait_s": {"type": "number", "default": 40},
         "return_mode": {"type": "string", "enum": ["content", "file_only"]},
         "yield_to_user": {"type": "boolean", "description": "Pause between parts while the user is at the computer (default for 'idle')."}}},
     "annotations": {"title": "Run a local job", "readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}},
    {"name": "local_result",
     "description": "Status or result of a local job. Waits up to wait_s (max 50) for it to finish. Call it once when "
                    "you need the result, not in a loop.",
     "inputSchema": {"type": "object", "required": ["job_id"], "properties": {
         "job_id": {"type": "string"}, "wait_s": {"type": "number", "default": 40}}},
     "annotations": {"title": "Local job result", "readOnlyHint": True, "openWorldHint": False}},
    {"name": "local_feedback",
     "description": "Record whether a local result was accepted, edited, redone or rejected. The redo-risk figures in "
                    "estimates learn from this.",
     "inputSchema": {"type": "object", "required": ["job_id", "outcome"], "properties": {
         "job_id": {"type": "string"},
         "outcome": {"type": "string", "enum": ["accepted", "edited", "redone", "rejected"]},
         "note": {"type": "string"}}},
     "annotations": {"title": "Local job feedback", "readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}},
    {"name": "local_admin",
     "description": "Setup and housekeeping. Actions: setup_local_only (returns planned changes unless user_approved), "
                    "calibrate (needs user_approved; runs as a job), set_priority (priority preset rush | balanced | "
                    "saver | night, optional until 'HH:MM', or schedule_rules [{from, to, priority}]), night_window "
                    "(start, end 'HH:MM'), jobs (recent jobs and tonight's queue), cancel (job_id), unload (free RAM "
                    "now), worker_check (starts the background worker without a job or the model and reports whether "
                    "it will outlive Claude's app), tasks, log_summary, allow_root (path; needs user_approved), "
                    "set_model (needs user_approved).",
     "inputSchema": {"type": "object", "required": ["action"], "properties": {
         "action": {"type": "string", "enum": ["setup_local_only", "calibrate", "set_priority", "night_window", "jobs",
                                               "cancel", "unload", "worker_check", "tasks", "log_summary", "allow_root",
                                               "set_model"]},
         "user_approved": {"type": "boolean", "default": False},
         "priority": {"type": "string", "enum": ["rush", "balanced", "saver", "night"]},
         "until": {"type": "string"}, "schedule_rules": {"type": "array", "items": {"type": "object"}},
         "start": {"type": "string"}, "end": {"type": "string"},
         "budget_minutes": {"type": "number"}, "context_length": {"type": "integer"},
         "path": {"type": "string"}, "job_id": {"type": "string"}, "model": {"type": "string"},
         "days": {"type": "number"}}},
     "annotations": {"title": "Local model admin", "readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}},
]


def make_handlers(svc: Service) -> dict[str, Any]:
    def wrap(fn: Any, *allowed: str) -> Any:
        def call(args: dict[str, Any], cancel: threading.Event | None = None) -> Any:
            kwargs = {k: v for k, v in (args or {}).items() if k in allowed}
            if fn is svc.local_result:
                kwargs["cancel"] = cancel
            return fn(**kwargs)
        return call

    return {
        "local_status": wrap(svc.local_status, "refresh"),
        "local_estimate": wrap(svc.local_estimate, "task", "params", "claude_context_tokens", "return_mode",
                               "include_script_result", "prefer_local", "need_by", "priority"),
        "local_run": wrap(svc.local_run, "estimate_id", "option", "mode", "when", "deadline", "job_id", "task", "params",
                          "user_approved", "max_minutes", "wait_s", "return_mode", "yield_to_user"),
        "local_result": wrap(svc.local_result, "job_id", "wait_s"),
        "local_feedback": wrap(svc.local_feedback, "job_id", "outcome", "note"),
        "local_admin": wrap(svc.local_admin, "action", "user_approved", "budget_minutes", "context_length", "path",
                            "job_id", "model", "days", "priority", "until", "schedule_rules", "start", "end"),
    }
