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
from typing import Any

from .errors import PcAgentError
from .licensing.ed25519 import generate_seed, public_key_from_seed, sign
from .release import verify_release_bundle
from .update_feed_hosting import (
    build_update_feed_hosting_package,
    list_hosting_profiles,
    serve_update_feed_hosting_package,
    verify_update_feed_hosting_package,
)
from .update_feed_publisher import (
    build_update_feed_package,
    verify_update_feed_package,
)
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


def _read_public_key(path: Path) -> bytes:
    key_file = path.expanduser().resolve()
    if not key_file.is_file() or key_file.is_symlink():
        raise PcAgentError(f"Release public key file was not found: {key_file}.")
    for raw_line in key_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            name, value = line.split("=", 1)
            if name.strip() != "PC_AGENT_RELEASE_PUBLIC_KEY":
                continue
            return b64url_decode(
                value.strip(),
                expected_length=32,
                label="Release public key",
            )
        return b64url_decode(
            line,
            expected_length=32,
            label="Release public key",
        )
    raise PcAgentError("Release public key file contains no key value.")


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


def _print_feed_package(info: Any, *, action: str) -> None:
    print(f"{action} signed update feed package: {info.summary()}")
    print(f"Location: {info.path}")
    print(f"Feed URL: {info.feed_url}")
    print(f"Bundle: releases/{info.bundle_name}")
    print(f"Signature: releases/{info.signature_name}")
    print(f"Bundle SHA-256: {info.bundle_sha256}")
    print(f"Feed SHA-256: {info.feed_sha256}")
    print(f"Publisher manifest SHA-256: {info.manifest_sha256}")
    print("Customer .env public values:")
    print("PC_AGENT_ONLINE_UPDATES=1")
    print(f"PC_AGENT_UPDATE_FEED_URL={info.feed_url}")
    print(f"PC_AGENT_UPDATE_CHANNEL={info.channel}")
    print(f"PC_AGENT_RELEASE_PUBLIC_KEY={info.public_key}")
    print(f"PC_AGENT_RELEASE_KEY_ID={info.key_id}")
    print("PC_AGENT_REQUIRE_SIGNED_UPDATES=1")
    print("Private key included: no")
    print("Uploaded: no (upload this verified static directory yourself)")


def _publish_feed(args: argparse.Namespace) -> int:
    info = build_update_feed_package(
        args.bundle,
        args.output,
        seed=_read_private_seed(args.private_key),
        key_id=args.key_id,
        base_url=args.base_url,
        channel=args.channel,
        published_at=args.published_at,
    )
    _print_feed_package(info, action="Built and verified")
    return 0


def _verify_feed(args: argparse.Namespace) -> int:
    public_key: str | bytes = (
        _read_public_key(args.public_key_file)
        if args.public_key_file is not None
        else args.public_key
    )
    info = verify_update_feed_package(
        args.package,
        public_key=public_key,
        key_id=args.key_id,
        expected_base_url=args.base_url,
        expected_channel=args.channel,
    )
    _print_feed_package(info, action="Verified")
    return 0


def _hosting_public_key(args: argparse.Namespace) -> str | bytes:
    if args.public_key_file is not None:
        return _read_public_key(args.public_key_file)
    return str(args.public_key)


def _print_hosting_package(info: Any, *, action: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps(info.to_dict(), indent=2))
        return
    print(f"{action} Windows HTTPS update-feed hosting package: {info.summary()}")
    print(f"Location: {info.path}")
    print(f"Feed URL: {info.feed_url}")
    print(f"Deployment: {info.deployment_name} at {info.deployment_root}")
    print(f"Bundle SHA-256: {info.bundle_sha256}")
    print(f"Hosting manifest SHA-256: {info.manifest_sha256}")
    print("TLS certificate/private key included: no")
    print("Uploaded/live verified: no (deploy first, then run hosting-check)")


def _hosting_profiles(args: argparse.Namespace) -> int:
    profiles = list_hosting_profiles()
    if args.json:
        print(json.dumps([profile.to_dict() for profile in profiles], indent=2))
        return 0
    print("Update-feed hosting profiles:")
    for profile in profiles:
        print(
            f"  {profile.profile_id}: platform={profile.platform}; "
            f"status={profile.status}; {profile.display_name}"
        )
    print("Linux/macOS entries are structure only in this Windows-first phase.")
    return 0


def _build_hosting(args: argparse.Namespace) -> int:
    info = build_update_feed_hosting_package(
        args.package,
        args.output,
        public_key=_hosting_public_key(args),
        key_id=args.key_id,
        profile_id=args.profile,
        deployment_name=args.deployment_name,
        deployment_root=args.deployment_root,
    )
    _print_hosting_package(
        info, action="Built and verified", as_json=bool(args.json)
    )
    return 0


def _verify_hosting(args: argparse.Namespace) -> int:
    info = verify_update_feed_hosting_package(
        args.package,
        public_key=_hosting_public_key(args),
        key_id=args.key_id,
        expected_profile=args.profile,
    )
    _print_hosting_package(info, action="Verified", as_json=bool(args.json))
    return 0


def _is_windows() -> bool:
    return sys.platform == "win32"


def _serve_hosting(args: argparse.Namespace) -> int:
    if not _is_windows():
        raise PcAgentError(
            "The built-in HTTPS update-feed host is enabled on Windows only "
            "in this phase; Linux/macOS hosting profiles are planned."
        )
    return serve_update_feed_hosting_package(
        args.package,
        public_key=_hosting_public_key(args),
        key_id=args.key_id,
        certificate_path=args.certificate,
        private_key_path=args.private_key,
        bind_host=args.bind,
        port=args.port,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pc-agent-release-admin",
        description=(
            "Vendor-only signing and static update-feed publishing for "
            "Self-Healing PC Agent portable bundles"
        ),
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

    publish_parser = subparsers.add_parser(
        "publish-feed",
        help="Build a verified static signed update-feed directory",
    )
    publish_parser.add_argument("bundle", type=Path)
    publish_parser.add_argument("--private-key", type=Path, required=True)
    publish_parser.add_argument("--key-id", default="release-1")
    publish_parser.add_argument(
        "--base-url",
        required=True,
        help="Static HTTPS directory URL where this package will be hosted",
    )
    publish_parser.add_argument("--channel", default="stable")
    publish_parser.add_argument("--output", type=Path, required=True)
    publish_parser.add_argument(
        "--published-at",
        default=None,
        help="Optional timezone-aware ISO timestamp; defaults to current UTC",
    )
    publish_parser.set_defaults(handler=_publish_feed)

    verify_parser = subparsers.add_parser(
        "verify-feed",
        help="Verify a static signed update-feed directory without network access",
    )
    verify_parser.add_argument("package", type=Path)
    key_group = verify_parser.add_mutually_exclusive_group(required=True)
    key_group.add_argument("--public-key")
    key_group.add_argument("--public-key-file", type=Path)
    verify_parser.add_argument("--key-id", default="release-1")
    verify_parser.add_argument("--base-url", default=None)
    verify_parser.add_argument("--channel", default=None)
    verify_parser.set_defaults(handler=_verify_feed)

    profiles_parser = subparsers.add_parser(
        "hosting-profiles",
        help="List Windows-first update-feed hosting profiles",
    )
    profiles_parser.add_argument("--json", action="store_true")
    profiles_parser.set_defaults(handler=_hosting_profiles)

    build_hosting_parser = subparsers.add_parser(
        "build-hosting",
        help="Build a Windows operational hosting package around a feed package",
    )
    build_hosting_parser.add_argument("package", type=Path)
    build_key_group = build_hosting_parser.add_mutually_exclusive_group(required=True)
    build_key_group.add_argument("--public-key")
    build_key_group.add_argument("--public-key-file", type=Path)
    build_hosting_parser.add_argument("--key-id", default="release-1")
    build_hosting_parser.add_argument("--profile", default="windows-https-task")
    build_hosting_parser.add_argument("--deployment-name", default=None)
    build_hosting_parser.add_argument("--deployment-root", default=None)
    build_hosting_parser.add_argument("--output", type=Path, required=True)
    build_hosting_parser.add_argument("--json", action="store_true")
    build_hosting_parser.set_defaults(handler=_build_hosting)

    verify_hosting_parser = subparsers.add_parser(
        "verify-hosting",
        help="Verify a Windows operational update-feed hosting package",
    )
    verify_hosting_parser.add_argument("package", type=Path)
    verify_key_group = verify_hosting_parser.add_mutually_exclusive_group(required=True)
    verify_key_group.add_argument("--public-key")
    verify_key_group.add_argument("--public-key-file", type=Path)
    verify_hosting_parser.add_argument("--key-id", default="release-1")
    verify_hosting_parser.add_argument("--profile", default=None)
    verify_hosting_parser.add_argument("--json", action="store_true")
    verify_hosting_parser.set_defaults(handler=_verify_hosting)

    serve_hosting_parser = subparsers.add_parser(
        "serve-hosting",
        help="Serve a verified Windows hosting package over HTTPS until stopped",
    )
    serve_hosting_parser.add_argument("package", type=Path)
    serve_key_group = serve_hosting_parser.add_mutually_exclusive_group(required=True)
    serve_key_group.add_argument("--public-key")
    serve_key_group.add_argument("--public-key-file", type=Path)
    serve_hosting_parser.add_argument("--key-id", default="release-1")
    serve_hosting_parser.add_argument("--certificate", type=Path, required=True)
    serve_hosting_parser.add_argument("--private-key", type=Path, required=True)
    serve_hosting_parser.add_argument("--bind", default="0.0.0.0")
    serve_hosting_parser.add_argument("--port", type=int, default=None)
    serve_hosting_parser.set_defaults(handler=_serve_hosting)
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
