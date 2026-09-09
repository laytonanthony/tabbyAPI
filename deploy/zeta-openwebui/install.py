#!/usr/bin/env python3
"""Install the versioned Zeta model-catalogue overlay into OpenWebUI.

The deployed OpenWebUI tree is not itself a Git checkout.  This installer
therefore validates the reviewed base file, creates a timestamped rollback
copy, and writes only the two overlay modules plus small marked integrations
in ``open_webui/main.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from datetime import datetime, timezone


EXPECTED_MAIN_SHA256 = "fa65867e7d07ccb133cb0e0f22b763762512289601371f56eedb0fdbf05f715f"
ROUTER_MARKER = "# BEGIN ZETA MODEL CATALOG ROUTER"
STATUS_MARKER = "# BEGIN ZETA MODEL CATALOG OFFLINE STATUS"

IMPORT_ANCHOR = """    memories,
    models,
    knowledge,
"""
IMPORT_REPLACEMENT = """    memories,
    models,
    zeta_model_catalog,
    knowledge,
"""

INCLUDE_ANCHOR = "app.include_router(models.router, prefix='/api/v1/models', tags=['models'])"
INCLUDE_REPLACEMENT = f"""{INCLUDE_ANCHOR}
{ROUTER_MARKER}
app.include_router(zeta_model_catalog.router, prefix='/api/v1/models', tags=['models'])
# END ZETA MODEL CATALOG ROUTER"""

STATUS_APPEND = """    # BEGIN ZETA MODEL CATALOG OFFLINE STATUS
    # Preserve the existing live status records, then add only authorised
    # catalogue members that are currently absent from provider discovery.
    try:
        catalogue_models = await zeta_model_catalog.catalogue_for_user(
            request,
            user,
            live_model_ids={
                str(item.get('id'))
                for item in data
                if isinstance(item, dict) and item.get('id')
            },
        )
    except Exception as exc:
        log.error(
            'zeta_model_status_catalogue_overlay_failed error_type=%s',
            type(exc).__name__,
        )
        catalogue_models = []

    data = zeta_model_catalog.merge_catalogue_status(data, catalogue_models)
    # END ZETA MODEL CATALOG OFFLINE STATUS

"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compile_python(path: Path) -> None:
    compile(path.read_text(encoding="utf-8"), str(path), "exec")


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"Expected one {label} anchor, found {count}")
    return source.replace(old, new, 1)


def transform_main(source: str, source_hash: str) -> str:
    installed = ROUTER_MARKER in source and STATUS_MARKER in source
    if installed:
        required = {
            "router import": IMPORT_REPLACEMENT,
            "router include": INCLUDE_REPLACEMENT,
            "status overlay": STATUS_APPEND,
        }
        missing = [
            label for label, block in required.items() if source.count(block) != 1
        ]
        if missing:
            raise RuntimeError(
                "Catalogue markers exist but the installation is incomplete: "
                + ", ".join(missing)
            )
        return source

    if ROUTER_MARKER in source or STATUS_MARKER in source:
        raise RuntimeError("Partial Zeta model-catalogue installation detected")
    if source_hash != EXPECTED_MAIN_SHA256:
        raise RuntimeError(
            "OpenWebUI main.py differs from the reviewed base; refusing to patch "
            f"(expected {EXPECTED_MAIN_SHA256}, found {source_hash})"
        )

    source = replace_once(source, IMPORT_ANCHOR, IMPORT_REPLACEMENT, "router import")
    source = replace_once(source, INCLUDE_ANCHOR, INCLUDE_REPLACEMENT, "router include")
    status_start = source.index("@app.get('/api/v1/models/status')")
    status_end = source.index("\n\n@app.get('/api/models')", status_start)
    status_block = source[status_start:status_end]
    return_anchor = "    return JSONResponse(\n"
    if status_block.count(return_anchor) != 1:
        raise RuntimeError("Unable to locate the status response anchor")
    status_block = status_block.replace(return_anchor, STATUS_APPEND + return_anchor, 1)
    return source[:status_start] + status_block + source[status_end:]


def atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def install(backend: Path, check_only: bool) -> tuple[Path | None, bool]:
    backend = backend.resolve()
    main_path = backend / "open_webui" / "main.py"
    if not main_path.is_file():
        raise RuntimeError(f"OpenWebUI main.py not found beneath {backend}")

    overlay_root = Path(__file__).resolve().parent / "files" / "open_webui"
    sources = {
        overlay_root / "utils" / "zeta_model_catalog.py": (
            backend / "open_webui" / "utils" / "zeta_model_catalog.py"
        ),
        overlay_root / "routers" / "zeta_model_catalog.py": (
            backend / "open_webui" / "routers" / "zeta_model_catalog.py"
        ),
    }
    for source in sources:
        compile_python(source)

    original = main_path.read_text(encoding="utf-8")
    transformed = transform_main(original, sha256(main_path))
    compile(transformed, str(main_path), "exec")

    targets_changed = transformed != original or any(
        not target.exists() or source.read_bytes() != target.read_bytes()
        for source, target in sources.items()
    )
    if check_only or not targets_changed:
        return None, targets_changed

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backend / ".zeta-backups" / f"model-catalog-{timestamp}"
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(main_path, backup / "main.py")
    for _, target in sources.items():
        if target.exists():
            relative = target.relative_to(backend)
            backup_target = backup / relative
            backup_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup_target)

    atomic_write(main_path, transformed.encode("utf-8"), main_path.stat().st_mode)
    for source, target in sources.items():
        mode = target.stat().st_mode if target.exists() else source.stat().st_mode
        atomic_write(target, source.read_bytes(), mode)

    compile_python(main_path)
    for target in sources.values():
        compile_python(target)
    return backup, True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backend", type=Path, help="Path containing open_webui/main.py")
    parser.add_argument("--check", action="store_true", help="Validate without writing")
    args = parser.parse_args()

    backup, changed = install(args.backend, args.check)
    if args.check:
        print("Overlay validation passed; changes required" if changed else "Overlay validation passed; already installed")
    elif changed:
        print(f"Overlay installed; backup: {backup}")
    else:
        print("Overlay already installed and current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
