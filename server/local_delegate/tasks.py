"""Task contracts: what each task reads, what scripts compute exactly, what the model is
asked, the JSON shape it must return, and how the answer is checked.

Every prompt puts the fixed instructions first (system message) and the variable input
last, so a run of similar jobs reuses Ollama's prompt cache for the shared prefix.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import guard, packs, parsers, textio, verify

SKIP_DIRS = {".git", "node_modules", "build", "dist", ".gradle", ".idea", ".venv", "venv", "__pycache__",
             ".dart_tool", ".next", "out", "target", ".pytest_cache", ".mypy_cache", ".tmp", "intermediates"}

CITE = {"type": "object", "properties": {"doc": {"type": "string"}, "line": {"type": "integer"},
                                         "quote": {"type": "string"}}, "required": ["doc", "line", "quote"]}
CITE_RULES = ("Lines are prefixed '<doc>:<line>|'. Each citation gives doc (for example \"D1\"), line (the number) "
              "and quote: an exact copy of 3 to 15 consecutive words from that line.")
BASE_RULES = ("Use only the text provided. Never invent facts, files, numbers, names or results. If something is "
              "not in the text, leave it out (empty list or null). Do not give verdicts, approvals or ratings. "
              "Return only JSON that matches the schema.")


class TaskInputError(Exception):
    pass


@dataclass
class Call:
    messages: list[dict[str, str]]
    schema: dict[str, Any] | None
    num_predict: int
    out_p50: int
    out_p90: int
    temperature: float = 0.0
    label: str = ""

    @property
    def key(self) -> str:
        """Stable id of this call's input, used to keep finished parts across restarts."""
        h = hashlib.sha1(json.dumps(self.messages, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        return h.hexdigest()[:16]


@dataclass
class Prepared:
    task: str
    params: dict[str, Any]
    calls: list[Call]
    facts: dict[str, Any] = field(default_factory=dict)
    docs: dict[str, list[str]] = field(default_factory=dict)
    doc_meta: list[dict[str, Any]] = field(default_factory=list)
    raw_chars: int = 0
    raw_lines: int = 0
    inline_chars: int = 0   # text Claude passed in, so already in its context
    raw_kind: str = "prose"
    files_count: int = 0
    claude_out_tokens: int = 300
    script_result: dict[str, Any] | None = None
    redactions: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    file_only_default: bool = False
    system_hash: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Task:
    name: str
    title: str
    kind: str          # "script" | "hybrid" | "llm"
    memory: str        # M0..M3, as in the delegation map
    summary: str
    params: dict[str, str]
    prepare: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], Prepared]
    finalize: Callable[[Prepared, list[dict[str, Any]]], dict[str, Any]] | None = None
    tier: str = "quality"   # "fast_ok": a smaller model may do it
    script_substitutes: bool = False  # the script result alone can replace the model's answer
    think_factor: float = 1.0  # how much reasoning this task tends to need, relative to others
    issues: Callable[[Prepared, int, dict[str, Any]], list[str]] | None = None  # checks for the fix-up pass
    partial_ok: bool = False   # several independent parts: a job cut short still returns the finished ones


# ===================================================================== input helpers

def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def walk(root: Path, cfg: dict[str, Any], exts: set[str] | None, max_files: int = 300,
         max_bytes: int = 40_000_000) -> list[Path]:
    out: list[Path] = []
    total = 0
    stack = [root]
    while stack and len(out) < max_files:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS and not p.name.startswith("."):
                    stack.append(p)
                continue
            if exts and p.suffix.lower() not in exts:
                continue
            ok, rp, _ = guard.check_path(str(p), cfg)
            if not ok or rp is None:
                continue
            try:
                size = rp.stat().st_size
            except OSError:
                continue
            if size > 2_000_000 or total + size > max_bytes:
                continue
            total += size
            out.append(rp)
            if len(out) >= max_files:
                break
    return out


def load_docs(params: dict[str, Any], cfg: dict[str, Any], key: str = "paths", text_key: str = "text",
              ids: list[str] | None = None, allow_dirs: bool = False, exts: set[str] | None = None,
              required: bool = True) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    files: list[Path] = []
    problems: list[str] = []
    for raw in _as_list(params.get(key)):
        ok, p, reason = guard.check_path(raw, cfg)
        if not ok or p is None:
            problems.append(reason)
            continue
        if p.is_dir():
            if not allow_dirs:
                problems.append(f"{p} is a folder; this task needs files")
                continue
            found = walk(p, cfg, exts or textio.TEXT_EXTS)
            if not found:
                problems.append(f"{p}: no readable text files found")
            files.extend(found)
        else:
            files.append(p)
    docs: list[dict[str, Any]] = []
    redactions: dict[str, int] = {}
    for p in files:
        try:
            text = textio.read_text(p)
        except (textio.ReadError, OSError) as exc:
            problems.append(str(exc))
            continue
        text, counts = guard.redact(text)
        for k, v in counts.items():
            redactions[k] = redactions.get(k, 0) + v
        docs.append({"path": str(p), "name": p.name, "text": text, "ext": p.suffix.lower()})
    inline = params.get(text_key)
    if isinstance(inline, str) and inline.strip():
        text, counts = guard.redact(inline)
        for k, v in counts.items():
            redactions[k] = redactions.get(k, 0) + v
        docs.append({"path": "(inline text)", "name": "inline", "text": text, "ext": ""})
    for i, d in enumerate(docs):
        d["id"] = ids[i] if ids and i < len(ids) else f"D{i + 1}"
        d["kind"] = textio.content_kind(d["text"], d["ext"])
    if required and not docs:
        raise TaskInputError("; ".join(problems) or f"give '{key}' (file paths) or '{text_key}'")
    return docs, problems, redactions


def _numbered(docs: list[dict[str, Any]]) -> str:
    parts = []
    for d in docs:
        start = int(d.get("start") or 1)
        parts.append(f"=== {d['id']}: {d['name']} ===\n{textio.number_lines(d['text'], d['id'], start)}")
    return "\n\n".join(parts)


def _base(task: str, params: dict[str, Any], docs: list[dict[str, Any]], calls: list[Call], **kw: Any) -> Prepared:
    raw_chars = sum(len(d["text"]) for d in docs)
    raw_lines = sum(d["text"].count("\n") + 1 for d in docs)
    kinds = [d["kind"] for d in docs] or ["prose"]
    kind = max(set(kinds), key=kinds.count)
    sys_text = calls[0].messages[0]["content"] if calls and calls[0].messages else ""
    inline = sum(len(d["text"]) for d in docs if d["path"] in ("(inline text)", "(notes)", "(instruction)"))
    prep = Prepared(task=task, params=params, calls=calls, raw_chars=raw_chars, raw_lines=raw_lines, raw_kind=kind,
                    inline_chars=inline,
                    files_count=sum(1 for d in docs if d["path"] != "(inline text)"),
                    docs={d["id"]: d["text"].split("\n") for d in docs},
                    doc_meta=[{"id": d["id"], "path": d["path"], "chars": len(d["text"]), "kind": d["kind"]} for d in docs],
                    system_hash=hashlib.sha1(sys_text.encode("utf-8")).hexdigest()[:12], **kw)
    return prep


def _tok(text: str, profile: dict[str, Any]) -> int:
    return textio.estimate_tokens(text, (profile or {}).get("token_ratio"))


def _prefill_tps(profile: dict[str, Any]) -> float:
    try:
        return float(profile["prefill"]["tps_4k"])
    except (KeyError, TypeError, ValueError):
        return 30.0


def _clamp(v: float, lo: float, hi: float) -> int:
    return int(max(lo, min(hi, v)))


def _json_tokens(obj: Any) -> int:
    return int(len(json.dumps(obj, ensure_ascii=False)) / 3.2) + 1


def _msgs(system: str, user: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _cites_out(model: dict[str, Any] | None, prep: Prepared) -> dict[str, Any]:
    if not isinstance(model, dict):
        return {"citations": 0, "confirmed": 0, "unverified": 0, "unverified_quotes": []}
    return verify.verify_cites(model, prep.docs)


def _pack_list(names: Any) -> list[dict[str, Any]]:
    return [packs.load(n) for n in _as_list(names)]


# ===================================================================== digest

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": ["object", "null"], "properties": {"text": {"type": "string"}, "cite": CITE}},
        "files": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "action": {"type": "string"}, "cite": CITE}, "required": ["path", "cite"]}},
        "tests": {"type": "array", "items": {"type": "object", "properties": {
            "what": {"type": "string"}, "result": {"type": "string"}, "cite": CITE}, "required": ["what", "cite"]}},
        "open_items": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "cite": CITE}, "required": ["text", "cite"]}},
        "approval_lines": {"type": "array", "items": CITE},
        "decisions": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "cite": CITE}, "required": ["text", "cite"]}},
        "notes": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "cite": CITE}, "required": ["text", "cite"]}},
    },
    "required": ["status", "files", "tests", "open_items", "approval_lines", "decisions", "notes"],
}

DIGEST_TEMPLATES = {
    "agent_report": ("Digest an engineering agent's report for a busy reviewer.\n"
                     "- status: what the report itself says about the outcome, or null.\n"
                     "- files: files the report says were created, changed, deleted or reviewed (path as written, action).\n"
                     "- tests: test runs it mentions (command or suite, result in the report's own words).\n"
                     "- open_items: unresolved issues, questions, TODOs, blockers, follow-ups.\n"
                     "- approval_lines: every line that asks for, grants or records approval or sign-off.\n"
                     "- decisions: decisions the report records.\n"
                     "- notes: at most 3 other points a reviewer must not miss."),
    "coordinator_log": ("Catch the reader up on a project coordinator log.\n"
                        "- status: the latest overall state, if stated.\n"
                        "- decisions: each decision recorded (who decided, if stated).\n"
                        "- open_items: open questions and pending work.\n"
                        "- approval_lines: every line that asks for, grants or records approval.\n"
                        "- files and tests: only if the log names them.\n"
                        "- notes: at most 3 other points the reader must not miss."),
    "generic": ("Summarize the document for a reviewer.\n"
                "- status: the document's own bottom line, if it states one.\n"
                "- decisions, open_items, notes (key points), approval_lines; files and tests only if named."),
}

KEYWORD_LINE = re.compile(r"(?i)\b(approv\w*|sign[- ]?off|signed off|PASS(?:ED)?\b|NOT PASS|FAIL(?:ED|URE)?\b|blocked|"
                          r"TODO|FIXME|needs? (?:your|the user'?s|[A-Z]\w*'s) (?:approval|decision))")


def _since_filter(text: str, since: str) -> tuple[str, int]:
    """Keep the part of a dated log from the first heading/line dated on or after `since` (YYYY-MM-DD)."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", line)
        if m and m.group(1) >= since and (line.lstrip().startswith(("#", "-", "*", "[", "20")) or i == 0):
            return "\n".join(lines[i:]), i
    return text, 0


def prep_digest(params: dict[str, Any], cfg: dict[str, Any], profile: dict[str, Any]) -> Prepared:
    template = params.get("template") or "agent_report"
    if template not in DIGEST_TEMPLATES:
        raise TaskInputError(f"template must be one of {', '.join(DIGEST_TEMPLATES)}")
    docs, problems, red = load_docs(params, cfg)
    offsets = {}
    originals = {d["id"]: d["text"] for d in docs}
    if params.get("since"):
        for d in docs:
            d["text"], offsets[d["id"]] = _since_filter(d["text"], str(params["since"]))
            d["start"] = offsets[d["id"]] + 1  # keep the file's own line numbers
    pack_text = "\n\n".join(p["guidance"] for p in _pack_list(params.get("packs")))
    system = (f"{DIGEST_TEMPLATES[template]}\nKeep each text field under 25 words.\n{CITE_RULES}\n{BASE_RULES}"
              + (f"\n\nProject rules:\n{pack_text}" if pack_text else ""))
    focus = f"Reviewer's focus: {params['focus']}\n\n" if params.get("focus") else ""
    user = focus + _numbered(docs)
    in_tok = _tok(user, profile)
    p50 = _clamp(160 + 0.05 * in_tok, 220, 900)
    call = Call(_msgs(system, user), DIGEST_SCHEMA, num_predict=int(min(1600, p50 * 1.9)), out_p50=p50,
                out_p90=int(p50 * 1.6))
    facts = {"paths_mentioned": [], "keyword_lines": []}
    for d in docs:
        for item in parsers.paths_in_text(d["text"])[:80]:
            facts["paths_mentioned"].append({"doc": d["id"], **item})
        for i, line in enumerate(d["text"].split("\n"), start=int(d.get("start") or 1)):
            if KEYWORD_LINE.search(line):
                facts["keyword_lines"].append({"doc": d["id"], "line": i, "text": line.strip()[:160]})
    facts["keyword_lines"] = facts["keyword_lines"][:60]
    if offsets and any(offsets.values()):
        facts["since_filter"] = {k: f"kept from line {v + 1}" for k, v in offsets.items()}
    prep = _base("digest", params, docs, [call], facts=facts, redactions=red, notes=problems,
                 claude_out_tokens=p50)
    prep.docs = {k: v.split("\n") for k, v in originals.items()}  # check citations against the whole file
    return prep


def fin_digest(prep: Prepared, outs: list[dict[str, Any]]) -> dict[str, Any]:
    model = outs[0].get("json") or {}
    checks = _cites_out(model, prep)
    cited = {(c.get("doc"), c.get("line")) for c in _all_cites(model)}
    uncovered = [k for k in prep.facts.get("keyword_lines", []) if (k["doc"], k["line"]) not in cited][:25]
    listed = {parsers.norm_path(str(f.get("path", ""))).lower() for f in model.get("files", []) if isinstance(f, dict)}
    missed_paths = [p for p in prep.facts.get("paths_mentioned", []) if p["path"].lower() not in listed][:15]
    return {"digest": model, "citation_check": checks, "keyword_lines_not_cited": uncovered,
            "paths_mentioned_not_in_files": missed_paths}


def _all_cites(obj: Any) -> list[dict[str, Any]]:
    out = []
    if isinstance(obj, dict):
        if "quote" in obj:
            out.append(obj)
        for v in obj.values():
            out.extend(_all_cites(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_all_cites(v))
    return out


# ===================================================================== test logs

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "first_failure": {"type": "object", "properties": {
            "test": {"type": ["string", "null"]}, "likely_cause": {"type": "string"},
            "evidence": {"type": "array", "items": CITE}}, "required": ["likely_cause", "evidence"]},
        "other_failures": {"type": ["string", "null"]},
        "next_step": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["first_failure", "next_step", "confidence"],
}


def prep_test_logs(params: dict[str, Any], cfg: dict[str, Any], profile: dict[str, Any]) -> Prepared:
    docs, problems, red = load_docs(params, cfg, allow_dirs=True, exts={".xml", ".log", ".txt", ".out"})
    facts = parsers.parse_test_logs(docs)
    excerpt_text = []
    for ex in facts["first_errors"][:3]:
        doc_id = ex.get("doc") or "D1"
        excerpt_text.append(f"=== {doc_id}: {ex['file']} lines {ex['start']}-{ex['end']} ===\n" +
                            "\n".join(f"{doc_id}:{n}| {t}" for n, t in ex["lines"]))
    compact = {k: facts[k] for k in ("verdict_from_counts", "totals", "tests_not_executed_this_run")}
    compact["failing"] = [f.get("test") for f in facts["failing"][:15]]
    compact["suites"] = [{k: s.get(k) for k in ("runner", "file", "passed", "failed", "errors", "skipped", "build")
                          if s.get(k) is not None} for s in facts["suites"][:8]]
    system = ("Explain a failing test run to a developer. The FACTS block was computed by a parser and is correct: "
              "do not restate or change any number. From the numbered EXCERPTS only, explain the most likely cause "
              "of the first failure in plain words and the single most useful next step. If the excerpts are not "
              "enough to tell, say so and set confidence to low.\n" + CITE_RULES + "\n" + BASE_RULES)
    user = "FACTS:\n" + json.dumps(compact, ensure_ascii=False) + "\n\nEXCERPTS:\n" + ("\n\n".join(excerpt_text) or "(none)")
    script_result = {"facts": _trim_facts(facts)}
    calls = []
    has_failure = facts["totals"]["failed"] or facts["totals"]["errors"] or facts["first_errors"]
    if params.get("explain", True) and has_failure:
        calls.append(Call(_msgs(system, user), TRIAGE_SCHEMA, num_predict=450, out_p50=180, out_p90=320))
    prep = _base("test_log_triage", params, docs, calls, facts=facts, redactions=red, notes=problems,
                 claude_out_tokens=250, script_result=script_result)
    if not calls:
        prep.notes.append("no failure to explain; the script result is complete")
    return prep


def _trim_facts(facts: dict[str, Any]) -> dict[str, Any]:
    out = dict(facts)
    out["first_errors"] = [{"file": e["file"], "start": e["start"], "end": e["end"],
                            "text": "\n".join(f"{n}| {t}" for n, t in e["lines"][:25])} for e in facts["first_errors"][:2]]
    for s in out.get("suites", []):
        if "failing" in s:
            s["failing"] = s["failing"][:10]
    return out


def fin_test_logs(prep: Prepared, outs: list[dict[str, Any]]) -> dict[str, Any]:
    res = {"facts": _trim_facts(prep.facts)}
    if outs:
        model = outs[0].get("json") or {}
        res["explanation"] = model
        res["citation_check"] = _cites_out(model, prep)
    return res


# ===================================================================== diff summary

DIFF_SCHEMA = {
    "type": "object",
    "properties": {
        "areas": {"type": "array", "items": {"type": "object", "properties": {
            "area": {"type": "string"}, "summary": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}},
            "required": ["area", "summary"]}},
        "risks": {"type": "array", "items": {"type": "object", "properties": {
            "file": {"type": "string"}, "why": {"type": "string"}}, "required": ["file", "why"]}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["areas", "risks", "questions"],
}


def _area(path: str, areas: dict[str, str]) -> str:
    best = ""
    for prefix in areas:
        if path.lower().startswith(prefix.lower().rstrip("/") + "/") or path.lower() == prefix.lower():
            if len(prefix) > len(best):
                best = prefix
    if best:
        return areas[best]
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else "(root)")


def prep_diff(params: dict[str, Any], cfg: dict[str, Any], profile: dict[str, Any]) -> Prepared:
    repo_raw = params.get("repo")
    if not repo_raw:
        raise TaskInputError("give 'repo' (the repository folder)")
    ok, repo, reason = guard.check_path(str(repo_raw), {**cfg, "deny_globs": [g for g in cfg.get("deny_globs", [])
                                                                               if ".git" not in g]})
    if not ok or repo is None:
        raise TaskInputError(reason)
    rev = str(params.get("rev") or "HEAD")
    try:
        files = parsers.changed_files(repo, rev)
    except parsers.GitError as exc:
        raise TaskInputError(f"git: {exc}")
    protected = [guard._glob_regex(g) for g in cfg.get("protected_paths", [])]
    for f in files:
        f["area"] = _area(f["path"], cfg.get("areas") or {})
        f["protected"] = any(rx.fullmatch(f["path"].lower()) for rx in protected)
        f["generated_or_binary"] = bool(parsers.GENERATED.search(f["path"])) or bool(f.get("binary"))
    budget_tokens = _clamp(_prefill_tps(profile) * 150, 2000, 12000)
    per_file = int(params.get("max_diff_lines_per_file") or 80)
    excerpts, used, raw_chars = [], 0, 0
    deny = [guard._glob_regex(g) for g in cfg.get("deny_globs", []) if ".git/" not in g]
    for f in files:
        full = str(repo / f["path"]).replace("\\", "/").lower()
        if any(rx.fullmatch(full) for rx in deny):
            f["diff_not_shown"] = "matches the never-read rules"
            continue
        if f["generated_or_binary"] or f["status"] == "D":
            continue
        try:
            text, truncated = parsers.file_diff(repo, rev, f["path"], per_file)
        except parsers.GitError:
            continue
        text, _ = guard.redact(text)
        raw_chars += len(text)
        t = _tok(text, profile)
        if used + t > budget_tokens:
            f["diff_not_shown"] = "over the reading budget"
            continue
        used += t
        excerpts.append(f"--- {f['path']} ({f['area']}){' [truncated]' if truncated else ''}\n{text}")
    table = [{k: f.get(k) for k in ("path", "status", "added", "deleted", "area", "protected") if f.get(k) is not None}
             for f in files]
    facts = {"rev": rev, "repo": str(repo), "files": table, "file_count": len(files),
             "added": sum(f.get("added") or 0 for f in files), "deleted": sum(f.get("deleted") or 0 for f in files),
             "protected_hits": [f["path"] for f in files if f["protected"]],
             "areas": sorted({f["area"] for f in files}),
             "not_shown_to_model": [f["path"] for f in files if f.get("diff_not_shown") or f["generated_or_binary"]]}
    system = ("Summarize a code change for a reviewer, area by area. The FILE TABLE comes from git and is exact; do "
              "not restate counts. For each area, write one to three sentences on what changed, using only the diff "
              "excerpts. List risks worth a reviewer's attention (database migrations, row-level security or auth, "
              "secrets or keys, build or dependency changes, deletions, protected paths), each with the file and why. "
              "Ask at most 3 questions the diff raises. No verdicts. Return only JSON matching the schema.")
    user = ("FILE TABLE:\n" + json.dumps(table, ensure_ascii=False) + "\n\nDIFF EXCERPTS:\n" + "\n\n".join(excerpts))
    n_areas = max(1, len(facts["areas"]))
    p50 = _clamp(120 + 70 * n_areas, 200, 900)
    calls = [Call(_msgs(system, user), DIFF_SCHEMA, num_predict=int(min(1400, p50 * 1.8)), out_p50=p50,
                  out_p90=int(p50 * 1.5))] if files else []
    prep = Prepared(task="diff_summary", params=params, calls=calls, facts=facts, raw_chars=raw_chars + len(json.dumps(table)),
                    raw_lines=raw_chars // 40 + len(files), raw_kind="code", files_count=len(files),
                    claude_out_tokens=p50, script_result={"facts": facts},
                    system_hash=hashlib.sha1(system.encode()).hexdigest()[:12])
    return prep


def fin_diff(prep: Prepared, outs: list[dict[str, Any]]) -> dict[str, Any]:
    res = {"facts": prep.facts}
    if outs:
        res["summary"] = outs[0].get("json") or {}
    return res


# ===================================================================== file list comparison (script only)

def prep_filelists(params: dict[str, Any], cfg: dict[str, Any], profile: dict[str, Any]) -> Prepared:
    sources = params.get("sources") or []
    if len(sources) < 2:
        raise TaskInputError("give 'sources': at least two of {label, path | text | git (rev, 'staged' or 'worktree'), repo}")
    named: dict[str, list[str]] = {}
    raw_chars = 0
    for i, s in enumerate(sources):
        label = s.get("label") or f"source{i + 1}"
        if s.get("git"):
            ok, repo, reason = guard.check_path(str(s.get("repo") or ""), {**cfg, "deny_globs": []})
            if not ok or repo is None:
                raise TaskInputError(f"{label}: {reason}")
            try:
                named[label] = [f["path"] for f in parsers.changed_files(repo, str(s["git"]))]
            except parsers.GitError as exc:
                raise TaskInputError(f"{label}: git: {exc}")
        elif s.get("path"):
            ok, p, reason = guard.check_path(str(s["path"]), cfg)
            if not ok or p is None:
                raise TaskInputError(f"{label}: {reason}")
            text = textio.read_text(p)
            raw_chars += len(text)
            mode = s.get("mode") or ("mentions" if p.suffix.lower() in (".md", ".txt") else "lines")
            named[label] = [x["path"] for x in parsers.paths_in_text(text)] if mode == "mentions" else parsers.list_from_text(text)
        elif s.get("text"):
            raw_chars += len(s["text"])
            named[label] = parsers.list_from_text(s["text"])
        else:
            raise TaskInputError(f"{label}: needs path, text or git")
    result = parsers.compare_lists(named)
    return Prepared(task="filelist_compare", params=params, calls=[], facts=result, raw_chars=raw_chars,
                    raw_lines=raw_chars // 40, files_count=len(sources), claude_out_tokens=150, script_result=result)


# ===================================================================== hash check (script only)

def prep_hashes(params: dict[str, Any], cfg: dict[str, Any], profile: dict[str, Any]) -> Prepared:
    ok, manifest, reason = guard.check_path(str(params.get("manifest") or ""), cfg)
    if not ok or manifest is None:
        raise TaskInputError(f"manifest: {reason}")
    base = Path(params.get("base_dir") or manifest.parent)
    entries = parsers.parse_manifest(textio.read_text(manifest), manifest.suffix.lower())
    if not entries:
        raise TaskInputError("no (path, sha256) pairs found in the manifest")
    rows = {"match": [], "mismatch": [], "missing": []}
    for rel, expected in sorted(entries.items()):
        p = (base / rel).resolve()
        ok, rp, _ = guard.check_path(str(p), cfg)
        if not p.exists():
            rows["missing"].append(rel)
            continue
        if not ok or rp is None:
            rows["missing"].append(f"{rel} (not readable under the access rules)")
            continue
        actual = parsers.sha256_file(rp)
        (rows["match"] if actual == expected else rows["mismatch"]).append(
            rel if actual == expected else {"path": rel, "expected": expected, "actual": actual})
    result = {"manifest": str(manifest), "base_dir": str(base), "checked": len(entries),
              "matches": len(rows["match"]), "mismatches": rows["mismatch"], "missing": rows["missing"]}
    if params.get("list_matches"):
        result["match_list"] = rows["match"]
    return Prepared(task="hash_check", params=params, calls=[], facts=result, raw_chars=0, files_count=len(entries),
                    claude_out_tokens=100, script_result=result)


# ===================================================================== context packer

PACK_SCHEMA = {"type": "object", "properties": {"picks": {"type": "array", "items": {"type": "object", "properties": {
    "chunk": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}, "why": {"type": "string"}},
    "required": ["chunk", "start", "end"]}}}, "required": ["picks"]}


def prep_context_pack(params: dict[str, Any], cfg: dict[str, Any], profile: dict[str, Any]) -> Prepared:
    question = str(params.get("question") or "").strip()
    if not question:
        raise TaskInputError("give 'question'")
    docs, problems, red = load_docs(params, cfg, allow_dirs=True)
    chunks = []
    for d in docs:
        lines = d["text"].split("\n")
        for s, e in parsers.chunk_lines(d["text"]):
            body = "\n".join(lines[s - 1:e])
            chunks.append({"doc": d["id"], "path": d["path"], "start": s, "end": e, "text": body,
                           "tokens": parsers.tokens(d["name"] + " " + body)})
    if not chunks:
        raise TaskInputError("nothing to search")
    bm = parsers.BM25([c["tokens"] for c in chunks])
    scores = bm.scores(parsers.tokens(question))
    order = sorted(range(len(chunks)), key=lambda i: -scores[i])
    budget = _clamp(_prefill_tps(profile) * 120, 1500, 6000)
    chosen, used = [], 0
    for i in order:
        if scores[i] <= 0 or len(chosen) >= 24:
            break
        t = _tok(chunks[i]["text"], profile)
        if used + t > budget:
            continue
        used += t
        chosen.append(i)
    max_ret = int(params.get("max_return_tokens") or 2500)
    lexical = []
    ret_used = 0
    for rank, i in enumerate(order[:12]):
        if scores[i] <= 0:
            break
        c = chunks[i]
        t = _tok(c["text"], profile)
        if ret_used + t > max_ret:
            continue
        ret_used += t
        lexical.append({"path": c["path"], "start": c["start"], "end": c["end"], "score": round(scores[i], 2),
                        "text": c["text"]})
    k = int(params.get("max_passages") or 6)
    system = (f"Select the passages a reviewer needs to answer a question. Each chunk is labelled [C<n>] with its "
              f"file and line range; every line starts with its real line number. Pick at most {k} passages: chunk "
              "id, start line, end line (inclusive, inside that chunk) and why (at most 12 words). Prefer tight "
              "ranges that answer the question or are needed to understand the answer. If nothing is relevant, "
              "return an empty list. Return only JSON matching the schema.")
    body = []
    for n, i in enumerate(chosen, start=1):
        c = chunks[i]
        numbered = "\n".join(f"{c['start'] + j}| {line}" for j, line in enumerate(c["text"].split("\n")))
        body.append(f"[C{n}] {c['path']} lines {c['start']}-{c['end']}\n{numbered}")
    user = f"QUESTION: {question}\n\n" + "\n\n".join(body)
    calls = [Call(_msgs(system, user), PACK_SCHEMA, num_predict=350, out_p50=120, out_p90=250)] if chosen else []
    prep = _base("context_pack", params, docs, calls, redactions=red, notes=problems,
                 claude_out_tokens=60, script_result={"passages": lexical, "method": "keyword ranking (BM25)"})
    prep.extra = {"chosen": [chunks[i] for i in chosen], "max_return_tokens": max_ret,
                  "lines": {d["path"]: d["text"].split("\n") for d in docs}}
    prep.facts = {"files_searched": len(docs), "chunks": len(chunks), "candidates_for_model": len(chosen)}
    return prep


def fin_context_pack(prep: Prepared, outs: list[dict[str, Any]]) -> dict[str, Any]:
    model = (outs[0].get("json") if outs else None) or {}
    chosen = prep.extra.get("chosen", [])
    lines_by_path = prep.extra.get("lines", {})
    passages, used = [], 0
    for pick in model.get("picks", []):
        try:
            idx = int(str(pick.get("chunk", "")).lstrip("Cc")) - 1
            c = chosen[idx]
        except (ValueError, IndexError):
            continue
        s = max(c["start"], int(pick.get("start") or c["start"]))
        e = min(c["end"], int(pick.get("end") or c["end"]))
        if e < s:
            s, e = c["start"], c["end"]
        text = "\n".join(lines_by_path[c["path"]][s - 1:e])
        t = textio.estimate_tokens(text)
        if used + t > prep.extra.get("max_return_tokens", 2500):
            continue
        used += t
        passages.append({"path": c["path"], "start": s, "end": e, "why": pick.get("why"), "text": text})
    passages.sort(key=lambda p: (p["path"], p["start"]))
    merged: list[dict[str, Any]] = []
    for p in passages:
        if merged and merged[-1]["path"] == p["path"] and p["start"] <= merged[-1]["end"] + 1:
            m = merged[-1]
            if p["end"] > m["end"]:
                m["text"] = "\n".join(lines_by_path[p["path"]][m["start"] - 1:p["end"]])
                m["end"] = p["end"]
            continue
        merged.append(p)
    if not merged:
        return {"passages": prep.script_result["passages"] if prep.script_result else [],
                "method": "keyword ranking (model picked nothing usable)"}
    return {"passages": merged, "method": "keyword shortlist, then local model picked exact line ranges; text is copied verbatim"}


# ===================================================================== job posting checklist

POSTING_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": ["string", "null"]}, "organization": {"type": ["string", "null"]},
        "location": {"type": ["string", "null"]}, "work_mode": {"type": ["string", "null"]},
        "seniority": {"type": ["string", "null"]},
        "languages_required": {"type": "array", "items": {"type": "string"}},
        "deadline": {"type": ["object", "null"], "properties": {"text": {"type": "string"}, "cite": CITE}},
        "must_have": {"type": "array", "items": {"type": "object", "properties": {
            "item": {"type": "string"}, "cite": CITE}, "required": ["item", "cite"]}},
        "nice_to_have": {"type": "array", "items": {"type": "object", "properties": {
            "item": {"type": "string"}, "cite": CITE}, "required": ["item", "cite"]}},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "organization", "must_have", "nice_to_have", "keywords"],
}


def prep_posting(params: dict[str, Any], cfg: dict[str, Any], profile: dict[str, Any]) -> Prepared:
    """One call per posting; several postings in one job share the cached instructions."""
    docs, problems, red = load_docs(params, cfg, ids=[f"P{i}" for i in range(1, 51)])
    system = ("Turn a job posting into a requirements checklist. Must-haves are stated as required, must, minimum "
              "or essential; nice-to-haves as preferred, bonus, asset or a plus. Keywords: tools, methods, domains "
              "and job titles named in the posting, as written. Write items in English; keep quotes in the "
              "posting's own language.\n" + CITE_RULES + "\n" + BASE_RULES)
    calls = []
    for d in docs:
        user = _numbered([d])
        in_tok = _tok(user, profile)
        p50 = _clamp(260 + 0.15 * in_tok, 300, 900)
        calls.append(Call(_msgs(system, user), POSTING_SCHEMA, num_predict=int(min(1500, p50 * 1.8)), out_p50=p50,
                          out_p90=int(p50 * 1.5), label=d["id"]))
    return _base("posting_checklist", params, docs, calls, redactions=red, notes=problems,
                 claude_out_tokens=sum(c.out_p50 for c in calls))


def fin_posting(prep: Prepared, outs: list[dict[str, Any]]) -> dict[str, Any]:
    items = []
    for meta, o in zip(prep.doc_meta, outs):
        model = o.get("json") or {}
        items.append({"posting": meta["path"], "checklist": model,
                      "citation_check": verify.verify_cites(model, {meta["id"]: prep.docs[meta["id"]]}, meta["id"])})
    return items[0] if len(items) == 1 else {"postings": items}


# ===================================================================== CV vs posting

CV_SCHEMA = {
    "type": "object",
    "properties": {
        "requirements": {"type": "array", "items": {"type": "object", "properties": {
            "requirement": {"type": "string"}, "kind": {"type": "string", "enum": ["must", "nice"]},
            "status": {"type": "string", "enum": ["covered", "partial", "missing"]},
            "posting_cite": CITE, "cv_cite": {"type": ["object", "null"], "properties": CITE["properties"]},
            "note": {"type": ["string", "null"]}}, "required": ["requirement", "kind", "status", "posting_cite"]}},
        "keywords_to_check": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["requirements", "keywords_to_check"],
}


def prep_cv_match(params: dict[str, Any], cfg: dict[str, Any], profile: dict[str, Any]) -> Prepared:
    post, p1, r1 = load_docs({"paths": params.get("posting_path"), "text": params.get("posting_text")}, cfg, ids=["P"])
    cv, p2, r2 = load_docs({"paths": params.get("cv_path"), "text": params.get("cv_text")}, cfg, ids=["C"])
    docs = [post[0], cv[0]]
    docs[1]["id"] = "C"
    system = ("Compare a CV (lines C:n) with a job posting (lines P:n). For each requirement in the posting, decide "
              "whether the CV covers it: covered (clear evidence), partial (related but weaker) or missing. Cite the "
              "posting line, and the CV line when covered or partial. Do not judge overall fit and do not suggest "
              "edits. Also list up to 15 keywords from the posting to check against the CV.\n" + CITE_RULES +
              "\n" + BASE_RULES)
    user = _numbered(docs)
    in_tok = _tok(user, profile)
    p50 = _clamp(420 + 0.08 * in_tok, 500, 1300)
    call = Call(_msgs(system, user), CV_SCHEMA, num_predict=int(min(2000, p50 * 1.7)), out_p50=p50, out_p90=int(p50 * 1.45))
    red = {k: r1.get(k, 0) + r2.get(k, 0) for k in set(r1) | set(r2)}
    return _base("cv_match", params, docs, [call], redactions=red, notes=p1 + p2, claude_out_tokens=p50)


def fin_cv_match(prep: Prepared, outs: list[dict[str, Any]]) -> dict[str, Any]:
    model = outs[0].get("json") or {}
    checks = _cites_out(model, prep)
    cv_text = "\n".join(prep.docs.get("C", []))
    present, absent = [], []
    for kw in model.get("keywords_to_check", [])[:25]:
        (present if packs._has(kw, cv_text) else absent).append(kw)
    counts: dict[str, int] = {}
    for r in model.get("requirements", []):
        counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
    return {"match": model, "status_counts": counts, "citation_check": checks,
            "keywords_in_cv (exact check)": present, "keywords_not_in_cv (exact check)": absent}


# ===================================================================== letters & forms

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "actions": {"type": "array", "items": {"type": "object", "properties": {
            "action": {"type": "string"}, "due": {"type": ["string", "null"]}, "cite": CITE}, "required": ["action", "cite"]}},
        "dates": {"type": "array", "items": {"type": "object", "properties": {
            "date_text": {"type": "string"}, "meaning": {"type": "string"}, "cite": CITE}, "required": ["date_text", "meaning", "cite"]}},
        "contacts": {"type": "array", "items": {"type": "object", "properties": {
            "who": {"type": "string"}, "how": {"type": "string"}, "cite": CITE}, "required": ["who", "cite"]}},
        "terms": {"type": "array", "items": {"type": "object", "properties": {
            "term": {"type": "string"}, "plain": {"type": "string"}}, "required": ["term", "plain"]}},
    },
    "required": ["summary", "actions", "dates", "contacts", "terms"],
}

LANG_NAMES = {"en": "English", "fr": "French", "he": "Hebrew", "es": "Spanish", "de": "German", "ar": "Arabic",
              "ru": "Russian"}


def prep_doc_summary(params: dict[str, Any], cfg: dict[str, Any], profile: dict[str, Any]) -> Prepared:
    """One call per document, so several letters can be summarized in one job."""
    docs, problems, red = load_docs(params, cfg)
    lang = LANG_NAMES.get(str(params.get("language") or "en").lower(), str(params.get("language") or "English"))
    system = (f"Summarize a letter or form for the person who must act on it. Write in {lang}, in plain words. "
              "Summary: at most 80 words. List every action the reader must take (with its deadline if stated), "
              "every date and what it is for, and who to contact and how. When the text uses a technical, legal or "
              "administrative term, keep the term and explain it in plain words under 'terms'.\n" + CITE_RULES +
              "\n" + BASE_RULES)
    calls = []
    for d in docs:
        user = _numbered([d])
        in_tok = _tok(user, profile)
        p50 = _clamp(260 + 0.08 * in_tok, 300, 900)
        calls.append(Call(_msgs(system, user), SUMMARY_SCHEMA, num_predict=int(min(1500, p50 * 1.8)), out_p50=p50,
                          out_p90=int(p50 * 1.5), label=d["id"]))
    facts = {d["id"]: parsers.letter_facts(d["text"]) for d in docs}
    return _base("doc_summary", params, docs, calls, facts=facts, redactions=red, notes=problems,
                 claude_out_tokens=sum(c.out_p50 for c in calls), file_only_default=bool(params.get("file_only", False)))


def fin_doc_summary(prep: Prepared, outs: list[dict[str, Any]]) -> dict[str, Any]:
    items = []
    for meta, o in zip(prep.doc_meta, outs):
        model = o.get("json") or {}
        doc = meta["id"]
        checks = verify.verify_cites(model, {doc: prep.docs[doc]}, doc)
        used_lines = {c.get("line") for c in _all_cites(model)}
        missed = [d for d in prep.facts.get(doc, {}).get("dates", []) if d["line"] not in used_lines][:20]
        items.append({"document": meta["path"], "summary": model, "citation_check": checks,
                      "dates_found_by_script_but_not_used": missed, "facts_from_script": prep.facts.get(doc)})
    return items[0] if len(items) == 1 else {"documents": items}


# ===================================================================== translation

def prep_translate(params: dict[str, Any], cfg: dict[str, Any], profile: dict[str, Any]) -> Prepared:
    docs, problems, red = load_docs(params, cfg)
    target = str(params.get("target") or "en").lower()
    lang = LANG_NAMES.get(target, target)
    system = (f"Translate the user's text into {lang}. Keep the meaning, names, numbers, dates and formatting (line "
              "breaks, lists, headings). Do not add notes or explanations. Output only the translation.")
    ratios = (profile or {}).get("token_ratio") or {}
    calls = []
    for d in docs:
        for i, chunk in enumerate(textio.split_paragraph_chunks(d["text"], 4500)):
            in_tok = textio.estimate_tokens(chunk, ratios, d["kind"])
            factor = 1.35 if target == "he" else 1.15
            p50 = int(in_tok * factor) + 20
            calls.append(Call(_msgs(system, chunk), None, num_predict=int(in_tok * 1.8) + 80, out_p50=p50,
                              out_p90=int(p50 * 1.25), label=f"{d['id']} part {i + 1}"))
    prep = _base("translate", params, docs, calls, redactions=red, notes=problems,
                 claude_out_tokens=sum(c.out_p50 for c in calls),
                 file_only_default=bool(params.get("file_only", True)))
    prep.extra = {"target": target}
    return prep


def fin_translate(prep: Prepared, outs: list[dict[str, Any]]) -> dict[str, Any]:
    text = "\n\n".join(o.get("text", "").strip() for o in outs)
    return {"translation": text, "target": prep.extra.get("target"), "parts": len(outs),
            "words": len(text.split()), "_file_ext": ".txt"}


# ===================================================================== drafting

DRAFT_SIZES = {"message": (180, 300, 420), "email": (300, 520, 750), "cover_letter": (650, 850, 1150),
               "log_entry": (150, 260, 400), "checklist_line": (60, 120, 200), "other": (300, 600, 850)}


def prep_draft(params: dict[str, Any], cfg: dict[str, Any], profile: dict[str, Any]) -> Prepared:
    notes = str(params.get("notes") or params.get("bullets") or "").strip()
    if not notes:
        raise TaskInputError("give 'notes' (the facts and points to cover)")
    kind = str(params.get("kind") or "other")
    if kind not in DRAFT_SIZES:
        kind = "other"
    lang = LANG_NAMES.get(str(params.get("language") or "en").lower(), str(params.get("language")))
    pk = _pack_list(params.get("packs"))
    pack_text = "\n\n".join(f"--- {p['name']} ---\n{p['guidance']}" for p in pk)
    ctx_docs, problems, red = load_docs(params, cfg, key="context_paths", text_key="context_text", required=False)
    system = (f"Write a first draft for the user to review and edit. Kind: {kind.replace('_', ' ')}. Language: {lang}. "
              "Follow the rules below exactly. Use only the facts in the notes and context; where something needed is "
              "missing, write [[TODO: what is needed]] instead of inventing it. Output only the draft text, with no "
              "preamble or commentary." + (f"\n\nRULES:\n{pack_text}" if pack_text else ""))
    ctx = "\n\n".join(f"CONTEXT ({d['name']}):\n{d['text']}" for d in ctx_docs)
    user = f"NOTES:\n{notes}" + (f"\n\n{ctx}" if ctx else "") + (f"\n\nRecipient: {params['to']}" if params.get("to") else "")
    p50, p90, cap = DRAFT_SIZES[kind]
    call = Call(_msgs(system, user), None, num_predict=cap, out_p50=p50, out_p90=p90, temperature=0.3)
    prep = _base("draft", params, ctx_docs or [{"id": "N", "path": "(notes)", "name": "notes", "text": notes, "ext": "",
                                                "kind": textio.content_kind(notes)}], [call],
                 redactions=red, notes=problems, claude_out_tokens=p50,
                 file_only_default=bool(params.get("file_only", False)))
    prep.extra = {"packs": pk, "target": params.get("target") or kind}
    prep.raw_chars = len(notes) + sum(len(d["text"]) for d in ctx_docs)
    prep.inline_chars = len(notes) + sum(len(d["text"]) for d in ctx_docs if d["path"] == "(inline text)")
    return prep


def fin_draft(prep: Prepared, outs: list[dict[str, Any]]) -> dict[str, Any]:
    text = re.sub(r"(?s)<think>.*?</think>", "", outs[0].get("text", "")).strip()
    todos = re.findall(r"\[\[TODO:[^\]]*\]\]", text)
    rule_check = packs.check(text, prep.extra.get("packs", []), prep.extra.get("target"))
    return {"label": "DRAFT - review before use", "draft": text, "todos": todos, **rule_check, "_file_ext": ".md"}


# ===================================================================== rules check

RULES_SCHEMA = {"type": "object", "properties": {"violations": {"type": "array", "items": {"type": "object", "properties": {
    "rule": {"type": "string"}, "cite": CITE, "fix": {"type": "string"}}, "required": ["rule", "cite"]}}},
    "required": ["violations"]}


def prep_rules_check(params: dict[str, Any], cfg: dict[str, Any], profile: dict[str, Any]) -> Prepared:
    docs, problems, red = load_docs(params, cfg)
    pk = _pack_list(params.get("packs"))
    if not pk:
        raise TaskInputError(f"give 'packs' (available: {', '.join(packs.available())})")
    target = params.get("target")
    script = {d["id"]: packs.check(d["text"], pk, target) for d in docs}
    guidance = "\n\n".join(f"--- {p['name']} ---\n{p['guidance']}" for p in pk)
    system = ("Check a draft against the user's rules. The exact-term rules were already checked by a script (see SCRIPT "
              "FINDINGS; do not repeat them). Check the remaining written rules below. Report only clear violations, "
              "each with the rule, a citation of the offending line and a suggested fix of at most 15 words. If there "
              "are none, return an empty list.\n" + CITE_RULES + "\n" + BASE_RULES + f"\n\nRULES:\n{guidance}")
    user = ("SCRIPT FINDINGS:\n" + json.dumps({k: v["script_findings"] for k, v in script.items()}, ensure_ascii=False)
            + (f"\n\nTARGET: {target}" if target else "") + "\n\nDRAFT:\n" + _numbered(docs))
    calls = [] if params.get("script_only") else [Call(_msgs(system, user), RULES_SCHEMA, num_predict=500, out_p50=140, out_p90=300)]
    return _base("rules_check", params, docs, calls, facts=script, redactions=red, notes=problems, claude_out_tokens=200,
                 script_result={"script_findings": script})


def fin_rules_check(prep: Prepared, outs: list[dict[str, Any]]) -> dict[str, Any]:
    res: dict[str, Any] = {"script_findings": prep.facts}
    if outs:
        model = outs[0].get("json") or {}
        res["model_findings"] = model.get("violations", [])
        res["citation_check"] = _cites_out(model, prep)
    return res


# ===================================================================== freeform

def prep_freeform(params: dict[str, Any], cfg: dict[str, Any], profile: dict[str, Any]) -> Prepared:
    instruction = str(params.get("instruction") or "").strip()
    if not instruction:
        raise TaskInputError("give 'instruction'")
    docs, problems, red = load_docs(params, cfg, required=False)
    max_words = int(params.get("max_words") or 250)
    as_json = params.get("output") == "json"
    system = ("Follow the instruction using only the text provided, if any. Be concise: at most "
              f"{max_words} words. " + ("Return only JSON." if as_json else "Output only the answer."))
    user = f"INSTRUCTION: {instruction}" + (("\n\n" + _numbered(docs)) if docs else "")
    p50 = int(max_words * 1.0)
    call = Call(_msgs(system, user), {"type": "object"} if as_json else None, num_predict=int(max_words * 2.2) + 40,
                out_p50=p50, out_p90=int(max_words * 1.6))
    prep = _base("freeform", params, docs or [{"id": "I", "path": "(instruction)", "name": "instruction",
                                               "text": instruction, "ext": "", "kind": "prose"}],
                 [call], redactions=red, notes=problems, claude_out_tokens=p50,
                 file_only_default=bool(params.get("file_only", False)))
    return prep


def fin_freeform(prep: Prepared, outs: list[dict[str, Any]]) -> dict[str, Any]:
    o = outs[0]
    if o.get("json") is not None:
        return {"answer": o["json"]}
    return {"answer": re.sub(r"(?s)<think>.*?</think>", "", o.get("text", "")).strip(), "_file_ext": ".md"}


# ===================================================================== checks for the fix-up pass (deep mode)
# Each returns plain-language problems found by scripts in one answer; an empty list means the
# answer passes every automatic check, so no fix-up pass is spent on it.

def _quote_issues(model: Any, docs: dict[str, list[str]], default_doc: str | None = None, limit: int = 8) -> list[str]:
    if not isinstance(model, (dict, list)):
        return []
    res = verify.verify_cites(copy.deepcopy(model), docs, default_doc)
    return [f"The quote \"{u['quote']}\" (cited as {u['doc']} line {u['line']}) is not in the text. Quote the exact "
            "words from the line you mean, or drop the item." for u in res.get("unverified_quotes", [])[:limit]]


def _base_issues(call: Call, out: dict[str, Any]) -> list[str]:
    if call.schema is not None:
        if out.get("json") is None:
            return ["The answer was not valid JSON" + (" because it was cut off; keep it shorter" if out.get("truncated")
                                                       else "") + "."]
        return [f"Format problem: {e}." for e in out.get("schema_errors", [])[:6]]
    if out.get("truncated"):
        return ["The answer was cut off before the end; make it complete but more concise."]
    return []


def issues_generic(prep: Prepared, idx: int, out: dict[str, Any]) -> list[str]:
    found = _base_issues(prep.calls[idx], out)
    if out.get("json") is not None:
        found += _quote_issues(out["json"], prep.docs)
    return found


def _issues_one_doc(prep: Prepared, idx: int, out: dict[str, Any]) -> list[str]:
    found = _base_issues(prep.calls[idx], out)
    doc = prep.doc_meta[idx]["id"] if idx < len(prep.doc_meta) else None
    if out.get("json") is not None and doc in prep.docs:
        found += _quote_issues(out["json"], {doc: prep.docs[doc]}, doc)
    return found


def issues_digest(prep: Prepared, idx: int, out: dict[str, Any]) -> list[str]:
    found = issues_generic(prep, idx, out)
    model = out.get("json")
    if isinstance(model, dict):
        cited = {(c.get("doc"), c.get("line")) for c in _all_cites(model)}
        for k in [k for k in prep.facts.get("keyword_lines", []) if (k["doc"], k["line"]) not in cited][:6]:
            found.append(f"Line {k['doc']}:{k['line']} (\"{k['text'][:90]}\") looks like an approval, a result or an "
                         "open item and is not in the digest; add it where it belongs if a reviewer needs it.")
        listed = {parsers.norm_path(str(f.get("path", ""))).lower() for f in model.get("files", []) if isinstance(f, dict)}
        for p_ in [p_ for p_ in prep.facts.get("paths_mentioned", []) if p_["path"].lower() not in listed][:5]:
            found.append(f"{p_['path']} is mentioned on line {p_['doc']}:{p_['line']}; if the report says it was "
                         "created, changed or deleted, list it under files.")
    return found


def issues_doc_summary(prep: Prepared, idx: int, out: dict[str, Any]) -> list[str]:
    found = _issues_one_doc(prep, idx, out)
    model = out.get("json")
    doc = prep.doc_meta[idx]["id"] if idx < len(prep.doc_meta) else None
    if isinstance(model, dict) and doc:
        used = {c.get("line") for c in _all_cites(model)}
        for d in [d for d in prep.facts.get(doc, {}).get("dates", []) if d["line"] not in used][:6]:
            found.append(f"The date \"{d['text']}\" on line {doc}:{d['line']} is not covered; add it to dates "
                         "(and to actions if something is due).")
    return found


def issues_cv(prep: Prepared, idx: int, out: dict[str, Any]) -> list[str]:
    found = issues_generic(prep, idx, out)
    model = out.get("json")
    if isinstance(model, dict):
        for r in model.get("requirements", [])[:40]:
            if isinstance(r, dict) and r.get("status") in ("covered", "partial") and not r.get("cv_cite"):
                found.append(f"\"{str(r.get('requirement'))[:80]}\" is marked {r.get('status')} without a CV line; cite "
                             "the CV line or mark it missing.")
    return found[:14]


def issues_diff(prep: Prepared, idx: int, out: dict[str, Any]) -> list[str]:
    found = _base_issues(prep.calls[idx], out)
    model = out.get("json")
    if isinstance(model, dict):
        named = {str(a.get("area", "")).lower() for a in model.get("areas", []) if isinstance(a, dict)}
        for area in [a for a in prep.facts.get("areas", []) if a.lower() not in named][:6]:
            found.append(f"The area \"{area}\" has changes but no summary.")
    return found


def issues_context_pack(prep: Prepared, idx: int, out: dict[str, Any]) -> list[str]:
    found = _base_issues(prep.calls[idx], out)
    n = len(prep.extra.get("chosen", []))
    model = out.get("json")
    if isinstance(model, dict):
        for pick in model.get("picks", []):
            try:
                ok = 1 <= int(str(pick.get("chunk", "")).lstrip("Cc")) <= n
            except ValueError:
                ok = False
            if not ok:
                found.append(f"Chunk id {pick.get('chunk')!r} does not exist; use ids C1 to C{n}.")
    return found


NUMBER = re.compile(r"(?<![\w.])\d[\d.,:/-]*\d|(?<![\w.])\d(?![\w])")


def issues_translate(prep: Prepared, idx: int, out: dict[str, Any]) -> list[str]:
    found = _base_issues(prep.calls[idx], out)
    src = prep.calls[idx].messages[-1]["content"]
    text = out.get("text") or ""
    ratio = len(text) / max(1, len(src))
    if ratio < 0.45:
        found.append("The translation is much shorter than the source; check for skipped passages and return the "
                     "full translation.")
    elif ratio > 2.5:
        found.append("The translation is much longer than the source; remove repetitions or added commentary.")
    missing = sorted({n for n in NUMBER.findall(src) if n not in text})[:10]
    if missing:
        found.append("These numbers from the source are missing in the translation: " + ", ".join(missing) + ".")
    return found


def issues_draft(prep: Prepared, idx: int, out: dict[str, Any]) -> list[str]:
    found = _base_issues(prep.calls[idx], out)
    text = re.sub(r"(?s)<think>.*?</think>", "", out.get("text") or "")
    check = packs.check(text, prep.extra.get("packs", []), prep.extra.get("target"))
    for h in check["script_findings"]:
        if h["severity"] == "violation":
            where = f" (line {h['line']}: \"{h['quote'][:80]}\")" if h.get("line") else ""
            found.append(f"Rule broken: {h['rule']}{where}.")
    return found


def issues_text(prep: Prepared, idx: int, out: dict[str, Any]) -> list[str]:
    return _base_issues(prep.calls[idx], out)


def repair_messages(call: Call, out: dict[str, Any], issues: list[str]) -> list[dict[str, str]]:
    """The original conversation, the previous answer, and the list of problems to fix. The shared
    beginning is still in Ollama's prompt cache, so only the new part has to be read."""
    ask = ("Automatic checks found these problems in your answer:\n" + "\n".join(f"- {i}" for i in issues[:12]) +
           "\n\nFix them, keep everything else that was right, and return the complete corrected answer" +
           (" as JSON matching the same schema." if call.schema is not None else " in the same format, with no commentary."))
    return list(call.messages) + [{"role": "assistant", "content": out.get("text") or ""},
                                  {"role": "user", "content": ask}]


# ===================================================================== registry

TASKS: dict[str, Task] = {t.name: t for t in [
    Task("digest", "Digest reports or logs", "llm", "M1",
         "Agent report, coordinator log or any document -> fixed template with line citations (every quote checked).",
         {"paths": "files", "text": "or inline text", "template": "agent_report | coordinator_log | generic",
          "since": "YYYY-MM-DD (logs: keep only entries from this date)", "focus": "optional question",
          "packs": "optional pack names"}, prep_digest, fin_digest, think_factor=1.2, issues=issues_digest),
    Task("test_log_triage", "Triage test logs", "hybrid", "M0",
         "Gradle/pytest/Deno/Jest/Vitest/Node logs and JUnit XML -> exact counts, failing tests, cached-vs-executed "
         "test tasks and first-error excerpts (script); optional plain-words cause and next step (model).",
         {"paths": "log files or folders of JUnit XML", "text": "or pasted log", "explain": "true (default) | false"},
         prep_test_logs, fin_test_logs, tier="fast_ok", script_substitutes=True, issues=issues_generic),
    Task("diff_summary", "Summarize a commit or diff", "hybrid", "M0",
         "git change -> exact file table with areas and protected-path flags (script); per-area summary and risks (model).",
         {"repo": "repository folder", "rev": "HEAD | <commit> | A..B | staged | worktree",
          "max_diff_lines_per_file": "default 80"}, prep_diff, fin_diff, think_factor=1.2, issues=issues_diff),
    Task("filelist_compare", "Compare file lists", "script", "M0",
         "Report vs commit vs staging list -> files missing from each (exact, instant).",
         {"sources": "list of {label, path | text | git: rev|staged|worktree, repo, mode: mentions|lines}"},
         prep_filelists, None),
    Task("hash_check", "Check hashes against a manifest", "script", "M0",
         "Manifest (sha256sum lines, JSON or CSV) -> match / mismatch / missing (exact, instant).",
         {"manifest": "manifest file", "base_dir": "default: manifest's folder", "list_matches": "false"},
         prep_hashes, None),
    Task("context_pack", "Pack relevant passages", "hybrid", "M2",
         "Question + files/folders -> only the passages that matter, verbatim with line ranges (keyword ranking; "
         "optionally refined by the model).",
         {"question": "what you need to answer", "paths": "files or folders", "max_return_tokens": "default 2500",
          "max_passages": "default 6"}, prep_context_pack, fin_context_pack, tier="fast_ok",
         script_substitutes=True, think_factor=0.6, issues=issues_context_pack),
    Task("posting_checklist", "Job posting -> checklist", "llm", "M0",
         "Posting(s) -> must-haves, nice-to-haves, keywords, languages, deadline, each quoted. Batch several "
         "postings in one job: a single posting is usually cheaper to read here.",
         {"paths": "one or more posting files", "text": "or one pasted posting"}, prep_posting, fin_posting,
         tier="fast_ok", think_factor=0.8, issues=_issues_one_doc, partial_ok=True),
    Task("cv_match", "CV vs posting", "llm", "M2",
         "Each requirement -> covered / partial / missing with CV evidence lines; keywords checked exactly.",
         {"posting_path": "or posting_text", "cv_path": "or cv_text"}, prep_cv_match, fin_cv_match,
         think_factor=1.5, issues=issues_cv),
    Task("doc_summary", "Letter or form -> actions and dates", "llm", "M0",
         "Plain-words summary, actions with deadlines, dates, contacts, terms explained; dates cross-checked by script.",
         {"paths": "one or more files", "text": "or pasted text", "language": "output language (en default)",
          "file_only": "true keeps the result on disk only"}, prep_doc_summary, fin_doc_summary,
         issues=issues_doc_summary, partial_ok=True),
    Task("translate", "Translate for reading", "llm", "M0",
         "Document -> translation in chunks; saved to a local file by default (Claude sees only the path).",
         {"paths": "file", "text": "or pasted text", "target": "en | fr | he | ...", "file_only": "default true"},
         prep_translate, fin_translate, think_factor=0.8, issues=issues_translate, partial_ok=True),
    Task("draft", "First draft from notes", "llm", "M1/M3",
         "Notes + rule packs -> a draft labelled DRAFT, with [[TODO]] gaps and exact-term rule checks.",
         {"notes": "facts and points", "kind": "message | email | cover_letter | log_entry | checklist_line | other",
          "language": "en | fr | he", "packs": "pack names", "to": "recipient role", "target": "rule scope, e.g. website",
          "context_paths": "optional files"}, prep_draft, fin_draft, issues=issues_draft),
    Task("rules_check", "Check a draft against rule packs", "hybrid", "M3",
         "Exact-term rules by script (instant); written rules by the model, each finding quoted.",
         {"paths": "file", "text": "or pasted draft", "packs": "pack names", "target": "cv | cover_letter | website | ...",
          "script_only": "false"}, prep_rules_check, fin_rules_check, script_substitutes=True,
         think_factor=0.8, issues=issues_generic),
    Task("freeform", "Small free-form job", "llm", "M0",
         "Instruction (+ optional files) -> short answer. Use for format conversion or explaining an error.",
         {"instruction": "what to do", "paths": "optional files", "text": "optional text", "max_words": "default 250",
          "output": "text | json"}, prep_freeform, fin_freeform, think_factor=0.8, issues=issues_text),
]}


def catalog() -> list[dict[str, Any]]:
    return [{"task": t.name, "title": t.title, "kind": t.kind, "memory": t.memory, "what": t.summary, "params": t.params}
            for t in TASKS.values()]
