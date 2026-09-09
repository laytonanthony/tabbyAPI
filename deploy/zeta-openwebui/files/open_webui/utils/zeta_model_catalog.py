"""Pure helpers for Zeta's authenticated desktop model catalogue.

The catalogue is deliberately a projection of OpenWebUI model records.  It
never serializes provider configuration, model parameters, access grants, or
other internal state.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
DEFAULT_CONTEXT_WINDOW = 32_768
MIN_CONTEXT_WINDOW = 1_024
MAX_CONTEXT_WINDOW = 4_194_304
DEFAULT_PRIORITY = 10_000
MAX_PRIORITY = 1_000_000
MAX_DISPLAY_NAME_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 2_000

REASONING_LEVELS = ("low", "medium", "high", "xhigh")
MODALITIES = ("text", "image", "audio")
CAPABILITY_NAMES = (
    "tools",
    "parallel_tools",
    "images",
    "web_search",
    "reasoning",
)
VISIBILITIES = ("list", "hidden")

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SIMPLE_CONTEXT_RE = re.compile(r"^(?P<value>[1-9][0-9]*)(?P<suffix>[kKmM]?)$")
_IPV4_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![A-Za-z0-9])"
)
_IPV6_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![A-Za-z0-9:])"
)
_UNSAFE_PUBLIC_TEXT_RE = re.compile(
    r"(?ix)"
    r"(?:[A-Za-z][A-Za-z0-9+.-]*://|"
    r"\blocalhost\b|\b127(?:\.[0-9]{1,3}){3}\b|"
    r"\b10(?:\.[0-9]{1,3}){3}\b|"
    r"\b192\.168(?:\.[0-9]{1,3}){2}\b|"
    r"\b172\.(?:1[6-9]|2[0-9]|3[01])(?:\.[0-9]{1,3}){2}\b|"
    r"\b169\.254(?:\.[0-9]{1,3}){2}\b|"
    r"\b0\.0\.0\.0\b|\[[0-9A-Fa-f:]+\](?::[0-9]{1,5})?|"
    r"\b[A-Za-z0-9.-]+\.(?:internal|local)(?::[0-9]{1,5})?\b|"
    r"(?:^|[\s=:('\"])/(?:home|media|etc|var|opt|srv)/|"
    r"(?:^|\s)[A-Za-z]:[\\/]|"
    r"\bsk-[A-Za-z0-9_-]{8,}\b|"
    r"\bAIza[A-Za-z0-9_-]{16,}\b|"
    r"\bgh[pousr]_[A-Za-z0-9_]{12,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{12,}\b|"
    r"\bAKIA[A-Z0-9]{16}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{10,})?\b|"
    r"\bbearer\s+[A-Za-z0-9._-]{8,}\b|"
    r"\b(?:authorization|api[_ -]?key|access[_ -]?token|secret)\s*[:=])"
)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def catalogue_config(model: Any) -> dict[str, Any]:
    meta = _as_dict(_field(model, "meta", {}))
    value = meta.get("zeta_catalog")
    return dict(value) if isinstance(value, Mapping) else {}


def _ip_addresses(value: str):
    for pattern in (_IPV4_CANDIDATE_RE, _IPV6_CANDIDATE_RE):
        for match in pattern.finditer(value):
            candidate = match.group(0)
            try:
                yield ipaddress.ip_address(candidate)
            except ValueError:
                continue


def _contains_non_global_ip(value: str) -> bool:
    return any(not address.is_global for address in _ip_addresses(value))


def _contains_ip_address(value: str) -> bool:
    return next(_ip_addresses(value), None) is not None


def is_safe_model_id(value: Any) -> bool:
    if not isinstance(value, str) or value != value.strip():
        return False
    if not _MODEL_ID_RE.fullmatch(value):
        return False
    if "://" in value or value.startswith("/") or "\\" in value:
        return False
    if re.match(r"^[A-Za-z]:", value):
        return False
    if any(segment in {"", ".", ".."} for segment in value.split("/")):
        return False
    if _UNSAFE_PUBLIC_TEXT_RE.search(value) or _contains_non_global_ip(value):
        return False
    return True


def is_catalogue_member(model: Any, live_model_ids: Iterable[str] = ()) -> bool:
    """Return whether a registry row belongs to the Responses catalogue.

    An exactly matched live model is routable by Responses and therefore wins
    over registry lifecycle/opt-in metadata. Explicitly enabled, active base
    rows persist while offline.
    """

    model_id = _field(model, "id")
    if not is_safe_model_id(model_id):
        return False
    live_lookup = {
        candidate for candidate in live_model_ids if isinstance(candidate, str)
    }
    if model_id in live_lookup:
        return True

    if _field(model, "is_active", False) is not True:
        return False
    if _field(model, "base_model_id") is not None:
        return False
    return catalogue_config(model).get("enabled") is True


def _clean_public_text(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value or len(value) > maximum:
        return None
    if _UNSAFE_PUBLIC_TEXT_RE.search(value) or _contains_ip_address(value):
        return None
    if re.search(r"\b[A-Za-z0-9_+/=-]{48,}\b", value):
        return None
    return value


def _bounded_integer(value: Any, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < minimum or value > maximum:
        return None
    return value


def _context_window(config: Mapping[str, Any], meta: Mapping[str, Any]) -> int:
    explicit = _bounded_integer(
        config.get("context_window"), MIN_CONTEXT_WINDOW, MAX_CONTEXT_WINDOW
    )
    if explicit is not None:
        return explicit

    legacy = meta.get("context")
    if isinstance(legacy, int):
        parsed = _bounded_integer(legacy, MIN_CONTEXT_WINDOW, MAX_CONTEXT_WINDOW)
        return parsed if parsed is not None else DEFAULT_CONTEXT_WINDOW
    if not isinstance(legacy, str):
        return DEFAULT_CONTEXT_WINDOW

    match = _SIMPLE_CONTEXT_RE.fullmatch(legacy.strip())
    if not match:
        return DEFAULT_CONTEXT_WINDOW
    multiplier = {"": 1, "k": 1_024, "m": 1_048_576}[
        match.group("suffix").casefold()
    ]
    parsed = int(match.group("value")) * multiplier
    return (
        parsed
        if MIN_CONTEXT_WINDOW <= parsed <= MAX_CONTEXT_WINDOW
        else DEFAULT_CONTEXT_WINDOW
    )


def _reasoning(config: Mapping[str, Any]) -> tuple[str, list[str]]:
    raw_levels = config.get("supported_reasoning_levels")
    selected = set()
    if isinstance(raw_levels, Sequence) and not isinstance(raw_levels, (str, bytes)):
        for value in raw_levels:
            if isinstance(value, str) and value in REASONING_LEVELS:
                selected.add(value)
    levels = [level for level in REASONING_LEVELS if level in selected]
    if not levels:
        levels = ["medium"]

    default = config.get("default_reasoning_level")
    if default not in levels:
        default = "medium" if "medium" in levels else levels[0]
    return default, levels


def _modalities(config: Mapping[str, Any]) -> list[str]:
    raw = config.get("input_modalities")
    selected = set()
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for value in raw:
            if isinstance(value, str) and value in MODALITIES:
                selected.add(value)
    # Every model exposed by this Responses catalogue must accept text.
    selected.add("text")
    return [value for value in MODALITIES if value in selected]


def _capabilities(config: Mapping[str, Any]) -> dict[str, bool]:
    raw = config.get("capabilities")
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        name: raw.get(name) if type(raw.get(name)) is bool else False
        for name in CAPABILITY_NAMES
    }


def normalise_catalogue_model(
    model: Any,
    *,
    model_order: Mapping[str, int] | None = None,
) -> dict[str, Any] | None:
    """Project one model record onto the public version-one schema."""

    model_id = _field(model, "id")
    if not is_safe_model_id(model_id):
        return None

    meta = _as_dict(_field(model, "meta", {}))
    config = catalogue_config(model)
    display_name = _clean_public_text(
        config.get("display_name"), MAX_DISPLAY_NAME_LENGTH
    ) or _clean_public_text(_field(model, "name"), MAX_DISPLAY_NAME_LENGTH)
    if display_name is None:
        display_name = model_id

    # Description is optional. Only the deliberately public catalogue field is
    # projected; generic model metadata may contain internal operator notes.
    description = _clean_public_text(config.get("description"), MAX_DESCRIPTION_LENGTH)

    priority = _bounded_integer(config.get("priority"), 0, MAX_PRIORITY)
    if priority is None:
        priority = (model_order or {}).get(model_id, DEFAULT_PRIORITY)
    validated_priority = _bounded_integer(priority, 0, MAX_PRIORITY)
    priority = (
        validated_priority
        if validated_priority is not None
        else DEFAULT_PRIORITY
    )

    visibility = config.get("visibility")
    if visibility not in VISIBILITIES:
        visibility = "list"

    default_reasoning, supported_reasoning = _reasoning(config)
    return {
        "id": model_id,
        "display_name": display_name,
        "description": description,
        "priority": priority,
        "visibility": visibility,
        "context_window": _context_window(config, meta),
        "default_reasoning_level": default_reasoning,
        "supported_reasoning_levels": supported_reasoning,
        "input_modalities": _modalities(config),
        "capabilities": _capabilities(config),
    }


def build_catalogue_models(
    models: Iterable[Any],
    *,
    live_model_ids: Iterable[str] = (),
    model_order_list: Sequence[str] = (),
) -> list[dict[str, Any]]:
    live_model_ids = tuple(live_model_ids)
    model_order = {
        model_id: index
        for index, model_id in enumerate(model_order_list)
        if isinstance(model_id, str)
    }
    result = []
    seen = set()
    for model in models:
        if not is_catalogue_member(model, live_model_ids):
            continue
        normalised = normalise_catalogue_model(model, model_order=model_order)
        if normalised is None:
            continue
        key = normalised["id"]
        if key in seen:
            continue
        seen.add(key)
        result.append(normalised)

    result.sort(
        key=lambda model: (
            model["priority"],
            model["display_name"].casefold(),
            model["id"],
        )
    )
    return result


def filter_authorised_models(
    models: Iterable[Any],
    *,
    user_id: str,
    user_role: str,
    bypass_model_access_control: bool,
    granted_model_ids: Iterable[str] = (),
) -> list[Any]:
    """Apply the same owner/read-grant boundary used by Responses routing."""

    models = list(models)
    if user_role == "admin" or bypass_model_access_control:
        return models
    granted = set(granted_model_ids)
    return [
        model
        for model in models
        if _field(model, "user_id") == user_id
        or _field(model, "id") in granted
    ]


def merge_catalogue_status(
    status_items: Sequence[Mapping[str, Any]],
    catalogue_models: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Append explicit offline state without changing existing status items."""

    result = [dict(item) for item in status_items]
    represented_ids = {
        item.get("id")
        for item in result
        if isinstance(item.get("id"), str) and item.get("id")
    }
    for catalogue_model in catalogue_models:
        model_id = catalogue_model.get("id")
        if not isinstance(model_id, str) or model_id in represented_ids:
            continue
        represented_ids.add(model_id)
        result.append(
            {
                "id": model_id,
                "state": "offline",
                "online": False,
                "loaded": False,
                "source": "unknown",
            }
        )
    return result


def catalogue_revision(models: Sequence[Mapping[str, Any]]) -> str:
    canonical = json.dumps(
        {"schema_version": SCHEMA_VERSION, "models": list(models)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def catalogue_etag(revision: str) -> str:
    return f'W/"{revision}"'


def if_none_match_matches(header_value: str | None, revision: str) -> bool:
    if not header_value:
        return False
    for raw_tag in header_value.split(","):
        tag = raw_tag.strip()
        if tag == "*":
            return True
        if tag[:2].casefold() == "w/":
            tag = tag[2:].strip()
        if len(tag) >= 2 and tag[0] == '"' and tag[-1] == '"':
            tag = tag[1:-1]
        if tag == revision:
            return True
    return False


def generated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def response_headers(revision: str) -> dict[str, str]:
    return {
        "ETag": catalogue_etag(revision),
        "Cache-Control": "private, max-age=5, must-revalidate",
        "Vary": "Authorization, Cookie",
    }
