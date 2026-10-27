"""Vendor-side license administration (``pc-agent-license-admin``).

This tool mints signing keys and licenses. It is part of the source/vendor
toolkit only: production customer builds must keep the private key (and
ideally this tool) off customer machines entirely. The client application
never imports this module at runtime.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..errors import LicenseError, PcAgentError
from . import ed25519
from .models import ALL_FEATURES, PLAN_FEATURES, b64u_decode, b64u_encode, issue_token

_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _validate_key_id(raw: str) -> str:
    value = raw.strip()
    if not _KEY_ID_PATTERN.fullmatch(value):
        raise LicenseError("key id must be 1-64 letters, digits, '.', '_' or '-'.")
    return value


def _read_seed(path: Path) -> bytes:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LicenseError(f"Private key file could not be read at {path}.") from exc
    for line in lines:
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if "=" in candidate:
            candidate = candidate.rsplit("=", 1)[-1].strip()
        seed = b64u_decode(candidate)
        if len(seed) != 32:
            raise LicenseError("Private key seed must decode to exactly 32 bytes.")
        return seed
    raise LicenseError("Private key file does not contain a base64url seed.")


def _keygen(args: argparse.Namespace) -> int:
    key_id = _validate_key_id(args.key_id)
    out_dir = Path(args.out_dir).expanduser()
    private_path = out_dir / "license_private_key.txt"
    public_path = out_dir / "license_public_key.txt"
    if private_path.exists() or public_path.exists():
        raise LicenseError(
            f"Key files already exist in {out_dir}; refusing to overwrite them."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    seed = ed25519.generate_seed()
    public_key = ed25519.public_key_from_seed(seed)
    private_path.write_text(
        "# VENDOR PRIVATE SIGNING KEY - KEEP SECRET AND OFFLINE.\n"
        f"# key_id: {key_id}\n"
        f"{b64u_encode(seed)}\n",
        encoding="utf-8",
    )
    try:
        private_path.chmod(0o600)
    except OSError:
        pass
    public_path.write_text(
        f"PC_AGENT_LICENSE_KEY_ID={key_id}\n"
        f"PC_AGENT_LICENSE_PUBLIC_KEY={b64u_encode(public_key)}\n",
        encoding="utf-8",
    )
    print(f"Private key (SECRET): {private_path}")
    print(f"Public key (.env values): {public_path}")
    print(
        "WARNING: anyone with the private key can mint licenses. Store it "
        "offline, never in the customer application folder, source control "
        "or chat."
    )
    return 0


def _issue(args: argparse.Namespace) -> int:
    key_id = _validate_key_id(args.key_id)
    if args.days is not None and args.expires_at is not None:
        raise LicenseError("Use either --days or --expires-at, not both.")
    now = datetime.now(UTC)
    if args.expires_at:
        text = args.expires_at.strip().replace("Z", "+00:00")
        try:
            expires_at = datetime.fromisoformat(text)
        except ValueError as exc:
            raise LicenseError("--expires-at must be an ISO-8601 timestamp.") from exc
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
    elif args.days is not None:
        if args.days < 1 or args.days > 36500:
            raise LicenseError("--days must be between 1 and 36500.")
        expires_at = now + timedelta(days=args.days)
    elif args.plan == "trial":
        expires_at = now + timedelta(days=14)
    else:
        expires_at = None
    extras = tuple(
        item.strip() for item in (args.features or "").split(",") if item.strip()
    )
    unknown = [feature for feature in extras if feature not in ALL_FEATURES]
    if unknown:
        raise LicenseError(f"Unknown feature(s): {', '.join(unknown)}")
    seed = _read_seed(Path(args.private_key_file).expanduser())
    token = issue_token(
        seed,
        key_id=key_id,
        licensee=args.licensee,
        plan=args.plan,
        expires_at=expires_at,
        extra_features=extras,
        seats=args.seats,
        machine_id=args.machine_id.strip() if args.machine_id else None,
        license_id=args.license_id.strip() if args.license_id else None,
        issued_at=now,
    )
    if args.out:
        out_path = Path(args.out).expanduser()
        if out_path.exists() and not args.force:
            raise LicenseError(f"{out_path} already exists; pass --force to replace it.")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(token + "\n", encoding="utf-8")
        print(f"License written: {out_path}")
    else:
        print(token)
    term = expires_at.isoformat() if expires_at else "perpetual"
    print(
        f"Issued plan={args.plan} licensee={args.licensee!r} term_end={term} "
        f"key_id={key_id}",
        file=sys.stderr,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pc-agent-license-admin",
        description="Vendor-only license key generation and issuing.",
    )
    subparsers = parser.add_subparsers(dest="command")

    keygen = subparsers.add_parser("keygen", help="Generate a new vendor signing keypair")
    keygen.add_argument("--key-id", default="vendor-1")
    keygen.add_argument("--out-dir", default="vendor-keys")

    issue = subparsers.add_parser("issue", help="Issue a signed license token")
    issue.add_argument("--private-key-file", required=True)
    issue.add_argument("--key-id", required=True)
    issue.add_argument("--licensee", required=True)
    issue.add_argument("--plan", choices=sorted(PLAN_FEATURES), default="pro")
    issue.add_argument("--days", type=int, default=None)
    issue.add_argument("--expires-at", default=None)
    issue.add_argument("--features", default="")
    issue.add_argument("--seats", type=int, default=1)
    issue.add_argument("--machine-id", default=None)
    issue.add_argument("--license-id", default=None)
    issue.add_argument("--out", default=None)
    issue.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "keygen":
            return _keygen(args)
        if args.command == "issue":
            return _issue(args)
    except PcAgentError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
