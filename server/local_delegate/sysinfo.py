"""Hardware and process facts, standard library only.

Windows paths use ctypes, the registry and PowerShell (language-independent output);
Linux paths read /proc (used by the tests). Every function degrades to "unknown"
instead of raising.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any

IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """Run a command without a console window; never raises."""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW,
                              stdin=subprocess.DEVNULL)
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        return proc.returncode, out, err
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]} timed out after {timeout}s"
    except Exception as exc:  # pragma: no cover - defensive
        return 1, "", str(exc)


def powershell_json(script: str, timeout: float = 15.0) -> Any:
    """Run a PowerShell snippet that ends in ConvertTo-Json; return parsed JSON or None."""
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return None
    rc, out, _ = run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                     timeout=timeout)
    if rc != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- memory

def memory() -> dict[str, int | None]:
    """Total and available physical RAM in bytes."""
    if IS_WINDOWS:
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return {"total": int(stat.ullTotalPhys), "available": int(stat.ullAvailPhys)}
        except Exception:
            pass
        return {"total": None, "available": None}
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    info[key] = int(parts[0]) * 1024
        return {"total": info.get("MemTotal"), "available": info.get("MemAvailable", info.get("MemFree"))}
    except Exception:
        pass
    try:  # macOS / other POSIX: total only
        return {"total": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"), "available": None}
    except Exception:
        return {"total": None, "available": None}


# ---------------------------------------------------------------- cpu

def cpu() -> dict[str, Any]:
    name = None
    if IS_WINDOWS:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except Exception:
            name = None
    else:
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        name = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
    return {"name": name, "logical_cores": os.cpu_count()}


# ---------------------------------------------------------------- power

def battery() -> dict[str, Any]:
    """{'present': bool|None, 'plugged': bool|None}. Laptops on battery run much slower."""
    if IS_WINDOWS:
        try:
            import ctypes

            class SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                            ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                            ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

            status = SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                present = status.BatteryFlag not in (128, 255)  # 128 = no battery, 255 = unknown
                plugged = {0: False, 1: True}.get(status.ACLineStatus)
                return {"present": present, "plugged": plugged}
        except Exception:
            pass
        return {"present": None, "plugged": None}
    base = "/sys/class/power_supply"
    try:
        names = os.listdir(base)
    except OSError:
        return {"present": False, "plugged": None}
    present = any(n.upper().startswith("BAT") for n in names)
    plugged = None
    for n in names:
        try:
            with open(os.path.join(base, n, "online"), encoding="ascii") as fh:
                plugged = fh.read().strip() == "1"
        except OSError:
            continue
    return {"present": present, "plugged": plugged}


# ---------------------------------------------------------------- gpu

def gpus() -> list[dict[str, Any]]:
    """GPU names and memory. NVIDIA via nvidia-smi; other cards by name only (Windows)."""
    found: list[dict[str, Any]] = []
    smi = shutil.which("nvidia-smi")
    if smi:
        rc, out, _ = run([smi, "--query-gpu=name,memory.total,memory.used,driver_version",
                          "--format=csv,noheader,nounits"], timeout=8)
        if rc == 0:
            for line in out.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    try:
                        found.append({"name": parts[0], "vram_total_mb": int(float(parts[1])),
                                      "vram_used_mb": int(float(parts[2])),
                                      "driver": parts[3] if len(parts) > 3 else None, "vendor": "nvidia"})
                    except ValueError:
                        continue
    if IS_WINDOWS:
        data = powershell_json("Get-CimInstance Win32_VideoController | "
                               "Select-Object Name, AdapterCompatibility | ConvertTo-Json -Compress")
        if isinstance(data, dict):
            data = [data]
        for item in data or []:
            name = (item or {}).get("Name")
            if name and not any(g["name"] == name for g in found):
                found.append({"name": name, "vendor": (item.get("AdapterCompatibility") or "").lower() or None,
                              "vram_total_mb": None})
    return found


# ---------------------------------------------------------------- listening sockets

def listeners(port: int) -> list[dict[str, Any]]:
    """Processes listening on TCP `port`: [{'addr', 'pid', 'name', 'path', 'start_utc'}]."""
    if IS_WINDOWS:
        script = (
            f"$c = Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue; "
            "$o = @(); foreach ($x in $c) { $p = Get-Process -Id $x.OwningProcess -ErrorAction SilentlyContinue; "
            "$st = $null; if ($p -and $p.StartTime) { $st = $p.StartTime.ToUniversalTime().ToString('o') }; "
            "$o += [pscustomobject]@{addr=[string]$x.LocalAddress; pid=[int]$x.OwningProcess; "
            "name=[string]$p.ProcessName; path=[string]$p.Path; start_utc=$st} }; "
            "ConvertTo-Json -InputObject @($o) -Compress"
        )
        data = powershell_json(script)
        if isinstance(data, dict):
            data = [data]
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        return _netstat_listeners(port)
    return _proc_listeners(port)


def _netstat_listeners(port: int) -> list[dict[str, Any]]:
    """Fallback: parse `netstat -ano` (state names are localized, so match by shape)."""
    rc, out, _ = run(["netstat", "-ano", "-p", "TCP"], timeout=10)
    rc6, out6, _ = run(["netstat", "-ano", "-p", "TCPv6"], timeout=10)
    found = []
    for line in (out + "\n" + out6).splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[0].upper().startswith("TCP"):
            local, remote, pid = parts[1], parts[2], parts[4]
            if local.rsplit(":", 1)[-1] == str(port) and remote.rsplit(":", 1)[-1] in ("0", "*"):
                addr = local.rsplit(":", 1)[0].strip("[]")
                found.append({"addr": addr, "pid": int(pid) if pid.isdigit() else None,
                              "name": None, "path": None, "start_utc": None})
    return found


def _hex_to_ip(hex_addr: str) -> str:
    if len(hex_addr) == 8:
        b = bytes.fromhex(hex_addr)[::-1]
        return ".".join(str(x) for x in b)
    words = [hex_addr[i:i + 8] for i in range(0, 32, 8)]
    raw = b"".join(bytes.fromhex(w)[::-1] for w in words)
    import ipaddress

    return str(ipaddress.IPv6Address(raw))


def _proc_listeners(port: int) -> list[dict[str, Any]]:
    inodes: dict[str, str] = {}
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as fh:
                next(fh)
                for line in fh:
                    parts = line.split()
                    local, state, inode = parts[1], parts[3], parts[9]
                    addr_hex, port_hex = local.split(":")
                    if state == "0A" and int(port_hex, 16) == port:
                        inodes[inode] = _hex_to_ip(addr_hex)
        except (OSError, StopIteration, ValueError):
            continue
    found = []
    if not inodes:
        return found
    pids: dict[str, int] = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                m = re.match(r"socket:\[(\d+)\]", target)
                if m and m.group(1) in inodes:
                    pids[m.group(1)] = int(pid)
        except OSError:
            continue
    for inode, addr in inodes.items():
        pid = pids.get(inode)
        name = start = path = None
        if pid:
            try:
                with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
                    name = fh.read().strip()
                path = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                pass
            start = _proc_start_utc(pid)
        found.append({"addr": addr, "pid": pid, "name": name, "path": path, "start_utc": start})
    return found


def _proc_start_utc(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        start_ticks = int(fields[19])
        with open("/proc/uptime", encoding="ascii") as fh:
            uptime = float(fh.read().split()[0])
        hz = os.sysconf("SC_CLK_TCK")
        started = time.time() - uptime + start_ticks / hz
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started)) + "Z"
    except Exception:
        return None


# ---------------------------------------------------------------- environment & processes

def user_env(name: str) -> str | None:
    """The value Ollama will inherit at its next start: HKCU\\Environment on Windows."""
    if IS_WINDOWS:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                return str(winreg.QueryValueEx(key, name)[0])
        except OSError:
            return None
        except Exception:
            return None
    return os.environ.get(name)


def set_user_env(name: str, value: str | None) -> bool:
    """Set (or delete, when value is None) a user environment variable on Windows and
    broadcast the change so newly started programs see it. Returns True on success."""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            if value is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
        result = ctypes.c_size_t()  # DWORD_PTR: pointer-sized
        ctypes.windll.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
                                                 SMTO_ABORTIFHUNG, 3000, ctypes.byref(result))
        return True
    except Exception:
        return False


def processes_named(names: list[str]) -> list[int]:
    """PIDs whose image name matches one of `names` (case-insensitive)."""
    wanted = {n.lower() for n in names}
    pids: list[int] = []
    if IS_WINDOWS:
        rc, out, _ = run(["tasklist", "/FO", "CSV", "/NH"], timeout=10)
        if rc == 0:
            for line in out.splitlines():
                cols = [c.strip('"') for c in line.split('","')]
                if len(cols) >= 2 and cols[0].strip('"').lower() in wanted:
                    try:
                        pids.append(int(cols[1].strip('"')))
                    except ValueError:
                        continue
        return pids
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
                    if fh.read().strip().lower() in wanted:
                        pids.append(int(pid))
            except OSError:
                continue
    return pids


def snapshot() -> dict[str, Any]:
    """Everything the estimator needs about the machine right now (fast: no subprocesses)."""
    return {"memory": memory(), "battery": battery(), "cpu": cpu()}


# ---------------------------------------------------------------- idle time, staying awake, job objects

def idle_seconds() -> float | None:
    """Seconds since the last keyboard or mouse input in this Windows session (None elsewhere)."""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return None
        tick = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((tick - lii.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return None


_awake_state = {"on": False}


def keep_awake(on: bool) -> bool:
    """Ask Windows not to sleep (the screen may still turn off) while `on`. Applies to the calling
    thread; call it from the thread that keeps running. Returns True if the request was made."""
    if not IS_WINDOWS:
        _awake_state["on"] = on
        return False
    try:
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        ok = bool(ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags)))
        _awake_state["on"] = on and ok
        return ok
    except Exception:
        return False


def on_mains_power() -> bool:
    b = battery()
    return not b.get("present") or b.get("plugged") is not False


# Windows job objects. Claude's app may start the connector inside a job object that ends every
# process in it when the app closes. The worker must not be in such a job. Layouts follow the
# Windows SDK (sizes checked in the tests: 144 bytes on 64-bit).

class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                               "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


JOB_BREAKAWAY_OK, JOB_SILENT_BREAKAWAY_OK, JOB_KILL_ON_CLOSE = 0x0800, 0x1000, 0x2000
_JOB_EXTENDED_LIMITS = 9
_K32: list[Any] = []


def _kernel32() -> Any:
    """kernel32 with declared signatures, as a private instance (other ctypes users are unaffected).
    Handles are pointer-sized: undeclared, ctypes passes them as 32-bit ints, and a call on the
    current-process handle then fails on 64-bit Windows."""
    if not _K32:
        k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        H, BOOL, DWORD = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32
        for name, args, res in (
                ("GetCurrentProcess", [], H),
                ("IsProcessInJob", [H, H, ctypes.POINTER(BOOL)], BOOL),
                ("QueryInformationJobObject", [H, ctypes.c_int, ctypes.POINTER(_ExtendedLimits), DWORD,
                                               ctypes.POINTER(DWORD)], BOOL),
                ("OpenProcess", [DWORD, BOOL, DWORD], H),
                ("GetExitCodeProcess", [H, ctypes.POINTER(DWORD)], BOOL),
                ("CloseHandle", [H], BOOL)):
            fn = getattr(k, name)
            fn.argtypes, fn.restype = args, res
        _K32.append(k)
    return _K32[0]


def job_flags(limit_flags: int) -> dict[str, bool]:
    return {"kill_on_close": bool(limit_flags & JOB_KILL_ON_CLOSE),
            "breakaway_ok": bool(limit_flags & JOB_BREAKAWAY_OK),
            "silent_breakaway": bool(limit_flags & JOB_SILENT_BREAKAWAY_OK)}


def job_object() -> dict[str, Any] | None:
    """The Windows job object this process belongs to: {"in_job": False}, or {"in_job": True,
    "kill_on_close", "breakaway_ok", "silent_breakaway"}. A process in a job that kills on close
    ends when the job's owner (e.g. Claude's app) closes. None when unknown or not on Windows."""
    if not IS_WINDOWS:
        return None
    try:
        k = _kernel32()
        inside = ctypes.c_int(0)
        if not k.IsProcessInJob(k.GetCurrentProcess(), None, ctypes.byref(inside)):
            return None
        if not inside.value:
            return {"in_job": False}
        info = _ExtendedLimits()
        if not k.QueryInformationJobObject(None, _JOB_EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info), None):
            return {"in_job": True}
        return {"in_job": True, **job_flags(info.BasicLimitInformation.LimitFlags)}
    except Exception:
        return None


def in_job_object() -> bool | None:
    """Whether this process runs inside a Windows job object (None when unknown)."""
    j = job_object()
    return None if j is None else bool(j.get("in_job"))


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if IS_WINDOWS:
        try:
            k = _kernel32()
            h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = ctypes.c_uint32(0)
                return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
            finally:
                k.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
