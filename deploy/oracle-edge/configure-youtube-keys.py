#!/usr/bin/env python3
"""Atomically merge private YouTube keys into the Oracle service environment."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
_ASSIGNMENT_RE = re.compile(r"^\s*YOUTUBE_API_KEYS\s*=")


def _keys_from_text(text: str) -> list[str]:
    for line in text.splitlines():
        if not _ASSIGNMENT_RE.match(line):
            continue
        value = line.split("=", 1)[1].strip().strip("'\"")
        return [
            item.strip()
            for item in value.split(",")
            if item.strip()
        ]
    return []


def _validate(values: object) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("the request must contain a key list")
    clean: list[str] = []
    for index, value in enumerate(values, 1):
        key = str(value or "").strip()
        if not _KEY_RE.fullmatch(key):
            raise ValueError(f"key #{index} has an invalid format")
        if key not in clean:
            clean.append(key)
    if not clean:
        raise ValueError("at least one key is required")
    return clean


def _updated_text(text: str, keys: list[str]) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    assignment = "YOUTUBE_API_KEYS=" + ",".join(keys)
    output: list[str] = []
    replaced = False
    for line in text.splitlines():
        if _ASSIGNMENT_RE.match(line):
            if not replaced:
                output.append(assignment)
                replaced = True
            continue
        output.append(line)
    if not replaced:
        if output and output[-1]:
            output.append("")
        output.append(assignment)
    return newline.join(output) + newline


def _atomic_write(path: Path, text: str) -> None:
    existing_stat = path.stat()
    fd, temporary = tempfile.mkstemp(
        prefix=".edge.env.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(existing_stat.st_mode))
        if hasattr(os, "chown"):
            os.chown(temporary, existing_stat.st_uid, existing_stat.st_gid)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def configure(
    env_path: Path,
    additions: list[str],
    *,
    restart: bool,
) -> dict[str, object]:
    if not env_path.is_file():
        raise FileNotFoundError("Oracle edge environment file is missing")
    original = env_path.read_text(encoding="utf-8")
    existing_values = _keys_from_text(original)
    existing = _validate(existing_values) if existing_values else []
    merged = list(dict.fromkeys((*existing, *additions)))
    updated = _updated_text(original, merged)
    changed = updated != original
    if changed:
        _atomic_write(env_path, updated)
    services_active = False
    restarted = False
    if restart:
        if changed:
            subprocess.run(
                [
                    "systemctl",
                    "restart",
                    "signal-edge.service",
                    "signal-edge-organizer.service",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            restarted = True
        active = subprocess.run(
            [
                "systemctl",
                "is-active",
                "--quiet",
                "signal-edge.service",
                "signal-edge-organizer.service",
            ],
            check=False,
        )
        services_active = active.returncode == 0
        if not services_active:
            raise RuntimeError("Oracle edge services did not become active")
    return {
        "status": "configured",
        "configured_keys": len(merged),
        "added_keys": len(merged) - len(existing),
        "changed": changed,
        "restarted": restarted,
        "services_active": services_active if restart else None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True)
    parser.add_argument("--restart", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request = json.load(sys.stdin)
        additions = _validate(request.get("keys") if isinstance(request, dict) else None)
        result = configure(
            Path(args.env),
            additions,
            restart=bool(args.restart),
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"configuration failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
