"""Clock helpers: tonight's window, start times, deadlines, the active priority, and how
times are shown (local, 24-hour)."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

WHEN_KINDS = ("now", "idle", "tonight", "at")


class WhenError(ValueError):
    pass


def parse_hhmm(s: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*", str(s or ""))
    if not m:
        raise WhenError(f"expected a time like 23:00, got {s!r}")
    h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 23 or mi > 59 or se > 59:
        raise WhenError(f"not a valid time: {s!r}")
    return h, mi, se


def _at_today(now: datetime, hhmm: str) -> datetime:
    h, m, s = parse_hhmm(hhmm)
    return now.replace(hour=h, minute=m, second=s, microsecond=0)


def night_bounds(now_ts: float, window: dict[str, str]) -> tuple[float, float, bool]:
    """(start, end, inside_now). If we are inside tonight's window, start is now."""
    now = datetime.fromtimestamp(now_ts)
    start_t = _at_today(now, window.get("start", "23:00"))
    end_t = _at_today(now, window.get("end", "07:00"))
    crosses = end_t <= start_t
    if crosses:
        if now >= start_t:                      # evening part of tonight
            return now_ts, (end_t + timedelta(days=1)).timestamp(), True
        if now < end_t:                         # early-morning part of last night
            return now_ts, end_t.timestamp(), True
        return start_t.timestamp(), (end_t + timedelta(days=1)).timestamp(), False
    if start_t <= now < end_t:
        return now_ts, end_t.timestamp(), True
    if now < start_t:
        return start_t.timestamp(), end_t.timestamp(), False
    return (start_t + timedelta(days=1)).timestamp(), (end_t + timedelta(days=1)).timestamp(), False


def next_clock(now_ts: float, hhmm: str) -> float:
    """The next time the clock shows hh:mm (today if still ahead, else tomorrow)."""
    now = datetime.fromtimestamp(now_ts)
    t = _at_today(now, hhmm)
    if t.timestamp() <= now_ts:
        t += timedelta(days=1)
    return t.timestamp()


def parse_when(when: str | None, now_ts: float, cfg: dict[str, Any]) -> dict[str, Any]:
    """'now' | 'idle' | 'tonight' | 'at:HH:MM[:SS]' | 'at:YYYY-MM-DDTHH:MM' -> start rule."""
    w = (when or "now").strip().lower()
    if w in ("now", "background", ""):
        return {"when": "now", "not_before": None, "window_end": None}
    if w in ("idle", "away", "when idle"):
        return {"when": "idle", "not_before": None, "window_end": None}
    if w in ("tonight", "night", "overnight"):
        start, end, _ = night_bounds(now_ts, cfg.get("night_window") or {})
        return {"when": "tonight", "not_before": start, "window_end": end}
    if w.startswith("at:") or w.startswith("at "):
        spec = w[3:].strip()
        if "t" in spec or "-" in spec:
            try:
                ts = datetime.fromisoformat(spec.replace("t", "T")).timestamp()
            except ValueError as exc:
                raise WhenError(f"bad start time {spec!r}") from exc
        else:
            ts = next_clock(now_ts, spec)
        return {"when": "at", "not_before": ts, "window_end": None}
    raise WhenError("when must be now, idle, tonight or at:HH:MM")


def parse_deadline(deadline: str | float | None, now_ts: float, after: float | None = None) -> float | None:
    if deadline in (None, ""):
        return None
    if isinstance(deadline, (int, float)):
        return float(deadline)
    d = str(deadline).strip()
    if re.fullmatch(r"\d+\s*(m|min|minutes?)", d):
        return now_ts + 60 * int(re.match(r"\d+", d).group(0))
    if "T" in d or "-" in d:
        return datetime.fromisoformat(d).timestamp()
    base = max(now_ts, after or now_ts)
    return next_clock(base - 1, d)


def active_priority(cfg: dict[str, Any], now_ts: float) -> tuple[str, str]:
    """(preset name, why): a temporary override > the schedule > the default preset."""
    presets = cfg.get("priorities") or {}
    ov = cfg.get("priority_override") or {}
    if ov.get("name") in presets and float(ov.get("until") or 0) > now_ts:
        return ov["name"], f"until {fmt_clock(float(ov['until']), now_ts)}"
    now = datetime.fromtimestamp(now_ts)
    for entry in cfg.get("priority_schedule") or []:
        try:
            start = _at_today(now, entry["from"])
            end = _at_today(now, entry["to"])
        except (KeyError, WhenError):
            continue
        inside = start <= now < end if start < end else (now >= start or now < end)
        if inside and entry.get("priority") in presets:
            return entry["priority"], f"schedule {entry['from']}-{entry['to']}"
    p = cfg.get("priority") if cfg.get("priority") in presets else "balanced"
    return p, "default"


def fmt_clock(ts: float, now_ts: float | None = None) -> str:
    now = datetime.fromtimestamp(now_ts or time.time())
    t = datetime.fromtimestamp(ts)
    day = ""
    if t.date() != now.date():
        day = "tomorrow " if t.date() == (now + timedelta(days=1)).date() else t.strftime("%a %d %b ")
    return f"{day}{t:%H:%M}"


def fmt_minutes(seconds: float) -> str:
    if seconds < 90:
        return f"{max(1, int(round(seconds)))} s"
    if seconds < 5400:
        return f"{int(round(seconds / 60))} min"
    return f"{seconds / 3600:.1f} h"


def fmt_range(lo_s: float, hi_s: float) -> str:
    if hi_s < 90:
        return fmt_minutes(hi_s)
    if hi_s >= 5400:
        return f"{lo_s / 3600:.1f}-{hi_s / 3600:.1f} h"
    a, b = max(1, int(round(lo_s / 60))), max(1, int(round(hi_s / 60)))
    return f"{a} min" if a == b else f"{a}-{b} min"
