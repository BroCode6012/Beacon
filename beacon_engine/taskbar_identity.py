"""Give a Flet app's window its own taskbar button.

Flet does not draw windows itself: every Flet app hands its page to one shared helper,
``flet.exe``, inside the venv. Windows groups taskbar buttons by the program that owns
the window unless the window carries its own AppUserModelID — so Beacon, Beacon and
Vault, which all run from Beacon's venv, collapsed into ONE button showing whichever app
opened first (2026-10-01: Beacon opened "inside" Vault's button).

``claim_taskbar_button`` stamps our window with a per-app ID as soon as flet.exe creates
it. ``stamp_shortcut`` writes the same ID onto the app's .lnk files, so a shortcut the
user pins and the window it opens are one button with one icon, not two.

Best-effort throughout: branding must never block or crash an app's startup.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

# Flet 0.28's desktop client (a Flutter runner) creates its main window with this class.
FLET_WINDOW_CLASS = "FLUTTER_RUNNER_WIN32_WINDOW"

_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _descendant_pids(root: int) -> set[int]:
    """Every process started (directly or not) by ``root`` — flet.exe is our child."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        return set()
    parent_of: dict[int, int] = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            parent_of[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    found, frontier = set(), {root}
    while frontier:
        frontier = {pid for pid, ppid in parent_of.items()
                    if ppid in frontier and pid not in found and pid != root}
        found |= frontier
    return found


def _flet_windows(pids: set[int]) -> list[int]:
    """Top-level Flet windows owned by ``pids`` (shown or not yet shown)."""
    import win32con
    import win32gui
    import win32process

    hits: list[int] = []

    def visit(hwnd, _):
        if (win32process.GetWindowThreadProcessId(hwnd)[1] in pids
                and not win32gui.GetWindow(hwnd, win32con.GW_OWNER)
                and win32gui.GetClassName(hwnd) == FLET_WINDOW_CLASS):
            hits.append(hwnd)
        return True

    win32gui.EnumWindows(visit, None)
    return hits


def window_app_id(hwnd: int) -> str | None:
    """The AppUserModelID a window currently carries (None = grouped by program)."""
    from win32com.propsys import propsys, pscon

    store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
    return store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()


def stamp_window(hwnd: int, app_id: str) -> None:
    """Put ``hwnd`` on the taskbar button named ``app_id`` (takes effect immediately)."""
    import pythoncom
    from win32com.propsys import propsys, pscon

    store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID,
                   propsys.PROPVARIANTType(app_id, pythoncom.VT_LPWSTR))
    store.Commit()


def _watch(app_id: str, timeout: float) -> None:
    import pythoncom

    pythoncom.CoInitialize()
    try:
        deadline = time.monotonic() + timeout
        stamped: set[int] = set()
        while time.monotonic() < deadline:
            for hwnd in _flet_windows(_descendant_pids(os.getpid())):
                if hwnd not in stamped:
                    stamp_window(hwnd, app_id)
                    stamped.add(hwnd)
                    log.info("taskbar: window %s now groups as %s", hwnd, app_id)
            if stamped:
                return
            time.sleep(0.2)
        log.warning("taskbar: no Flet window appeared within %.0fs — %s stays grouped "
                    "with other Flet apps", timeout, app_id)
    except Exception as ex:  # noqa: BLE001 -- never let branding break the app
        log.warning("taskbar: could not claim a button for %s: %s", app_id, ex)
    finally:
        pythoncom.CoUninitialize()


def claim_taskbar_button(app_id: str, timeout: float = 90.0) -> threading.Thread | None:
    """Give this app's window its own taskbar button, separate from other Flet apps.

    Call from the app's Flet ``main``. Returns at once; a background thread waits for
    flet.exe's window (it can appear after ``main`` starts) and stamps it.
    """
    if sys.platform != "win32":
        return None
    try:
        import win32gui  # noqa: F401 -- fail here, quietly, if pywin32 is missing
    except ImportError:
        log.warning("taskbar: pywin32 missing — %s will share a button with other "
                    "Flet apps (pip install pywin32)", app_id)
        return None
    thread = threading.Thread(target=_watch, args=(app_id, timeout), daemon=True,
                              name="taskbar-identity")
    thread.start()
    return thread


def stamp_shortcut(lnk_path: str, app_id: str) -> None:
    """Write ``app_id`` onto a .lnk so pinning it matches the app's running window."""
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shellcon

    store = propsys.SHGetPropertyStoreFromParsingName(
        os.path.abspath(lnk_path), None, shellcon.GPS_READWRITE, propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID,
                   propsys.PROPVARIANTType(app_id, pythoncom.VT_LPWSTR))
    store.Commit()


if __name__ == "__main__":
    # python -m beacon_engine.taskbar_identity stamp <shortcut.lnk> <app-id>
    if len(sys.argv) == 4 and sys.argv[1] == "stamp":
        stamp_shortcut(sys.argv[2], sys.argv[3])
        print(f"stamped {sys.argv[2]} -> {sys.argv[3]}")
    else:
        print(__doc__)
        print("usage: python -m beacon_engine.taskbar_identity stamp <shortcut.lnk> <app-id>")
        sys.exit(2)
