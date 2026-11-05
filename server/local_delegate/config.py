"""Paths and settings.

Settings live in <data dir>/config.json (created on first run from DEFAULTS plus the
plugin's seed_config.json). The data dir is %LOCALAPPDATA%\\local-delegate on Windows.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from . import __version__

APP = "local-delegate"
VERSION = __version__
SERVER_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = SERVER_DIR.parent
IS_WINDOWS = sys.platform == "win32"

DEFAULTS: dict[str, Any] = {
    # Only loopback URLs are accepted (guard.check_url refuses anything else).
    "ollama_url": "http://127.0.0.1:11434",
    "model": "qwen3.8:27b-q4_K_M",
    "keep_alive": "10m",
    # strict_local: refuse to run unless the running Ollama server is proven cloud-disabled
    # and bound to loopback.
    "strict_local": True,
    # Folders the connector may read. "~" means your user folder.
    "allowed_roots": ["~"],
    # Never read these (checked on every path, case-insensitive). Secrets and key stores.
    "deny_globs": [
        "**/.ssh/**", "**/.gnupg/**", "**/.aws/**", "**/.azure/**", "**/.kube/**", "**/.docker/**",
        "**/.ollama/**", "**/.android/**", "**/.supabase/**", "**/.codex/**", "**/.cursor/**",
        "**/.claude/**", "**/.claude-science/**", "**/.git/**", "**/.config/gh/**",
        "**/AppData/Roaming/Claude/**", "**/AppData/Roaming/Microsoft/Credentials/**",
        "**/AppData/Local/Microsoft/Credentials/**", "**/AppData/Local/Google/Chrome/User Data/**",
        "**/AppData/Local/Microsoft/Edge/User Data/**", "**/AppData/Roaming/Mozilla/**",
        "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/*.p12", "**/*.pfx", "**/*.jks",
        "**/*.keystore", "**/id_rsa*", "**/id_ed25519*", "**/*.kdbx", "**/credentials*",
        "**/secrets*", "**/local.properties", "**/google-services.json", "**/service-account*.json",
        "**/*.gpg", "**/auth.json",
    ],
    # Paths flagged in diff summaries (glob patterns relative to the repo root).
    "protected_paths": [],
    # Path prefix -> area name, for diff summaries. Longest prefix wins.
    "areas": {},
    # Context sizes the connector uses. Few, fixed sizes: changing num_ctx reloads the model.
    "ctx_buckets": [4096, 8192, 16384, 24576, 32768, 49152, 65536],
    # RAM to keep free after the model and its context are loaded (Windows + your apps).
    "ram_headroom_gb": 4.0,
    # Whether to check free RAM before loading the model (turn off only if you accept swapping).
    "ram_check": True,
    # ---- how hard the local model thinks. Reasoning is extra writing, and writing is the slow
    # part on most machines, so each level has a hard budget (tokens of reasoning per answer).
    # "think" names the model's reasoning level (Qwen3.8: none, low, medium, xhigh); the
    # sampling values are the model card's recommendations for each mode.
    "modes": {
        "fast": {"think": "off", "think_budget": 0, "repair": False},
        "careful": {"think": "low", "think_budget": 3000, "repair": False,
                    "temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0},
        "deep": {"think": "medium", "think_budget": 8000, "night_think_budget": 16000, "repair": True,
                 "temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0},
    },
    # Typical reasoning length per answer before any of your jobs have been measured; the log
    # replaces these with real figures per task and level.
    "think_prior_tokens": {"careful": 900, "deep": 3500},
    # ---- when jobs run. Times are local, 24-hour.
    "night_window": {"start": "23:00", "end": "07:00"},
    "idle_minutes": 10,
    # Keep the computer from sleeping while jobs wait or run (only on mains power).
    "keep_awake": True,
    # ---- priorities. Each preset prices waiting in Claude tokens: "minute" = one minute until
    # the result is ready, "tonight" = getting it tomorrow morning instead of today (None = never
    # recommend tonight), "quality" = bonus per reasoning level (careful 1x, deep 2x), and
    # "min_saving" = smallest expected saving worth asking about. The option with the highest
    # (expected tokens saved + quality bonus - waiting cost) is recommended.
    "priority": "balanced",
    # A temporary preset, e.g. {"name": "rush", "until": <epoch seconds>}.
    "priority_override": None,
    # e.g. [{"from": "07:00", "to": "11:00", "priority": "rush"}]
    "priority_schedule": [],
    "priorities": {
        "rush": {"minute": 6000, "tonight": None, "quality": 0, "min_saving": 3000,
                 "about": "short on time: Claude does it unless a local result is nearly instant"},
        "balanced": {"minute": 400, "tonight": 15000, "quality": 2000, "min_saving": 3000,
                     "about": "local when it saves tokens and is ready soon; long jobs offered for tonight"},
        "saver": {"minute": 60, "tonight": 6000, "quality": 2500, "min_saving": 1000,
                  "about": "save tokens: local whenever it saves, with the deepest reasoning that's ready in time"},
        "night": {"minute": 400, "tonight": 0, "quality": 4000, "min_saving": 1000,
                  "about": "batch: delegable jobs go to tonight's queue at the deepest level"},
    },
    "router": {
        # "Now" options longer than this are not offered (they're offered for tonight instead).
        "now_max_minutes": 180,
        # Rough model of Claude's own cost, used for the "here" comparison.
        "claude_context_tokens": 30000,
        "cache_read_weight": 0.1,
        # Text Claude reads stays in the conversation and is re-read (from cache) at every later
        # step; this many later steps are counted when comparing "here" with "delegated".
        "future_steps": 5,
        "claude_write_tps": 60,
        "claude_roundtrip_s": 4,
        "claude_chars_per_token": {"prose": 3.6, "code": 3.0, "fr": 3.2, "he": 1.8},
        # Share of Claude's own effort still needed when a local result needs editing.
        "edit_fraction": 0.3,
        # Slow-down factor applied when running on battery (laptops throttle).
        "battery_factor": 1.8,
    },
    # Uncalibrated fallbacks (CPU-only guesses for a 27B model). Calibration replaces them.
    "fallback_rates": {
        "load_s": 30.0, "reload_s": 15.0, "prefill_tps": 30.0, "decode_tps": 3.0,
        "model_bytes": 18.5e9, "kv_bytes_per_token": 65536,
    },
    "retention_days": 14,
}

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def home() -> Path:
    return Path(os.path.expanduser("~"))


def data_dir() -> Path:
    override = os.environ.get("LOCAL_DELEGATE_HOME")
    if override:
        p = Path(override)
    elif IS_WINDOWS:
        p = Path(os.environ.get("LOCALAPPDATA") or (home() / "AppData" / "Local")) / APP
    else:
        p = Path(os.environ.get("XDG_DATA_HOME") or (home() / ".local" / "share")) / APP
    p.mkdir(parents=True, exist_ok=True)
    return p


def sub_dir(name: str) -> Path:
    p = data_dir() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def ollama_home() -> Path:
    """Folder holding Ollama's server.json (C:\\Users\\<you>\\.ollama on Windows)."""
    return Path(os.environ.get("LOCAL_DELEGATE_OLLAMA_HOME") or (home() / ".ollama"))


def ollama_log_path() -> Path | None:
    override = os.environ.get("LOCAL_DELEGATE_OLLAMA_LOG")
    if override:
        return Path(override)
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "Ollama" / "server.log"
    mac = home() / ".ollama" / "logs" / "server.log"
    return mac if mac.exists() else None


def ollama_app_path() -> Path | None:
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        if base:
            p = Path(base) / "Programs" / "Ollama" / "ollama app.exe"
            if p.exists():
                return p
    return None


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def config_path() -> Path:
    return data_dir() / "config.json"


def _seed() -> dict:
    seed = SERVER_DIR / "seed_config.json"
    if seed.exists():
        try:
            return json.loads(seed.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def load(refresh: bool = False) -> dict[str, Any]:
    """Defaults <- seed_config.json <- <data dir>/config.json. Writes config.json on first run."""
    global _cache
    with _lock:
        if _cache is not None and not refresh:
            return _cache
        cfg = _deep_merge(DEFAULTS, _seed())
        path = config_path()
        if path.exists():
            try:
                user = json.loads(path.read_text(encoding="utf-8-sig"))
                user = {k: v for k, v in user.items() if not k.startswith("_")}
                cfg = _deep_merge(cfg, user)
            except Exception as exc:  # keep running on a broken file, but say so
                cfg["_config_error"] = f"config.json could not be read ({exc}); using defaults"
        else:
            try:
                starter = {"_note": "Edit values here; anything missing falls back to the defaults "
                                    "listed in README.md. Restart Claude to apply."}
                starter.update({k: cfg[k] for k in ("model", "allowed_roots", "protected_paths", "areas",
                                                     "keep_alive", "ram_headroom_gb", "strict_local", "priority",
                                                     "night_window")})
                path.write_text(json.dumps(starter, indent=2), encoding="utf-8")
            except OSError:
                pass
        _cache = cfg
        return cfg


def save_user_setting(key: str, value: Any) -> None:
    """Update one top-level key in config.json (keeps the user's other edits)."""
    path = config_path()
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            data = {}
    data[key] = value
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    load(refresh=True)


def expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))
