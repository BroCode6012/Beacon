"""Secure SSH transport for Signal edge bundles and acknowledgements."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from beacon_engine.edge.plan import load_discovery_plan
from beacon_engine.edge.protocol import (
    DEFAULT_MAX_BUNDLE_BYTES,
    EDGE_PROTOCOL,
    EDGE_PROTOCOL_VERSION,
)
from beacon_engine.proc_util import run as run_hidden

SSH_HOST_ENV = "SIGNAL_EDGE_SSH_HOST"
SSH_USER_ENV = "SIGNAL_EDGE_SSH_USER"
SSH_KEY_ENV = "SIGNAL_EDGE_SSH_KEY"
SSH_PORT_ENV = "SIGNAL_EDGE_SSH_PORT"
SSH_ROOT_ENV = "SIGNAL_EDGE_REMOTE_ROOT"
SSH_BIN_ENV = "SIGNAL_EDGE_SSH_BIN"

_USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")
_REMOTE_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
_BUNDLE_RE = re.compile(
    r"^\d{14}-([0-9a-f]{16})\.signal-edge\.json\.gz$"
)
_ACK_RE = re.compile(r"^([0-9a-f]{64})\.ack\.json$")
_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]+\b")
_KEY_PARAM_RE = re.compile(r"(?i)(\bkey=)[^&\s]+")
_API_KEY_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
SSH_BATCH_SIZE = 20


class EdgeSSHTransportError(RuntimeError):
    """The configured SSH transport is invalid or a remote operation failed."""


def _safe_error(value: object) -> str:
    message = str(value).replace("\r", " ").replace("\n", " ")
    message = _API_KEY_RE.sub("[redacted-key]", message)
    message = _KEY_PARAM_RE.sub(r"\1[redacted]", message)
    return re.sub(r"\s+", " ", message).strip()[:400]


def bundle_id_prefix(filename: str) -> str:
    match = _BUNDLE_RE.fullmatch(str(filename or ""))
    if not match:
        raise EdgeSSHTransportError("unsafe remote bundle filename")
    return match.group(1)


def _bundle_names(filenames: Iterable[str]) -> list[str]:
    names = list(dict.fromkeys(str(name or "") for name in filenames))
    for name in names:
        bundle_id_prefix(name)
    return names


def _canonical_json(data: object) -> bytes:
    return json.dumps(
        data,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def verified_ack_payload(
    path: str | os.PathLike,
    *,
    signing_key: str | bytes,
) -> dict[str, Any]:
    """Read once and validate a signed post-commit acknowledgement."""
    ack_path = Path(path)
    match = _ACK_RE.fullmatch(ack_path.name)
    if not match:
        raise EdgeSSHTransportError("unsafe acknowledgement filename")
    try:
        with ack_path.open("rb") as handle:
            raw = handle.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise EdgeSSHTransportError("acknowledgement exceeds size limit")
        envelope = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise EdgeSSHTransportError(
            f"invalid acknowledgement: {_safe_error(exc)}"
        ) from exc
    if not isinstance(envelope, dict):
        raise EdgeSSHTransportError("acknowledgement must be an object")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise EdgeSSHTransportError("acknowledgement payload is missing")
    bundle_id = str(payload.get("bundle_id") or "")
    if bundle_id != match.group(1):
        raise EdgeSSHTransportError("acknowledgement filename does not match")
    if payload.get("protocol") != EDGE_PROTOCOL:
        raise EdgeSSHTransportError("acknowledgement protocol is unsupported")
    if int(payload.get("version") or 0) != EDGE_PROTOCOL_VERSION:
        raise EdgeSSHTransportError("acknowledgement version is unsupported")
    payload_sha256 = hashlib.sha256(_canonical_json(payload)).hexdigest()
    if not hmac.compare_digest(
        payload_sha256,
        str(envelope.get("payload_sha256") or ""),
    ):
        raise EdgeSSHTransportError("acknowledgement checksum mismatch")
    secret = (
        signing_key
        if isinstance(signing_key, bytes)
        else str(signing_key).encode("utf-8")
    )
    if not secret:
        raise EdgeSSHTransportError("an acknowledgement signing key is required")
    message = (
        f"{EDGE_PROTOCOL}:{EDGE_PROTOCOL_VERSION}:"
        f"{bundle_id}:{payload_sha256}"
    ).encode("ascii")
    expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(
        expected,
        str(envelope.get("signature") or ""),
    ):
        raise EdgeSSHTransportError("acknowledgement signature mismatch")
    return payload


def verified_ack_bundle_id(
    path: str | os.PathLike,
    *,
    signing_key: str | bytes,
) -> str:
    """Validate one locally generated acknowledgement before remote cleanup."""
    return str(verified_ack_payload(path, signing_key=signing_key)["bundle_id"])


@dataclass(frozen=True)
class EdgeSSHConfig:
    host: str
    user: str
    key_path: Path
    port: int = 22
    remote_root: str = "/var/lib/signal-edge"
    ssh_bin: str = "ssh"

    @classmethod
    def from_env(cls) -> "EdgeSSHConfig | None":
        host = str(os.getenv(SSH_HOST_ENV) or "").strip()
        if not host:
            return None
        user = str(os.getenv(SSH_USER_ENV) or "ubuntu").strip()
        key_value = str(os.getenv(SSH_KEY_ENV) or "").strip()
        if not _HOST_RE.fullmatch(host):
            raise EdgeSSHTransportError("the configured SSH host is invalid")
        if not _USER_RE.fullmatch(user):
            raise EdgeSSHTransportError("the configured SSH user is invalid")
        if not key_value:
            raise EdgeSSHTransportError(f"{SSH_KEY_ENV} is required")
        key_path = Path(os.path.expandvars(key_value)).expanduser()
        if not key_path.is_absolute() or not key_path.is_file():
            raise EdgeSSHTransportError(
                "the configured SSH private-key path is missing"
            )
        try:
            port = int(os.getenv(SSH_PORT_ENV) or 22)
        except ValueError as exc:
            raise EdgeSSHTransportError("the configured SSH port is invalid") from exc
        if port < 1 or port > 65_535:
            raise EdgeSSHTransportError("the configured SSH port is invalid")
        remote_root = str(
            os.getenv(SSH_ROOT_ENV) or "/var/lib/signal-edge"
        ).rstrip("/")
        remote_path = PurePosixPath(remote_root)
        if (
            not _REMOTE_RE.fullmatch(remote_root)
            or not remote_path.is_absolute()
            or ".." in remote_path.parts
        ):
            raise EdgeSSHTransportError(
                "the configured remote Signal edge root is invalid"
            )
        ssh_bin = str(os.getenv(SSH_BIN_ENV) or "").strip()
        if not ssh_bin:
            ssh_bin = shutil.which("ssh") or "ssh"
        return cls(
            host=host,
            user=user,
            key_path=key_path,
            port=port,
            remote_root=remote_root,
            ssh_bin=ssh_bin,
        )


class EdgeSSHTransport:
    """Stream immutable files over OpenSSH without exposing an application port."""

    def __init__(self, config: EdgeSSHConfig) -> None:
        self.config = config

    @classmethod
    def from_env(cls) -> "EdgeSSHTransport | None":
        config = EdgeSSHConfig.from_env()
        return cls(config) if config is not None else None

    def _argv(self, remote_command: str) -> list[str]:
        return [
            self.config.ssh_bin,
            "-i",
            str(self.config.key_path),
            "-p",
            str(self.config.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "LogLevel=ERROR",
            f"{self.config.user}@{self.config.host}",
            remote_command,
        ]

    def _run(
        self,
        remote_command: str,
        *,
        input_bytes: bytes | None = None,
        timeout: int = 180,
    ) -> bytes:
        try:
            completed = run_hidden(
                self._argv(remote_command),
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EdgeSSHTransportError(
                f"SSH transport failed: {_safe_error(exc)}"
            ) from exc
        if completed.returncode != 0:
            detail = _safe_error(
                completed.stderr.decode("utf-8", errors="replace")
            )
            raise EdgeSSHTransportError(
                f"SSH transport exited with {completed.returncode}: {detail}"
            )
        return completed.stdout

    def configure_youtube_keys(
        self,
        keys: Iterable[str],
    ) -> dict[str, Any]:
        """Merge keys into Oracle's private env through stdin, never argv."""
        clean = list(
            dict.fromkeys(str(value or "").strip() for value in keys)
        )
        if not clean or any(
            not _API_KEY_VALUE_RE.fullmatch(value)
            for value in clean
        ):
            raise EdgeSSHTransportError("invalid YouTube API key input")
        payload = _canonical_json({"keys": clean})
        output = self._run(
            "sudo -n /opt/signal-edge/current/.venv/bin/python "
            "/opt/signal-edge/current/deploy/oracle-edge/"
            "configure-youtube-keys.py "
            "--env /etc/signal-edge/edge.env --restart",
            input_bytes=payload,
            timeout=180,
        )
        try:
            result = json.loads(output.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EdgeSSHTransportError(
                "Oracle returned an invalid key-configuration result"
            ) from exc
        if not isinstance(result, dict) or result.get("status") != "configured":
            raise EdgeSSHTransportError(
                "Oracle did not confirm key configuration"
            )
        return result

    def list_bundle_names(self) -> list[str]:
        root = self.config.remote_root
        output = self._run(
            "sudo -n find "
            f"{root}/outbox -maxdepth 1 -type f "
            "-name '*.signal-edge.json.gz' -printf '%f\\n'"
        )
        names = sorted(
            {
                line.strip()
                for line in output.decode("utf-8", errors="strict").splitlines()
                if line.strip()
            }
        )
        for name in names:
            bundle_id_prefix(name)
        return names

    def read_worker_status(self) -> dict[str, Any]:
        output = self._run(
            f"sudo -n cat {self.config.remote_root}/worker_status.json",
            timeout=30,
        )
        try:
            payload = json.loads(output.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EdgeSSHTransportError(
                f"remote worker status is invalid: {_safe_error(exc)}"
            ) from exc
        if not isinstance(payload, dict):
            raise EdgeSSHTransportError("remote worker status must be an object")
        return payload

    def read_organizer_status(self) -> dict[str, Any]:
        status_path = (
            f"{self.config.remote_root}/organizer/organizer_status.json"
        )
        output = self._run(
            "sudo -n sh -c '"
            f"if [ -f {status_path} ]; then cat {status_path}; "
            "else printf \"{}\"; fi'",
            timeout=30,
        )
        try:
            payload = json.loads(output.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EdgeSSHTransportError(
                f"remote organizer status is invalid: {_safe_error(exc)}"
            ) from exc
        if not isinstance(payload, dict):
            raise EdgeSSHTransportError(
                "remote organizer status must be an object"
            )
        return payload

    def publish_discovery_plan(
        self,
        plan_path: str | os.PathLike,
        *,
        signing_key: str | bytes,
    ) -> str:
        """Verify and atomically publish one immutable discovery-plan revision."""
        source = Path(plan_path)
        prefix = bundle_id_prefix(source.name)
        plan = load_discovery_plan(source, signing_key=signing_key)
        if not plan.revision.startswith(prefix):
            raise EdgeSSHTransportError(
                "discovery plan filename does not match its immutable identity"
            )
        payload = source.read_bytes()
        if len(payload) > DEFAULT_MAX_BUNDLE_BYTES:
            raise EdgeSSHTransportError(
                "discovery plan exceeds the transport size limit"
            )
        root = self.config.remote_root
        remote_plan = f"{root}/jobs/{source.name}"
        temporary = remote_plan + ".partial"
        command = (
            f"sudo -n install -d -m 0750 -o signal-edge -g signal-edge "
            f"{root}/jobs && "
            f"sudo -n tee {temporary} >/dev/null && "
            f"sudo -n chown signal-edge:signal-edge {temporary} && "
            f"sudo -n chmod 0600 {temporary} && "
            f"sudo -n mv -f {temporary} {remote_plan}"
        )
        self._run(command, input_bytes=payload, timeout=300)
        return plan.revision

    def publish_organization_job(
        self,
        job_path: str | os.PathLike,
        *,
        signing_key: str | bytes,
    ) -> str:
        """Verify and atomically publish one immutable organization job."""
        from beacon_engine.edge.organization import load_organization_job

        source = Path(job_path)
        prefix = bundle_id_prefix(source.name)
        job = load_organization_job(source, signing_key=signing_key)
        if not job.revision.startswith(prefix):
            raise EdgeSSHTransportError(
                "organization job filename does not match its immutable identity"
            )
        payload = source.read_bytes()
        if len(payload) > DEFAULT_MAX_BUNDLE_BYTES:
            raise EdgeSSHTransportError(
                "organization job exceeds the transport size limit"
            )
        root = self.config.remote_root
        remote_job = f"{root}/organizer/jobs/{source.name}"
        temporary = remote_job + ".partial"
        command = (
            f"sudo -n install -d -m 0750 -o signal-edge -g signal-edge "
            f"{root}/organizer/jobs && "
            f"sudo -n tee {temporary} >/dev/null && "
            f"sudo -n chown signal-edge:signal-edge {temporary} && "
            f"sudo -n chmod 0600 {temporary} && "
            f"sudo -n mv -f {temporary} {remote_job}"
        )
        self._run(command, input_bytes=payload, timeout=300)
        return job.revision

    def download_bundle(self, filename: str) -> bytes:
        bundle_id_prefix(filename)
        return self._run(
            f"sudo -n cat {self.config.remote_root}/outbox/{filename}",
            timeout=300,
        )

    def download_bundles(self, filenames: Iterable[str]) -> dict[str, bytes]:
        """Download a bounded group of immutable bundles over one SSH session."""
        names = _bundle_names(filenames)
        if not names:
            return {}
        if len(names) > SSH_BATCH_SIZE:
            raise EdgeSSHTransportError(
                f"SSH bundle batches are limited to {SSH_BATCH_SIZE} files"
            )
        root = self.config.remote_root
        archive_bytes = self._run(
            f"sudo -n tar -C {root}/outbox -cf - -- {' '.join(names)}",
            timeout=600,
        )
        expected = set(names)
        found: dict[str, bytes] = {}
        try:
            with tarfile.open(
                fileobj=io.BytesIO(archive_bytes),
                mode="r:",
            ) as archive:
                for member in archive.getmembers():
                    if (
                        member.name not in expected
                        or member.name in found
                        or not member.isfile()
                    ):
                        raise EdgeSSHTransportError(
                            "remote bundle archive contains an unexpected member"
                        )
                    if member.size > DEFAULT_MAX_BUNDLE_BYTES:
                        raise EdgeSSHTransportError(
                            "remote bundle exceeds the transport size limit"
                        )
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise EdgeSSHTransportError(
                            "remote bundle archive member cannot be read"
                        )
                    payload = extracted.read(DEFAULT_MAX_BUNDLE_BYTES + 1)
                    if len(payload) > DEFAULT_MAX_BUNDLE_BYTES:
                        raise EdgeSSHTransportError(
                            "remote bundle exceeds the transport size limit"
                        )
                    found[member.name] = payload
        except (OSError, tarfile.TarError) as exc:
            raise EdgeSSHTransportError(
                f"remote bundle archive is invalid: {_safe_error(exc)}"
            ) from exc
        missing = expected - set(found)
        if missing:
            raise EdgeSSHTransportError(
                "remote bundle archive is incomplete"
            )
        return found

    def acknowledge(
        self,
        *,
        bundle_filename: str,
        ack_path: str | os.PathLike,
        signing_key: str | bytes,
    ) -> str:
        prefix = bundle_id_prefix(bundle_filename)
        ack_file = Path(ack_path)
        bundle_id = verified_ack_bundle_id(
            ack_file,
            signing_key=signing_key,
        )
        if not bundle_id.startswith(prefix):
            raise EdgeSSHTransportError(
                "acknowledgement does not match the remote bundle"
            )
        root = self.config.remote_root
        remote_ack = f"{root}/acks/{ack_file.name}"
        temporary = remote_ack + ".partial"
        command = (
            f"sudo -n install -d -m 0750 -o signal-edge -g signal-edge "
            f"{root}/acks && "
            f"sudo -n tee {temporary} >/dev/null && "
            f"sudo -n chown signal-edge:signal-edge {temporary} && "
            f"sudo -n chmod 0600 {temporary} && "
            f"sudo -n mv -f {temporary} {remote_ack} && "
            f"sudo -n rm -f -- {root}/outbox/{bundle_filename}"
        )
        self._run(command, input_bytes=ack_file.read_bytes())
        return bundle_id

    def acknowledge_many(
        self,
        items: Iterable[tuple[str, str | os.PathLike]],
        *,
        signing_key: str | bytes,
    ) -> list[str]:
        """Publish signed acknowledgements and clean many bundles in one session."""
        pairs = list(items)
        if not pairs:
            return []
        if len(pairs) > SSH_BATCH_SIZE:
            raise EdgeSSHTransportError(
                f"SSH acknowledgement batches are limited to {SSH_BATCH_SIZE} files"
            )
        bundle_ids: list[str] = []
        bundle_names: list[str] = []
        ack_files: list[Path] = []
        seen_acks: set[str] = set()
        for bundle_filename, ack_path in pairs:
            prefix = bundle_id_prefix(bundle_filename)
            ack_file = Path(ack_path)
            bundle_id = verified_ack_bundle_id(
                ack_file,
                signing_key=signing_key,
            )
            if not bundle_id.startswith(prefix):
                raise EdgeSSHTransportError(
                    "acknowledgement does not match the remote bundle"
                )
            if ack_file.name in seen_acks:
                raise EdgeSSHTransportError(
                    "duplicate acknowledgement in SSH batch"
                )
            seen_acks.add(ack_file.name)
            bundle_ids.append(bundle_id)
            bundle_names.append(bundle_filename)
            ack_files.append(ack_file)

        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for ack_file in ack_files:
                payload = ack_file.read_bytes()
                member = tarfile.TarInfo(name=ack_file.name)
                member.size = len(payload)
                member.mode = 0o600
                member.mtime = 0
                tar.addfile(member, io.BytesIO(payload))

        root = self.config.remote_root
        partial_dir = f"{root}/acks/.batch-{secrets.token_hex(8)}"
        partial_acks = [
            f"{partial_dir}/{ack_file.name}" for ack_file in ack_files
        ]
        moves = " && ".join(
            f"sudo -n mv -f -- {partial} {root}/acks/{ack_file.name}"
            for partial, ack_file in zip(partial_acks, ack_files)
        )
        command = (
            f"sudo -n install -d -m 0750 -o signal-edge -g signal-edge "
            f"{root}/acks {partial_dir} && "
            f"sudo -n tar -xf - -C {partial_dir} && "
            f"sudo -n chown signal-edge:signal-edge "
            f"{' '.join(partial_acks)} && "
            f"sudo -n chmod 0600 {' '.join(partial_acks)} && "
            f"{moves} && "
            f"sudo -n rm -f -- "
            + " ".join(
                f"{root}/outbox/{bundle_name}"
                for bundle_name in bundle_names
            )
            + f" && sudo -n rmdir {partial_dir}"
        )
        self._run(command, input_bytes=archive.getvalue(), timeout=600)
        return bundle_ids
