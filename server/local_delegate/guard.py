"""Local-only guarantees, file-access policy and secret redaction.

The connector refuses to run (in strict mode) unless all of these hold:
  1. it talks to Ollama on a loopback address (enforced again in ollama.py);
  2. the running Ollama server listens on loopback only (not 0.0.0.0);
  3. the running Ollama server has cloud features disabled, proven by its own log line
     "Ollama cloud disabled: true", or by server.json / OLLAMA_NO_CLOUD having been set
     before the server process started;
  4. the selected model is a downloaded local model, not a ":cloud" model.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, sysinfo
from .ollama import Ollama, OllamaError, parse_loopback_url

_cache: dict[str, Any] = {"at": 0.0, "report": None}

# ------------------------------------------------------------------ cloud & network checks


def model_is_cloud_name(name: str) -> bool:
    n = name.lower().strip()
    tag = n.split(":", 1)[1] if ":" in n else ""
    return n.endswith("-cloud") or tag == "cloud" or tag.endswith("-cloud") or tag.startswith("cloud")


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    s = iso.strip().replace("Z", "+00:00")
    m = re.match(r"(.*\.\d{6})\d*(.*)$", s)  # trim >6 fractional digits (PowerShell gives 7)
    if m:
        s = m.group(1) + m.group(2)
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def read_server_json() -> tuple[dict[str, Any] | None, float | None, Path]:
    path = config.ollama_home() / "server.json"
    if not path.exists():
        return None, None, path
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), path.stat().st_mtime, path
    except Exception:
        return {"_unreadable": True}, path.stat().st_mtime, path


def _env_key_mtime() -> float | None:
    """Last-write time of HKCU\\Environment (Windows), as epoch seconds."""
    if not sysinfo.IS_WINDOWS:
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            ft = winreg.QueryInfoKey(key)[2]  # 100 ns intervals since 1601-01-01
        return ft / 1e7 - 11644473600
    except Exception:
        return None


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def cloud_log_state() -> tuple[bool | None, str, float | None]:
    """Read Ollama's own server log: (disabled?, detail, log mtime)."""
    log = config.ollama_log_path()
    if not log or not log.exists():
        return None, "Ollama server log not found", None
    try:
        size = log.stat().st_size
        with open(log, "rb") as fh:
            if size > 4_000_000:  # startup lines are at the top (the log rotates at each start)
                head = fh.read(2_000_000)
                fh.seek(size - 2_000_000)
                data = head + b"\n" + fh.read()
            else:
                data = fh.read()
        text = data.decode("utf-8", errors="replace")
    except OSError as exc:
        return None, f"could not read {log} ({exc})", None
    hits = re.findall(r"Ollama cloud disabled:\s*(true|false)", text, flags=re.IGNORECASE)
    mtime = log.stat().st_mtime
    if hits:
        val = hits[-1].lower() == "true"
        return val, f"server.log: 'Ollama cloud disabled: {hits[-1].lower()}'", mtime
    m = re.findall(r"OLLAMA_NO_CLOUD:(\S+?)[\s\]]", text)
    if m:
        return _truthy(m[-1]), f"server.log server config: OLLAMA_NO_CLOUD:{m[-1]}", mtime
    return None, "server.log has no cloud-setting line (older Ollama?)", mtime


def report(cfg: dict[str, Any], client: Ollama | None = None, refresh: bool = False,
           max_age_s: float = 60.0) -> dict[str, Any]:
    """All local-only checks. Cached for a minute (the Windows process checks are slow)."""
    if not refresh and _cache["report"] is not None and time.time() - _cache["at"] < max_age_s:
        return _cache["report"]
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool | None, detail: str, fix: str | None = None) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, **({"fix": fix} if fix and ok is not True else {})})

    ok_url, host, port, detail = parse_loopback_url(cfg.get("ollama_url", ""))
    add("loopback_url", ok_url, detail, "Set ollama_url to http://127.0.0.1:11434 in config.json")

    version = None
    if ok_url:
        try:
            client = client or Ollama(cfg["ollama_url"])
            version = client.version()
            add("ollama_running", True, f"Ollama {version} answers on {host}:{port}")
        except OllamaError as exc:
            add("ollama_running", False, str(exc), "Start Ollama from the Start menu")

    # -- what is listening, and since when
    listen = sysinfo.listeners(port) if ok_url else []
    server_start = None
    if listen:
        addrs = sorted({str(x.get("addr")) for x in listen})
        nonloop = [a for a in addrs if a not in ("127.0.0.1", "::1") and not a.startswith("127.")]
        names = sorted({str(x.get("name") or "?") for x in listen})
        starts = [_epoch(x.get("start_utc")) for x in listen if x.get("start_utc")]
        server_start = max([s for s in starts if s] or [0]) or None
        if nonloop:
            add("listen_loopback_only", False, f"port {port} is open on {', '.join(nonloop)} ({', '.join(names)})",
                "Remove OLLAMA_HOST (or set it to 127.0.0.1:11434) and restart Ollama")
        else:
            add("listen_loopback_only", True, f"port {port} listens on {', '.join(addrs)} only ({', '.join(names)})")
    else:
        env_host = sysinfo.user_env("OLLAMA_HOST")
        if env_host and not re.match(r"^(127\.\d+\.\d+\.\d+|localhost|\[?::1\]?)(:\d+)?$", env_host.strip()):
            add("listen_loopback_only", False, f"OLLAMA_HOST={env_host} exposes Ollama beyond this computer",
                "Set OLLAMA_HOST to 127.0.0.1:11434 and restart Ollama")
        else:
            add("listen_loopback_only", None, "could not list listening sockets; OLLAMA_HOST is not set to a "
                                              "network address (Ollama's default is 127.0.0.1)")

    # -- cloud features off in the RUNNING server
    log_state, log_detail, _ = cloud_log_state()
    sj, sj_mtime, sj_path = read_server_json()
    sj_disabled = bool(sj and sj.get("disable_ollama_cloud") is True)
    env_val = sysinfo.user_env("OLLAMA_NO_CLOUD")
    env_disabled = _truthy(env_val) if env_val is not None else False
    env_mtime = _env_key_mtime()

    if log_state is True:
        add("cloud_disabled_now", True, log_detail)
    elif log_state is False:
        add("cloud_disabled_now", False, log_detail + " (cloud models and web search are available)",
            "Run setup (local_admin action=setup_local_only), which sets it and restarts Ollama")
    else:
        applied = []
        if sj_disabled and server_start and sj_mtime and server_start > sj_mtime:
            applied.append("server.json was set before Ollama started")
        if env_disabled and server_start and env_mtime and server_start > env_mtime:
            applied.append("OLLAMA_NO_CLOUD was set before Ollama started")
        if applied:
            add("cloud_disabled_now", True, "; ".join(applied) + f" ({log_detail})")
        else:
            add("cloud_disabled_now", None, f"not proven: {log_detail}",
                "Run setup (local_admin action=setup_local_only), or quit Ollama from the tray and start it again")

    cfg_bits = [f"{sj_path}: disable_ollama_cloud={'true' if sj_disabled else 'not set'}",
                f"user env OLLAMA_NO_CLOUD={env_val if env_val is not None else 'not set'}"]
    if sj_disabled or env_disabled:
        add("cloud_disabled_config", True, "; ".join(cfg_bits) + " (stays off after restarts)")
    elif log_state is True:
        add("cloud_disabled_config", None, "; ".join(cfg_bits) + " — disabled some other way (system variable?); "
                                                                  "setup would make it permanent")
    else:
        add("cloud_disabled_config", False, "; ".join(cfg_bits), "Run setup (local_admin action=setup_local_only)")

    # -- model is local
    model = cfg.get("model", "")
    if model_is_cloud_name(model):
        add("model_is_local", False, f"{model} is an Ollama cloud model", "Pick a downloaded model")
    elif version is not None and client is not None:
        try:
            tags = client.tags()
            entry = next((t for t in tags if t.get("name") == model or t.get("model") == model), None)
            if entry is None:
                close = [t.get("name") for t in tags if str(t.get("name", "")).split(":")[0] == model.split(":")[0]]
                add("model_is_local", False, f"{model} is not downloaded" + (f" (found: {', '.join(close)})" if close else ""),
                    f"ollama pull {model}  (or set 'model' in config.json to one you have)")
            elif entry.get("remote_host") or entry.get("remote_model") or not entry.get("size"):
                add("model_is_local", False, f"{model} points to a remote host", "Pick a downloaded model")
            else:
                add("model_is_local", True, f"{model}: {entry.get('size', 0) / 1e9:.1f} GB on disk")
        except OllamaError as exc:
            add("model_is_local", None, f"could not list models ({exc})")
    else:
        add("model_is_local", None, "Ollama not reachable")

    by = {c["name"]: c["ok"] for c in checks}
    strict = bool(cfg.get("strict_local", True))
    blocking = [c for c in checks if c["ok"] is False or (strict and c["name"] == "cloud_disabled_now" and c["ok"] is not True)]
    ok = by.get("loopback_url") is True and by.get("ollama_running") is True and by.get("model_is_local") is True \
        and not blocking
    rep = {"ok": ok, "strict": strict, "ollama_version": version, "checks": checks,
           "problems": [f"{c['name']}: {c['detail']}" + (f" -> fix: {c['fix']}" if c.get("fix") else "")
                        for c in checks if c["ok"] is not True]}
    _cache.update(at=time.time(), report=rep)
    return rep


def clear_cache() -> None:
    _cache.update(at=0.0, report=None)


# ------------------------------------------------------------------ file access policy

def _glob_regex(pattern: str) -> re.Pattern[str]:
    p = pattern.replace("\\", "/").lower()
    out, i = [], 0
    while i < len(p):
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif p[i] == "*":
            out.append("[^/]*")
            i += 1
        elif p[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(p[i]))
            i += 1
    return re.compile("".join(out))


def _norm(path: Path) -> str:
    return str(path).replace("\\", "/").lower()


def check_path(raw: str, cfg: dict[str, Any]) -> tuple[bool, Path | None, str]:
    """(ok, resolved path, reason). Paths must exist, sit under an allowed root and match no deny pattern."""
    try:
        path = config.expand(raw).resolve()
    except (OSError, RuntimeError) as exc:
        return False, None, f"{raw}: cannot resolve ({exc})"
    if not path.exists():
        return False, None, f"{raw}: not found"
    roots = []
    for r in cfg.get("allowed_roots") or ["~"]:
        try:
            roots.append(config.expand(r).resolve())
        except (OSError, RuntimeError):
            continue
    np = _norm(path)
    if not any(np == _norm(r) or np.startswith(_norm(r).rstrip("/") + "/") for r in roots):
        return False, None, (f"{path} is outside the allowed folders ({', '.join(str(r) for r in roots)}). "
                             "Add its folder with local_admin action=allow_root (asks the user first).")
    for pat in cfg.get("deny_globs") or []:
        if _glob_regex(pat).fullmatch(np):
            return False, None, f"{path} matches the never-read rule '{pat}' (secrets/keys)"
    return True, path, "ok"


# ------------------------------------------------------------------ secret redaction

SECRET_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"), 0),
    ("openrouter_key", re.compile(r"sk-or-v1-[0-9a-fA-F]{20,}"), 0),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), 0),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), 0),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), 0),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"), 0),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), 0),
    ("supabase_secret", re.compile(r"\bsb_secret_[A-Za-z0-9_\-]{16,}"), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), 0),
    ("bearer", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9\-._~+/]{20,}=*)"), 1),
    ("assignment", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|client_secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)\b"
        r"\s*[:=]\s*[\"']?((?=[^\s\"']*[A-Za-z])(?=[^\s\"']*\d)[^\s\"']{12,})"), 1),
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for name, rx, group in SECRET_PATTERNS:
        def repl(m: re.Match[str], name: str = name, group: int = group) -> str:
            counts[name] = counts.get(name, 0) + 1
            if group == 0:
                return f"[REDACTED:{name}]"
            whole = m.group(0)
            s, e = m.start(group) - m.start(0), m.end(group) - m.start(0)
            return whole[:s] + f"[REDACTED:{name}]" + whole[e:]

        text = rx.sub(repl, text)
    return text, counts
