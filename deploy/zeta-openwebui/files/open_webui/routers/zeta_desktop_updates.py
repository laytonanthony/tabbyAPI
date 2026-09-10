"""Public, read-only Zeta desktop update feed.

Publishing remains a local administrative operation.  These routes expose
only a verified current manifest/signature pair and immutable, retained
installers; no authentication credential or upload surface is involved.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import stat
from typing import Iterator

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from open_webui.utils.zeta_desktop_updates import (
    CURRENT_VERSION_FILENAME,
    NoRelease,
    ReleaseStore,
    SemVer,
    StoreError,
    UpdateNotFoundError,
    get_public_key_path,
    get_release_root,
    etag_bytes,
    if_none_match,
)


router = APIRouter()
log = logger.bind(component="zeta_desktop_updates")

_LATEST_CACHE_CONTROL = "no-cache"
_INSTALLER_CACHE_CONTROL = "public, max-age=31536000, immutable, no-transform"
_ERROR_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


def _json_error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail},
        headers=_ERROR_HEADERS,
    )


def _not_published() -> JSONResponse:
    return _json_error(
        status.HTTP_404_NOT_FOUND,
        "No stable Zeta desktop release is published",
    )


def _not_found() -> JSONResponse:
    return _json_error(status.HTTP_404_NOT_FOUND, "Desktop update not found")


def _unavailable(exc: Exception) -> JSONResponse:
    log.bind(
        event="zeta_desktop_update_unavailable",
        error_type=type(exc).__name__,
    ).error("Zeta desktop update feed unavailable")
    return _json_error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "Desktop update feed unavailable",
    )


def _external_config_path(path: Path, *, label: str) -> Path:
    """Reject update state or keys placed beneath the live source checkout."""

    application_root = Path(__file__).resolve().parents[3]
    lexical_path = Path(os.path.abspath(path))
    resolved_path = path.resolve(strict=False)
    resolved_application_root = application_root.resolve()
    for candidate in (lexical_path, resolved_path):
        if candidate == resolved_application_root or candidate.is_relative_to(
            resolved_application_root
        ):
            raise StoreError(f"{label} must be outside the application source tree")
    return path


def _configured_store() -> ReleaseStore:
    """Open a configured external store without creating public state."""

    root = _external_config_path(get_release_root(), label="update-store root")
    public_key = _external_config_path(
        get_public_key_path(), label="desktop-update public key"
    )
    if root.is_symlink():
        raise StoreError("update-store root is unsafe")
    if not root.exists():
        raise NoRelease("the update store has no current release")
    if not root.is_dir():
        raise StoreError("update-store root is unsafe")
    pointer = root / CURRENT_VERSION_FILENAME
    if pointer.is_symlink():
        raise StoreError("current release pointer is unsafe")
    if not pointer.exists():
        raise NoRelease("the update store has no current release")
    if not pointer.is_file():
        raise StoreError("current release pointer is unsafe")
    return ReleaseStore(root=root, public_key_path=public_key)


def _current_release():
    store = _configured_store()
    return store, store.current_release()


def _exact_bytes_response(
    request: Request,
    content: bytes,
    *,
    content_type: str,
) -> Response:
    etag = etag_bytes(content)
    headers = {
        "ETag": etag,
        "Cache-Control": _LATEST_CACHE_CONTROL,
        "Content-Type": content_type,
        "X-Content-Type-Options": "nosniff",
    }
    if if_none_match(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=content, headers=headers)


@router.get("/stable/latest.json", include_in_schema=False)
def get_latest_manifest(request: Request):
    try:
        _, release = _current_release()
        return _exact_bytes_response(
            request,
            release.manifest,
            content_type="application/json; charset=utf-8",
        )
    except NoRelease:
        return _not_published()
    except Exception as exc:
        return _unavailable(exc)


@router.get("/stable/latest.json.sig", include_in_schema=False)
def get_latest_signature(request: Request):
    try:
        _, release = _current_release()
        return _exact_bytes_response(
            request,
            release.signature,
            content_type="text/plain; charset=us-ascii",
        )
    except NoRelease:
        return _not_published()
    except Exception as exc:
        return _unavailable(exc)


def _installer_version(filename: str) -> str | None:
    prefix = "Zeta-Setup-"
    suffix = ".exe"
    if not filename.startswith(prefix) or not filename.endswith(suffix):
        return None
    version = filename[len(prefix) : -len(suffix)]
    try:
        SemVer.parse(version)
    except StoreError:
        return None
    return version


def _installer_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Return one inclusive byte range, or raise for malformed/unsatisfiable input."""

    if value is None:
        return None
    if len(value) > 256 or not value.startswith("bytes=") or "," in value:
        raise ValueError("unsupported byte range")
    specifier = value[6:]
    if specifier.count("-") != 1:
        raise ValueError("invalid byte range")
    first, last = specifier.split("-", 1)
    if not first:
        if not last.isdigit() or int(last) <= 0:
            raise ValueError("invalid suffix byte range")
        suffix = int(last)
        return max(0, size - suffix), size - 1
    if not first.isdigit():
        raise ValueError("invalid byte range start")
    start = int(first)
    if start >= size:
        raise ValueError("byte range is unsatisfiable")
    if not last:
        return start, size - 1
    if not last.isdigit():
        raise ValueError("invalid byte range end")
    end = int(last)
    if end < start:
        raise ValueError("byte range is unsatisfiable")
    return start, min(end, size - 1)


def _verified_installer_descriptor(release) -> tuple[int, os.stat_result]:
    """Open, hash, and pin the exact installer inode that will be streamed."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(release.installer_path, flags)
    except OSError as exc:
        raise StoreError("stored installer is missing or unsafe") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size != release.installer.size
        ):
            raise StoreError("stored installer metadata changed")

        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > release.installer.size:
                raise StoreError("stored installer grew while it was verified")
            digest.update(chunk)
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if total != release.installer.size or any(
            getattr(before, field) != getattr(after, field) for field in stable_fields
        ):
            raise StoreError("stored installer changed while it was verified")
        if not hmac.compare_digest(digest.hexdigest(), release.installer_sha256):
            raise StoreError("stored installer digest does not match signed metadata")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, before
    except Exception:
        os.close(descriptor)
        raise


def _stream_descriptor(descriptor: int, *, start: int, length: int) -> Iterator[bytes]:
    """Stream a bounded section from an already verified and pinned descriptor."""

    try:
        os.lseek(descriptor, start, os.SEEK_SET)
        remaining = length
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise StoreError("verified installer ended while it was streamed")
            remaining -= len(chunk)
            yield chunk
    finally:
        os.close(descriptor)


def _range_not_satisfiable(size: int) -> JSONResponse:
    headers = {
        **_ERROR_HEADERS,
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes */{size}",
    }
    return JSONResponse(
        status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
        content={"detail": "Requested byte range is not satisfiable"},
        headers=headers,
    )


@router.get("/stable/{filename}", include_in_schema=False)
def get_installer(filename: str, request: Request):
    version = _installer_version(filename)
    if version is None:
        return _not_found()

    try:
        store = _configured_store()
        release = store.release(version)
        if release.installer.filename != filename:
            return _not_found()

        descriptor, details = _verified_installer_descriptor(release)
        descriptor_owned = True
        etag = f'"{release.installer_sha256}"'
        headers = {
            "ETag": etag,
            "Cache-Control": _INSTALLER_CACHE_CONTROL,
            "X-Content-Type-Options": "nosniff",
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
        try:
            if if_none_match(request.headers.get("if-none-match"), etag):
                return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
            try:
                selected_range = _installer_range(
                    request.headers.get("range"), details.st_size
                )
            except ValueError:
                return _range_not_satisfiable(details.st_size)

            if selected_range is None:
                first, last = 0, details.st_size - 1
                response_status = status.HTTP_200_OK
            else:
                first, last = selected_range
                response_status = status.HTTP_206_PARTIAL_CONTENT
                headers["Content-Range"] = f"bytes {first}-{last}/{details.st_size}"
            length = last - first + 1
            headers["Content-Length"] = str(length)
            body = _stream_descriptor(descriptor, start=first, length=length)
            response = StreamingResponse(
                body,
                status_code=response_status,
                media_type="application/octet-stream",
                headers=headers,
            )
            descriptor_owned = False
            return response
        finally:
            if descriptor_owned:
                os.close(descriptor)
    except (NoRelease, UpdateNotFoundError):
        return _not_found()
    except Exception as exc:
        return _unavailable(exc)


@router.get("/{unmatched_path:path}", include_in_schema=False)
def reject_unknown_update_path(unmatched_path: str):
    """Prevent unknown update URLs from falling through to the SPA index."""

    return _not_found()
