"""Versioned, signed, immutable result bundles for remote Signal workers."""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

EDGE_PROTOCOL = "signal-edge"
EDGE_PROTOCOL_VERSION = 1
DEFAULT_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EdgeProtocolError(ValueError):
    """The bundle is malformed, unsupported, too large, or untrusted."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(data: object) -> bytes:
    try:
        return json.dumps(
            data,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EdgeProtocolError(f"bundle data is not canonical JSON: {exc}") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key_bytes(key: str | bytes | None) -> bytes:
    if key is None:
        return b""
    return key if isinstance(key, bytes) else str(key).encode("utf-8")


def _signature_message(payload_sha256: str, bundle_id: str) -> bytes:
    return (
        f"{EDGE_PROTOCOL}:{EDGE_PROTOCOL_VERSION}:{bundle_id}:{payload_sha256}"
    ).encode("ascii")


def _sign(payload_sha256: str, bundle_id: str, key: str | bytes | None) -> str:
    secret = _key_bytes(key)
    if not secret:
        return ""
    return hmac.new(
        secret,
        _signature_message(payload_sha256, bundle_id),
        hashlib.sha256,
    ).hexdigest()


def _clean_name(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not _NAME_RE.fullmatch(text):
        raise EdgeProtocolError(
            f"{field} must start with a letter/number and contain only "
            "letters, numbers, dot, underscore, or hyphen"
        )
    return text


def _normalize_records(
    records: Mapping[str, list[Mapping[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(records, Mapping):
        raise EdgeProtocolError("records must be an object of dataset arrays")
    normalized: dict[str, list[dict[str, Any]]] = {}
    for dataset, rows in records.items():
        name = _clean_name(str(dataset), "dataset name")
        if not isinstance(rows, list):
            raise EdgeProtocolError(f"dataset '{name}' must be an array")
        clean_rows: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise EdgeProtocolError(
                    f"dataset '{name}' row {index} must be an object"
                )
            clean_rows.append(dict(row))
        normalized[name] = clean_rows
    # Validate every nested value and detach caller-owned mutable objects.
    return json.loads(_canonical_json(normalized).decode("utf-8"))


@dataclass(frozen=True)
class EdgeBundle:
    manifest: dict[str, Any]
    payload: dict[str, Any]

    @property
    def bundle_id(self) -> str:
        return str(self.manifest["bundle_id"])

    @property
    def node_id(self) -> str:
        return str(self.payload["node_id"])

    @property
    def kind(self) -> str:
        return str(self.payload["kind"])

    @property
    def records(self) -> dict[str, list[dict[str, Any]]]:
        return self.payload["records"]

    def as_dict(self) -> dict[str, Any]:
        return {"manifest": dict(self.manifest), "payload": dict(self.payload)}


def build_bundle(
    *,
    node_id: str,
    kind: str,
    records: Mapping[str, list[Mapping[str, Any]]],
    job_id: str = "",
    metadata: Mapping[str, Any] | None = None,
    created_at: str | None = None,
    signing_key: str | bytes | None = None,
) -> EdgeBundle:
    """Build an immutable result bundle from one completed edge-node job."""
    clean_node = _clean_name(node_id, "node_id")
    clean_kind = _clean_name(kind, "kind")
    clean_job = _clean_name(job_id, "job_id") if job_id else ""
    clean_records = _normalize_records(records)
    clean_metadata = dict(metadata or {})
    # Validate/detach metadata just like records.
    clean_metadata = json.loads(_canonical_json(clean_metadata).decode("utf-8"))
    payload = {
        "node_id": clean_node,
        "kind": clean_kind,
        "job_id": clean_job,
        "created_at": str(created_at or utc_now()),
        "metadata": clean_metadata,
        "records": clean_records,
    }
    payload_bytes = _canonical_json(payload)
    payload_sha256 = _sha256(payload_bytes)
    bundle_id = payload_sha256
    counts = {name: len(rows) for name, rows in clean_records.items()}
    manifest = {
        "protocol": EDGE_PROTOCOL,
        "version": EDGE_PROTOCOL_VERSION,
        "bundle_id": bundle_id,
        "payload_sha256": payload_sha256,
        "signature": _sign(payload_sha256, bundle_id, signing_key),
        "counts": counts,
    }
    return EdgeBundle(manifest=manifest, payload=payload)


def _validate_bundle(
    envelope: object,
    *,
    signing_key: str | bytes | None,
    require_signature: bool,
) -> EdgeBundle:
    if not isinstance(envelope, dict):
        raise EdgeProtocolError("bundle envelope must be an object")
    manifest = envelope.get("manifest")
    payload = envelope.get("payload")
    if not isinstance(manifest, dict) or not isinstance(payload, dict):
        raise EdgeProtocolError("bundle requires manifest and payload objects")
    if manifest.get("protocol") != EDGE_PROTOCOL:
        raise EdgeProtocolError("unsupported edge protocol")
    try:
        version = int(manifest.get("version"))
    except (TypeError, ValueError) as exc:
        raise EdgeProtocolError("invalid protocol version") from exc
    if version != EDGE_PROTOCOL_VERSION:
        raise EdgeProtocolError(
            f"unsupported edge protocol version {version}; "
            f"expected {EDGE_PROTOCOL_VERSION}"
        )
    payload_sha256 = _sha256(_canonical_json(payload))
    if not hmac.compare_digest(
        payload_sha256,
        str(manifest.get("payload_sha256") or ""),
    ):
        raise EdgeProtocolError("payload checksum mismatch")
    bundle_id = str(manifest.get("bundle_id") or "")
    if not hmac.compare_digest(bundle_id, payload_sha256):
        raise EdgeProtocolError("bundle_id does not match the immutable payload")
    signature = str(manifest.get("signature") or "")
    secret = _key_bytes(signing_key)
    if require_signature and not secret:
        raise EdgeProtocolError("a signing key is required for this import")
    if require_signature and not signature:
        raise EdgeProtocolError("bundle is unsigned")
    if signature:
        if not secret:
            raise EdgeProtocolError("cannot verify signed bundle without a key")
        expected = _sign(payload_sha256, bundle_id, secret)
        if not hmac.compare_digest(signature, expected):
            raise EdgeProtocolError("bundle signature mismatch")

    node_id = _clean_name(str(payload.get("node_id") or ""), "node_id")
    kind = _clean_name(str(payload.get("kind") or ""), "kind")
    job_id = str(payload.get("job_id") or "")
    if job_id:
        _clean_name(job_id, "job_id")
    records = payload.get("records")
    if not isinstance(records, dict):
        raise EdgeProtocolError("payload records must be an object")
    expected_counts = manifest.get("counts")
    if not isinstance(expected_counts, dict):
        raise EdgeProtocolError("manifest counts must be an object")
    actual_counts: dict[str, int] = {}
    for dataset, rows in records.items():
        _clean_name(str(dataset), "dataset name")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) for row in rows
        ):
            raise EdgeProtocolError(f"dataset '{dataset}' is malformed")
        actual_counts[str(dataset)] = len(rows)
    if actual_counts != {
        str(name): int(count) for name, count in expected_counts.items()
    }:
        raise EdgeProtocolError("manifest record counts do not match payload")

    clean_payload = dict(payload)
    clean_payload["node_id"] = node_id
    clean_payload["kind"] = kind
    return EdgeBundle(manifest=dict(manifest), payload=clean_payload)


def write_bundle(bundle: EdgeBundle, outbox: str | os.PathLike) -> Path:
    """Atomically publish one compressed immutable bundle into an outbox."""
    outbox_path = Path(outbox)
    outbox_path.mkdir(parents=True, exist_ok=True)
    created = re.sub(r"[^0-9]", "", str(bundle.payload["created_at"]))[:14]
    filename = f"{created or 'edge'}-{bundle.bundle_id[:16]}.signal-edge.json.gz"
    final_path = outbox_path / filename
    encoded = _canonical_json(bundle.as_dict())
    compressed = gzip.compress(encoded, compresslevel=6, mtime=0)
    tmp_path = final_path.with_name(final_path.name + ".partial")
    tmp_path.write_bytes(compressed)
    os.replace(tmp_path, final_path)
    return final_path


def load_bundle(
    path: str | os.PathLike,
    *,
    signing_key: str | bytes | None = None,
    require_signature: bool = True,
    max_uncompressed_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> EdgeBundle:
    """Load and verify one compressed bundle with a decompression size cap."""
    bundle_path = Path(path)
    if bundle_path.stat().st_size > max_uncompressed_bytes:
        raise EdgeProtocolError("compressed bundle exceeds the size limit")
    try:
        with gzip.open(bundle_path, "rb") as fh:
            encoded = fh.read(max_uncompressed_bytes + 1)
    except (OSError, EOFError) as exc:
        raise EdgeProtocolError(f"invalid gzip bundle: {exc}") from exc
    if len(encoded) > max_uncompressed_bytes:
        raise EdgeProtocolError("uncompressed bundle exceeds the size limit")
    try:
        envelope = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError,
            MemoryError, ValueError) as exc:
        # json.loads runs BEFORE the HMAC check, so an untrusted node could feed
        # deeply-nested JSON (RecursionError) or a max-size document (MemoryError);
        # normalize EVERY parse failure to the protocol's typed, caught error.
        raise EdgeProtocolError(f"bundle is not valid JSON: {exc}") from exc
    return _validate_bundle(
        envelope,
        signing_key=signing_key,
        require_signature=require_signature,
    )


def write_ack(
    bundle: EdgeBundle,
    ack_dir: str | os.PathLike,
    *,
    counts: Mapping[str, int],
    signing_key: str | bytes | None = None,
    status: str = "imported",
) -> Path:
    """Atomically write a signed acknowledgement for transport back to the node."""
    clean_status = _clean_name(status, "ack status")
    payload = {
        "protocol": EDGE_PROTOCOL,
        "version": EDGE_PROTOCOL_VERSION,
        "bundle_id": bundle.bundle_id,
        "node_id": bundle.node_id,
        "status": clean_status,
        "acknowledged_at": utc_now(),
        "counts": {str(name): int(value) for name, value in counts.items()},
    }
    payload_sha256 = _sha256(_canonical_json(payload))
    ack = {
        "payload": payload,
        "payload_sha256": payload_sha256,
        "signature": _sign(payload_sha256, bundle.bundle_id, signing_key),
    }
    directory = Path(ack_dir)
    directory.mkdir(parents=True, exist_ok=True)
    final_path = directory / f"{bundle.bundle_id}.ack.json"
    tmp_path = final_path.with_name(final_path.name + ".partial")
    tmp_path.write_bytes(_canonical_json(ack))
    os.replace(tmp_path, final_path)
    return final_path
