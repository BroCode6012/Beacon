"""Beacon background-worker command line; no writing-app dependencies."""
from __future__ import annotations
import argparse
import os
import sys
from rich.console import Console
from rich.table import Table
console = Console()


class _RadarLock:
    """Cross-platform single-instance lock. The OS releases it when the process
    exits -- including a hard kill -- so a crashed cycle never wedges the next."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        try:
            self._fh = open(self.path, "w")
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if self._fh:
                self._fh.close()
                self._fh = None
            return False

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None


def cmd_radar_worker(args) -> int:
    # Under pythonw (the hidden scheduled task) there is no console, so
    # sys.stdout/stderr are None -- any library that prints (e.g. a vendor
    # SDK's progress bars) crashes. That silently killed the auto-GPU step every
    # cycle. Give the process real (null) streams so printing can't crash it.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    from beacon_engine.research.radar import (
        DEFAULT_CONFIG_PATH,
        DEFAULT_STATUS_PATH,
        DEFAULT_STOP_PATH,
        load_config,
        run_worker,
        save_config,
    )

    config_path = args.config or str(DEFAULT_CONFIG_PATH)
    cfg = load_config(config_path)
    changed = False
    if args.watchlist:
        cfg.watchlist = _split_watchlist(args.watchlist)
        changed = True
    if args.interval is not None:
        cfg.interval_minutes = args.interval
        changed = True
    if args.db:
        cfg.db_path = args.db
        changed = True
    for arg_name in [
        "seed_limit",
        "max_channels",
        "videos_per_channel",
        "enrich_top",
        "outliers_limit",
        "min_channel_videos",
    ]:
        value = getattr(args, arg_name)
        if value is not None:
            setattr(cfg, arg_name, value)
            changed = True
    if args.min_score is not None:
        cfg.min_score = args.min_score
        changed = True
    if args.no_faceless_filter:
        cfg.faceless_only = False
        changed = True
    if changed or not args.config:
        save_config(cfg, config_path)

    console.print(f"[bold]Faceless radar worker[/] config={config_path}")
    console.print(
        f"[dim]DB: {cfg.db_path} | every {cfg.interval_minutes} min | "
        f"niches: {len(cfg.watchlist)}[/]"
    )
    # Single-instance guard: the scheduler fires every 15 min, but a cycle (esp.
    # one with a GPU run) can take longer. Without this, cycles overlap and two
    # radars fight over the same GPU -- exactly what left an orphaned L4 billing.
    # An OS file-lock auto-releases even on hard-kill, so it can never wedge.
    lock = _RadarLock(os.path.join(os.getenv("BEACON_ENGINE_STATE_DIR") or "storage", "radar.lock"))
    if not lock.acquire():
        console.print("[yellow]Another radar cycle is already running — skipping this one.[/]")
        return 0

    # Tee every step to storage/radar.log: the hidden worker has no console, so
    # without this a failing step (like the GPU one) leaves no trace anywhere.
    import datetime as _dt
    radar_log = os.path.join(os.getenv("BEACON_ENGINE_STATE_DIR") or "storage", "radar.log")

    def _step(m: str) -> None:
        console.print(f"[dim]· {m}[/]")
        try:
            with open(radar_log, "a", encoding="utf-8") as fh:
                fh.write(f"[{_dt.datetime.now().strftime('%m-%d %H:%M:%S')}] {m}\n")
        except OSError:
            pass

    try:
        return run_worker(
            config_path=config_path,
            status_path=args.status or str(DEFAULT_STATUS_PATH),
            stop_path=args.stop_file or str(DEFAULT_STOP_PATH),
            once=args.once,
            on_step=_step,
        )
    finally:
        lock.release()


def cmd_radar_status(args) -> int:
    from beacon_engine.research.radar import DEFAULT_CONFIG_PATH, DEFAULT_STATUS_PATH, load_config, read_status

    status = read_status(args.status or str(DEFAULT_STATUS_PATH))
    cfg = load_config(args.config or str(DEFAULT_CONFIG_PATH))
    table = Table(title="Faceless radar status")
    table.add_column("field")
    table.add_column("value")
    for key in [
        "state",
        "updated_at",
        "cycle",
        "current_query",
        "message",
        "discovered",
        "channels",
        "enriched",
        "outliers",
        "next_scan_seconds",
        "error",
    ]:
        if key in status:
            table.add_row(key, str(status.get(key)))
    table.add_row("config_db", cfg.db_path)
    table.add_row("watchlist", ", ".join(cfg.watchlist[:8]))
    table.add_row("interval_minutes", str(cfg.interval_minutes))
    console.print(table)
    return 0


def cmd_radar_stop(args) -> int:
    from beacon_engine.research.radar import DEFAULT_STOP_PATH, request_stop

    request_stop(args.stop_file or str(DEFAULT_STOP_PATH))
    console.print("[yellow]Stop requested.[/] The worker exits after the current provider call finishes.")
    return 0


def _split_watchlist(value: str) -> list[str]:
    import re

    return [x.strip() for x in re.split(r"[\n,;]+", value or "") if x.strip()]


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("radar-worker", help="Run the global faceless radar worker")
    sp.add_argument("--config", help="Radar config JSON path")
    sp.add_argument("--status", help="Radar status JSON path")
    sp.add_argument("--stop-file", help="Stop-request flag path")
    sp.add_argument("--once", action="store_true", help="Run one scan cycle and exit")
    sp.add_argument("--watchlist", help="Comma/newline separated niches to scan")
    sp.add_argument("--interval", type=int, help="Minutes between cycles")
    sp.add_argument("--db", help="SQLite index path")
    sp.add_argument("--seed-limit", type=int)
    sp.add_argument("--max-channels", type=int)
    sp.add_argument("--videos-per-channel", type=int)
    sp.add_argument("--enrich-top", type=int)
    sp.add_argument("--outliers-limit", type=int)
    sp.add_argument("--min-channel-videos", type=int)
    sp.add_argument("--min-score", type=float)
    sp.add_argument("--no-faceless-filter", action="store_true")
    sp.set_defaults(func=cmd_radar_worker)

    sp = sub.add_parser("radar-status", help="Show global faceless radar worker status")
    sp.add_argument("--config", help="Radar config JSON path")
    sp.add_argument("--status", help="Radar status JSON path")
    sp.set_defaults(func=cmd_radar_status)

    sp = sub.add_parser("radar-stop", help="Request the radar worker to stop")
    sp.add_argument("--stop-file", help="Stop-request flag path")
    sp.set_defaults(func=cmd_radar_stop)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)

if __name__ == "__main__":
    raise SystemExit(main())
