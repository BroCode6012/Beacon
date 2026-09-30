"""Beacon-side control for the shared background scraper.

Beacon REUSES the engine's existing "radar" worker (a durable background scraper
that keeps filling the shared index by cycling the editable watchlist) rather than
running its own. This module just:
  * starts it   -> launches `python -m scripter.cli radar-worker` detached + hidden
  * stops it    -> writes the cooperative stop-flag file the worker polls
  * reads status -> reads radar_status.json directly
  * edits targets -> updates the `watchlist` in scraper_watchlist.json

Operational paths are ABSOLUTE and stay in Scripter's storage folder even though
the large shared index lives in Beacon's storage folder. The worker is launched
with cwd = the Scripter root so its relative config/status paths remain stable.

Shared query-policy helpers come from the engine; this module remains the
Beacon-side control adapter for that worker.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import threading
from collections import Counter, defaultdict
from pathlib import Path

import engine_link
from scripter.research.query_policy import (
    QUERY_AGENT_MAX_PER_CALL,
    QUERY_AUTONOMOUS_MAX_PER_CALL,
    QUERY_COOLDOWN_SECONDS,
    QUERY_MAX_TOTAL,
    QUERY_MIN_COVERAGE,
    QUERY_NEAR_DUPLICATE_JACCARD,
    QUERY_POLICY_KEY,
    normalize_query as _normalize_query,
    query_attempts,
    query_family as _query_family,
    token_jaccard as _token_jaccard,
    watchlist_config_lock,
)

DB = Path(os.environ["YOUTUBE_INDEX_DB"]).resolve()
SCRIPTER_ROOT = engine_link.LAB_REPO.resolve()
STORAGE = SCRIPTER_ROOT / "storage"

STATUS_PATH = STORAGE / "radar_status.json"
STOP_PATH = STORAGE / "radar_stop.flag"
# Beacon-owned "the user deliberately paused the scraper" marker. Distinct from the engine's
# transient STOP_PATH (which the worker consumes/deletes on exit): PAUSE_PATH persists until an
# explicit Start, so the auto-start keep-alive can tell "user stopped it" from "it crashed".
PAUSE_PATH = STORAGE / "signal_scraper_paused.flag"
PID_PATH = STORAGE / "radar_worker.pid"
WATCHLIST_PATH = STORAGE / "scraper_watchlist.json"
HARVEST_YIELD_PATH = STORAGE / "harvest_yield.json"
# On a server the scraper is a system service (deploy/server/systemd/beacon-radar.service), not
# a process Beacon launches: set in that machine's beacon_host.env. Empty = the laptop way.
RADAR_UNIT = os.getenv("BEACON_RADAR_UNIT", "").strip()

_WATCHLIST_LOCK = threading.Lock()


def _python() -> str:
    """Prefer the Scripter venv's pythonw (no console window)."""
    for name in ("pythonw.exe", "python.exe"):
        p = SCRIPTER_ROOT / ".venv" / "Scripts" / name
        if p.exists():
            return str(p)
    return sys.executable


def read_status() -> dict:
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"state": "unknown"}
    except FileNotFoundError:
        return {"state": "not_started"}
    except Exception as ex:  # noqa: BLE001
        return {"state": "unreadable", "error": str(ex)}


def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness. NOTE: on Windows os.kill(pid, 0) is NOT a probe
    (signal 0 == CTRL_C_EVENT), so use the Win32 API there."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _win_process_cmdline(pid: int):
    """Full command line of a Windows process (fast, no subprocess) via
    NtQueryInformationProcess(ProcessCommandLineInformation). Returns the string, or None if it
    can't be read (so the caller can fall back)."""
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ProcessCommandLineInformation = 60
    STATUS_SUCCESS = 0

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_ushort),
                    ("MaximumLength", ctypes.c_ushort),
                    ("Buffer", ctypes.c_void_p)]

    k32 = ctypes.windll.kernel32
    ntdll = ctypes.windll.ntdll
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        need = ctypes.c_ulong(0)
        ntdll.NtQueryInformationProcess(
            handle, ProcessCommandLineInformation, None, 0, ctypes.byref(need))
        if need.value == 0:
            return None
        buf = (ctypes.c_byte * need.value)()
        status = ntdll.NtQueryInformationProcess(
            handle, ProcessCommandLineInformation, buf, need.value, ctypes.byref(need))
        if status != STATUS_SUCCESS:
            return None
        us = ctypes.cast(buf, ctypes.POINTER(_UNICODE_STRING)).contents
        if not us.Buffer or us.Length == 0:
            return ""
        return ctypes.wstring_at(us.Buffer, us.Length // 2)
    except Exception:  # noqa: BLE001
        return None
    finally:
        k32.CloseHandle(handle)


def _pid_is_worker(pid: int) -> bool:
    """Confirm a pid is ACTUALLY our radar worker, so a REUSED pid (after an unclean death, when
    Windows hands that number to another process — INCLUDING the Beacon app itself, which also
    runs under python.exe) is not mistaken for a live worker. Checks the command line for
    'radar-worker'. If the command line can't be read, falls back to the weaker exe-name signal —
    the worker's own pid-file singleton is the real backstop against ever running two."""
    if pid <= 0:
        return False
    if os.name == "nt":
        cmdline = _win_process_cmdline(pid)
        if cmdline is not None:
            return "radar-worker" in cmdline.lower()
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return True
        try:
            size = ctypes.c_ulong(2048)
            buf = ctypes.create_unicode_buffer(size.value)
            if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return True
            exe = (buf.value or "").lower()
            return exe.endswith("python.exe") or exe.endswith("pythonw.exe")
        finally:
            k32.CloseHandle(handle)
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return b"radar-worker" in fh.read().lower()
    except Exception:  # noqa: BLE001
        return True


def is_running() -> bool:
    """True if the worker process is alive AND is actually our worker (pidfile + liveness +
    identity check, so a recycled pid can't masquerade as a running scraper)."""
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:  # noqa: BLE001
        return False
    return _pid_alive(pid) and _pid_is_worker(pid)


def start_worker(clear_stop: bool = True) -> bool:
    """Launch the background scraper (continuous), detached + hidden. No-op if
    already running. Returns True if it launched a new process.

    clear_stop=True  → an EXPLICIT user Start: cancel any pending stop + the user's pause, so
                       Start always wins (fixes Stop-then-Start silently exiting the worker).
    clear_stop=False → the auto-start KEEP-ALIVE: must RESPECT a deliberate user pause — it may
                       revive a crashed worker but must never override a Stop, else the keep-alive
                       would wipe the stop request every few minutes and Stop could never stick."""
    if clear_stop:
        # explicit Start — cancel a pending stop AND the persistent user pause.
        for p in (STOP_PATH, PAUSE_PATH):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
    elif PAUSE_PATH.exists():
        return False   # user deliberately paused it — the keep-alive must not relaunch it
    if is_running():
        return False
    if RADAR_UNIT:
        return _systemctl("start", "--no-block", RADAR_UNIT)
    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS: survives Beacon closing; pythonw = no console window.
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        [_python(), "-m", "scripter.cli", "radar-worker"],
        cwd=str(SCRIPTER_ROOT),
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    return True


def stop_worker() -> None:
    """Ask the worker to stop after its current step, and mark it PAUSED so the auto-start
    keep-alive won't relaunch it within minutes. Only an explicit Start clears the pause."""
    STOP_PATH.parent.mkdir(parents=True, exist_ok=True)
    STOP_PATH.write_text("stop", encoding="utf-8")
    PAUSE_PATH.write_text("paused by user", encoding="utf-8")


AUTOSTART_SCRIPT = Path(__file__).with_name("install_radar_autostart.ps1")


def _systemctl(*args: str) -> bool:
    """Server mode. Starting the scraper's service is the ONE system action Beacon's account
    may take (a polkit rule installed by deploy/server/install.sh allows exactly that unit)."""
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, text=True,
                              timeout=30).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def autostart_installed() -> bool:
    """True if the 'BeaconRadarAutostart' scheduled task exists (keeps the scraper running
    on login + as a keep-alive). Shells to PowerShell — call OFF the UI thread.
    Server mode: the service itself starts at boot (unless paused), so this reads that."""
    if RADAR_UNIT:
        return _systemctl("is-enabled", "--quiet", RADAR_UNIT)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(AUTOSTART_SCRIPT), "-Status"],
            capture_output=True, text=True, timeout=25)
        # -Status prints "enabled:<State>". Only Ready/Running actually RUNS — a task that
        # exists but is Disabled would never fire, so it must read as OFF (not "enabled").
        return out.stdout.strip().lower() in ("enabled:ready", "enabled:running")
    except Exception:  # noqa: BLE001
        return False


def set_autostart(on: bool) -> bool:
    """Install (on) or remove (off) the auto-start-on-login keep-alive task. Returns success.
    No elevation needed (schtasks /SC MINUTE /IT). Call OFF the UI thread.
    Server mode: boot-start belongs to the machine's owner, not to the app — Stop/Start still
    work, and a Stop survives a reboot (the service skips itself while paused)."""
    if RADAR_UNIT:
        return False
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(AUTOSTART_SCRIPT)]
    if not on:
        cmd.append("-Uninstall")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        return out.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def get_niches() -> list[str]:
    """The editable list of niches / search queries the scraper cycles through."""
    data = _read_watchlist_config()
    wl = data.get("watchlist") if isinstance(data, dict) else data
    return [str(x) for x in (wl or [])]


def save_niches(niches: list[str]) -> None:
    """Update ONLY the watchlist, preserving all other radar settings in the file."""
    with _WATCHLIST_LOCK, watchlist_config_lock(WATCHLIST_PATH):
        data = _read_watchlist_config()
        data["watchlist"] = [n.strip() for n in niches if n.strip()]
        _write_watchlist_config(data)


def update_niches(mutator) -> list[str]:
    """Atomic read-modify-write of the watchlist under the cross-process lock. `mutator` takes
    the CURRENT list and returns the new list; the read + compute + write all happen while the
    lock is held, so a concurrent writer (Autopilot top-up, the radar worker) can't have its
    entry silently overwritten by a read-then-write race."""
    with _WATCHLIST_LOCK, watchlist_config_lock(WATCHLIST_PATH):
        data = _read_watchlist_config()
        current = [str(x) for x in (data.get("watchlist") or [])]
        result = mutator(list(current))
        data["watchlist"] = [str(n).strip() for n in (result or []) if str(n).strip()]
        _write_watchlist_config(data)
        return data["watchlist"]


def update_config(mutator) -> dict:
    """Atomic read-modify-write of the WHOLE scraper config dict under the cross-process lock.
    `mutator` receives the current config dict, mutates it in place (or returns a new dict).
    Use for non-watchlist settings (e.g. harvest_per_cycle) so a concurrent watchlist writer's
    entry isn't clobbered by an unlocked read-then-write of the shared file."""
    with _WATCHLIST_LOCK, watchlist_config_lock(WATCHLIST_PATH):
        data = _read_watchlist_config()
        result = mutator(data)
        if isinstance(result, dict):
            data = result
        _write_watchlist_config(data)
        return data


def _read_watchlist_config() -> dict:
    try:
        loaded = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded
        if isinstance(loaded, list):
            return {"watchlist": loaded}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _write_watchlist_config(data: dict) -> None:
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCHLIST_PATH.with_name(WATCHLIST_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(WATCHLIST_PATH)  # atomic


def _read_yield_state() -> dict:
    try:
        data = json.loads(HARVEST_YIELD_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _expanded_queries(config: dict, watchlist: list[str]) -> list[str]:
    if not config.get("harvest_expand", True):
        return list(dict.fromkeys(watchlist))
    try:
        from scripter.research.channel_discovery import expand_queries
        return expand_queries(watchlist)
    except Exception:  # noqa: BLE001 — a summary must still work if engine import fails
        return list(dict.fromkeys(watchlist))


def _watchlist_summary_from(
    config: dict,
    yield_state: dict,
    *,
    now: float,
) -> dict:
    watchlist = [str(q).strip() for q in config.get("watchlist", []) if str(q).strip()]
    expanded = _expanded_queries(config, watchlist)
    searched = [
        q for q in expanded if query_attempts(yield_state.get(q)) > 0
    ]
    retire_after = max(1, int(config.get("harvest_retire_after", 3) or 3))
    active = [
        q for q in expanded
        if int((yield_state.get(q) or {}).get("zero_streak", 0)) < retire_after
    ]
    productive = [
        q for q in searched if int((yield_state.get(q) or {}).get("total_new", 0)) > 0
    ]
    coverage = len(searched) / len(expanded) if expanded else 1.0
    harvest_per_cycle = max(1, int(config.get("harvest_per_cycle", 30) or 30))
    underfed_threshold = harvest_per_cycle * 3
    underfed = len(active) < underfed_threshold

    families = [_query_family(q) for q in watchlist]
    family_counts = Counter(f for f in families if f)
    family_yield = defaultdict(lambda: {"searches": 0, "total_new": 0})
    expanded_set = set(expanded)
    for query, raw in yield_state.items():
        if query not in expanded_set or not isinstance(raw, dict):
            continue
        family = _query_family(query)
        if not family:
            continue
        family_yield[family]["searches"] += int(raw.get("searches", 0) or 0)
        family_yield[family]["total_new"] += int(raw.get("total_new", 0) or 0)
    top_yield = []
    for family, stats in family_yield.items():
        searches_n = stats["searches"]
        if searches_n <= 0:
            continue
        top_yield.append({
            "family": family,
            "new_channels": stats["total_new"],
            "searches": searches_n,
            "avg_new_per_search": round(stats["total_new"] / searches_n, 2),
        })
    top_yield.sort(
        key=lambda row: (row["new_channels"], row["avg_new_per_search"]), reverse=True)

    policy = config.get(QUERY_POLICY_KEY)
    policy = policy if isinstance(policy, dict) else {}
    try:
        last_added_ts = float(policy.get("last_added_ts", 0) or 0)
    except (TypeError, ValueError):
        last_added_ts = 0.0
    cooldown_remaining = max(0, int(QUERY_COOLDOWN_SECONDS - (now - last_added_ts)))
    slots_left = max(0, QUERY_MAX_TOTAL - len(watchlist))
    can_add = (
        slots_left > 0
        and cooldown_remaining == 0
        and (underfed or coverage >= QUERY_MIN_COVERAGE)
    )
    if slots_left <= 0:
        block_reason = "watchlist_at_capacity"
    elif cooldown_remaining > 0:
        block_reason = "cooldown_active"
    elif not underfed and coverage < QUERY_MIN_COVERAGE:
        block_reason = "existing_query_backlog"
    else:
        block_reason = None

    recent = policy.get("recent_additions")
    recent = recent if isinstance(recent, list) else []
    return {
        "count": len(watchlist),
        "max_total": QUERY_MAX_TOTAL,
        "slots_left": slots_left,
        "families": len(family_counts),
        "duplicate_family_excess": sum(
            count - 1 for count in family_counts.values() if count > 1),
        "expanded_queries": len(expanded),
        "searched_queries": len(searched),
        "unsearched_queries": max(0, len(expanded) - len(searched)),
        "coverage_pct": round(coverage * 100, 1),
        "active_queries": len(active),
        "underfed_threshold": underfed_threshold,
        "underfed": underfed,
        "productive_pct": round(
            (len(productive) / len(searched) * 100) if searched else 0.0, 1),
        "cooldown_remaining_minutes": (cooldown_remaining + 59) // 60,
        "can_add_autonomously": can_add,
        "block_reason": block_reason,
        "top_yield_families": top_yield[:8],
        "recent_additions": recent[:5],
    }


def watchlist_summary(*, now: float | None = None) -> dict:
    """Full measured summary used by the agent instead of an arbitrary first-25 sample."""
    config = _read_watchlist_config()
    return _watchlist_summary_from(
        config, _read_yield_state(), now=time_now() if now is None else float(now))


def add_watchlist_queries(
    queries: list[str],
    *,
    autonomous: bool = False,
    source: str | None = None,
    now: float | None = None,
) -> dict:
    """Policy-controlled query addition with deterministic dedup/cap/cooldown gates."""
    now_ts = time_now() if now is None else float(now)
    source = source or ("autopilot" if autonomous else "agent")
    requested = [" ".join(str(q).split()) for q in (queries or []) if str(q).strip()]
    with _WATCHLIST_LOCK, watchlist_config_lock(WATCHLIST_PATH):
        config = _read_watchlist_config()
        yield_state = _read_yield_state()
        summary = _watchlist_summary_from(config, yield_state, now=now_ts)
        watchlist = [
            str(q).strip() for q in config.get("watchlist", []) if str(q).strip()
        ]
        if autonomous and not summary["can_add_autonomously"]:
            return {
                "requested": len(requested),
                "accepted": [],
                "rejected": [
                    {"query": query, "reason": summary["block_reason"]}
                    for query in requested
                ],
                "added": 0,
                "total": len(watchlist),
                "policy": summary,
            }

        max_per_call = (
            QUERY_AUTONOMOUS_MAX_PER_CALL if autonomous else QUERY_AGENT_MAX_PER_CALL)
        existing_norm = {_normalize_query(q) for q in watchlist}
        existing_families = [_query_family(q) for q in watchlist]
        accepted: list[str] = []
        rejected: list[dict] = []

        for query in requested:
            normalized = _normalize_query(query)
            tokens = normalized.split()
            family = _query_family(query)
            reason = None
            if len(tokens) < 2:
                reason = "query_too_broad"
            elif len(tokens) > 10:
                reason = "query_too_long"
            elif normalized in existing_norm:
                reason = "exact_duplicate"
            elif family in existing_families:
                reason = "family_budget_reached"
            elif any(
                    _token_jaccard(family, other) >= QUERY_NEAR_DUPLICATE_JACCARD
                    for other in existing_families if family and other):
                reason = "near_duplicate"
            elif len(watchlist) + len(accepted) >= QUERY_MAX_TOTAL:
                reason = "watchlist_at_capacity"
            elif len(accepted) >= max_per_call:
                reason = "per_call_limit"

            if reason:
                rejected.append({"query": query, "reason": reason})
                continue
            accepted.append(query)
            existing_norm.add(normalized)
            existing_families.append(family)

        if accepted:
            config["watchlist"] = watchlist + accepted
            policy = config.get(QUERY_POLICY_KEY)
            policy = dict(policy) if isinstance(policy, dict) else {}
            stamp = dt.datetime.fromtimestamp(
                now_ts, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
            previous = policy.get("recent_additions")
            previous = previous if isinstance(previous, list) else []
            policy.update({
                "last_added_ts": now_ts,
                "last_added_at": stamp,
                "last_source": source,
                "recent_additions": [
                    {"query": query, "family": _query_family(query), "at": stamp,
                     "source": source}
                    for query in accepted
                ] + previous[:48],
            })
            config[QUERY_POLICY_KEY] = policy
            _write_watchlist_config(config)
            summary = _watchlist_summary_from(config, yield_state, now=now_ts)

        return {
            "requested": len(requested),
            "accepted": accepted,
            "rejected": rejected,
            "added": len(accepted),
            "total": len(watchlist) + len(accepted),
            "policy": summary,
        }


def time_now() -> float:
    """Small seam for deterministic tests."""
    import time
    return time.time()
