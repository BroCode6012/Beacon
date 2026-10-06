"""Secure local + Oracle enrollment for additional YouTube API keys."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from dotenv import dotenv_values, load_dotenv

from beacon_engine.edge.ssh_transport import EdgeSSHTransport

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
_ASSIGNMENT_RE = re.compile(r"^\s*YOUTUBE_API_KEYS\s*=")


def validate_api_key(value: str, *, position: int | None = None) -> str:
    clean = str(value or "").strip()
    if not _KEY_RE.fullmatch(clean):
        label = f" #{position}" if position is not None else ""
        raise ValueError(
            f"YouTube API key{label} has an invalid format; nothing was changed"
        )
    return clean


def merge_api_keys(
    existing: Iterable[str],
    additions: Iterable[str],
) -> list[str]:
    return list(
        dict.fromkeys(
            validate_api_key(value)
            for value in (*tuple(existing), *tuple(additions))
            if str(value or "").strip()
        )
    )


def read_env_api_keys(path: str | os.PathLike) -> list[str]:
    values = dotenv_values(Path(path))
    pooled = str(values.get("YOUTUBE_API_KEYS") or "")
    keys = [value.strip() for value in pooled.split(",") if value.strip()]
    single = str(values.get("YOUTUBE_API_KEY") or "").strip()
    if single:
        keys.append(single)
    return merge_api_keys((), keys)


def _updated_env_text(text: str, keys: list[str]) -> str:
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


def write_env_api_keys(
    path: str | os.PathLike,
    additions: Iterable[str],
) -> dict[str, int]:
    env_path = Path(path)
    if not env_path.is_file():
        raise FileNotFoundError("the local Scripter .env file is missing")
    existing = read_env_api_keys(env_path)
    merged = merge_api_keys(existing, additions)
    original = env_path.read_text(encoding="utf-8")
    updated = _updated_env_text(original, merged)
    changed = updated != original
    if changed:
        fd, temporary = tempfile.mkstemp(
            prefix=".env.",
            suffix=".tmp",
            dir=env_path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, env_path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    return {
        "configured_keys": len(merged),
        "added_keys": len(merged) - len(existing),
        "changed": int(changed),
    }


def prompt_api_keys(count: int) -> list[str]:
    requested = max(1, min(int(count), 100))
    keys: list[str] = []
    while len(keys) < requested:
        position = len(keys) + 1
        value = getpass.getpass(
            f"Paste YouTube API key {position}/{requested} "
            "(input hidden): "
        )
        try:
            clean = validate_api_key(value, position=position)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            continue
        if clean in keys:
            print(
                f"Key #{position} duplicates one entered in this session; "
                "paste a different key.",
                file=sys.stderr,
            )
            continue
        keys.append(clean)
    return keys


def enroll_api_keys(
    additions: Iterable[str],
    *,
    env_path: str | os.PathLike,
    transport: EdgeSSHTransport | None = None,
) -> dict[str, object]:
    clean = merge_api_keys((), additions)
    if not clean:
        raise ValueError("at least one API key is required")
    client = transport or EdgeSSHTransport.from_env()
    if client is None:
        raise RuntimeError("Oracle SSH is not configured in the local .env")
    remote = client.configure_youtube_keys(clean)
    local = write_env_api_keys(env_path, clean)
    return {
        "status": "configured",
        "submitted_keys": len(clean),
        "local": local,
        "oracle": remote,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Securely add YouTube API keys to Signal's laptop and Oracle "
            "workers without echoing or putting keys on the command line."
        )
    )
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--env", dest="env_path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    env_path = Path(args.env_path) if args.env_path else root / ".env"
    load_dotenv(env_path, override=False)
    try:
        additions = prompt_api_keys(args.count)
        result = enroll_api_keys(additions, env_path=env_path)
    except Exception as exc:  # noqa: BLE001
        print(
            f"Key enrollment failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
