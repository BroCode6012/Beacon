"""Keep Oracle SSH ingress locked to the laptop's current public IPv4 /32.

This file is intentionally runnable directly by a small dedicated virtual
environment. It never imports the rest of the Scripter package and it preserves
every non-managed security-list rule exactly as Oracle returned it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

CONFIG_ENV = "SIGNAL_EDGE_OCI_CONFIG"
PROFILE_ENV = "SIGNAL_EDGE_OCI_PROFILE"
SECURITY_LIST_ENV = "SIGNAL_EDGE_OCI_SECURITY_LIST_ID"
DESCRIPTION_ENV = "SIGNAL_EDGE_OCI_MANAGED_DESCRIPTION"
VERIFY_SECONDS_ENV = "SIGNAL_EDGE_OCI_VERIFY_SECONDS"
SSH_PORT_ENV = "SIGNAL_EDGE_OCI_SSH_PORT"
FALLBACK_SSH_PORT_ENV = "SIGNAL_EDGE_SSH_PORT"
DEFAULT_PROFILE = "SIGNAL_EDGE_IP"
DEFAULT_DESCRIPTION = "Signal laptop dynamic SSH /32"
DEFAULT_VERIFY_SECONDS = 600
PUBLIC_IP_ENDPOINTS = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(number, maximum))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _recent(timestamp: object, *, seconds: int, now: dt.datetime) -> bool:
    try:
        parsed = dt.datetime.fromisoformat(
            str(timestamp or "").replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    age = now - parsed.astimezone(dt.timezone.utc)
    return dt.timedelta(0) <= age <= dt.timedelta(seconds=max(0, seconds))


@dataclass(frozen=True)
class AllowlistConfig:
    security_list_id: str
    config_path: Path
    profile: str = DEFAULT_PROFILE
    ssh_port: int = 22
    description: str = DEFAULT_DESCRIPTION
    verify_seconds: int = DEFAULT_VERIFY_SECONDS

    @classmethod
    def from_env(cls) -> "AllowlistConfig | None":
        security_list_id = str(os.getenv(SECURITY_LIST_ENV) or "").strip()
        if not security_list_id:
            return None
        if not security_list_id.startswith("ocid1.securitylist."):
            raise ValueError(f"{SECURITY_LIST_ENV} is invalid")
        config_value = str(
            os.getenv(CONFIG_ENV) or (Path.home() / ".oci" / "config")
        )
        config_path = Path(os.path.expandvars(config_value)).expanduser()
        if not config_path.is_absolute() or not config_path.is_file():
            raise ValueError(f"{CONFIG_ENV} does not point to an OCI config file")
        profile = str(os.getenv(PROFILE_ENV) or DEFAULT_PROFILE).strip()
        if not profile:
            raise ValueError(f"{PROFILE_ENV} may not be empty")
        ssh_port = _safe_int(
            os.getenv(SSH_PORT_ENV) or os.getenv(FALLBACK_SSH_PORT_ENV) or 22,
            22,
            1,
            65_535,
        )
        description = str(
            os.getenv(DESCRIPTION_ENV) or DEFAULT_DESCRIPTION
        ).strip()
        if not description or len(description) > 255:
            raise ValueError(f"{DESCRIPTION_ENV} must contain 1-255 characters")
        verify_seconds = _safe_int(
            os.getenv(VERIFY_SECONDS_ENV) or DEFAULT_VERIFY_SECONDS,
            DEFAULT_VERIFY_SECONDS,
            0,
            86_400,
        )
        return cls(
            security_list_id=security_list_id,
            config_path=config_path,
            profile=profile,
            ssh_port=ssh_port,
            description=description,
            verify_seconds=verify_seconds,
        )


def normalize_public_ipv4(value: object) -> str:
    text = str(value or "").strip()
    address = ipaddress.ip_address(text)
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_global:
        raise ValueError("the detected address is not a public IPv4 address")
    return str(address)


def discover_public_ipv4(
    fetch: Callable[..., Any] = urllib.request.urlopen,
) -> str:
    errors: list[str] = []
    for url in PUBLIC_IP_ENDPOINTS:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Signal-Edge-IP-Updater/1"},
        )
        try:
            with fetch(request, timeout=10) as response:
                return normalize_public_ipv4(
                    response.read(128).decode("ascii", errors="strict")
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {type(exc).__name__}")
    raise RuntimeError(
        "could not determine the laptop public IPv4 address ("
        + "; ".join(errors)
        + ")"
    )


def _exact_tcp_port_rule(rule: object, port: int) -> bool:
    if str(getattr(rule, "protocol", "") or "") != "6":
        return False
    tcp_options = getattr(rule, "tcp_options", None)
    destination = getattr(tcp_options, "destination_port_range", None)
    if destination is None:
        return False
    return (
        int(getattr(destination, "min", -1)) == port
        and int(getattr(destination, "max", -1)) == port
    )


def select_managed_rule(
    rules: list[object],
    *,
    port: int,
    description: str,
) -> tuple[int, bool]:
    managed = [
        index
        for index, rule in enumerate(rules)
        if str(getattr(rule, "description", "") or "") == description
    ]
    if len(managed) > 1:
        raise RuntimeError("multiple OCI ingress rules use the managed description")
    if managed:
        index = managed[0]
        if not _exact_tcp_port_rule(rules[index], port):
            raise RuntimeError("the managed OCI ingress rule is not the SSH port rule")
        return index, False

    candidates = [
        index
        for index, rule in enumerate(rules)
        if _exact_tcp_port_rule(rule, port)
        and str(getattr(rule, "source_type", "") or "CIDR_BLOCK")
        == "CIDR_BLOCK"
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            "expected exactly one TCP SSH ingress rule to adopt; "
            f"found {len(candidates)}"
        )
    return candidates[0], True


def reconcile_security_list(
    client: object,
    models: object,
    config: AllowlistConfig,
    public_ip: str,
) -> dict[str, Any]:
    desired_source = normalize_public_ipv4(public_ip) + "/32"
    response = client.get_security_list(config.security_list_id)
    security_list = response.data
    rules = list(security_list.ingress_security_rules or [])
    index, adopted = select_managed_rule(
        rules,
        port=config.ssh_port,
        description=config.description,
    )
    managed_rule = rules[index]
    previous_source = str(getattr(managed_rule, "source", "") or "")
    description_changed = (
        str(getattr(managed_rule, "description", "") or "") != config.description
    )
    source_changed = previous_source != desired_source

    if source_changed or description_changed:
        managed_rule.source = desired_source
        managed_rule.description = config.description
        details = models.UpdateSecurityListDetails(
            display_name=security_list.display_name,
            ingress_security_rules=rules,
            egress_security_rules=list(
                security_list.egress_security_rules or []
            ),
            defined_tags=security_list.defined_tags,
            freeform_tags=security_list.freeform_tags,
        )
        client.update_security_list(
            config.security_list_id,
            details,
            if_match=response.headers.get("etag"),
        )

    verified = client.get_security_list(config.security_list_id).data
    verified_rules = list(verified.ingress_security_rules or [])
    verified_index, _ = select_managed_rule(
        verified_rules,
        port=config.ssh_port,
        description=config.description,
    )
    current_source = str(verified_rules[verified_index].source or "")
    if current_source != desired_source:
        raise RuntimeError("Oracle did not retain the requested SSH /32 source")
    return {
        "configured": True,
        "state": "updated" if source_changed or description_changed else "unchanged",
        "public_ip": public_ip,
        "previous_source": previous_source,
        "current_source": current_source,
        "ssh_port": config.ssh_port,
        "adopted_rule": adopted,
        "security_list": str(verified.display_name or ""),
        "verified_at": _utc_now(),
    }


def ensure_current_ip(
    config: AllowlistConfig,
    *,
    status_path: Path,
    public_ip: str | None = None,
    now: dt.datetime | None = None,
    oci_module: object | None = None,
) -> dict[str, Any]:
    current_ip = normalize_public_ipv4(
        public_ip if public_ip is not None else discover_public_ipv4()
    )
    current_time = now or dt.datetime.now(dt.timezone.utc)
    previous = _read_json(status_path)
    desired_source = current_ip + "/32"
    if (
        previous.get("configured")
        and previous.get("current_source") == desired_source
        and _recent(
            previous.get("verified_at"),
            seconds=config.verify_seconds,
            now=current_time,
        )
    ):
        cached = {
            **previous,
            "state": "cached",
            "public_ip": current_ip,
            "checked_at": _utc_now(),
        }
        _write_json(status_path, cached)
        return cached

    if oci_module is None:
        try:
            import oci as oci_module  # type: ignore[no-redef]
        except ImportError as exc:
            raise RuntimeError(
                "OCI updater dependency is missing; run "
                "scripts/install_edge_oci_updater.ps1"
            ) from exc
    sdk_config = oci_module.config.from_file(
        str(config.config_path),
        config.profile,
    )
    oci_module.config.validate_config(sdk_config)
    client = oci_module.core.VirtualNetworkClient(sdk_config)
    result = reconcile_security_list(
        client,
        oci_module.core.models,
        config,
        current_ip,
    )
    result["checked_at"] = _utc_now()
    _write_json(status_path, result)
    return result


def _parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description=(
            "Keep the Oracle Signal edge SSH rule restricted to this laptop's "
            "current public IPv4 /32."
        )
    )
    parser.add_argument(
        "--status",
        default=str(
            repo_root / "storage" / "edge_sync" / "oci_allowlist_status.json"
        ),
    )
    parser.add_argument(
        "--public-ip",
        default="",
        help="Override public-IP discovery (verification only).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    load_dotenv(repo_root / ".env")
    args = _parser().parse_args(argv)
    status_path = Path(args.status).expanduser()
    try:
        config = AllowlistConfig.from_env()
        if config is None:
            result = {
                "configured": False,
                "state": "disabled",
                "checked_at": _utc_now(),
            }
            _write_json(status_path, result)
        else:
            result = ensure_current_ip(
                config,
                status_path=status_path,
                public_ip=args.public_ip or None,
            )
    except Exception as exc:  # noqa: BLE001
        result = {
            "configured": True,
            "state": "error",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "checked_at": _utc_now(),
        }
        _write_json(status_path, result)
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
