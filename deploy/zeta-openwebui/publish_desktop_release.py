#!/usr/bin/env python3
"""Publish or roll back a signed Zeta desktop release from the local host.

This command has no network or signing capability.  It verifies the supplied
artifacts with the configured RSA public key, then delegates the durable,
atomic store update to the shared desktop-update storage module.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys


OVERLAY_MODULE = (
    Path(__file__).resolve().parent
    / "files"
    / "open_webui"
    / "utils"
    / "zeta_desktop_updates.py"
)


class PublisherError(RuntimeError):
    """A safe local configuration error suitable for concise CLI output."""


def load_update_module():
    """Load the exact helper that the overlay installer deploys."""

    spec = importlib.util.spec_from_file_location(
        "zeta_desktop_updates_publisher", OVERLAY_MODULE
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load desktop-update helper: {OVERLAY_MODULE}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their defining module through sys.modules.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def absolute_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def existing_file(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    if not path.is_file() or path.is_symlink():
        raise argparse.ArgumentTypeError(
            "path must be an existing non-symlink regular file"
        )
    return path


def external_openwebui_path(path: Path, *, label: str) -> Path:
    """Reject configured state beneath a recognizable OpenWebUI source tree."""

    candidate = path.resolve(strict=False)
    for ancestor in (candidate, *candidate.parents):
        if (ancestor / "backend" / "open_webui" / "main.py").is_file() or (
            ancestor / "open_webui" / "main.py"
        ).is_file():
            raise PublisherError(
                f"{label} must be outside the application source tree"
            )
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify and publish signed Zeta desktop update artifacts"
    )
    parser.add_argument(
        "--release-dir",
        type=absolute_path,
        help="Update-store root (otherwise ZETA_DESKTOP_UPDATE_DIR/default)",
    )
    parser.add_argument(
        "--public-key",
        type=absolute_path,
        help=(
            "RSA public PEM (otherwise "
            "ZETA_DESKTOP_UPDATE_PUBLIC_KEY/default)"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    publish = commands.add_parser(
        "publish", help="verify and atomically publish a new stable release"
    )
    publish.add_argument("--exe", required=True, type=existing_file)
    publish.add_argument("--latest", required=True, type=existing_file)
    publish.add_argument("--sig", required=True, type=existing_file)
    publish.add_argument("--release-summary", required=True, type=existing_file)

    rollback = commands.add_parser(
        "rollback", help="point the feed at an older retained verified release"
    )
    rollback.add_argument("version", help="retained strict Semantic Version")
    return parser


def run(args: argparse.Namespace, updates) -> int:
    release_dir = external_openwebui_path(
        args.release_dir or updates.get_release_root(),
        label="update-store root",
    )
    public_key = external_openwebui_path(
        args.public_key or updates.get_public_key_path(),
        label="desktop-update public key",
    )
    store = updates.ReleaseStore(
        root=release_dir,
        public_key_path=public_key,
    )
    if args.command == "publish":
        result = store.publish_files(
            manifest_path=args.latest,
            signature_path=args.sig,
            summary_path=args.release_summary,
            installer_source=args.exe,
        )
        state = "published" if result.changed else "already current"
        print(
            f"Zeta {result.release.version} {state}; "
            f"installer_sha256={result.release.installer_sha256}"
        )
        return 0

    if args.command == "rollback":
        release = store.rollback(args.version)
        print(
            f"Zeta feed rolled back to {release.version}; "
            f"installer_sha256={release.installer_sha256}"
        )
        return 0

    raise RuntimeError(f"Unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        updates = load_update_module()
        return run(args, updates)
    except Exception as exc:
        # Expected storage/validation failures are safe and useful locally. Do
        # not dump artifact bytes, signature data, or a traceback by default.
        if isinstance(exc, PublisherError) or (
            "updates" in locals() and isinstance(exc, updates.StoreError)
        ):
            parser.exit(2, f"desktop release not published: {exc}\n")
        if isinstance(exc, OSError):
            parser.exit(2, f"desktop release not published: {exc.strerror or 'I/O error'}\n")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
