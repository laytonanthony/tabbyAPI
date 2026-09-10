"""Verified, immutable storage for signed Zeta desktop update releases.

This module deliberately has no signing capability.  A publisher supplies the
exact ``latest.json`` bytes, their detached RSA-SHA256 signature, the exact
``release-summary.json`` bytes, and the installer.  The server owns only the
public key and refuses to publish anything it cannot verify.

Release directories are immutable.  Publication stages and fsyncs the
installer before the metadata, commits the complete directory, and replaces a
small ``current-version`` pointer last.  A crash can therefore leave an
unreferenced release, but never a pointer to a partial release.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

try:  # Linux is the deployment target; keep import failure explicit elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on unsupported hosts.
    fcntl = None  # type: ignore[assignment]


LATEST_FILENAME = "latest.json"
SIGNATURE_FILENAME = "latest.json.sig"
SUMMARY_FILENAME = "release-summary.json"
CURRENT_VERSION_FILENAME = "current-version"
RELEASES_DIRECTORY = "releases"
STORE_MARKER_FILENAME = ".zeta-desktop-update-store"
STORE_MARKER_BYTES = b"zeta-desktop-update-store-v1\n"

MAX_MANIFEST_BYTES = 64 * 1024
MAX_SUMMARY_BYTES = 256 * 1024
MAX_SIGNATURE_BYTES = 16 * 1024
MAX_INSTALLER_BYTES = 64 * 1024 * 1024 * 1024
MIN_RSA_BITS = 2048

DEFAULT_RELEASE_ROOT = Path("/home/anthony/.local/share/zeta/desktop-updates")
DEFAULT_PUBLIC_KEY_PATH = Path(
    "/home/anthony/.config/zeta/desktop-updater/manifest-public-key.pem"
)
RELEASE_ROOT_ENV = "ZETA_DESKTOP_UPDATE_DIR"
PUBLIC_KEY_ENV = "ZETA_DESKTOP_UPDATE_PUBLIC_KEY"
DEFAULT_PRODUCT = "Zeta"
DEFAULT_CHANNEL = "stable"

_SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_SEMVER = re.compile(
    r"(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>"
    r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*"
    r"))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z"
)


class StoreError(RuntimeError):
    """Base class for update-store failures safe to map at an API boundary."""


class DesktopUpdateError(StoreError):
    """Backward-compatible descriptive base name for store failures."""


class UpdateValidationError(DesktopUpdateError):
    """Supplied or stored update metadata is invalid."""


class UpdateSignatureError(UpdateValidationError):
    """A detached signature or public key is invalid."""


class UpdateConflictError(DesktopUpdateError):
    """An immutable version already exists with different content."""


class UpdateVersionError(DesktopUpdateError):
    """A publication or rollback violates version ordering."""


class UpdateNotFoundError(DesktopUpdateError):
    """A requested release does not exist."""


class NoRelease(UpdateNotFoundError):
    """The store has no current release, or the selected release is absent."""


@dataclass(frozen=True)
class SemVer:
    """Strict Semantic Versioning 2.0.0 value with precedence comparison."""

    text: str
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> "SemVer":
        if not isinstance(value, str) or not value or len(value) > 128:
            raise UpdateValidationError("version must be a bounded SemVer string")
        match = _SEMVER.fullmatch(value)
        if match is None:
            raise UpdateValidationError("version must use strict Semantic Versioning")
        return cls(
            text=value,
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=tuple((match.group("prerelease") or "").split("."))
            if match.group("prerelease")
            else (),
        )

    def _compare(self, other: "SemVer") -> int:
        left_core = (self.major, self.minor, self.patch)
        right_core = (other.major, other.minor, other.patch)
        if left_core != right_core:
            return -1 if left_core < right_core else 1
        if not self.prerelease and not other.prerelease:
            return 0
        if not self.prerelease:
            return 1
        if not other.prerelease:
            return -1
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_numeric = left.isdigit()
            right_numeric = right.isdigit()
            if left_numeric and right_numeric:
                return -1 if int(left) < int(right) else 1
            if left_numeric != right_numeric:
                return -1 if left_numeric else 1
            return -1 if left < right else 1
        if len(self.prerelease) == len(other.prerelease):
            return 0
        return -1 if len(self.prerelease) < len(other.prerelease) else 1

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._compare(other) < 0

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._compare(other) == 0

    def __hash__(self) -> int:
        # Build metadata is excluded from SemVer precedence and equality.
        return hash((self.major, self.minor, self.patch, self.prerelease))


@dataclass(frozen=True)
class InstallerMetadata:
    filename: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ValidatedManifest:
    document: Mapping[str, Any]
    exact_bytes: bytes
    version: SemVer
    installer: InstallerMetadata


@dataclass(frozen=True)
class StoredRelease:
    version: str
    directory: Path
    manifest: bytes
    signature: bytes
    summary: bytes
    installer: InstallerMetadata

    @property
    def manifest_path(self) -> Path:
        return self.directory / LATEST_FILENAME

    @property
    def signature_path(self) -> Path:
        return self.directory / SIGNATURE_FILENAME

    @property
    def summary_path(self) -> Path:
        return self.directory / SUMMARY_FILENAME

    @property
    def installer_path(self) -> Path:
        return self.directory / self.installer.filename

    @property
    def installer_sha256(self) -> str:
        return self.installer.sha256


@dataclass(frozen=True)
class PublicationResult:
    release: StoredRelease
    changed: bool


def _decode_public_key(public_key_pem: bytes) -> rsa.RSAPublicKey:
    if not isinstance(public_key_pem, bytes) or len(public_key_pem) > 64 * 1024:
        raise UpdateSignatureError("public key must be bounded PEM bytes")
    try:
        key = serialization.load_pem_public_key(public_key_pem)
    except (TypeError, ValueError) as exc:
        raise UpdateSignatureError("public key must be valid PEM") from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise UpdateSignatureError("public key is not RSA")
    if key.key_size < MIN_RSA_BITS:
        raise UpdateSignatureError(f"RSA public key must be at least {MIN_RSA_BITS} bits")
    exponent = key.public_numbers().e
    if exponent < 3 or exponent % 2 == 0:
        raise UpdateSignatureError("RSA public exponent is invalid")
    return key


def _decode_signature_text(exact_signature_bytes: bytes) -> bytes:
    if (
        not isinstance(exact_signature_bytes, bytes)
        or not exact_signature_bytes
        or len(exact_signature_bytes) > MAX_SIGNATURE_BYTES
    ):
        raise UpdateSignatureError("detached signature must be bounded base64 text")
    if exact_signature_bytes.endswith(b"\r\n"):
        encoded = exact_signature_bytes[:-2]
    elif exact_signature_bytes.endswith(b"\n"):
        encoded = exact_signature_bytes[:-1]
    else:
        encoded = exact_signature_bytes
    if not encoded or any(byte > 0x7F for byte in encoded):
        raise UpdateSignatureError("detached signature must be ASCII base64")
    try:
        signature = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise UpdateSignatureError("detached signature is not canonical base64") from exc
    if base64.b64encode(signature) != encoded:
        raise UpdateSignatureError("detached signature is not canonical base64")
    return signature


def verify_rsa_sha256(
    public_key_pem: bytes, exact_message: bytes, exact_signature_bytes: bytes
) -> None:
    """Verify canonical base64 RSA PKCS#1 v1.5/SHA-256 signature text."""

    key = _decode_public_key(public_key_pem)
    signature = _decode_signature_text(exact_signature_bytes)
    try:
        key.verify(signature, exact_message, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise UpdateSignatureError("detached signature verification failed")


def _json_object(exact_bytes: bytes, *, label: str, limit: int) -> dict[str, Any]:
    if not isinstance(exact_bytes, bytes) or not exact_bytes or len(exact_bytes) > limit:
        raise UpdateValidationError(f"{label} must be bounded non-empty bytes")
    try:
        text = exact_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateValidationError(f"{label} must be valid UTF-8") from exc

    def reject_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise UpdateValidationError(f"{label} contains a duplicate key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise UpdateValidationError(f"{label} contains non-finite number {value}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except UpdateValidationError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise UpdateValidationError(f"{label} must be a valid JSON object") from exc
    if not isinstance(value, dict):
        raise UpdateValidationError(f"{label} must be a JSON object")
    return value


def _require_text(value: object, *, field: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise UpdateValidationError(f"{field} must be a bounded non-empty string")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise UpdateValidationError(f"{field} contains control characters")
    return value


def _validate_filename(value: object) -> str:
    filename = _require_text(value, field="installer.filename", limit=128)
    if _SAFE_FILENAME.fullmatch(filename) is None or filename in {".", ".."}:
        raise UpdateValidationError("installer.filename is not a safe file name")
    return filename


def _validate_https_url(value: object, filename: str) -> str:
    url = _require_text(value, field="installer.url", limit=2048)
    if any(character.isspace() for character in url) or "\\" in url:
        raise UpdateValidationError("installer.url contains unsafe characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UpdateValidationError("installer.url is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or port is not None and not 1 <= port <= 65535
    ):
        raise UpdateValidationError(
            "installer.url must be a credential-free HTTPS URL without query or fragment"
        )
    if "%" in parsed.path or not parsed.path.startswith("/"):
        raise UpdateValidationError("installer.url path is unsafe")
    segments = parsed.path.split("/")[1:]
    if not segments or any(not segment or segment in {".", ".."} for segment in segments):
        raise UpdateValidationError("installer.url path is unsafe")
    if segments[-1] != filename:
        raise UpdateValidationError("installer.url must end with installer.filename")
    if any(_SAFE_FILENAME.fullmatch(segment) is None for segment in segments):
        raise UpdateValidationError("installer.url path contains unsafe characters")
    return url


def validate_manifest(
    exact_bytes: bytes,
    signature: bytes,
    public_key_pem: bytes,
    *,
    expected_product: str,
    expected_channel: str,
) -> ValidatedManifest:
    if len(signature) > MAX_SIGNATURE_BYTES:
        raise UpdateSignatureError("detached signature is too large")
    verify_rsa_sha256(public_key_pem, exact_bytes, signature)
    document = _json_object(
        exact_bytes, label=LATEST_FILENAME, limit=MAX_MANIFEST_BYTES
    )
    schema_version = document.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != 1:
        raise UpdateValidationError("schema_version must be integer 1")
    product = _require_text(document.get("product"), field="product")
    channel = _require_text(document.get("channel"), field="channel")
    if product != expected_product:
        raise UpdateValidationError("manifest product does not match this store")
    if channel != expected_channel:
        raise UpdateValidationError("manifest channel does not match this store")
    version = SemVer.parse(document.get("version"))
    installer_value = document.get("installer")
    if not isinstance(installer_value, dict):
        raise UpdateValidationError("installer must be a JSON object")
    filename = _validate_filename(installer_value.get("filename"))
    if filename != f"Zeta-Setup-{version.text}.exe":
        raise UpdateValidationError(
            "installer.filename must be Zeta-Setup-{version}.exe"
        )
    url = _validate_https_url(installer_value.get("url"), filename)
    size = installer_value.get("size")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
        or size > MAX_INSTALLER_BYTES
    ):
        raise UpdateValidationError("installer.size is outside the allowed range")
    sha256 = installer_value.get("sha256")
    if (
        not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
    ):
        raise UpdateValidationError("installer.sha256 must be lowercase SHA-256 hex")
    return ValidatedManifest(
        document=document,
        exact_bytes=exact_bytes,
        version=version,
        installer=InstallerMetadata(filename, url, size, sha256),
    )


def validate_summary(
    exact_bytes: bytes,
    *,
    product: str,
    channel: str,
    version: str,
) -> Mapping[str, Any]:
    document = _json_object(
        exact_bytes, label=SUMMARY_FILENAME, limit=MAX_SUMMARY_BYTES
    )
    for field, expected in (
        ("product", product),
        ("channel", channel),
        ("version", version),
    ):
        if field in document and document[field] != expected:
            raise UpdateValidationError(
                f"{SUMMARY_FILENAME} {field} does not match the manifest"
            )
    return document


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_file(path: Path, data: bytes, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _read_regular_file(path: Path, *, limit: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except (FileNotFoundError, OSError) as exc:
        raise UpdateValidationError(f"{label} is missing or unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > limit:
            raise UpdateValidationError(f"{label} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > limit:
            raise UpdateValidationError(f"{label} exceeds the size limit")
        return value
    finally:
        os.close(descriptor)


def _read_private_public_key(path: Path) -> bytes:
    """Read the trust anchor only from private service-owned key storage."""

    _require_owned_private_directory(
        path.parent,
        label="desktop update public-key directory",
    )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise UpdateSignatureError(
            "desktop update public key is missing or unsafe"
        ) from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise UpdateSignatureError("desktop update public key must be a regular file")
        if not hasattr(os, "geteuid") or details.st_uid != os.geteuid():
            raise UpdateSignatureError(
                "desktop update public key must be owned by the service user"
            )
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise UpdateSignatureError("desktop update public key permissions must be 0600")
        if details.st_size <= 0 or details.st_size > 64 * 1024:
            raise UpdateSignatureError("desktop update public key size is invalid")
        chunks: list[bytes] = []
        remaining = 64 * 1024 + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        ending_details = os.fstat(descriptor)
        if (
            len(value) != details.st_size
            or ending_details.st_dev != details.st_dev
            or ending_details.st_ino != details.st_ino
            or ending_details.st_size != details.st_size
            or ending_details.st_mtime_ns != details.st_mtime_ns
        ):
            raise UpdateSignatureError("desktop update public key changed while read")
        return value
    finally:
        os.close(descriptor)


def _regular_file_digest(
    path: Path, *, limit: int, label: str, hash_content: bool
) -> tuple[int, str | None]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except (FileNotFoundError, OSError) as exc:
        raise UpdateValidationError(f"{label} is missing or unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_size <= 0
            or details.st_size > limit
        ):
            raise UpdateValidationError(f"{label} is not a bounded regular file")
        if not hash_content:
            return details.st_size, None
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise UpdateValidationError(f"{label} exceeds the size limit")
            digest.update(chunk)
        ending_details = os.fstat(descriptor)
        if (
            ending_details.st_dev != details.st_dev
            or ending_details.st_ino != details.st_ino
            or ending_details.st_size != details.st_size
            or ending_details.st_mtime_ns != details.st_mtime_ns
        ):
            raise UpdateValidationError(f"{label} changed while it was read")
        return total, digest.hexdigest()
    finally:
        os.close(descriptor)


def _copy_installer(source: Path, destination: Path, expected: InstallerMetadata) -> None:
    if source.name != expected.filename:
        raise UpdateValidationError("installer source name does not match the manifest")
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(source, source_flags)
    except OSError as exc:
        raise UpdateValidationError("installer source is missing or unsafe") from exc
    destination_flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        source_details = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_details.st_mode):
            raise UpdateValidationError("installer source must be a regular file")
        if (
            source_details.st_size != expected.size
            or source_details.st_size <= 0
            or source_details.st_size > MAX_INSTALLER_BYTES
        ):
            raise UpdateValidationError(
                "installer source size does not match manifest metadata"
            )
        destination_descriptor = os.open(destination, destination_flags, 0o600)
        try:
            os.fchmod(destination_descriptor, 0o600)
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected.size:
                    raise UpdateValidationError(
                        "installer grew beyond its signed size while being copied"
                    )
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_descriptor, view)
                    if written <= 0:
                        raise OSError("unable to write staged installer")
                    view = view[written:]
            os.fsync(destination_descriptor)
        finally:
            os.close(destination_descriptor)
        if total != expected.size or digest.hexdigest() != expected.sha256:
            raise UpdateValidationError("installer bytes do not match manifest metadata")
        ending_details = os.fstat(source_descriptor)
        if (
            ending_details.st_dev != source_details.st_dev
            or ending_details.st_ino != source_details.st_ino
            or ending_details.st_size != source_details.st_size
            or ending_details.st_mtime_ns != source_details.st_mtime_ns
        ):
            raise UpdateValidationError("installer source changed while it was copied")
    finally:
        os.close(source_descriptor)


def _absolute_configured_path(value: str, *, label: str) -> Path:
    if not value or "\x00" in value:
        raise UpdateValidationError(f"{label} is invalid")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise UpdateValidationError(f"{label} must be absolute")
    return path


def get_release_root() -> Path:
    """Return the configured local update-store root."""

    configured = os.environ.get(RELEASE_ROOT_ENV)
    if configured is None:
        return DEFAULT_RELEASE_ROOT
    return _absolute_configured_path(configured, label=RELEASE_ROOT_ENV)


def get_public_key_path() -> Path:
    """Return the configured public verification-key path."""

    configured = os.environ.get(PUBLIC_KEY_ENV)
    if configured is None:
        return DEFAULT_PUBLIC_KEY_PATH
    return _absolute_configured_path(configured, label=PUBLIC_KEY_ENV)


def _reject_application_source_path(path: Path, *, label: str) -> None:
    application_root = Path(__file__).resolve().parents[3]
    lexical_path = Path(os.path.abspath(path))
    resolved_path = path.resolve(strict=False)
    for candidate in (lexical_path, resolved_path):
        if (
            candidate == application_root
            or candidate.is_relative_to(application_root)
            or application_root.is_relative_to(candidate)
        ):
            raise UpdateValidationError(
                f"{label} cannot be inside the application source tree"
            )


def _reject_broad_store_root(path: Path) -> None:
    candidate = path.resolve(strict=False)
    filesystem_root = Path(candidate.anchor)
    home = Path.home().resolve()
    if candidate == filesystem_root or candidate == home or home.is_relative_to(candidate):
        raise UpdateValidationError(
            "update-store root cannot be a filesystem root, user home, or ancestor"
        )


def _require_owned_private_directory(path: Path, *, label: str) -> None:
    try:
        details = path.lstat()
    except OSError as exc:
        raise UpdateValidationError(f"{label} is missing or unreadable") from exc
    if not stat.S_ISDIR(details.st_mode) or path.is_symlink():
        raise UpdateValidationError(f"{label} must be a real directory")
    if not hasattr(os, "geteuid") or details.st_uid != os.geteuid():
        raise UpdateValidationError(f"{label} must be owned by the service user")
    if stat.S_IMODE(details.st_mode) != 0o700:
        raise UpdateValidationError(f"{label} permissions must be 0700")


def _read_store_marker(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise UpdateValidationError("update-store marker is missing or unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise UpdateValidationError("update-store marker must be a regular file")
        if not hasattr(os, "geteuid") or details.st_uid != os.geteuid():
            raise UpdateValidationError("update-store marker must be owned by the service user")
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise UpdateValidationError("update-store marker permissions must be 0600")
        if details.st_size != len(STORE_MARKER_BYTES):
            raise UpdateValidationError("update-store marker is invalid")
        value = os.read(descriptor, len(STORE_MARKER_BYTES) + 1)
        if value != STORE_MARKER_BYTES or os.read(descriptor, 1):
            raise UpdateValidationError("update-store marker is invalid")
        return value
    finally:
        os.close(descriptor)


def etag_bytes(data: bytes) -> str:
    """Return a strong SHA-256 entity tag for exact response bytes."""

    if not isinstance(data, bytes):
        raise TypeError("ETag input must be bytes")
    return f'"{hashlib.sha256(data).hexdigest()}"'


def if_none_match(header: str | None, etag: str) -> bool:
    """Apply HTTP weak comparison for a GET/HEAD If-None-Match header."""

    if header is None or not isinstance(header, str) or len(header) > 8192:
        return False
    if not re.fullmatch(r'"[0-9a-f]{64}"', etag):
        raise ValueError("etag must be produced by etag_bytes")
    expected = etag
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate == expected:
            return True
    return False


class ReleaseStore:
    """Publish and select signed immutable desktop releases."""

    def __init__(
        self,
        root: Path | None = None,
        public_key_pem: bytes | None = None,
        *,
        public_key_path: Path | None = None,
        product: str = DEFAULT_PRODUCT,
        channel: str = DEFAULT_CHANNEL,
    ):
        self.root = Path(root) if root is not None else get_release_root()
        if not self.root.is_absolute():
            raise UpdateValidationError("update-store root must be absolute")
        if self.root.is_symlink():
            raise UpdateValidationError("update-store root cannot be a symlink")
        _reject_broad_store_root(self.root)
        _reject_application_source_path(self.root, label="update-store root")
        if public_key_pem is not None and public_key_path is not None:
            raise UpdateValidationError(
                "provide public_key_pem or public_key_path, not both"
            )
        self._public_key_pem = public_key_pem
        if public_key_pem is None:
            selected_key_path = (
                Path(public_key_path)
                if public_key_path is not None
                else get_public_key_path()
            )
            if not selected_key_path.is_absolute():
                raise UpdateValidationError("public-key path must be absolute")
            _reject_application_source_path(
                selected_key_path, label="public-key path"
            )
            self._public_key_path: Path | None = selected_key_path
        else:
            self._public_key_path = None
        self.product = _require_text(product, field="expected product")
        self.channel = _require_text(channel, field="expected channel")

    @property
    def public_key_pem(self) -> bytes:
        if self._public_key_pem is None:
            if self._public_key_path is None:  # Defensive invariant.
                raise UpdateSignatureError("desktop update public key is not configured")
            self._public_key_pem = _read_private_public_key(self._public_key_path)
        _decode_public_key(self._public_key_pem)
        return self._public_key_pem

    @property
    def releases_directory(self) -> Path:
        return self.root / RELEASES_DIRECTORY

    @property
    def current_version_path(self) -> Path:
        return self.root / CURRENT_VERSION_FILENAME

    @property
    def marker_path(self) -> Path:
        return self.root / STORE_MARKER_FILENAME

    def _validate_existing_root(self, *, require_releases: bool) -> None:
        _reject_broad_store_root(self.root)
        _reject_application_source_path(self.root, label="update-store root")
        _require_owned_private_directory(self.root, label="update-store root")
        _read_store_marker(self.marker_path)
        if require_releases:
            _require_owned_private_directory(
                self.releases_directory,
                label="update-store releases directory",
            )

    def _prepare_root(self) -> None:
        _reject_broad_store_root(self.root)
        _reject_application_source_path(self.root, label="update-store root")
        created_root = False
        try:
            os.mkdir(self.root, 0o700)
            created_root = True
            os.chmod(self.root, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise UpdateValidationError("unable to create update-store root") from exc

        _require_owned_private_directory(self.root, label="update-store root")
        if not self.marker_path.exists():
            if not created_root:
                try:
                    with os.scandir(self.root) as entries:
                        has_entries = any(entries)
                except OSError as exc:
                    raise UpdateValidationError(
                        "unable to inspect unmarked update-store root"
                    ) from exc
                if has_entries:
                    raise UpdateValidationError(
                        "existing update-store root is not an initialized dedicated store"
                    )
            try:
                _write_file(self.marker_path, STORE_MARKER_BYTES)
                _fsync_directory(self.root)
            except FileExistsError:
                # Another publisher may have initialized the same empty directory.
                pass
        _read_store_marker(self.marker_path)

        try:
            os.mkdir(self.releases_directory, 0o700)
            os.chmod(self.releases_directory, 0o700)
            _fsync_directory(self.root)
        except FileExistsError:
            pass
        except OSError as exc:
            raise UpdateValidationError(
                "unable to create update-store releases directory"
            ) from exc
        _require_owned_private_directory(
            self.releases_directory,
            label="update-store releases directory",
        )

    @contextmanager
    def _lock(self) -> Iterator[None]:
        if fcntl is None:
            raise DesktopUpdateError("update publication requires POSIX file locking")
        self._prepare_root()
        lock_path = self.root / ".publish.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _release_directory(self, version: SemVer) -> Path:
        return self.releases_directory / version.text

    def _read_current_version_unlocked(self) -> SemVer | None:
        if not self.current_version_path.exists():
            return None
        exact = _read_regular_file(
            self.current_version_path,
            limit=130,
            label=CURRENT_VERSION_FILENAME,
        )
        try:
            value = exact.decode("ascii")
        except UnicodeDecodeError as exc:
            raise UpdateValidationError("current-version is not ASCII") from exc
        if not value.endswith("\n") or value.count("\n") != 1:
            raise UpdateValidationError("current-version has invalid framing")
        return SemVer.parse(value[:-1])

    def _validate_release_unlocked(
        self, version: SemVer, *, verify_installer_hash: bool = False
    ) -> StoredRelease:
        directory = self._release_directory(version)
        if self.releases_directory.is_symlink() or directory.is_symlink():
            raise UpdateValidationError("release storage contains an unsafe symlink")
        if not directory.is_dir():
            raise NoRelease(f"release {version.text} does not exist")
        manifest_bytes = _read_regular_file(
            directory / LATEST_FILENAME,
            limit=MAX_MANIFEST_BYTES,
            label=LATEST_FILENAME,
        )
        signature_bytes = _read_regular_file(
            directory / SIGNATURE_FILENAME,
            limit=MAX_SIGNATURE_BYTES,
            label=SIGNATURE_FILENAME,
        )
        summary_bytes = _read_regular_file(
            directory / SUMMARY_FILENAME,
            limit=MAX_SUMMARY_BYTES,
            label=SUMMARY_FILENAME,
        )
        manifest = validate_manifest(
            manifest_bytes,
            signature_bytes,
            self.public_key_pem,
            expected_product=self.product,
            expected_channel=self.channel,
        )
        if manifest.version.text != version.text:
            raise UpdateValidationError("release directory and manifest version differ")
        validate_summary(
            summary_bytes,
            product=self.product,
            channel=self.channel,
            version=version.text,
        )
        installer_size, installer_sha256 = _regular_file_digest(
            directory / manifest.installer.filename,
            limit=MAX_INSTALLER_BYTES,
            label="installer",
            hash_content=verify_installer_hash,
        )
        if (
            installer_size != manifest.installer.size
            or verify_installer_hash
            and installer_sha256 != manifest.installer.sha256
        ):
            raise UpdateValidationError("stored installer does not match its manifest")
        return StoredRelease(
            version=version.text,
            directory=directory,
            manifest=manifest_bytes,
            signature=signature_bytes,
            summary=summary_bytes,
            installer=manifest.installer,
        )

    def _replace_current_unlocked(self, version: SemVer) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".current-version.", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write((version.text + "\n").encode("ascii"))
                handle.flush()
                os.fsync(handle.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.current_version_path)
            _fsync_directory(self.root)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()

    def current_release(self) -> StoredRelease:
        """Return current signed metadata without locking or hashing the payload."""

        if not self.root.is_dir() or not self.current_version_path.exists():
            raise NoRelease("the update store has no current release")
        self._validate_existing_root(require_releases=True)
        current = self._read_current_version_unlocked()
        if current is None:  # The existence check and atomic read should make this rare.
            raise NoRelease("the update store has no current release")
        return self._validate_release_unlocked(current)

    def release(
        self, version: str, *, verify_installer_hash: bool = False
    ) -> StoredRelease:
        """Return any retained immutable release after strict revalidation."""

        parsed = SemVer.parse(version)
        if not self.root.is_dir() or not self.releases_directory.is_dir():
            raise NoRelease(f"release {parsed.text} does not exist")
        self._validate_existing_root(require_releases=True)
        return self._validate_release_unlocked(
            parsed, verify_installer_hash=verify_installer_hash
        )

    get_release = release

    def publish(
        self,
        *,
        manifest_bytes: bytes,
        signature_bytes: bytes,
        summary_bytes: bytes,
        installer_source: Path,
    ) -> PublicationResult:
        """Verify and publish a release, changing the current pointer last."""

        manifest = validate_manifest(
            manifest_bytes,
            signature_bytes,
            self.public_key_pem,
            expected_product=self.product,
            expected_channel=self.channel,
        )
        validate_summary(
            summary_bytes,
            product=self.product,
            channel=self.channel,
            version=manifest.version.text,
        )
        installer_source = Path(installer_source)
        if installer_source.name != manifest.installer.filename:
            raise UpdateValidationError("installer source name does not match the manifest")

        with self._lock():
            current_version = self._read_current_version_unlocked()
            current_release = (
                self._validate_release_unlocked(current_version)
                if current_version is not None
                else None
            )
            if current_version is not None:
                comparison = manifest.version._compare(current_version)
                if comparison < 0:
                    raise UpdateVersionError("normal publication cannot downgrade")
                if comparison == 0 and manifest.version.text != current_version.text:
                    raise UpdateVersionError(
                        "normal publication cannot replace an equal-precedence version"
                    )

            final_directory = self._release_directory(manifest.version)
            if final_directory.exists() or final_directory.is_symlink():
                existing = self._validate_release_unlocked(
                    manifest.version, verify_installer_hash=True
                )
                if (
                    existing.manifest != manifest_bytes
                    or existing.signature != signature_bytes
                    or existing.summary != summary_bytes
                ):
                    raise UpdateConflictError(
                        "immutable release version already has different metadata"
                    )
                source_size, source_sha256 = _regular_file_digest(
                    installer_source,
                    limit=MAX_INSTALLER_BYTES,
                    label="installer source",
                    hash_content=True,
                )
                if (
                    source_size != existing.installer.size
                    or source_sha256 != existing.installer.sha256
                ):
                    raise UpdateConflictError(
                        "immutable release version has different installer bytes"
                    )
                if current_release is not None and current_version == manifest.version:
                    return PublicationResult(existing, changed=False)
                self._replace_current_unlocked(manifest.version)
                return PublicationResult(existing, changed=True)

            stage = Path(
                tempfile.mkdtemp(prefix=".stage-", dir=self.releases_directory)
            )
            os.chmod(stage, 0o700)
            try:
                # Installer first: metadata is never committed around a missing payload.
                _copy_installer(
                    installer_source,
                    stage / manifest.installer.filename,
                    manifest.installer,
                )
                _write_file(stage / LATEST_FILENAME, manifest_bytes)
                _write_file(stage / SIGNATURE_FILENAME, signature_bytes)
                _write_file(stage / SUMMARY_FILENAME, summary_bytes)
                os.chmod(stage, 0o700)
                _fsync_directory(stage)
                os.rename(stage, final_directory)
                _fsync_directory(self.releases_directory)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)

            stored = self._validate_release_unlocked(
                manifest.version, verify_installer_hash=True
            )
            self._replace_current_unlocked(manifest.version)
            return PublicationResult(stored, changed=True)

    def publish_files(
        self,
        *,
        manifest_path: Path,
        signature_path: Path,
        summary_path: Path,
        installer_source: Path,
    ) -> PublicationResult:
        """Publish files whose metadata artifact names are part of the contract."""

        manifest_path = Path(manifest_path)
        signature_path = Path(signature_path)
        summary_path = Path(summary_path)
        if manifest_path.name != LATEST_FILENAME:
            raise UpdateValidationError(f"manifest must be named {LATEST_FILENAME}")
        if signature_path.name != SIGNATURE_FILENAME:
            raise UpdateValidationError(f"signature must be named {SIGNATURE_FILENAME}")
        if summary_path.name != SUMMARY_FILENAME:
            raise UpdateValidationError(f"summary must be named {SUMMARY_FILENAME}")
        return self.publish(
            manifest_bytes=_read_regular_file(
                manifest_path, limit=MAX_MANIFEST_BYTES, label=LATEST_FILENAME
            ),
            signature_bytes=_read_regular_file(
                signature_path,
                limit=MAX_SIGNATURE_BYTES,
                label=SIGNATURE_FILENAME,
            ),
            summary_bytes=_read_regular_file(
                summary_path, limit=MAX_SUMMARY_BYTES, label=SUMMARY_FILENAME
            ),
            installer_source=installer_source,
        )

    def rollback(self, target_version: str) -> StoredRelease:
        """Atomically point to an older, fully revalidated immutable release."""

        target = SemVer.parse(target_version)
        with self._lock():
            current = self._read_current_version_unlocked()
            if current is None:
                raise UpdateVersionError("cannot roll back an empty update store")
            release = self._validate_release_unlocked(
                target, verify_installer_hash=True
            )
            if not target < current:
                raise UpdateVersionError("rollback target must be older than current")
            self._replace_current_unlocked(target)
            return release
