"""The move kit — carry Beacon from one machine to another, and prove it arrived intact.

The operator's rule (2026-09-29): "let's test Oracle — when we find better options we move."
So a move is four reusable steps, none of them tied to Oracle:

    code      pack the program (Beacon + its engine) — refuses if anything secret-looking
              is in it. The target installs it with deploy/server/install.sh.
    pack      a frozen, consistent snapshot of the LIVE library (the scraper may keep
              writing), cut into compressed parts with a fingerprint each.
    send      upload the parts over SSH. Re-running it skips every part the target already
              holds with the right fingerprint — a dropped connection resumes, never restarts.
    receive   ON THE TARGET: check every fingerprint, rebuild the library, run SQLite's
              health check, compare channel and video counts with the snapshot, and only then
              swap it in (the previous copy is kept as .previous).

Measured on this library (2026-09-29): gzip level 1 shrinks it to ~25% at ~100 MB/s; level 6
only reaches ~23% at half the speed, so level 1 is the default. Standard library only — the
target may not have Beacon's packages yet.

    python beacon_move.py code    --out D:\\Youtube\\beacon_move
    python beacon_move.py pack    --out D:\\Youtube\\beacon_move\\lib-20260929
    python beacon_move.py send    --out D:\\Youtube\\beacon_move\\lib-20260929 --host ubuntu@IP --key KEYFILE
    python3 beacon_move.py receive --dir ~/beacon-incoming/lib-20260929 --into /var/lib/beacon/data/youtube_index.sqlite3 --owner beacon

And the scraper's memory (searches, progress bookmarks, face/faceless models), so the target
continues where this machine stopped instead of starting from zero:

    python beacon_move.py settings --out D:\\Youtube\\beacon_move --host ubuntu@IP --key KEYFILE
    sudo python3 beacon_move.py apply-settings --package ~/beacon-incoming/beacon_settings.tar.gz --engine-storage /var/lib/beacon/engine-storage --into /var/lib/beacon/data/youtube_index.sqlite3 --owner beacon
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORMAT = "beacon-move-v1"
PART_MB = 512
LEVEL = 1
BLOCK = 16 * 1024 * 1024                     # streaming block: memory stays small

# What a code package must never carry. Names first (cheap), then contents.
SECRET_NAMES = re.compile(
    r"(^|/)\.env($|\.)|token|secret|password|credential|\.sqlite3|\.db$|\.pem$|\.key$|"
    r"id_rsa|id_ed25519|_oracle_ed25519", re.I)
SECRET_TEXT = re.compile(
    rb"(sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|ghp_[A-Za-z0-9]{30,}"
    rb"|-----BEGIN [A-Z ]*PRIVATE KEY-----)")


class MoveError(RuntimeError):
    """Something a person must look at — the move stopped before changing anything."""


def say(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


@contextlib.contextmanager
def keep_awake():
    """Stop Windows from idle-sleeping mid-move (the screen may still turn off)."""
    if os.name != "nt":
        yield
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetThreadExecutionState(0x80000000 | 0x00000001)   # CONTINUOUS | SYSTEM_REQUIRED
    try:
        yield
    finally:
        k32.SetThreadExecutionState(0x80000000)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


# -- code --------------------------------------------------------------------------------------
def code_files(root: Path) -> list[Path]:
    """What git considers this project's source (tracked + new, minus .gitignore'd)."""
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=root, capture_output=True, text=True, check=True).stdout
    return [root / line for line in out.splitlines() if line and (root / line).is_file()]


def package_code(out: Path, beacon: Path = HERE) -> dict:
    """Package one self-contained Beacon checkout, excluding private runtime state."""
    entries = [(path, f"Beacon/{path.relative_to(beacon).as_posix()}") for path in code_files(beacon)]
    flagged = [arc for _path, arc in entries if SECRET_NAMES.search(arc)]
    for path, arc in entries:
        if SECRET_TEXT.search(path.read_bytes()):
            flagged.append(arc)
    if flagged:
        raise MoveError("refusing to package — secret-looking files: " + ", ".join(flagged[:10]))
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        for path, arc in entries:
            tar.add(path, arcname=arc)
    return {"files": len(entries), "bytes": out.stat().st_size, "path": str(out)}


# -- pack --------------------------------------------------------------------------------------
def snapshot(db: Path, dest: Path) -> None:
    """A consistent copy of a database other programs are still writing to.

    SQLite's online backup copied in ONE step (pages=-1) reads a single consistent snapshot and
    is never restarted by other writers — stepwise backups restart whenever the source changes,
    which with the scraper writing every few seconds could mean never finishing.
    """
    if dest.exists():
        dest.unlink()
    src = sqlite3.connect(str(db), timeout=120)
    try:
        src.execute("PRAGMA query_only=ON")
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst, pages=-1)
        finally:
            dst.close()
    finally:
        src.close()


def health_check(db: Path) -> str:
    """SQLite's quick_check — with a 1 GB page cache.

    With the default cache it re-reads the same pages again and again: on Oracle's disk it
    read 99.5 GB for a 34 GB library and was still going after an hour (2026-09-29), while a
    plain count with a big cache took 6 seconds.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA cache_size=-1000000")
        return con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()


def db_facts(db: Path) -> dict:
    """What must be identical on both sides of a move."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("channels", "videos") if t in tables}
        return {"counts": counts,
                "user_version": con.execute("PRAGMA user_version").fetchone()[0],
                "page_size": con.execute("PRAGMA page_size").fetchone()[0]}
    finally:
        con.close()


def split(source: Path, outdir: Path, part_bytes: int, level: int) -> dict:
    """Cut the snapshot into gzip parts (plain gzip: `gunzip` can read them too)."""
    outdir.mkdir(parents=True, exist_ok=True)
    raw_digest = hashlib.sha256()
    parts, raw_total = [], 0
    with open(source, "rb") as fh:
        index = 0
        while True:
            name = f"part-{index:04d}.gz"
            compressor = zlib.compressobj(level, zlib.DEFLATED, 31)       # 31 = gzip wrapper
            part_digest, raw_in_part = hashlib.sha256(), 0
            with open(outdir / name, "wb") as out:
                while raw_in_part < part_bytes:
                    block = fh.read(min(BLOCK, part_bytes - raw_in_part))
                    if not block:
                        break
                    raw_in_part += len(block)
                    raw_digest.update(block)
                    data = compressor.compress(block)
                    out.write(data)
                    part_digest.update(data)
                tail = compressor.flush()
                out.write(tail)
                part_digest.update(tail)
            if raw_in_part == 0:
                (outdir / name).unlink()
                break
            parts.append({"name": name, "raw_bytes": raw_in_part,
                          "bytes": (outdir / name).stat().st_size,
                          "sha256": part_digest.hexdigest()})
            raw_total += raw_in_part
            say(f"packed {name}: {raw_total / 1e9:.1f} GB so far")
            index += 1
    return {"parts": parts, "raw_bytes": raw_total, "raw_sha256": raw_digest.hexdigest()}


def pack(db: Path, outdir: Path, part_mb: int = PART_MB, level: int = LEVEL,
         check: str = "quick") -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    snap = outdir / "snapshot.sqlite3"
    say(f"freezing a consistent snapshot of {db} ({db.stat().st_size / 1e9:.1f} GB)")
    started = time.time()
    snapshot(db, snap)
    say(f"snapshot taken in {time.time() - started:.0f}s — counting what it holds")
    facts = db_facts(snap)
    say(f"snapshot holds {facts['counts']}")
    if check == "quick":
        say("health-checking the snapshot here, where the disk is fast")
        facts["health_check"] = health_check(snap)
        if facts["health_check"] != "ok":
            snap.unlink()                          # never leave a 34 GB bad copy behind
            raise MoveError(f"the snapshot failed its health check: {facts['health_check']}")
    pieces = split(snap, outdir, part_mb * 1024 * 1024, level)
    snap.unlink()                                  # the parts ARE the snapshot now
    manifest = {
        "format": FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": socket.gethostname(),
        "level": level,
        "database": {"raw_bytes": pieces["raw_bytes"], "raw_sha256": pieces["raw_sha256"],
                     **facts},
        "parts": pieces["parts"],
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    packed = sum(p["bytes"] for p in manifest["parts"])
    say(f"packed {len(manifest['parts'])} parts: {pieces['raw_bytes'] / 1e9:.1f} GB -> "
        f"{packed / 1e9:.1f} GB")
    return manifest


# -- send --------------------------------------------------------------------------------------
class SSH:
    """The target machine, reached with plain ssh/scp (Windows' built-in OpenSSH or any other)."""

    def __init__(self, host: str, key: str, port: int = 22):
        self.host, self.key, self.port = host, key, port
        win = Path(r"C:\Windows\System32\OpenSSH")
        self.ssh = str(win / "ssh.exe") if (win / "ssh.exe").exists() else "ssh"
        self.scp = str(win / "scp.exe") if (win / "scp.exe").exists() else "scp"
        self.options = ["-i", key, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                        "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=20",
                        "-o", "ServerAliveInterval=15"]

    def run(self, command: str, timeout: int = 600) -> tuple[int, str]:
        proc = subprocess.run([self.ssh, *self.options, "-p", str(self.port), self.host,
                               command], capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def put(self, local: Path, remote: str, timeout: int = 3600) -> None:
        proc = subprocess.run([self.scp, "-q", *self.options, "-P", str(self.port),
                               str(local), f"{self.host}:{remote}"],
                              capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise MoveError(f"upload of {local.name} failed: {(proc.stderr or '').strip()}")

    def hashes(self, remote_dir: str) -> dict[str, str]:
        code, out = self.run(f"cd {remote_dir} 2>/dev/null && sha256sum part-*.gz 2>/dev/null")
        found = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and len(parts[0]) == 64:
                found[parts[1].lstrip("*")] = parts[0]
        return found


def parts_to_send(manifest: dict, remote: dict[str, str]) -> list[dict]:
    """Only what the target is missing or holds damaged — this is what makes send resumable."""
    return [p for p in manifest["parts"] if remote.get(p["name"]) != p["sha256"]]


def send(outdir: Path, target: SSH, remote_dir: str, tries: int = 3) -> dict:
    manifest = json.loads((outdir / "manifest.json").read_text(encoding="utf-8"))
    code, out = target.run(f"mkdir -p {remote_dir}")
    if code != 0:
        raise MoveError(f"cannot create {remote_dir} on the target: {out.strip()}")
    todo = parts_to_send(manifest, target.hashes(remote_dir))
    skipped = len(manifest["parts"]) - len(todo)
    if skipped:
        say(f"{skipped} parts already on the target and intact — skipping them")
    total = sum(p["bytes"] for p in todo)
    sent, started = 0, time.time()
    for part in todo:
        for attempt in range(1, tries + 1):
            try:
                target.put(outdir / part["name"], f"{remote_dir}/{part['name']}")
                break
            except (MoveError, subprocess.TimeoutExpired) as exc:
                if attempt == tries:
                    raise MoveError(f"{exc} — run send again: finished parts are kept") from exc
                say(f"{part['name']}: {exc} — retrying in {10 * attempt}s")
                time.sleep(10 * attempt)
        sent += part["bytes"]
        rate = sent / max(1.0, time.time() - started)
        left = (total - sent) / max(1.0, rate)
        say(f"sent {part['name']} — {sent / 1e9:.2f}/{total / 1e9:.2f} GB, "
            f"{rate * 8 / 1e6:.1f} Mbit/s, ~{left / 60:.0f} min left")
    target.put(outdir / "manifest.json", f"{remote_dir}/manifest.json")  # last: marks complete
    return {"sent": len(todo), "skipped": skipped, "bytes": sent,
            "minutes": round((time.time() - started) / 60, 1)}


# -- receive (runs on the target) --------------------------------------------------------------
def receive(indir: Path, into: Path, owner: str | None = None, check: str = "none") -> dict:
    """Rebuild + verify on the target. The default skips a second health check: the rebuilt
    file's sha256 equal to the snapshot's proves it is byte-for-byte the copy that was already
    checked on the source. `check="quick"` re-checks anyway (slow on a slow disk)."""
    manifest = json.loads((indir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise MoveError(f"unknown package format {manifest.get('format')!r}")
    say(f"checking {len(manifest['parts'])} parts")
    for part in manifest["parts"]:
        path = indir / part["name"]
        if not path.exists():
            raise MoveError(f"{part['name']} is missing — run send again")
        if sha256_file(path) != part["sha256"]:
            raise MoveError(f"{part['name']} is damaged — delete it and run send again")
    into.parent.mkdir(parents=True, exist_ok=True)
    incoming = into.with_name(into.name + ".incoming")
    digest, written = hashlib.sha256(), 0
    say(f"rebuilding the library at {incoming}")
    with open(incoming, "wb") as out:
        for part in manifest["parts"]:
            decompressor = zlib.decompressobj(31)
            with open(indir / part["name"], "rb") as fh:
                for block in iter(lambda: fh.read(BLOCK), b""):
                    data = decompressor.decompress(block)
                    out.write(data)
                    digest.update(data)
                    written += len(data)
                tail = decompressor.flush()
                out.write(tail)
                digest.update(tail)
                written += len(tail)
        out.flush()
        os.fsync(out.fileno())
    expected = manifest["database"]
    if written != expected["raw_bytes"] or digest.hexdigest() != expected["raw_sha256"]:
        incoming.unlink()
        raise MoveError("the rebuilt library does not match the snapshot — nothing was replaced")
    if check == "quick":
        say("running SQLite's health check again (reads the whole library)")
        result = health_check(incoming)
        if result != "ok":
            raise MoveError(f"health check failed: {result} — nothing was replaced")
    facts = db_facts(incoming)
    if facts["counts"] != expected["counts"]:
        raise MoveError(f"counts differ: {facts['counts']} vs {expected['counts']}")
    # Swap. A leftover -wal/-shm from the OLD library next to the NEW one would be replayed
    # into it and corrupt it, so they travel with the old copy.
    previous = into.with_name(into.name + ".previous")
    for suffix in ("", "-wal", "-shm"):
        old_prev = Path(str(previous) + suffix)
        if old_prev.exists():
            old_prev.unlink()
    for suffix in ("", "-wal", "-shm"):
        current = Path(str(into) + suffix)
        if current.exists():
            current.rename(Path(str(previous) + suffix))
    incoming.rename(into)
    if owner and os.name != "nt":
        import pwd
        user = pwd.getpwnam(owner)
        os.chown(into, user.pw_uid, user.pw_gid)
    say(f"library in place: {into} — {facts['counts']}")
    return {"ok": True, "into": str(into), "counts": facts["counts"],
            "bytes": written, "health_check": check}


# -- settings (the scraper's memory) -----------------------------------------------------------
# A library without these still works, but the scraper there would start from zero: re-ask
# 4,000 searches it already finished, forget which ones stopped paying off, and download or
# retrain its face/faceless models. Allowlisted by name, never "the whole folder" — that folder
# also holds logs, screenshots, old test databases and (on the laptop) a keys file.
ENGINE_FILES = ("scraper_watchlist.json", "harvest_yield.json", "harvest_cursor.json",
                "niche_cursor.json", "models/face_detection_yunet_2023mar.onnx",
                "models/face_recognition_sface_2021dec.onnx")
# Carried when present. The plan status lets the target recognise the collector's current plan
# as its own (same searches → "unchanged"), instead of re-sending it; the health ledger keeps
# "when did the cloud last deliver" honest across the move.
OPTIONAL_ENGINE_FILES = ("edge_sync/plan_sync_status.json", "edge_sync/edge_health.json")
BESIDE_DB_FILES = ("faceless_text_model.joblib",)        # the engine reads it next to the library
WORK_QUEUE = "radar_work_queue.sqlite3"                  # a database: copied consistently
CONFIG = "scraper_watchlist.json"
SETTINGS_FORMAT = "beacon-settings-v1"


def pack_settings(out: Path, engine_storage: Path, db: Path, work_queue: Path | None = None
                  ) -> dict:
    """The scraper's settings and memory as one small tar.gz with a fingerprint per file."""
    if work_queue is None:
        cfg = json.loads((engine_storage / CONFIG).read_text(encoding="utf-8"))
        work_queue = Path(cfg.get("work_queue_path") or db.with_name(WORK_QUEUE))
    wanted = [(engine_storage / name, f"engine/{name}") for name in ENGINE_FILES]
    wanted += [(db.with_name(name), f"beside-db/{name}") for name in BESIDE_DB_FILES]
    missing = [arc for path, arc in wanted if not path.is_file()]
    if missing:
        raise MoveError("cannot find " + ", ".join(missing))
    out.parent.mkdir(parents=True, exist_ok=True)
    queue_copy = out.with_name(out.name + ".queue.tmp")
    entries = list(wanted) + [(engine_storage / name, f"engine/{name}")
                              for name in OPTIONAL_ENGINE_FILES
                              if (engine_storage / name).is_file()]
    if work_queue.is_file():
        snapshot(work_queue, queue_copy)
        entries.append((queue_copy, f"beside-db/{WORK_QUEUE}"))
    try:
        flagged = [arc for path, arc in entries if SECRET_TEXT.search(path.read_bytes())]
        if flagged:
            raise MoveError("refusing — secret-looking content in " + ", ".join(flagged))
        files = {arc: {"sha256": sha256_file(path), "bytes": path.stat().st_size}
                 for path, arc in entries}
        manifest = {"format": SETTINGS_FORMAT, "source": socket.gethostname(),
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "files": files}
        blob = json.dumps(manifest, indent=2).encode("utf-8")
        with tarfile.open(out, "w:gz") as tar:
            for path, arc in entries:
                tar.add(path, arcname=arc)
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mtime = len(blob), int(time.time())
            tar.addfile(info, io.BytesIO(blob))
    finally:
        if queue_copy.exists():
            queue_copy.unlink()
    return {"files": len(files), "bytes": out.stat().st_size, "path": str(out)}


def retarget(config: dict, db: Path, overrides: dict | None = None) -> dict:
    """The laptop's config names laptop paths (D:\\...). On the target they must name ITS
    library — and an empty work_queue_path means "beside the library", wherever that is."""
    out = dict(config)
    out["db_path"] = str(db)
    out["work_queue_path"] = ""
    for key, value in (overrides or {}).items():
        if key not in config:
            raise MoveError(f"{key!r} is not a scraper setting — check the spelling")
        out[key] = value
    return out


def parse_overrides(pairs: list[str]) -> dict:
    """--set face_check_workers=2 --set harvest_enabled=false (values are JSON when they parse)."""
    found = {}
    for pair in pairs or []:
        key, sep, raw = pair.partition("=")
        if not sep or not key.strip():
            raise MoveError(f"--set needs NAME=VALUE, got {pair!r}")
        try:
            found[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            found[key.strip()] = raw
    return found


def _replace(path: Path, data: bytes, owner: str | None) -> None:
    """Write beside, then rename over — a crash never leaves half a file; the old one is kept."""
    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        if owner and os.name != "nt":
            import pwd
            user = pwd.getpwnam(owner)
            os.chown(path.parent, user.pw_uid, user.pw_gid)
    tmp = path.with_name(path.name + ".incoming")
    tmp.write_bytes(data)
    if path.exists():
        previous = path.with_name(path.name + ".previous")
        if previous.exists():
            previous.unlink()
        path.rename(previous)
    tmp.rename(path)
    if owner and os.name != "nt":
        import pwd
        user = pwd.getpwnam(owner)
        os.chown(path, user.pw_uid, user.pw_gid)


def apply_settings(package: Path, engine_storage: Path, db: Path, owner: str | None = None,
                   overrides: dict | None = None) -> dict:
    """ON THE TARGET: check every fingerprint first, then put each file in place."""
    with tarfile.open(package, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers() if m.isfile()}
        if "manifest.json" not in members:
            raise MoveError("not a settings package (no manifest)")
        manifest = json.loads(tar.extractfile(members["manifest.json"]).read())
        if manifest.get("format") != SETTINGS_FORMAT:
            raise MoveError(f"unknown settings format {manifest.get('format')!r}")
        blobs = {}
        for arc, want in manifest["files"].items():
            if arc not in members or ".." in Path(arc).parts or Path(arc).is_absolute():
                raise MoveError(f"{arc} is missing or unsafe — nothing was changed")
            data = tar.extractfile(members[arc]).read()
            if hashlib.sha256(data).hexdigest() != want["sha256"]:
                raise MoveError(f"{arc} is damaged — nothing was changed")
            blobs[arc] = data
    config = json.loads(blobs[f"engine/{CONFIG}"])
    blobs[f"engine/{CONFIG}"] = json.dumps(retarget(config, db, overrides), indent=2
                                           ).encode("utf-8")
    placed = []
    for arc, data in blobs.items():
        area, _, name = arc.partition("/")
        dest = (engine_storage if area == "engine" else db.parent) / name
        if area == "beside-db" and name == WORK_QUEUE:
            for suffix in ("-wal", "-shm"):   # the old journal travels with the old copy:
                stale = Path(str(dest) + suffix)                # left here it would be
                if stale.exists():                              # replayed into the new one
                    kept = Path(str(dest) + ".previous" + suffix)
                    if kept.exists():
                        kept.unlink()
                    stale.rename(kept)
        _replace(dest, data, owner)
        placed.append(str(dest))
    say(f"settings in place: {len(placed)} files; the scraper now uses {db}")
    return {"ok": True, "placed": placed, "db_path": str(db),
            "overrides": sorted((overrides or {}))}


# -- command line ------------------------------------------------------------------------------
def _engine_storage() -> Path:
    sys.path.insert(0, str(HERE))
    import beacon_host
    return HERE / "storage"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("code")
    c.add_argument("--out", required=True)
    p = sub.add_parser("pack")
    p.add_argument("--out", required=True)
    p.add_argument("--db", default=os.getenv("YOUTUBE_INDEX_DB")
                   or str(HERE / "storage" / "youtube_index.sqlite3"))
    p.add_argument("--part-mb", type=int, default=PART_MB)
    p.add_argument("--level", type=int, default=LEVEL)
    p.add_argument("--check", choices=("quick", "none"), default="quick")
    s = sub.add_parser("send")
    s.add_argument("--out", required=True)
    s.add_argument("--host", required=True)
    s.add_argument("--key", required=True)
    s.add_argument("--remote-dir")
    g = sub.add_parser("settings")
    g.add_argument("--out", required=True)
    g.add_argument("--engine-storage")
    g.add_argument("--db", default=os.getenv("YOUTUBE_INDEX_DB")
                   or str(HERE / "storage" / "youtube_index.sqlite3"))
    g.add_argument("--host")
    g.add_argument("--key")
    g.add_argument("--remote-dir", default="beacon-incoming")
    a = sub.add_parser("apply-settings")
    a.add_argument("--package", required=True)
    a.add_argument("--engine-storage", required=True)
    a.add_argument("--into", required=True, help="the library the scraper must write to")
    a.add_argument("--owner")
    a.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    r = sub.add_parser("receive")
    r.add_argument("--dir", required=True)
    r.add_argument("--into", required=True)
    r.add_argument("--owner")
    r.add_argument("--check", choices=("quick", "none"), default="none")
    args = parser.parse_args(argv)
    try:
        if args.command == "code":
            out = Path(args.out) / "beacon_code.tar.gz"
            report = package_code(out)
            say(f"code package: {report['files']} files, {report['bytes'] / 1e6:.1f} MB, "
                "no secrets")
        elif args.command == "pack":
            with keep_awake():
                report = {"parts": len(pack(Path(args.db), Path(args.out), args.part_mb,
                                            args.level, args.check)["parts"])}
        elif args.command == "send":
            outdir = Path(args.out)
            remote = args.remote_dir or f"beacon-incoming/{outdir.name}"
            with keep_awake():
                report = send(outdir, SSH(args.host, args.key), remote)
            say(f"send finished: {report}")
        elif args.command == "settings":
            storage = Path(args.engine_storage) if args.engine_storage else _engine_storage()
            out = Path(args.out) / "beacon_settings.tar.gz"
            report = pack_settings(out, storage, Path(args.db))
            say(f"settings package: {report['files']} files, {report['bytes'] / 1e6:.1f} MB")
            if args.host:
                target = SSH(args.host, args.key)
                code, text = target.run(f"mkdir -p {args.remote_dir}")
                if code != 0:
                    raise MoveError(f"cannot create {args.remote_dir}: {text.strip()}")
                target.put(out, f"{args.remote_dir}/{out.name}")
                say(f"uploaded to {args.host}:{args.remote_dir}/{out.name}")
        elif args.command == "apply-settings":
            report = apply_settings(Path(args.package), Path(args.engine_storage),
                                    Path(args.into), args.owner, parse_overrides(args.set))
        else:
            report = receive(Path(args.dir), Path(args.into), args.owner, args.check)
        print(json.dumps({"ok": True, **report} if isinstance(report, dict) else report))
        return 0
    except MoveError as exc:
        say(f"STOPPED: {exc}")
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
