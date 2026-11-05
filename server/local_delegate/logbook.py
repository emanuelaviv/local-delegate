"""Local log of delegated jobs (metadata only, never the text) and what it teaches.

Each finished job appends one line to <data dir>/log.jsonl; feedback (accepted, edited, redone,
rejected) is appended later. The log drives:
  * speed corrections: if jobs run slower or faster than estimated, the estimator scales its
    reading (prefill) and writing (decode) times by the median ratio of recent jobs;
  * reasoning length: how many reasoning tokens each task actually uses at each level;
  * how often each task needs fixing or redoing at each level (the "redo risk" shown with
    every option), estimated with a small Bayesian model: fixed starting guesses per level,
    pulled towards your own feedback as it accumulates (first across all tasks for that level,
    then for the task itself);
  * a summary of real savings, and which tasks are not worth delegating.
"""

from __future__ import annotations

import json
import statistics
import time
from typing import Any

from . import config

MODES = ("fast", "careful", "deep")
# Starting guesses (accepted, edited, redone) per level, worth STRENGTH observations each.
PRIOR = {"fast": (0.60, 0.25, 0.15), "careful": (0.70, 0.22, 0.08), "deep": (0.75, 0.20, 0.05)}
STRENGTH = 6.0


def path():
    return config.data_dir() / "log.jsonl"


def append(entry: dict[str, Any]) -> None:
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    with open(path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def feedback(job_id: str, outcome: str, note: str | None = None) -> None:
    append({"type": "feedback", "job_id": job_id, "outcome": outcome, **({"note": note[:300]} if note else {})})


def entries(days: float | None = None) -> list[dict[str, Any]]:
    p = path()
    if not p.exists():
        return []
    cutoff = time.time() - days * 86400 if days else None
    out = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff:
                try:
                    if time.mktime(time.strptime(e["ts"], "%Y-%m-%dT%H:%M:%S")) < cutoff:
                        continue
                except (KeyError, ValueError):
                    continue
            out.append(e)
    return out


def _jobs(es: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [e for e in (es if es is not None else entries()) if e.get("type") == "job"]


def adjustments(model: str, n: int = 20) -> dict[str, float]:
    """Median actual/predicted ratios of reading and writing time for the last `n` finished jobs."""
    runs = [e for e in _jobs() if e.get("model") == model and e.get("status") in ("done", "partial")][-n:]
    out = {}
    for part in ("prefill", "decode"):
        ratios = []
        for e in runs:
            pred, act = e.get(f"pred_{part}_s"), e.get(f"{part}_s")
            if pred and act and pred > 1 and act > 0:
                ratios.append(act / pred)
        if len(ratios) >= 3:
            out[part] = max(0.5, min(3.0, statistics.median(ratios)))
    return out


def think_ratio(task: str, mode: str, n: int = 20) -> float | None:
    """How much more (or less) reasoning this task really used than the estimate, at this level."""
    ratios = []
    for e in [e for e in _jobs() if e.get("task") == task and e.get("mode") == mode][-n:]:
        pred, act = e.get("pred_think_tokens"), e.get("think_tokens")
        if pred and act is not None and pred > 50:
            ratios.append(max(0.05, act / pred))
    if len(ratios) < 3:
        return None
    return max(0.25, min(4.0, statistics.median(ratios)))


def repair_rate(task: str) -> tuple[float, int]:
    """Share of deep-level answers for this task that needed the fix-up pass (prior 0.5)."""
    runs = [e for e in _jobs() if e.get("task") == task and e.get("mode") == "deep" and e.get("calls")]
    needed = sum(min(1.0, (e.get("repair_calls") or 0) / max(1, e.get("calls") or 1)) for e in runs)
    return (needed + 1.0) / (len(runs) + 2.0), len(runs)


def outcome_counts(es: list[dict[str, Any]] | None = None) -> dict[tuple[str, str], list[float]]:
    es = es if es is not None else entries()
    by_job = {e["job_id"]: e for e in _jobs(es) if e.get("job_id")}
    counts: dict[tuple[str, str], list[float]] = {}
    for e in es:
        if e.get("type") != "feedback":
            continue
        j = by_job.get(e.get("job_id"))
        if not j:
            continue
        key = (j.get("task", "?"), j.get("mode") or "fast")
        c = counts.setdefault(key, [0.0, 0.0, 0.0])
        o = e.get("outcome")
        c[0 if o == "accepted" else 1 if o == "edited" else 2] += 1
    return counts


def outcome_rates(task: str, mode: str, counts: dict[tuple[str, str], list[float]] | None = None) -> dict[str, Any]:
    """P(accepted), P(edited), P(redone) for a task at a level: the level's starting guess, updated by
    your feedback across all tasks at that level, then by the feedback for this task."""
    counts = counts if counts is not None else outcome_counts()
    prior = PRIOR.get(mode, PRIOR["fast"])
    pooled = [0.0, 0.0, 0.0]
    for (t, m), c in counts.items():
        if m == mode:
            pooled = [a + b for a, b in zip(pooled, c)]
    n_pool = sum(pooled)
    level = [(prior[i] * STRENGTH + pooled[i]) / (STRENGTH + n_pool) for i in range(3)]
    own = counts.get((task, mode), [0.0, 0.0, 0.0])
    n_own = sum(own)
    p = [(level[i] * STRENGTH + own[i]) / (STRENGTH + n_own) for i in range(3)]
    return {"accept": p[0], "edit": p[1], "redo": p[2], "n": int(n_own), "n_level": int(n_pool)}


def summary(days: float = 30) -> dict[str, Any]:
    es = entries(days)
    jobs = _jobs(es)
    fb = {e["job_id"]: e["outcome"] for e in es if e.get("type") == "feedback" and "job_id" in e}
    per: dict[str, dict[str, Any]] = {}
    for j in jobs:
        key = f"{j.get('task', '?')} / {j.get('mode') or 'fast'}"
        t = per.setdefault(key, {"runs": 0, "done": 0, "partial": 0, "aborted": 0, "failed": 0, "accepted": 0,
                                 "edited": 0, "redone": 0, "rejected": 0, "wall_s": [], "est_ratio": [],
                                 "think_tokens": [], "fixups": 0, "claude_tokens_saved_est": 0})
        t["runs"] += 1
        st = j.get("status", "done")
        if st in t:
            t[st] += 1
        o = fb.get(j.get("job_id"))
        if o in t:
            t[o] += 1
        if j.get("wall_s"):
            t["wall_s"].append(j["wall_s"])
        if j.get("wall_s") and j.get("est_p50_s"):
            t["est_ratio"].append(j["wall_s"] / max(1.0, j["est_p50_s"]))
        if j.get("think_tokens") is not None:
            t["think_tokens"].append(j["think_tokens"])
        t["fixups"] += int(j.get("repair_calls") or 0)
        if st in ("done", "partial") and o not in ("redone", "rejected"):
            t["claude_tokens_saved_est"] += int(j.get("claude_tokens_saved_est") or 0)
    advice = []
    for name, t in per.items():
        t["median_minutes"] = round(statistics.median(t["wall_s"]) / 60, 1) if t["wall_s"] else None
        t["actual_vs_estimate"] = round(statistics.median(t["est_ratio"]), 2) if t["est_ratio"] else None
        t["median_reasoning_tokens"] = int(statistics.median(t["think_tokens"])) if t["think_tokens"] else None
        del t["wall_s"], t["est_ratio"], t["think_tokens"]
        rated = t["accepted"] + t["edited"] + t["redone"] + t["rejected"]
        if rated >= 5 and (t["redone"] + t["rejected"]) / rated > 0.3:
            advice.append(f"{name}: redone or rejected in more than 30% of rated runs; use a deeper level or keep it on Claude")
        if t["actual_vs_estimate"] and not 0.67 <= t["actual_vs_estimate"] <= 1.5 and t["runs"] >= 5:
            advice.append(f"{name}: real time is {t['actual_vs_estimate']}x the estimate; recalibrate")
    return {"days": days, "jobs": len(jobs), "per_task_and_level": per, "advice": advice,
            "claude_tokens_saved_est": sum(t["claude_tokens_saved_est"] for t in per.values())}
