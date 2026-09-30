"""Render the Caddyfile template for this server (run on the server, as root).

    sudo python3 render_caddyfile.py ADDRESS PWHASH_FILE [--name HOST ...] [--web-port 8570]
                                     [--gateway-port 8770]

Writes /etc/caddy/Caddyfile only if `caddy validate` accepts it (the old one is kept as
Caddyfile.previous), so a typo can never take the front door down.
"""
import argparse
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = Path("/etc/caddy/Caddyfile")


def _has_user(name: str) -> bool:
    import pwd
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False

parser = argparse.ArgumentParser()
parser.add_argument("address")
parser.add_argument("pwhash_file")
parser.add_argument("--name", action="append", default=[],
                    help="a DNS name for the same server (repeatable), e.g. 51-170-132-206.sslip.io")
parser.add_argument("--web-port", default="8570")
parser.add_argument("--gateway-port", default="8770")
args = parser.parse_args()

pwhash = Path(args.pwhash_file).read_text(encoding="utf-8").strip()
if not pwhash.startswith("$2"):
    raise SystemExit("that is not a bcrypt hash — make it with: caddy hash-password")
text = (HERE / "Caddyfile.template").read_text(encoding="utf-8")
sites = ", ".join(f"https://{host}" for host in [args.address, *args.name])
for key, value in {"@SITES@": sites, "@ADDRESS@": args.address, "@PWHASH@": pwhash,
                   "@WEB_PORT@": args.web_port, "@GATEWAY_PORT@": args.gateway_port}.items():
    text = text.replace(key, value)
candidate = TARGET.with_name("Caddyfile.candidate")
candidate.write_text(text, encoding="utf-8")
# Validate AS the account Caddy runs under: validating as root opens (creates) the log file as
# root, and the real service then cannot write it — the reload fails (seen 2026-09-29).
as_caddy = ["runuser", "-u", "caddy", "--"] if shutil.which("runuser") and _has_user("caddy") else []
check = subprocess.run(as_caddy + ["caddy", "validate", "--adapter", "caddyfile",
                                   "--config", str(candidate)], capture_output=True, text=True)
if check.returncode != 0:
    candidate.unlink()
    raise SystemExit("caddy rejected the new settings — nothing changed:\n" + check.stderr[-800:])
if TARGET.exists():
    shutil.copy2(TARGET, TARGET.with_name("Caddyfile.previous"))
candidate.replace(TARGET)
print(f"Caddyfile written for {sites} (validated)")
