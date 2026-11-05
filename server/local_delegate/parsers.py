"""Deterministic preprocessing: everything here is exact and instant, no model involved.

Numbers, file lists, hashes, dates and test counts always come from these functions,
never from model text.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ===================================================================== test logs

SUMMARY_LINE = re.compile(
    r"^(?:=+ .* in [\d.]+s|BUILD (?:SUCCESSFUL|FAILED)|(?:ok|FAILED) \| \d+ passed|Tests:\s|Test Suites:\s|"
    r"\s*Tests\s+\d|[#ℹ] (?:tests|pass|fail|skipped|todo|cancelled) \d+|\d+ tests? completed|"
    r"\d+ actionable tasks?:|FAILURE: Build failed)", re.IGNORECASE)

ERROR_MARK = re.compile(
    r"(?:\bFAILED\b|\bFAILURE\b|^E\s{2,}\S|Traceback \(most recent call last\)|AssertionError|"
    r"\b\w*Exception\b|^e: |\berror(?:\[\w+\])?:|\bError:|✖|^\s*●\s|^not ok\b|panicked at|Caused by:|"
    r"^\s*at .*\(.*:\d+\)$|\bassert\b.*==)", re.IGNORECASE)


def _counts(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for n, word in re.findall(r"(\d+)\s+(failed|passed|skipped|errors?|xfailed|xpassed|deselected|ignored|"
                              r"todo|total|pending)", text, flags=re.IGNORECASE):
        key = word.lower()
        key = "errors" if key.startswith("error") else key
        out[key] = out.get(key, 0) + int(n)
    return out


def _pytest(lines: list[str], name: str) -> dict[str, Any] | None:
    for i in range(len(lines) - 1, -1, -1):
        m = re.match(r"^=+ (.+?) in ([\d.]+)s\b.*=+\s*$", lines[i].strip())
        if m and re.search(r"passed|failed|error|skipped|no tests ran", m.group(1)):
            c = _counts(m.group(1))
            return {"runner": "pytest", "file": name, "line": i + 1, "passed": c.get("passed", 0),
                    "failed": c.get("failed", 0), "errors": c.get("errors", 0), "skipped": c.get("skipped", 0),
                    "duration_s": float(m.group(2))}
    return None


def _gradle(lines: list[str], name: str) -> dict[str, Any] | None:
    res: dict[str, Any] = {}
    test_tasks: list[dict[str, Any]] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        m = re.match(r"^BUILD (SUCCESSFUL|FAILED) in (.+)$", line)
        if m:
            res.update(build=m.group(1).lower(), build_time=m.group(2), build_line=i + 1)
        m = re.search(r"(\d+) tests? completed, (\d+) failed(?:, (\d+) skipped)?", line)
        if m:
            res["completed"] = res.get("completed", 0) + int(m.group(1))
            res["failed"] = res.get("failed", 0) + int(m.group(2))
            res["skipped"] = res.get("skipped", 0) + int(m.group(3) or 0)
        m = re.match(r"^(\d+) actionable tasks?: (.+)$", line)
        if m:
            parts = dict((k.replace("-", "_").replace(" ", "_"), int(v))
                         for v, k in re.findall(r"(\d+) ([a-z -]+?)(?:,|$)", m.group(2)))
            res["tasks"] = {"actionable": int(m.group(1)), **parts}
        m = re.match(r"^> Task (\S+)(?:\s+(UP-TO-DATE|FROM-CACHE|NO-SOURCE|SKIPPED|FAILED))?$", line)
        if m and re.search(r"test", m.group(1), re.IGNORECASE):
            outcome = (m.group(2) or "EXECUTED").lower().replace("-", "_")
            test_tasks.append({"task": m.group(1), "outcome": outcome, "line": i + 1})
        m = re.search(r"Tests on (.+?) failed: (\d+) of (\d+)", line)
        if m:
            res.setdefault("devices", []).append({"device": m.group(1), "failed": int(m.group(2)),
                                                  "total": int(m.group(3))})
    if test_tasks:
        res["test_tasks"] = test_tasks
        not_run = [t["task"] for t in test_tasks if t["outcome"] in ("up_to_date", "from_cache", "no_source", "skipped")]
        if not_run:
            res["tests_not_executed_this_run"] = not_run
    if not res:
        return None
    if "completed" in res:
        res["passed"] = res["completed"] - res.get("failed", 0) - res.get("skipped", 0)
    return {"runner": "gradle", "file": name, **res}


def _deno(lines: list[str], name: str) -> dict[str, Any] | None:
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if re.match(r"^(ok|FAILED) \| \d+ passed", line):
            c = _counts(line)
            m = re.search(r"\(([\d.]+)(ms|s|m)\)\s*$", line)
            dur = None
            if m:
                dur = float(m.group(1)) / (1000 if m.group(2) == "ms" else 1) * (60 if m.group(2) == "m" else 1)
            return {"runner": "deno", "file": name, "line": i + 1, "passed": c.get("passed", 0),
                    "failed": c.get("failed", 0), "skipped": c.get("ignored", 0), "duration_s": dur}
    return None


def _jest_vitest(lines: list[str], name: str) -> dict[str, Any] | None:
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        m = re.match(r"^Tests:\s+(.*)$", line)
        if m:
            c = _counts(m.group(1))
            return {"runner": "jest", "file": name, "line": i + 1, "passed": c.get("passed", 0),
                    "failed": c.get("failed", 0), "skipped": c.get("skipped", 0) + c.get("todo", 0),
                    "total": c.get("total")}
        m = re.match(r"^Tests\s+(\d+ (?:failed|passed).*)$", line)
        if m:
            c = _counts(m.group(1))
            return {"runner": "vitest", "file": name, "line": i + 1, "passed": c.get("passed", 0),
                    "failed": c.get("failed", 0), "skipped": c.get("skipped", 0)}
    return None


def _node(lines: list[str], name: str) -> dict[str, Any] | None:
    found: dict[str, int] = {}
    last = 0
    for i, raw in enumerate(lines):
        m = re.match(r"^[#ℹ]\s*(tests|pass|fail|skipped|todo|cancelled)\s+(\d+)\s*$", raw.strip())
        if m:
            found[m.group(1)] = int(m.group(2))
            last = i + 1
    if not found or "pass" not in found:
        return None
    return {"runner": "node", "file": name, "line": last, "passed": found.get("pass", 0),
            "failed": found.get("fail", 0) + found.get("cancelled", 0),
            "skipped": found.get("skipped", 0) + found.get("todo", 0), "total": found.get("tests")}


def parse_junit(text: str, name: str) -> list[dict[str, Any]]:
    try:
        root = ElementTree.fromstring(text.encode("utf-8"))
    except ElementTree.ParseError:
        return []
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    out = []
    for s in suites:
        failing = []
        for case in s.iter("testcase"):
            for kind in ("failure", "error"):
                node = case.find(kind)
                if node is not None:
                    msg = (node.get("message") or (node.text or "")).strip().split("\n")[0][:200]
                    failing.append({"test": f"{case.get('classname', '')}.{case.get('name', '')}".strip("."),
                                    "kind": kind, "message": msg})
        def num(attr: str) -> int:
            try:
                return int(float(s.get(attr, "0") or 0))
            except ValueError:
                return 0
        tests, failures, errors, skipped = num("tests"), num("failures"), num("errors"), num("skipped")
        out.append({"runner": "junit-xml", "file": name, "suite": s.get("name"), "passed": tests - failures - errors - skipped,
                    "failed": failures, "errors": errors, "skipped": skipped,
                    "duration_s": float(s.get("time") or 0) or None, "failing": failing})
    return out


def failing_names(lines: list[str], name: str) -> list[dict[str, Any]]:
    out = []
    pats = [
        (re.compile(r"^(?:FAILED|ERROR) (\S+?)(?: - (.*))?$"), "pytest"),
        (re.compile(r"^(\S+) > (.+?) FAILED$"), "gradle"),
        (re.compile(r"^\s*●\s+(.+?)\s*$"), "jest"),
        (re.compile(r"^not ok \d+ - (.+)$"), "tap"),
        (re.compile(r"^\s*✖\s+(.+?)(?:\s+\([\d.]+m?s\))?$"), "node"),
        (re.compile(r"^(.+?) => (\S+:\d+:\d+)$"), "deno"),
        (re.compile(r"^(.+?) \.\.\. FAILED"), "deno"),
    ]
    seen = set()
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        for rx, runner in pats:
            m = rx.match(line)
            if m:
                test = " > ".join(g for g in m.groups()[:2] if g) if runner == "gradle" else m.group(1)
                if runner == "jest" and ("Console" in test or test.startswith("Test")):
                    continue
                key = (runner, test)
                if key not in seen:
                    seen.add(key)
                    out.append({"test": test[:200], "runner": runner, "file": name, "line": i + 1})
                break
    return out


def first_error_excerpts(lines: list[str], name: str, max_excerpts: int = 2, before: int = 3,
                         after: int = 25, max_lines: int = 40) -> list[dict[str, Any]]:
    excerpts: list[dict[str, Any]] = []
    i = 0
    while i < len(lines) and len(excerpts) < max_excerpts:
        line = lines[i]
        if ERROR_MARK.search(line) and not SUMMARY_LINE.match(line.strip()):
            s = max(0, i - before)
            e = min(len(lines), i + after)
            e = min(e, s + max_lines)
            excerpts.append({"file": name, "start": s + 1, "end": e,
                             "lines": [(n + 1, lines[n]) for n in range(s, e)]})
            i = e
        else:
            i += 1
    return excerpts


def parse_test_logs(docs: list[dict[str, Any]]) -> dict[str, Any]:
    suites: list[dict[str, Any]] = []
    failing: list[dict[str, Any]] = []
    excerpts: list[dict[str, Any]] = []
    for doc in docs:
        text, name = doc["text"], doc["name"]
        if "<testsuite" in text[:20000]:
            js = parse_junit(text, name)
            if js:
                suites.extend(js)
                for s in js:
                    failing.extend({"test": f["test"], "runner": "junit-xml", "file": name, "message": f["message"]}
                                   for f in s.get("failing", []))
                continue
        lines = text.split("\n")
        for parser in (_pytest, _gradle, _deno, _jest_vitest, _node):
            s = parser(lines, name)
            if s:
                suites.append(s)
        failing.extend(failing_names(lines, name))
        for ex in first_error_excerpts(lines, name):
            ex["doc"] = doc.get("id")
            excerpts.append(ex)
    totals = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for s in suites:
        for k in totals:
            v = s.get(k)
            if isinstance(v, int):
                totals[k] += v
    not_run = [t for s in suites for t in s.get("tests_not_executed_this_run", [])]
    verdict = "no test summary found"
    if suites:
        verdict = "failures" if totals["failed"] or totals["errors"] else "all passed"
        if any(s.get("build") == "failed" for s in suites) and verdict == "all passed":
            verdict = "build failed"
    return {"verdict_from_counts": verdict, "totals": totals, "suites": suites,
            "failing": failing[:60], "failing_count_listed": len(failing),
            "tests_not_executed_this_run": not_run, "first_errors": excerpts}


# ===================================================================== git

class GitError(Exception):
    pass


def git(repo: Path, args: list[str], timeout: float = 60.0) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=timeout,
                              creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args)} timed out")
    if proc.returncode != 0:
        raise GitError(proc.stderr.decode("utf-8", "replace").strip()[:400])
    return proc.stdout.decode("utf-8", "replace")


def _rev_args(rev: str) -> list[str]:
    rev = (rev or "HEAD").strip()
    if rev == "staged":
        return ["--cached"]
    if rev == "worktree":
        return []
    if ".." in rev:
        return [rev]
    return [f"{rev}^!"]


def changed_files(repo: Path, rev: str) -> list[dict[str, Any]]:
    args = _rev_args(rev)
    numstat = git(repo, ["diff", "--numstat", "-M", *args])
    status = git(repo, ["diff", "--name-status", "-M", *args])
    files: dict[str, dict[str, Any]] = {}
    for line in status.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            code = parts[0]
            path = parts[-1]
            entry = {"path": path, "status": code[0], "old_path": parts[1] if code.startswith(("R", "C")) else None}
            files[path] = entry
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            path = parts[-1]
            if "=>" in path:  # rename shown as a/{x => y}/b
                path = re.sub(r"\{.*? => (.*?)\}", r"\1", path).replace("//", "/")
            e = files.setdefault(path, {"path": path, "status": "M", "old_path": None})
            e["added"] = None if parts[0] == "-" else int(parts[0])
            e["deleted"] = None if parts[1] == "-" else int(parts[1])
            e["binary"] = parts[0] == "-"
    if rev == "worktree":
        for line in git(repo, ["status", "--porcelain", "--untracked-files=all"]).splitlines():
            if line.startswith("??"):
                p = line[3:].strip().strip('"')
                files.setdefault(p, {"path": p, "status": "?", "old_path": None, "added": None, "deleted": None})
    return sorted(files.values(), key=lambda f: f["path"])


def file_diff(repo: Path, rev: str, path: str, max_lines: int) -> tuple[str, bool]:
    text = git(repo, ["diff", "-M", "--unified=2", *_rev_args(rev), "--", path])
    lines = text.split("\n")
    lines = [ln for ln in lines if not ln.startswith(("diff --git", "index ", "similarity index"))]
    truncated = len(lines) > max_lines
    return "\n".join(lines[:max_lines]), truncated


GENERATED = re.compile(r"(?i)(?:^|/)(?:package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|gradle/wrapper/|"
                       r"build/|dist/|\.idea/|node_modules/|__pycache__/)|\.(?:webp|png|jpe?g|gif|ico|ttf|otf|woff2?|"
                       r"mp4|mp3|wav|zip|jar|aar|apk|aab|so|dll|exe|bin|pdf)$")


# ===================================================================== dates, amounts, contacts

MONTHS_EN = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|" \
            r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
MONTHS_FR = r"(?:janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[ûu]t|septembre|octobre|novembre|d[ée]cembre)"
MONTHS_HE = r"(?:ינואר|פברואר|מרץ|מרס|אפריל|מאי|יוני|יולי|אוגוסט|ספטמבר|אוקטובר|נובמבר|דצמבר)"
DATE_PATTERNS = [
    re.compile(r"\b(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b"),
    re.compile(r"\b(?:0?[1-9]|[12]\d|3[01])[/.](?:0?[1-9]|1[0-2])[/.](?:19|20)?\d{2}\b"),
    re.compile(r"\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/(?:19|20)?\d{2}\b"),
    re.compile(rf"\b{MONTHS_EN}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+(?:19|20)\d{{2}})?\b", re.I),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTHS_EN}\.?(?:,?\s+(?:19|20)\d{{2}})?\b", re.I),
    re.compile(rf"\b(?:1er|\d{{1,2}})\s+{MONTHS_FR}(?:\s+(?:19|20)\d{{2}})?\b", re.I),
    re.compile(rf"\d{{1,2}}\s+ב?{MONTHS_HE}(?:\s+(?:19|20)\d{{2}})?"),
]
AMOUNT = re.compile(r"(?:(?:CA)?\$|€|£|₪)\s?\d[\d,  ]*(?:[.,]\d{2})?|\b\d[\d,  ]*(?:[.,]\d{2})?\s?(?:\$|€|£|₪|CAD|USD|"
                    r"dollars?|euros?|NIS|shekels?)\b", re.I)
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")


def find_spans(lines: list[str], patterns: list[re.Pattern[str]], limit: int = 80) -> list[dict[str, Any]]:
    out, seen = [], set()
    for i, line in enumerate(lines):
        taken: list[tuple[int, int]] = []
        for rx in patterns:
            for m in rx.finditer(line):
                if any(m.start() < e and s < m.end() for s, e in taken):
                    continue
                taken.append((m.start(), m.end()))
                key = (i, m.group(0).strip())
                if key not in seen:
                    seen.add(key)
                    out.append({"text": m.group(0).strip(), "line": i + 1})
                if len(out) >= limit:
                    return out
    return out


def letter_facts(text: str) -> dict[str, Any]:
    lines = text.split("\n")
    return {"dates": find_spans(lines, DATE_PATTERNS), "amounts": find_spans(lines, [AMOUNT], 40),
            "emails": find_spans(lines, [EMAIL], 20), "phones": find_spans(lines, [PHONE], 20)}


# ===================================================================== paths in reports, file lists

PATH_RX = re.compile(r"(?<![\w@:/\\.-])((?:[\w.-]+[/\\])+[\w.-]+\.[A-Za-z0-9]{1,8})(?![\w/\\])")


def norm_path(p: str) -> str:
    p = p.strip().strip("`'\"").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def paths_in_text(text: str) -> list[dict[str, Any]]:
    out, seen = [], set()
    for i, line in enumerate(text.split("\n")):
        for m in PATH_RX.finditer(line):
            p = norm_path(m.group(1))
            if p.lower().startswith(("http", "www.")) or p.count("/") == 0:
                continue
            if p.lower() not in seen:
                seen.add(p.lower())
                out.append({"path": p, "line": i + 1})
    return out


def list_from_text(text: str) -> list[str]:
    """A file list pasted as lines (status prefixes like 'M ' or '?? ' are stripped)."""
    items = []
    for line in text.split("\n"):
        s = line.strip().strip("-*•").strip()
        s = re.sub(r"^(?:\?\?|[AMDRCU]{1,2}\d*)\s+", "", s)
        if s and not s.startswith("#"):
            items.append(norm_path(s.split("\t")[-1]))
    return items


def compare_lists(named: dict[str, list[str]]) -> dict[str, Any]:
    keyed = {k: {p.lower(): p for p in v} for k, v in named.items()}
    names = list(named)
    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            pairs.append({"a": a, "b": b,
                          "only_in_a": sorted(keyed[a][k] for k in keyed[a].keys() - keyed[b].keys()),
                          "only_in_b": sorted(keyed[b][k] for k in keyed[b].keys() - keyed[a].keys()),
                          "in_both": len(keyed[a].keys() & keyed[b].keys())})
    return {"counts": {k: len(v) for k, v in keyed.items()}, "pairs": pairs}


# ===================================================================== hashes

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def parse_manifest(text: str, ext: str) -> dict[str, str]:
    text = text.strip()
    if ext == ".json" or text.startswith(("{", "[")):
        data = json.loads(text)
        if isinstance(data, dict):
            if all(isinstance(v, str) for v in data.values()):
                return {norm_path(k): v.lower() for k, v in data.items()}
            for key in ("files", "assets", "items"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if isinstance(data, list):
            out = {}
            for item in data:
                if isinstance(item, dict):
                    p = item.get("path") or item.get("file") or item.get("name")
                    h = item.get("sha256") or item.get("hash") or item.get("sha")
                    if p and h:
                        out[norm_path(p)] = str(h).lower()
            return out
        return {}
    if ext in (".csv", ".tsv") or ("," in text.split("\n")[0] and "sha" in text.split("\n")[0].lower()):
        reader = csv.DictReader(io.StringIO(text), delimiter="\t" if ext == ".tsv" else ",")
        out = {}
        for row in reader:
            low = {k.lower().strip(): v for k, v in row.items() if k}
            p = low.get("path") or low.get("file") or low.get("filename")
            h = low.get("sha256") or low.get("hash") or low.get("sha")
            if p and h:
                out[norm_path(p)] = h.strip().lower()
        return out
    out = {}
    for line in text.split("\n"):
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line.strip()) or None
        if m:
            out[norm_path(m.group(2))] = m.group(1).lower()
            continue
        m = re.match(r"^(.+?)\s+([0-9a-fA-F]{64})$", line.strip())
        if m:
            out[norm_path(m.group(1))] = m.group(2).lower()
    return out


# ===================================================================== BM25 for the context packer

STOP = set("""a an and are as at be by for from has have in is it its of on or that the this to was were will with
le la les de des du un une et est en pour dans que qui sur pas au aux ce ces""".split())


def tokens(text: str) -> list[str]:
    out = []
    for word in re.findall(r"[A-Za-zÀ-ɏ֐-׿0-9_]+", text):
        parts = [word]
        if "_" in word or re.search(r"[a-z][A-Z]", word):
            parts += [p for p in re.split(r"_|(?<=[a-z])(?=[A-Z])", word) if p]
        for p in parts:
            lp = p.lower()
            if len(lp) > 1 and lp not in STOP:
                out.append(lp)
    return out


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.avg = sum(len(d) for d in docs) / max(1, self.n)
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in docs]

    def scores(self, query: list[str]) -> list[float]:
        out = []
        q = set(query)
        for i, tf in enumerate(self.tf):
            dl = len(self.docs[i])
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += self.idf.get(t, 0.0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avg))
            out.append(s)
        return out


def chunk_lines(text: str, window: int = 40, step: int = 30) -> list[tuple[int, int]]:
    lines = text.split("\n")
    n = len(lines)
    if n <= window:
        return [(1, n)]
    spans = []
    start = 0
    while start < n:
        end = min(n, start + window)
        spans.append((start + 1, end))
        if end == n:
            break
        start += step
    return spans
