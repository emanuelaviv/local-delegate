"""Checks applied to model output before Claude sees it.

* Every citation {doc, line, quote} is checked against the real file: the quote must
  appear on (or within two lines of) the cited line. Quotes found elsewhere are
  relocated; quotes found nowhere are flagged "unverified".
* JSON output is checked against the task's schema (a small validator for the subset
  of JSON Schema the tasks use).
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s).strip().casefold()
    return s.strip(" .,:;\"'`*")


def _find(lines: list[str], line_no: int, quote: str, window: int = 2) -> tuple[str, int | None]:
    q = _norm(quote)
    if not q:
        return "empty", None
    lo, hi = max(1, line_no - window), min(len(lines), line_no + window)
    joined_near = " ".join(_norm(lines[i - 1]) for i in range(lo, hi + 1)) if lines else ""
    if line_no and 1 <= line_no <= len(lines):
        for i in range(lo, hi + 1):
            if q in _norm(lines[i - 1]):
                return "verified", i
        if q in joined_near:
            return "verified", line_no
    for i, line in enumerate(lines, start=1):
        if q in _norm(line):
            return "relocated", i
    # fuzzy: long-ish quotes that differ by a few characters
    if len(q) >= 20 and line_no and 1 <= line_no <= len(lines):
        best, best_i = 0.0, None
        for i in range(lo, hi + 1):
            cand = _norm(lines[i - 1])
            if not cand:
                continue
            sm = difflib.SequenceMatcher(None, q, cand)
            m = sm.find_longest_match(0, len(q), 0, len(cand))
            seg = cand[max(0, m.b - m.a): max(0, m.b - m.a) + len(q)]
            r = difflib.SequenceMatcher(None, q, seg).ratio()
            if r > best:
                best, best_i = r, i
        if best >= 0.85:
            return "close", best_i
    return "unverified", None


def verify_cites(obj: Any, docs: dict[str, list[str]], default_doc: str | None = None) -> dict[str, Any]:
    """Walk `obj`; every dict holding a 'quote' (and optionally 'doc'/'line') gets a 'check' field.
    Returns counts plus the list of unverified quotes."""
    stats = {"verified": 0, "relocated": 0, "close": 0, "unverified": 0, "empty": 0}
    unverified: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if "quote" in node and isinstance(node.get("quote"), str):
                doc = node.get("doc") or default_doc
                if doc not in docs and len(docs) == 1:
                    doc = next(iter(docs))
                lines = docs.get(doc or "", [])
                try:
                    line_no = int(node.get("line") or 0)
                except (TypeError, ValueError):
                    line_no = 0
                status, found = _find(lines, line_no, node["quote"])
                stats[status] = stats.get(status, 0) + 1
                if status != "verified":  # only mark what needs attention
                    node["check"] = status
                if status in ("relocated", "verified", "close") and found and found != line_no:
                    node["line"] = found
                if status == "unverified":
                    unverified.append({"doc": doc, "line": line_no, "quote": node["quote"][:120]})
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(obj)
    total = sum(stats.values()) - stats.get("empty", 0)
    ok = stats["verified"] + stats["relocated"] + stats["close"]
    out: dict[str, Any] = {"citations": total, "confirmed": ok}
    out.update({k: v for k, v in stats.items() if v and k not in ("verified",)})
    out["unverified_quotes"] = unverified
    return out


# ------------------------------------------------------------------ minimal JSON-schema validation

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is_type(v: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, _TYPES.get(t, object))


def validate(obj: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errs: list[str] = []
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(obj, x) for x in types):
            return [f"{path}: expected {t}, got {type(obj).__name__}"]
    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path}: {obj!r} not in {schema['enum']}")
    if isinstance(obj, dict):
        for req in schema.get("required", []):
            if req not in obj:
                errs.append(f"{path}: missing '{req}'")
        for k, sub in (schema.get("properties") or {}).items():
            if k in obj:
                errs.extend(validate(obj[k], sub, f"{path}.{k}"))
    if isinstance(obj, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(obj):
            errs.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errs[:20]
