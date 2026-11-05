"""Rule sheets ("memory packs") that give the local model the context it needs.

Packs are Markdown files in <data dir>/packs (seeded from the plugin's packs/ folder on
first use, then yours to edit). A ```rules block holds checks a script applies exactly:

    forbid: OldProductName           term must not appear
    forbid@website: FormerEmployer   only when the target is "website"
    forbid_near: language | Esperanto  on lines containing "language", "Esperanto" must not appear
    require_near: French | B2        on lines containing "French", "B2" must appear
    require: Lisbon                  must appear somewhere
    warn: synergy                    flag, softer

Everything else in the file is guidance the model reads.
"""

from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any

from . import config

RULE_LINE = re.compile(r"^\s*(forbid|forbid_near|require|require_near|warn)(?:@([\w-]+))?\s*:\s*(.+?)\s*$", re.I)


def packs_dir() -> Path:
    d = config.sub_dir("packs")
    src = config.PLUGIN_ROOT / "packs"
    if src.exists():
        for f in src.glob("*.md"):
            dest = d / f.name
            if not dest.exists():
                shutil.copyfile(f, dest)
    return d


def available() -> list[str]:
    return sorted(p.stem for p in packs_dir().glob("*.md"))


def load(name: str) -> dict[str, Any]:
    safe = re.sub(r"[^\w-]", "", name)
    path = packs_dir() / f"{safe}.md"
    if not path.exists():
        raise FileNotFoundError(f"pack '{name}' not found; available: {', '.join(available()) or 'none'}")
    text = path.read_text(encoding="utf-8-sig")
    rules: list[dict[str, Any]] = []
    for block in re.findall(r"```rules\s*\n(.*?)```", text, flags=re.S):
        for line in block.split("\n"):
            m = RULE_LINE.match(line)
            if not m:
                continue
            kind, scope, arg = m.group(1).lower(), (m.group(2) or "").lower() or None, m.group(3)
            if kind.endswith("_near"):
                anchor, _, term = arg.partition("|")
                rules.append({"kind": kind, "scope": scope, "anchor": anchor.strip(), "term": term.strip(), "src": line.strip()})
            else:
                rules.append({"kind": kind, "scope": scope, "term": arg.strip(), "src": line.strip()})
    guidance = re.sub(r"```rules\s*\n.*?```", "", text, flags=re.S).strip()
    return {"name": safe, "path": str(path), "rules": rules, "guidance": guidance}


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def _has(term: str, text: str) -> list[int]:
    rx = re.compile(r"(?<!\w)" + re.escape(_fold(term)) + r"(?!\w)")
    return [m.start() for m in rx.finditer(_fold(text))]


def check(text: str, pack_list: list[dict[str, Any]], target: str | None) -> dict[str, Any]:
    """Apply every rule whose scope matches `target` (unscoped rules always apply)."""
    lines = text.split("\n")
    hits: list[dict[str, Any]] = []
    skipped: list[str] = []
    for pack in pack_list:
        for r in pack["rules"]:
            if r["scope"] and r["scope"] != (target or "").lower():
                skipped.append(f"{pack['name']}: {r['src']}")
                continue
            if r["kind"] in ("forbid", "warn"):
                for i, line in enumerate(lines, start=1):
                    if _has(r["term"], line):
                        hits.append({"rule": r["src"], "pack": pack["name"], "severity": "warn" if r["kind"] == "warn" else "violation",
                                     "line": i, "quote": line.strip()[:160]})
            elif r["kind"] == "require":
                if not _has(r["term"], text):
                    hits.append({"rule": r["src"], "pack": pack["name"], "severity": "violation", "line": None,
                                 "quote": f"'{r['term']}' does not appear"})
            elif r["kind"] in ("forbid_near", "require_near"):
                for i, line in enumerate(lines, start=1):
                    if _has(r["anchor"], line):
                        present = bool(_has(r["term"], line))
                        if (r["kind"] == "forbid_near" and present) or (r["kind"] == "require_near" and not present):
                            hits.append({"rule": r["src"], "pack": pack["name"], "severity": "violation", "line": i,
                                         "quote": line.strip()[:160]})
    return {"script_findings": hits, "rules_not_applied_for_target": skipped}
