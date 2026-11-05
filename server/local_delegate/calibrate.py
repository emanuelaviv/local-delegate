"""Measure this computer instead of assuming.

Records, for the configured model: cold load and reload time; RAM used by the weights
and by each token of context (measured from Ollama's own accounting, so it reflects the
real architecture: Qwen3.8-27B keeps a growing cache in only 16 of its 64 layers);
how much sits on a GPU, if any; prompt-reading (prefill) speed at several lengths;
writing (decode) speed at short and long context; whether Ollama reuses a shared prompt
prefix; characters per token for English prose, code, French and Hebrew; and what
reasoning ("thinking") costs on a one-line question, with it on and off.

Everything adapts to a time budget so a slow, CPU-only machine still finishes.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Callable

from . import config, sysinfo
from .ollama import Ollama, OllamaError, model_max_context, think_off_value

WORDS = ("time year people way day man thing woman life child world school state family student group country "
         "problem hand part place case week company system program question work government number night point "
         "home water room mother area money story fact month lot right study book eye job word business issue "
         "side kind head house service friend father power hour game line end member law car city community name "
         "president team minute idea kid body information back parent face others level office door health person "
         "art war history party result change morning reason research girl guy moment air teacher force education "
         "foot boy age policy process music market sense nation plan college interest death experience effect use "
         "class control care field development role effort rate heart drug show leader light voice wife police mind "
         "price report decision son view relationship town road arm difference value building action model season "
         "society tax director position player record paper space ground form event official matter center couple "
         "site project activity star table need court oil situation cost industry figure street image phone data "
         "picture practice piece land product doctor wall patient worker news test movie north love support "
         "technology step baby computer type attention film tree source organization hair window evidence population").split()
VERBS = "is was has makes takes shows gives finds tells keeps runs moves helps needs builds checks uses reads".split()

FR_SAMPLE = ("Le rapport de la semaine décrit les changements apportés à l'application, les essais qui ont réussi "
             "et ceux qui restent à corriger. L'équipe propose de vérifier les écrans en français avant la prochaine "
             "version, puis de comparer chaque phrase avec le glossaire approuvé. Les rendez-vous de suivi auront lieu "
             "le mardi matin, et chacun doit noter les questions ouvertes dans le journal du projet. ")
HE_SAMPLE = ("הדוח השבועי מתאר את השינויים שנעשו באפליקציה, את הבדיקות שעברו ואת אלה שעדיין צריך לתקן. הצוות "
             "מציע לבדוק את המסכים בעברית לפני הגרסה הבאה, ולהשוות כל משפט למילון המאושר. פגישות המעקב יתקיימו "
             "ביום שלישי בבוקר, וכל אחד צריך לרשום את השאלות הפתוחות ביומן הפרויקט. ")


def synth_prose(n_words: int, seed: int) -> str:
    rng = random.Random(seed)
    out, sentence = [], []
    for i in range(n_words):
        w = rng.choice(WORDS) if i % 3 else rng.choice(VERBS)
        sentence.append(w)
        if len(sentence) >= rng.randint(9, 16):
            s = " ".join(sentence)
            out.append(s[0].upper() + s[1:] + ".")
            sentence = []
    if sentence:
        out.append(" ".join(sentence) + ".")
    return " ".join(out)


def synth_code(n_lines: int, seed: int) -> str:
    rng = random.Random(seed)
    lines = []
    for i in range(n_lines // 4):
        a, b = rng.choice(WORDS), rng.choice(WORDS)
        lines += [f"def {a}_{b}_{i}(items, limit={rng.randint(2, 99)}):",
                  f"    total = sum(x.{a} for x in items if x.{b} < limit)",
                  f"    return {{'{a}': total, 'count': len(items)}}  # step {i}", ""]
    return "\n".join(lines)


def _fit_prefill(points: list[tuple[int, float]]) -> tuple[float, float]:
    """Least squares for T = a*n + b*n^2 with a, b >= 0."""
    pts = [(n, t) for n, t in points if n > 0 and t > 0]
    if not pts:
        return 1 / 30.0, 0.0
    s11 = sum(n * n for n, _ in pts)
    s12 = sum(n ** 3 for n, _ in pts)
    s22 = sum(n ** 4 for n, _ in pts)
    y1 = sum(n * t for n, t in pts)
    y2 = sum(n * n * t for n, t in pts)
    det = s11 * s22 - s12 * s12
    if len(pts) >= 2 and det > 0:
        a = (y1 * s22 - y2 * s12) / det
        b = (s11 * y2 - s12 * y1) / det
        if a > 0 and b >= 0:
            return a, b
    return y1 / s11, 0.0


def _fit_decode(points: list[tuple[int, float]]) -> tuple[float, float]:
    """points: (context position, seconds per token). Line g + h*c with g, h >= 0."""
    pts = [(c, s) for c, s in points if s > 0]
    if not pts:
        return 1 / 3.0, 0.0
    if len(pts) == 1 or max(c for c, _ in pts) - min(c for c, _ in pts) < 500:
        return sum(s for _, s in pts) / len(pts), 0.0
    pts.sort()
    (c1, s1), (c2, s2) = pts[0], pts[-1]
    h = max(0.0, (s2 - s1) / (c2 - c1))
    g = max(1e-4, s1 - h * c1)
    return g, h


class Calibrator:
    def __init__(self, cfg: dict[str, Any], model: str | None = None, budget_s: float = 720.0,
                 progress: Callable[[str], None] | None = None, cancel: Any = None):
        self.cfg = cfg
        self.model = model or cfg["model"]
        self.client = Ollama(cfg["ollama_url"])
        self.budget_s = budget_s
        self.start = time.monotonic()
        self.progress = progress or (lambda s: None)
        self.cancel = cancel
        self.nonce = 0
        self.notes: list[str] = []

    def left(self) -> float:
        return self.budget_s - (time.monotonic() - self.start)

    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise RuntimeError("calibration cancelled")

    def _chat(self, text: str, ctx: int, num_predict: int, think: Any, system: str | None = None,
              budget: float | None = None) -> dict[str, Any]:
        self._check_cancel()
        self.nonce += 1
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": text}]
        res = self.client.chat(self.model, msgs, options={"num_ctx": ctx, "num_predict": num_predict, "temperature": 0,
                                                          "seed": 7},
                               think=think, keep_alive="15m", budget_s=budget or max(30.0, self.left()),
                               first_token_timeout_s=budget or max(30.0, self.left()), cancel=self.cancel)
        if res.aborted:
            raise RuntimeError(f"calibration step stopped: {res.abort_reason}")
        st = dict(res.stats)
        st["_content"] = res.content
        st["_thinking"] = res.thinking
        st["_wall"] = res.wall_s
        return st

    def run(self) -> dict[str, Any]:
        cfg, m = self.cfg, self.model
        say = self.progress
        say("checking the model")
        show = self.client.show(m)
        think = think_off_value(show)
        max_ctx = model_max_context(show) or 32768
        hw = sysinfo.snapshot()
        hw["gpus"] = sysinfo.gpus()
        say("measuring load time (unloading first)")
        try:
            self.client.unload(m)
        except OllamaError:
            pass
        time.sleep(2)
        mem_before = sysinfo.memory().get("available")
        c1 = 4096
        st = self._chat("Reply with the word OK.", c1, 2, think)
        load_s = (st.get("load_duration") or 0) / 1e9
        ps1 = self.client.loaded(m) or {}
        size1 = int(ps1.get("size") or 0)
        mem_after = sysinfo.memory().get("available")
        tiny_tokens = int(st.get("prompt_eval_count") or 20)

        say("measuring tokens per character (English, code, French, Hebrew)")
        ratios: dict[str, float] = {}
        prefill_pts: list[tuple[int, float]] = []
        samples = {"prose": synth_prose(320, 1), "code": synth_code(60, 2), "fr": FR_SAMPLE * 2, "he": HE_SAMPLE * 2}
        for kind, text in samples.items():
            body = f"[{self.nonce}-{kind}]\n{text}"
            st = self._chat(body, c1, 1, think)
            n = int(st.get("prompt_eval_count") or 0) - int(st.get("prompt_eval_cached_count") or 0)
            if n > tiny_tokens:
                ratios[kind] = round(len(body) / max(1, n - (tiny_tokens - 8)), 3)
                dur = (st.get("prompt_eval_duration") or 0) / 1e9
                if dur > 0:
                    prefill_pts.append((n, dur))
        small_rate = sum(n for n, _ in prefill_pts) / max(1e-6, sum(t for _, t in prefill_pts)) if prefill_pts else 30.0

        avail_gb = (mem_after or 0) / 1e9
        c2 = 16384 if avail_gb > float(cfg.get("ram_headroom_gb", 4)) + 2 else 8192
        say(f"measuring reload time and memory per token of context (context {c2})")
        st = self._chat("Reply with the word OK.", c2, 2, think)
        reload_s = (st.get("load_duration") or 0) / 1e9
        ps2 = self.client.loaded(m) or {}
        size2, vram2 = int(ps2.get("size") or 0), int(ps2.get("size_vram") or 0)
        kv = (size2 - size1) / (c2 - c1) if size2 > size1 else None
        if not kv:
            kv = float(cfg["fallback_rates"]["kv_bytes_per_token"])
            self.notes.append("memory per context token not measurable from Ollama's report; used the default")
        base = max(0.0, size2 - kv * c2) if size2 else float(cfg["fallback_rates"]["model_bytes"])
        vram_frac = (vram2 / size2) if size2 else 0.0

        say("measuring prompt reading speed")
        sizes = [1500, 4000, 8000]
        long_prompt = None
        long_n = 0
        for target in sizes:
            predicted = target / max(1.0, small_rate) * 1.3
            if predicted > self.left() * 0.35 or target + 200 > c2:
                self.notes.append(f"skipped the {target}-token reading test (would take ~{predicted:.0f}s)")
                continue
            words = int(target * 0.75)
            body = f"[{self.nonce}-p{target}] Read this notice.\n" + synth_prose(words, 10 + target)
            st = self._chat(body, c2, 1, think)
            n = int(st.get("prompt_eval_count") or 0) - int(st.get("prompt_eval_cached_count") or 0)
            dur = (st.get("prompt_eval_duration") or 0) / 1e9
            if n > 0 and dur > 0:
                prefill_pts.append((n, dur))
                small_rate = n / dur
                long_prompt, long_n = body, n

        decode_pts: list[tuple[int, float]] = []
        prefix_ratio = 0.0
        if long_prompt and self.left() > 60:
            # Must follow the long prompt directly: Ollama keeps only the last prompt in its cache.
            say("checking prompt-prefix reuse and writing speed with a long context")
            st = self._chat(long_prompt + "\nNow list ten words from the notice, separated by commas.", c2, 48, think)
            cached = int(st.get("prompt_eval_cached_count") or 0)
            prefix_ratio = round(min(1.0, cached / max(1, long_n)), 2)
            if st.get("eval_count") and st.get("eval_duration"):
                decode_pts.append((int(st.get("prompt_eval_count") or long_n), st["eval_duration"] / 1e9 / st["eval_count"]))
        say("measuring writing speed")
        st = self._chat("Count from one to two hundred in words, separated by commas.", c2, 96, think)
        if st.get("eval_count") and st.get("eval_duration"):
            decode_pts.insert(0, (int(st.get("prompt_eval_count") or 30), st["eval_duration"] / 1e9 / st["eval_count"]))

        think_info: dict[str, Any] = {"values": (show.get("thinking") or {}).get("values"),
                                      "default": (show.get("thinking") or {}).get("default"), "off_value": think}
        caps = show.get("capabilities") or []
        if ("thinking" in caps or show.get("thinking")) and decode_pts and self.left() > 90:
            say("measuring what reasoning costs on a one-line question")
            q = "In one sentence, what is computer hardware?"
            per_tok = decode_pts[0][1]
            cap = int(max(32, min(256, (self.left() * 0.4) / max(per_tok, 1e-3))))
            st_on = self._chat(q, c2, cap, None)
            st_off = self._chat(q, c2, 80, think)
            think_info["one_line_question"] = {
                "with_default_reasoning": {"output_tokens": st_on.get("eval_count"), "seconds": round(st_on["_wall"], 1),
                                           "hit_cap": st_on.get("done_reason") == "length", "cap": cap},
                "with_reasoning_off": {"output_tokens": st_off.get("eval_count"), "seconds": round(st_off["_wall"], 1),
                                       "thinking_chars": len(st_off.get("_thinking") or "")},
            }
            if len(st_off.get("_thinking") or "") > 200:
                self.notes.append("reasoning did not fully switch off; prefer an instruct variant of the model")

        a, b = _fit_prefill(prefill_pts)
        g, h = _fit_decode(decode_pts)
        mem_now = sysinfo.memory().get("available") or 0
        headroom = float(cfg.get("ram_headroom_gb", 4.0)) * 1e9
        per_tok_ram = kv * max(0.05, 1 - vram_frac)
        max_ctx_mem = int(c2 + max(0.0, mem_now - headroom) / per_tok_ram) if per_tok_ram > 0 else max_ctx
        interactive_s = float(cfg["router"].get("interactive_minutes", 8)) * 60 * 0.6
        n_int = int((-a + (a * a + 4 * b * interactive_s) ** 0.5) / (2 * b)) if b > 0 else int(interactive_s / a)
        default_ctx = 16384 if max_ctx_mem >= 16384 else (8192 if max_ctx_mem >= 8192 else 4096)
        tps4k = 4000 / (a * 4000 + b * 4000 * 4000)
        profile_model = {
            "calibrated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "digest": next((t.get("digest") for t in self.client.tags() if t.get("name") == m), None),
            "details": show.get("details"),
            "load_s": round(load_s, 2), "reload_s": round(reload_s, 2),
            "prefill": {"a": a, "b": b, "tps_4k": round(tps4k, 1), "points": prefill_pts},
            "decode": {"g": g, "h": h, "tps_short": round(1 / decode_pts[0][1], 2) if decode_pts else None,
                       "tps_long": round(1 / decode_pts[-1][1], 2) if len(decode_pts) > 1 else None,
                       "points": decode_pts},
            "memory": {"base_bytes": base, "kv_bytes_per_token": round(kv), "vram_fraction": round(vram_frac, 3),
                       "size_at_ctx": {str(c1): size1, str(c2): size2}, "size_vram": vram2,
                       "ram_available_before_load": mem_before, "ram_available_after_load": mem_after},
            "prefix_cache_ratio": prefix_ratio,
            "think": think_info,
            "limits": {"model_max_ctx": max_ctx, "max_ctx_memory_now": min(max_ctx, max_ctx_mem),
                       "max_prompt_tokens_interactive": n_int, "default_ctx": default_ctx},
            "power_at_calibration": hw.get("battery"),
            "notes": self.notes,
        }
        profile = load_profile()
        profile.setdefault("models", {})[m] = profile_model
        profile["token_ratio"] = {**profile.get("token_ratio", {}), **ratios}
        profile["hardware"] = hw
        try:
            profile["ollama_version"] = self.client.version()
        except OllamaError:
            pass
        profile["updated_at"] = profile_model["calibrated_at"]
        save_profile(profile)
        say("done")
        return summarize(profile, m)


def profile_path():
    return config.data_dir() / "profile.json"


def load_profile() -> dict[str, Any]:
    p = profile_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_profile(profile: dict[str, Any]) -> None:
    p = profile_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def summarize(profile: dict[str, Any], model: str) -> dict[str, Any]:
    pm = (profile.get("models") or {}).get(model)
    if not pm:
        return {"calibrated": False}
    gpu = [g.get("name") for g in (profile.get("hardware") or {}).get("gpus") or []]
    mem = (profile.get("hardware") or {}).get("memory") or {}
    t1 = (pm.get("think") or {}).get("one_line_question")
    out = {
        "calibrated": True, "model": model, "when": pm.get("calibrated_at"),
        "ram_total_gb": round((mem.get("total") or 0) / 1e9, 1), "gpus": gpu,
        "on_gpu_share": pm["memory"].get("vram_fraction"),
        "load_s": pm.get("load_s"), "reload_s": pm.get("reload_s"),
        "reading_tokens_per_s_at_4k": pm["prefill"].get("tps_4k"),
        "writing_tokens_per_s": pm["decode"].get("tps_short"),
        "context_memory_kb_per_token": round(pm["memory"]["kv_bytes_per_token"] / 1024, 1),
        "prefix_reuse": pm.get("prefix_cache_ratio"),
        "limits": pm.get("limits"),
        "notes": pm.get("notes"),
    }
    if t1:
        out["reasoning_cost_one_line_question"] = t1
    return out
