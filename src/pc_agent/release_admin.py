"""Vendor-only release signing tool for portable update bundles.

This entry point is deliberately separate from the customer CLI.  It creates
an Ed25519 release-signing keypair and signs the exact SHA-256/version of a
verified portable bundle.  Private keys must stay on the vendor's signing
machine and must never be copied into a customer installation or bundle.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .errors import PcAgentError
from .licensing.ed25519 import generate_seed, public_key_from_seed, sign
from .release import verify_release_bundle
from .updates import (
    b64url_decode,
    b64url_encode,
    build_signature_payload,
    default_signature_path,
    signature_payload_bytes,
    validate_key_id,
)


def _write_new_or_forced(path: Path, content: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise PcAgentError(
            f"Refusing to overwrite existing file: {path}. Pass --force only "
            "when replacing it is intentional."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _keygen(args: argparse.Namespace) -> int:
    key_id = validate_key_id(args.key_id)
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    private_path = out_dir / "release-private-key.txt"
    public_path = out_dir / "release-public-key.txt"
    if not args.force and (private_path.exists() or public_path.exists()):
        raise PcAgentError(
            "Release key files already exist; refusing to overwrite without --force."
        )
    seed = generate_seed()
    public = public_key_from_seed(seed)
    private_text = (
        "# Self-Healing PC Agent release signing private key (Ed25519 seed).\n"
        "# KEEP SECRET. Never ship this file or put it in a release bundle.\n"
        f"PC_AGENT_RELEASE_PRIVATE_KEY={b64url_encode(seed)}\n"
        f"PC_AGENT_RELEASE_KEY_ID={key_id}\n"
    )
    public_text = (
        "# Self-Healing PC Agent release signing public key. Safe to distribute.\n"
        f"PC_AGENT_RELEASE_PUBLIC_KEY={b64url_encode(public)}\n"
        f"PC_AGENT_RELEASE_KEY_ID={key_id}\n"
    )
    _write_new_or_forced(private_path, private_text, force=bool(args.force))
    try:
        os.chmod(private_path, 0o600)
    except OSError:
        pass
    _write_new_or_forced(public_path, public_text, force=bool(args.force))
    print(f"Release signing private key: {private_path}")
    print(f"Release signing public key:  {public_path}")
    print("Customer .env values:")
    print(f"PC_AGENT_RELEASE_PUBLIC_KEY={b64url_encode(public)}")
    print(f"PC_AGENT_RELEASE_KEY_ID={key_id}")
    print("PC_AGENT_REQUIRE_SIGNED_UPDATES=1")
    return 0


def _read_private_seed(path: Path) -> bytes:
    key_file = path.expanduser().resolve()
    if not key_file.is_file() or key_file.is_symlink():
        raise PcAgentError(f"Release private key file was not found: {key_file}.")
    for raw_line in key_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            name, value = line.split("=", 1)
            if name.strip() != "PC_AGENT_RELEASE_PRIVATE_KEY":
                continue
            return b64url_decode(
                value.strip(),
                expected_length=32,
                label="Release private key",
            )
        return b64url_decode(
            line,
            expected_length=32,
            label="Release private key",
        )
    raise PcAgentError("Release private key file contains no key value.")


def _sign(args: argparse.Namespace) -> int:
    key_id = validate_key_id(args.key_id)
    bundle = verify_release_bundle(args.bundle.expanduser())
    seed = _read_private_seed(args.private_key)
    payload = build_signature_payload(bundle, key_id=key_id)
    document = dict(payload)
    document["signature"] = b64url_encode(
        sign(seed, signature_payload_bytes(payload))
    )
    output = (
        args.output.expanduser().resolve()
        if args.output is not None
        else default_signature_path(bundle.path).resolve()
    )
    _write_new_or_forced(
        output,
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        force=bool(args.force),
    )
    print(f"Signed release bundle: {bundle.path}")
    print(f"Version: {bundle.version}")
    print(f"Bundle SHA-256: {bundle.bundle_sha256}")
    print(f"Signature: {output}")
    print("Customer .env public key:")
    print(f"PC_AGENT_RELEASE_PUBLIC_KEY={b64url_encode(public_key_from_seed(seed))}")
    print(f"PC_AGENT_RELEASE_KEY_ID={key_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pc-agent-release-admin",
        description="Vendor-only signing for Self-Healing PC Agent portable bundles",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    keygen = subparsers.add_parser(
        "keygen", help="Create a release-signing Ed25519 keypair"
    )
    keygen.add_argument("--out-dir", type=Path, required=True)
    keygen.add_argument("--key-id", default="release-1")
    keygen.add_argument("--force", action="store_true")
    keygen.set_defaults(handler=_keygen)

    sign_parser = subparsers.add_parser(
        "sign", help="Sign one verified portable release bundle"
    )
    sign_parser.add_argument("bundle", type=Path)
    sign_parser.add_argument("--private-key", type=Path, required=True)
    sign_parser.add_argument("--key-id", default="release-1")
    sign_parser.add_argument("--output", type=Path, default=None)
    sign_parser.add_argument("--force", action="store_true")
    sign_parser.set_defaults(handler=_sign)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (PcAgentError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
