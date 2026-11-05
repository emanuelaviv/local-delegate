"""Options, times and tokens for one job, and which option to recommend.

For a job it prices every way of doing it:
  * Claude, in this chat;
  * the exact script result alone (when that is enough);
  * the local model at three levels -- fast (answers directly), careful (reasons briefly first),
    deep (reasons longer, then a fix-up pass on whatever the automatic checks flag) -- either now
    or tonight.

Local time = load (if needed) + reading the input (prefill) + writing (decode) of the reasoning
and the answer, from this computer's calibrated rates:
    prefill(n uncached tokens after p cached) = a*n + b*((p+n)^2 - p^2)
    decode(k tokens at context c)            = k * (g + h*(c + k/2))
Reasoning length per answer starts from a per-level guess scaled by the task and input size,
then follows what your jobs really used (logbook.think_ratio), capped by the level's budget.

Tokens: what Claude would spend here (reading + writing + tool steps, and the read text carried
through later steps), minus what delegating costs (the steps to estimate, ask and collect, and
the result Claude reads). Expected saving weighs that by how often results at that level get
accepted, edited or redone (logbook.outcome_rates).

Recommendation: the active priority preset puts a price on waiting (per minute until ready),
on getting the result tomorrow instead of today, and a bonus on deeper reasoning. The option
with the highest (expected tokens saved + quality bonus - waiting cost) is recommended.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from . import logbook, schedule, textio
from .tasks import Prepared, Task

INLINE_WAIT_S = 45.0
MODES = ("fast", "careful", "deep")
RANK = {"fast": 0, "careful": 1, "deep": 2, "script": 1, "claude": 3}
QUALITY_WORDS = {"fast": "answers directly", "careful": "reasons briefly first",
                 "deep": "reasons longer, then fixes what the checks flag"}


def rates_for(model: str, profile: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    prof = ((profile or {}).get("models") or {}).get(model)
    fb = cfg["fallback_rates"]
    if prof:
        mem = prof.get("memory", {})
        return {
            "calibrated": True, "model": model,
            "load_s": float(prof.get("load_s", fb["load_s"])), "reload_s": float(prof.get("reload_s", fb["reload_s"])),
            "pp_a": float(prof["prefill"]["a"]), "pp_b": float(prof["prefill"].get("b", 0.0)),
            "tg_g": float(prof["decode"]["g"]), "tg_h": float(prof["decode"].get("h", 0.0)),
            "base_bytes": float(mem.get("base_bytes", fb["model_bytes"])),
            "kv": float(mem.get("kv_bytes_per_token", fb["kv_bytes_per_token"])),
            "vram_frac": float(mem.get("vram_fraction", 0.0)),
            "prefix_cache": float(prof.get("prefix_cache_ratio", 0.0)),
            "max_ctx": int((prof.get("limits") or {}).get("model_max_ctx") or 32768),
            "default_ctx": int((prof.get("limits") or {}).get("default_ctx") or 16384),
            "power": prof.get("power_at_calibration") or {},
            "adjust": prof.get("adjust") or {"prefill": 1.0, "decode": 1.0},
        }
    return {
        "calibrated": False, "model": model,
        "load_s": float(fb["load_s"]), "reload_s": float(fb["reload_s"]),
        "pp_a": 1.0 / float(fb["prefill_tps"]), "pp_b": 0.0,
        "tg_g": 1.0 / float(fb["decode_tps"]), "tg_h": 0.0,
        "base_bytes": float(fb["model_bytes"]), "kv": float(fb["kv_bytes_per_token"]), "vram_frac": 0.0,
        "prefix_cache": 0.0, "max_ctx": 32768, "default_ctx": 16384, "power": {},
        "adjust": {"prefill": 1.0, "decode": 1.0},
    }


def call_times(r: dict[str, Any], n_prompt: int, n_cached: int, n_out: int) -> tuple[float, float]:
    n_u = max(0, n_prompt - n_cached)
    p = max(0, n_cached)
    prefill = r["pp_a"] * n_u + r["pp_b"] * ((p + n_u) ** 2 - p ** 2)
    decode = n_out * (r["tg_g"] + r["tg_h"] * (n_prompt + n_out / 2.0))
    return prefill * r["adjust"].get("prefill", 1.0), decode * r["adjust"].get("decode", 1.0)


def pick_ctx(need: int, buckets: list[int], loaded_ctx: int | None, max_ctx: int, default_ctx: int) -> int | None:
    if loaded_ctx and loaded_ctx >= need:
        return loaded_ctx
    floor = default_ctx if need <= default_ctx else 0
    for b in sorted(buckets):
        if b >= need and b >= floor and b <= max_ctx:
            return b
    return None


def mode_budget(cfg: dict[str, Any], mode: str, night: bool) -> tuple[bool, int]:
    mc = cfg["modes"][mode]
    thinking = mc.get("think") not in (None, "off", "none", False)
    if not thinking:
        return False, 0
    budget = int(mc.get("night_think_budget") or 0) if night else 0
    return True, budget or int(mc.get("think_budget") or 0)


def think_guess(cfg: dict[str, Any], task: Task, mode: str, in_tokens: int, budget: int) -> tuple[int, int]:
    """Typical and long reasoning length for one answer (tokens)."""
    base = float((cfg.get("think_prior_tokens") or {}).get(mode, 1000))
    ratio = logbook.think_ratio(task.name, mode) or 1.0
    p50 = base * task.think_factor * (1 + in_tokens / 20000.0) * ratio
    return int(min(budget, p50)), int(min(budget, p50 * 2.5))


def local_time(prep: Prepared, task: Task, cfg: dict[str, Any], r: dict[str, Any], mode: str, night: bool,
               loaded: dict[str, Any] | None, last_hash: str | None, ratios: dict[str, float] | None) -> dict[str, Any]:
    thinking, budget = mode_budget(cfg, mode, night)
    mc = cfg["modes"][mode]
    repair = bool(mc.get("repair")) and task.issues is not None
    p_repair = logbook.repair_rate(task.name)[0] if repair else 0.0
    _, repair_budget = mode_budget(cfg, "careful", night)
    infos, need_ctx = [], 0
    for c in prep.calls:
        sys_t = textio.estimate_tokens(c.messages[0]["content"], ratios)
        user_t = sum(textio.estimate_tokens(m["content"], ratios) for m in c.messages[1:])
        n_prompt = sys_t + user_t + 24
        infos.append((c, n_prompt, sys_t, user_t))
        extra = budget + (repair_budget + c.num_predict + 300 if repair else 0)
        need_ctx = max(need_ctx, n_prompt + c.num_predict + extra + 64)
    loaded_ctx = int(loaded.get("context_length") or 0) if loaded and not night else None
    ctx = pick_ctx(need_ctx, cfg["ctx_buckets"], loaded_ctx, r["max_ctx"], r["default_ctx"]) if prep.calls else None
    if night or not loaded:
        load_s = r["load_s"]
    elif ctx == loaded_ctx:
        load_s = 0.0
    else:
        load_s = r["reload_s"]
    t50 = t90 = pf_total = dec_total = think_total = rep_total = 0.0
    prompt_total = 0
    think50_total = 0
    for i, (c, n_prompt, sys_t, user_t) in enumerate(infos):
        cached = 0
        if r["prefix_cache"] > 0 and (i > 0 or (load_s == 0 and last_hash == prep.system_hash)):
            cached = int(sys_t * r["prefix_cache"])
        th50, th90 = think_guess(cfg, task, mode, user_t, budget) if thinking else (0, 0)
        pf, d50 = call_times(r, n_prompt, cached, c.out_p50 + th50)
        _, d90 = call_times(r, n_prompt, cached, c.out_p90 + th90)
        _, think_s = call_times(r, n_prompt, cached, th50)
        rep = rep_full = 0.0
        if repair:
            rth50, _ = think_guess(cfg, task, "careful", user_t, repair_budget)
            n2 = n_prompt + c.out_p50 + 250
            rpf, rdec = call_times(r, n2, int(n_prompt * max(r["prefix_cache"], 0.5)), c.out_p50 + rth50)
            rep_full = rpf + rdec + 1.5
            rep = p_repair * rep_full
        pf_total += pf
        dec_total += d50
        think_total += think_s
        rep_total += rep
        prompt_total += n_prompt
        think50_total += th50
        t50 += pf + d50 + rep + 1.5
        t90 += pf * 1.15 + d90 * 1.15 + rep_full + 2.0
    return {"ctx": ctx, "need_ctx": need_ctx, "load_s": load_s, "prefill_s": pf_total, "decode_s": dec_total,
            "think_s": think_total, "repair_s": rep_total, "p50_s": load_s + t50, "p90_s": load_s * 1.3 + t90,
            "prompt_tokens": prompt_total, "think_tokens_p50": think50_total, "thinking": thinking,
            "think_budget": budget, "repair": repair}


def memory_need(r: dict[str, Any], ctx: int, loaded: dict[str, Any] | None) -> float:
    """Extra system RAM (bytes) the job needs beyond what is already in use."""
    cpu = 1.0 - r["vram_frac"]
    if loaded:
        lctx = int(loaded.get("context_length") or 0)
        return 0.0 if ctx <= lctx else r["kv"] * (ctx - lctx) * cpu
    return (r["base_bytes"] + r["kv"] * ctx) * cpu


def estimate(prep: Prepared, task: Task, cfg: dict[str, Any], profile: dict[str, Any], state: dict[str, Any],
             model: str | None = None, guard_ok: bool = True, claude_context_tokens: int | None = None,
             prefer_local: bool = False, need_by: float | None = None, priority: str | None = None,
             now_ts: float | None = None) -> dict[str, Any]:
    now = now_ts or time.time()
    router = cfg["router"]
    model = model or cfg["model"]
    r = rates_for(model, profile, cfg)
    ratios = (profile or {}).get("token_ratio")
    loaded = state.get("loaded") if (state.get("loaded") or {}).get("model") == model else None
    pname, pwhy = schedule.active_priority(cfg, now)
    if priority and priority in cfg["priorities"]:
        pname, pwhy = priority, "chosen for this job"
    pr = cfg["priorities"][pname]

    # ---- Claude's side: every tool call is a step that re-reads the conversation (from cache), and
    # whatever Claude reads stays in the conversation for the steps that follow.
    ctx_tokens = int(claude_context_tokens or router["claude_context_tokens"])
    weight = float(router["cache_read_weight"])
    carry = weight * float(router.get("future_steps", 5))
    step_cost = ctx_tokens * weight + 250
    cpt = router["claude_chars_per_token"].get(prep.raw_kind, 3.4)
    file_chars = max(0, prep.raw_chars - prep.inline_chars)
    claude_read = int(file_chars / cpt)
    file_lines = int(prep.raw_lines * (file_chars / prep.raw_chars)) if prep.raw_chars else 0
    read_calls = (1 + max(prep.files_count, math.ceil(file_lines / 2000))) if file_chars else 0
    here_tokens = claude_read + prep.claude_out_tokens + int(read_calls * step_cost) + int(claude_read * carry)
    here_s = read_calls * router["claude_roundtrip_s"] + prep.claude_out_tokens / router["claude_write_tps"] + 3
    out_p50_total = sum(c.out_p50 for c in prep.calls)
    file_only = bool(state.get("file_only", prep.file_only_default))
    result_tokens = 120 if file_only else int(out_p50_total * 1.35) + 150
    kept_out = max(0, claude_read - result_tokens)

    def delegated_tokens(local_s: float, tonight: bool) -> int:
        steps = 3 + (1 if (local_s > INLINE_WAIT_S or tonight) else 0)
        return result_tokens + int(steps * step_cost) + int(result_tokens * carry)

    counts = logbook.outcome_counts()
    edit_frac = float(router.get("edit_fraction", 0.3))

    def expected(net: int, deleg: int, mode: str) -> tuple[int, dict[str, Any]]:
        rates = logbook.outcome_rates(task.name, mode, counts)
        e = (rates["accept"] + rates["edit"]) * net - rates["edit"] * edit_frac * here_tokens - rates["redo"] * deleg
        return int(e), rates

    # ---- script-only result
    script_opt = None
    if prep.script_result is not None:
        st = int(len(json.dumps(prep.script_result, ensure_ascii=False, default=str)) / 3.2) + 1
        net = here_tokens - (st + int(step_cost) + int(st * carry))
        script_opt = {"id": "script", "label": "Script result only", "kind": "script", "ready_s": 1.0, "ready90_s": 1.0,
                      "saves": net, "expected": net, "rank": RANK["script"],
                      "description": f"Instant · exact facts, no explanation · saves ~{_k(net)} tokens",
                      "substitutes": bool(task.script_substitutes) or task.kind == "script"}

    notes: list[str] = []
    if not r["calibrated"]:
        notes.append("rough figures: this computer is not calibrated yet (local_admin action=calibrate)")
    battery = state.get("battery") or {}
    slow = 1.0
    if battery.get("present") and battery.get("plugged") is False and r["power"].get("plugged") is not False:
        slow = float(router.get("battery_factor", 1.8))
        notes.append("on battery: local times include a slow-down, and tonight's queue needs the charger")

    # ---- local options
    options: list[dict[str, Any]] = []
    infeasible: list[dict[str, str]] = []
    mem_avail = (state.get("memory") or {}).get("available")
    mem_total = (state.get("memory") or {}).get("total")
    headroom = float(cfg.get("ram_headroom_gb", 4.0)) * 1e9
    night_start, night_end, inside_night = schedule.night_bounds(now, cfg.get("night_window") or {})
    tonight_queue = float(state.get("tonight_queue_s") or 0.0)
    now_queue = float(state.get("queue_eta_s") or 0.0)
    too_large = False
    runs: dict[str, dict[str, Any]] = {}
    for mode in (MODES if prep.calls else ()):
        for when in ("now", "tonight"):
            night = when == "tonight"
            lt = local_time(prep, task, cfg, r, mode, night, loaded, state.get("last_system_hash"), ratios)
            oid = f"{mode}_{when}"
            if lt["ctx"] is None:
                too_large = True
                infeasible.append({"id": oid, "why": f"needs ~{lt['need_ctx']:,} tokens of context; split the input"})
                continue
            p50, p90 = lt["p50_s"] * slow, lt["p90_s"] * slow
            start = (night_start + tonight_queue) if night else (now + now_queue)
            ready, ready90 = start + p50, start + p90
            runs[oid] = {"mode": mode, "when": when, "ctx": lt["ctx"], "p50_s": round(p50, 1), "p90_s": round(p90, 1),
                         "load_s": round(lt["load_s"], 1), "prefill_s": round(lt["prefill_s"], 1),
                         "decode_s": round(lt["decode_s"], 1), "think_s": round(lt["think_s"], 1),
                         "think_tokens_p50": lt["think_tokens_p50"], "repair_s": round(lt["repair_s"], 1),
                         "output_tokens_p50": out_p50_total, "prompt_tokens": lt["prompt_tokens"],
                         "think_budget": lt["think_budget"], "model": model,
                         "pred_think_tokens": lt["think_tokens_p50"]}
            why = None
            need = memory_need(r, lt["ctx"], None if night else loaded)
            if cfg.get("ram_check", True):
                if night and mem_total and mem_total - need < headroom * 0.5:
                    why = f"this computer's {mem_total / 1e9:.0f} GB of RAM is too little for this context"
                elif not night and mem_avail is not None and mem_avail - need < headroom:
                    why = (f"needs ~{need / 1e9:.1f} GB more RAM, {mem_avail / 1e9:.1f} GB free now "
                           "(close heavy apps, or run it tonight)")
            if not why and not night and p90 > router["now_max_minutes"] * 60:
                why = f"would take up to {schedule.fmt_minutes(p90)} now; offered for tonight instead"
            if not why and night and ready90 > night_end:
                why = (f"would finish around {schedule.fmt_clock(ready90, now)}, after tonight's window ends "
                       f"({schedule.fmt_clock(night_end, now)}); split it or use a lighter level")
            if not why and need_by and ready90 > need_by:
                why = f"ready by {schedule.fmt_clock(ready90, now)}, after you need it"
            if why:
                infeasible.append({"id": oid, "why": why})
                continue
            deleg = delegated_tokens(p50, night)
            net = here_tokens - deleg
            exp, rates = expected(net, deleg, mode)
            runs[oid]["claude_tokens_saved_est"] = net
            risk = f"~{round(rates['redo'] * 100)}% redo risk" + ("" if rates["n"] >= 5 else " (starting guess)")
            if night:
                timing = (f"Starts {schedule.fmt_clock(start, now)}, ready by ~{schedule.fmt_clock(ready90, now)}"
                          if start - now > 120 else f"Runs now through the night, ready by ~{schedule.fmt_clock(ready90, now)}")
            else:
                timing = f"Ready ~{schedule.fmt_clock(ready, now)} ({schedule.fmt_range(ready - now, ready90 - now)})"
            saves_txt = (f"saves ~{_k(net)} tokens (~{_k(max(0, exp))} allowing for redos)" if net > 0
                         else f"costs ~{_k(-net)} tokens more")
            options.append({
                "id": oid, "label": f"Local {mode}, {'tonight' if night else 'now'}", "kind": "local", "mode": mode,
                "when": when, "ready_s": ready - now, "ready90_s": ready90 - now, "start_s": start - now,
                "saves": net, "expected": exp, "rank": RANK[mode], "redo": round(rates["redo"], 3),
                "description": f"{timing} · {saves_txt} · {QUALITY_WORDS[mode]} · {risk}",
            })

    claude_opt = {"id": "claude", "label": "Claude now", "kind": "claude", "ready_s": here_s, "ready90_s": here_s * 2,
                  "saves": 0, "expected": 0, "rank": RANK["claude"],
                  "description": f"Ready in ~{schedule.fmt_minutes(here_s)} · uses ~{_k(here_tokens)} Claude tokens"
                                 + (" · sends the text to Claude" if prefer_local else "")}

    # ---- scoring under the active preset
    def utility(o: dict[str, Any]) -> float:
        if o["kind"] == "claude":
            return float("-inf") if prefer_local else -pr["minute"] * o["ready_s"] / 60 + pr["quality"] * o["rank"]
        u = o["expected"] + pr["quality"] * o["rank"]
        if o.get("when") == "tonight":
            if pr.get("tonight") is None:
                return float("-inf")
            return u - float(pr["tonight"])
        wait = o["ready_s"] + 0.25 * (o["ready90_s"] - o["ready_s"])
        return u - pr["minute"] * wait / 60

    pool = list(options)
    if script_opt:
        pool.append(script_opt)
    for o in pool:
        o["score"] = utility(o)
    claude_opt["score"] = utility(claude_opt)
    eligible = [o for o in pool if (prefer_local and o["kind"] == "local") or o["kind"] == "script"
                or o["expected"] >= pr["min_saving"]]
    candidates = eligible if prefer_local and eligible else eligible + [claude_opt]
    best = max(candidates, key=lambda o: (o["score"], o["rank"]))

    # ---- what to do
    if not guard_ok:
        action = "blocked"
    elif task.kind == "script" or not prep.calls:
        action = "script"
        best = script_opt or best
    elif not options and too_large:
        action = "too_large"
    elif script_opt and script_opt["substitutes"] and best["kind"] in ("claude", "script") and script_opt["saves"] > 0:
        action, best = "script", script_opt   # exact and instant beats both waiting and re-reading
    elif best["kind"] == "script" and best.get("substitutes"):
        action = "script"
    elif best["kind"] == "claude":
        action = "do_here"
    else:
        action = "ask"

    # ---- which options to show (at most 4): the recommendation, Claude, the quickest other local
    # option and the deepest one, then the next best by score.
    shown: list[dict[str, Any]] = []

    cap = 3 if prefer_local else 4   # with privacy first, the last slot stays for "Claude now"

    def add(o: dict[str, Any] | None) -> None:
        if o and o not in shown and len(shown) < cap:
            shown.append(o)

    ranked = sorted(options, key=lambda o: -o["score"] if o["score"] > float("-inf") else float("inf"))
    add(best)
    if not prefer_local:
        add(claude_opt)
    locals_now = [o for o in options if o["when"] == "now"]
    add(min(locals_now, key=lambda o: o["ready90_s"]) if locals_now else None)
    tonight = sorted([o for o in options if o["when"] == "tonight"], key=lambda o: (-o["rank"], o["ready90_s"]))
    if tonight and best.get("when") != "tonight":
        add(tonight[0])                      # the deepest overnight option, as the "no rush" choice
    else:
        add(next((o for o in ranked if o["when"] == "now" and o not in shown), None))
    for o in ranked + ([script_opt] if script_opt else []):
        add(o)
    if prefer_local:
        shown.append(claude_opt)
    for o in shown:
        o["recommended"] = o is best

    def public(o: dict[str, Any]) -> dict[str, Any]:
        keep = {k: o[k] for k in ("id", "label", "description", "recommended") if k in o}
        if o["kind"] == "local":
            keep.update({"minutes": [round(o["ready_s"] / 60, 1), round(o["ready90_s"] / 60, 1)],
                         "saves": _round(o["saves"]), "expected_saves": _round(o["expected"])})
        elif o["kind"] == "claude":
            keep["claude_tokens"] = _round(here_tokens)
        return keep

    others = [o for o in options if o not in shown]
    title = f"{task.title}: {prep.files_count or 'inline'} input(s), {prep.raw_chars / 1000:.1f}k chars"
    question = f"{title}. How should it run? (priority: {pname})"
    ask_lines = [question] + [f"{i + 1}. {o['label']}{' (recommended)' if o['recommended'] else ''}: {o['description']}"
                              for i, o in enumerate(shown)]
    return {
        "action": action,
        "recommended": best["id"],
        "priority": {"name": pname, "why": pwhy, "about": pr.get("about")},
        "question": question,
        "options": [public(o) for o in shown],
        "more_options": [{"id": o["id"], "description": o["description"]} for o in others][:4] or None,
        "not_possible": infeasible[:6] or None,
        "ask_text": "\n".join(ask_lines),
        "claude_here": {"tokens": here_tokens, "seconds": round(here_s, 1), "reads": read_calls},
        "context_kept_out_tokens": kept_out,
        "notes": notes or None,
        "_runs": runs,
        "_best_local": max(options, key=lambda o: o["score"])["description"] if options else None,
    }


def _k(n: float) -> str:
    n = abs(n)
    return f"{n / 1000:.1f}k" if n < 10000 else f"{round(n / 1000)}k"


def _round(n: float) -> int:
    return int(round(n, -2))
