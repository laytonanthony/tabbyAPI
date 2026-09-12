#!/usr/bin/env python3
"""Install the versioned Zeta OpenWebUI overlay.

The deployed OpenWebUI tree is not itself a Git checkout.  This installer
therefore validates reviewed base files, creates a timestamped rollback copy,
and writes only the overlay modules plus small marked integrations in
``open_webui/main.py``, ``open_webui/routers/openai.py``,
``open_webui/utils/models.py``, and the reviewed frontend model editor.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
from datetime import datetime, timezone

import deepseek_routing_patch


EXPECTED_MAIN_SHA256 = "fa65867e7d07ccb133cb0e0f22b763762512289601371f56eedb0fdbf05f715f"
EXPECTED_OPENAI_SHA256 = "66ae2ff0705f711fe8dc5b6ce9d1ef07b06e8d1c1956c432ca779e0591691363"
EXPECTED_MODELS_SHA256 = {
    # Reviewed source before the emergency identity-safe removal hotfix.
    "be64923a505d5b2d2e54cac187132debf9d5bc0b3386400fbaf54a7e1bf92b39",
    # The same reviewed source with that one-line-equivalent hotfix applied.
    "58dd7d739870a97eb4df53ea73e67aaec2e6aa34806606e65237cbc1da71a8e2",
}
ROUTER_MARKER = "# BEGIN ZETA MODEL CATALOG ROUTER"
STATUS_MARKER = "# BEGIN ZETA MODEL CATALOG OFFLINE STATUS"
DESKTOP_UPDATE_ROUTER_MARKER = "# BEGIN ZETA DESKTOP UPDATE ROUTER"
DESKTOP_UPDATE_ROUTER_END_MARKER = "# END ZETA DESKTOP UPDATE ROUTER"
DESKTOP_UPDATE_IMPORT_LINE = "    zeta_desktop_updates,"
DESKTOP_UPDATE_INCLUDE_LINE = (
    "app.include_router(zeta_desktop_updates.router, "
    "prefix='/api/desktop/updates', tags=['desktop-updates'])"
)
OPENAI_ENABLEMENT_MARKER = "# BEGIN ZETA MODEL ENABLEMENT HELPERS"
MODELS_LOOKUP_MARKER = "# BEGIN ZETA EXACT MODEL OVERRIDE LOOKUPS"
MODELS_REMOVAL_MARKER = "# BEGIN ZETA IDENTITY-SAFE MODEL REMOVAL"

IMPORT_ANCHOR = """    memories,
    models,
    knowledge,
"""
IMPORT_REPLACEMENT = """    memories,
    models,
    zeta_model_catalog,
    knowledge,
"""

DESKTOP_UPDATE_IMPORT_REPLACEMENT = """    memories,
    models,
    zeta_model_catalog,
    zeta_desktop_updates,
    knowledge,
"""

INCLUDE_ANCHOR = "app.include_router(models.router, prefix='/api/v1/models', tags=['models'])"
INCLUDE_REPLACEMENT = f"""{INCLUDE_ANCHOR}
{ROUTER_MARKER}
app.include_router(zeta_model_catalog.router, prefix='/api/v1/models', tags=['models'])
# END ZETA MODEL CATALOG ROUTER"""

DESKTOP_UPDATE_INCLUDE_REPLACEMENT = f"""{INCLUDE_REPLACEMENT}
{DESKTOP_UPDATE_ROUTER_MARKER}
{DESKTOP_UPDATE_INCLUDE_LINE}
{DESKTOP_UPDATE_ROUTER_END_MARKER}"""

OLD_STATUS_APPEND = """    # BEGIN ZETA MODEL CATALOG OFFLINE STATUS
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

STATUS_APPEND = """    # BEGIN ZETA MODEL CATALOG OFFLINE STATUS
    # Keep administrative enablement separate from provider availability.
    # Disabled rows are omitted; enabled catalogue rows absent from provider
    # discovery are appended as offline.
    try:
        catalogue_models, disabled_model_ids = (
            await zeta_model_catalog.catalogue_state_for_user(
                request,
                user,
                live_model_ids={
                    str(item.get('id'))
                    for item in data
                    if isinstance(item, dict) and item.get('id')
                },
            )
        )
    except Exception as exc:
        log.error(
            'zeta_model_status_catalogue_overlay_failed error_type=%s',
            type(exc).__name__,
        )
        # Do not publish an unverified raw provider list as current status.
        return JSONResponse(
            status_code=503,
            content={'detail': 'Unable to verify model availability'},
            headers={'Cache-Control': 'no-store'},
        )

    data = zeta_model_catalog.merge_catalogue_status(
        data,
        catalogue_models,
        disabled_model_ids=disabled_model_ids,
    )
    # END ZETA MODEL CATALOG OFFLINE STATUS

"""

OPENAI_IMPORT_ANCHOR = """from open_webui.internal.db import get_session

from open_webui.models.models import Models
"""
OPENAI_IMPORT_REPLACEMENT = """from open_webui.internal.db import get_db, get_session

from open_webui.models.models import Model, Models
from open_webui.utils.zeta_model_catalog import (
    filter_inactive_model_response,
    unavailable_registered_model_ids,
)
"""

OPENAI_HELPER_ANCHOR = """async def get_filtered_models(models, user, db=None):
"""
OPENAI_HELPERS = """# BEGIN ZETA MODEL ENABLEMENT HELPERS
def _registered_model_enablement_rows():
    \"\"\"Read only fields needed to enforce the global model switch.\"\"\"
    try:
        with get_db() as db:
            return db.query(
                Model.id,
                Model.base_model_id,
                Model.is_active,
            ).all()
    except Exception as exc:
        log.error(
            'zeta_model_enablement_registry_query_failed error_type=%s',
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail='Unable to verify model availability',
        ) from None


def _disabled_registered_model_ids():
    return unavailable_registered_model_ids(_registered_model_enablement_rows())


def _require_registered_model_enabled(model_id):
    if isinstance(model_id, str) and model_id in _disabled_registered_model_ids():
        # Use the same non-enumerating response as an unknown provider model.
        raise HTTPException(status_code=404, detail='Model not found')


def _filter_disabled_registered_models(models):
    return filter_inactive_model_response(
        models,
        _registered_model_enablement_rows(),
    )
# END ZETA MODEL ENABLEMENT HELPERS


"""

OPENAI_MODELS_RETURN_ANCHOR = """    if user.role == 'user' and not BYPASS_MODEL_ACCESS_CONTROL:
        models['data'] = await get_filtered_models(models, user)

    return models
"""
OPENAI_MODELS_RETURN_REPLACEMENT = """    if user.role == 'user' and not BYPASS_MODEL_ACCESS_CONTROL:
        models['data'] = await get_filtered_models(models, user)

    # BEGIN ZETA MODEL ENABLEMENT LIST FILTER
    models = _filter_disabled_registered_models(models)
    # END ZETA MODEL ENABLEMENT LIST FILTER
    return models
"""

OPENAI_CHAT_ANCHOR = """    model_id = form_data.get('model')
    model_info = Models.get_model_by_id(model_id)
"""
OPENAI_CHAT_REPLACEMENT = """    model_id = form_data.get('model')
    # BEGIN ZETA MODEL ENABLEMENT CHAT GATE
    _require_registered_model_enabled(model_id)
    # END ZETA MODEL ENABLEMENT CHAT GATE
    model_info = Models.get_model_by_id(model_id)
"""

OPENAI_EMBEDDINGS_ANCHOR = """    model_id = form_data.get('model')
    # Check if model is already in app state cache to avoid expensive get_all_models() call
"""
OPENAI_EMBEDDINGS_REPLACEMENT = """    model_id = form_data.get('model')
    # BEGIN ZETA MODEL ENABLEMENT EMBEDDINGS GATE
    _require_registered_model_enabled(model_id)
    # END ZETA MODEL ENABLEMENT EMBEDDINGS GATE
    # Check if model is already in app state cache to avoid expensive get_all_models() call
"""

OPENAI_RESPONSES_ACCESS_ANCHOR = """async def _require_responses_model_access(model: Optional[dict], user: UserModel) -> None:
    \"\"\"Apply the same model visibility rules used by the OpenAI model list.\"\"\"
    if model is None:
        raise HTTPException(status_code=404, detail='Model not found')

    if user.role == 'user' and not BYPASS_MODEL_ACCESS_CONTROL:
        allowed_models = await get_filtered_models({'data': [model]}, user)
        if not allowed_models.get('data'):
            raise HTTPException(status_code=403, detail='Model not found')
"""
OPENAI_RESPONSES_ACCESS_REPLACEMENT = """async def _require_responses_model_access(model: Optional[dict], user: UserModel) -> None:
    \"\"\"Apply model enablement and the OpenAI model-list access rules.\"\"\"
    if model is None:
        raise HTTPException(status_code=404, detail='Model not found')

    # BEGIN ZETA MODEL ENABLEMENT RESPONSES GATE
    _require_registered_model_enabled(model.get('id'))
    # END ZETA MODEL ENABLEMENT RESPONSES GATE
    if user.role == 'user' and not BYPASS_MODEL_ACCESS_CONTROL:
        allowed_models = await get_filtered_models({'data': [model]}, user)
        # Current OpenWebUI returns a list; tolerate the historical mapping
        # shape so older local tests/extensions retain compatibility.
        if isinstance(allowed_models, dict):
            allowed_models = allowed_models.get('data', [])
        if not allowed_models:
            raise HTTPException(status_code=403, detail='Model not found')
"""

OPENAI_TOKEN_COUNT_ANCHOR = """    if not model_id or not isinstance(messages, list):
        raise HTTPException(status_code=400, detail='Model and messages are required')

    models = request.app.state.OPENAI_MODELS
"""
OPENAI_TOKEN_COUNT_REPLACEMENT = """    if not model_id or not isinstance(messages, list):
        raise HTTPException(status_code=400, detail='Model and messages are required')

    # BEGIN ZETA MODEL ENABLEMENT TOKEN COUNT GATE
    _require_registered_model_enabled(model_id)
    # END ZETA MODEL ENABLEMENT TOKEN COUNT GATE
    models = request.app.state.OPENAI_MODELS
"""

OPENAI_PROXY_ANCHOR = """    idx = 0
    model_id = payload.get('model') if isinstance(payload, dict) else None
    if model_id:
        models = request.app.state.OPENAI_MODELS
"""
OPENAI_PROXY_REPLACEMENT = """    idx = 0
    model_id = payload.get('model') if isinstance(payload, dict) else None
    if model_id:
        # BEGIN ZETA MODEL ENABLEMENT GENERIC PROXY GATE
        _require_registered_model_enabled(model_id)
        # END ZETA MODEL ENABLEMENT GENERIC PROXY GATE
        models = request.app.state.OPENAI_MODELS
"""

OPENAI_SPEECH_ANCHOR = """        body = await request.body()
        name = hashlib.sha256(body).hexdigest()
"""
OPENAI_SPEECH_REPLACEMENT = """        body = await request.body()
        # BEGIN ZETA MODEL ENABLEMENT SPEECH GATE
        try:
            speech_payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            speech_payload = None
        if isinstance(speech_payload, dict):
            _require_registered_model_enabled(speech_payload.get('model'))
        # END ZETA MODEL ENABLEMENT SPEECH GATE
        name = hashlib.sha256(body).hexdigest()
"""

OPENAI_REQUIRED_BLOCKS = {
    "enablement imports": OPENAI_IMPORT_REPLACEMENT,
    "enablement helpers": OPENAI_HELPERS,
    "model-list filter": OPENAI_MODELS_RETURN_REPLACEMENT,
    "chat gate": OPENAI_CHAT_REPLACEMENT,
    "embeddings gate": OPENAI_EMBEDDINGS_REPLACEMENT,
    "Responses gate": OPENAI_RESPONSES_ACCESS_REPLACEMENT,
    "token-count gate": OPENAI_TOKEN_COUNT_REPLACEMENT,
    "generic proxy gate": OPENAI_PROXY_REPLACEMENT,
    "speech gate": OPENAI_SPEECH_REPLACEMENT,
}

MODELS_LOOKUP_ANCHOR = """    # Single O(1) lookup: Ollama base names first, then exact IDs (exact wins).
    base_model_lookup = {}
    for model in models:
        if model.get('owned_by') == 'ollama':
            base_model_lookup.setdefault(model['id'].split(':')[0], model)
        base_model_lookup[model['id']] = model
"""
MODELS_LOOKUP_REPLACEMENT = """    # BEGIN ZETA EXACT MODEL OVERRIDE LOOKUPS
    # A direct model override/disable must match the provider ID exactly.
    # Ollama's colonless shorthand remains available only for preset bases.
    exact_model_lookup = {model['id']: model for model in models}
    base_model_lookup = dict(exact_model_lookup)
    for model in models:
        if model.get('owned_by') == 'ollama':
            base_model_lookup.setdefault(model['id'].split(':')[0], model)
    # END ZETA EXACT MODEL OVERRIDE LOOKUPS
"""

MODELS_DIRECT_LOOKUP_ANCHOR = """            model = base_model_lookup.get(custom_model.id)
"""
MODELS_DIRECT_LOOKUP_REPLACEMENT = """            model = exact_model_lookup.get(custom_model.id)
"""

MODELS_ORIGINAL_REMOVAL = """                else:
                    models.remove(model)
"""
MODELS_HOTFIX_REMOVAL = """                else:
                    # Multiple disabled registry IDs can resolve to the same
                    # Ollama base-name alias. Remove that exact provider
                    # object at most once instead of raising on a stale alias.
                    models[:] = [
                        candidate for candidate in models if candidate is not model
                    ]
"""
MODELS_REMOVAL_REPLACEMENT = """                else:
                    # BEGIN ZETA IDENTITY-SAFE MODEL REMOVAL
                    # Never remove an equal-but-distinct provider record, and
                    # make a stale/missing exact target a harmless no-op.
                    models[:] = [
                        candidate for candidate in models if candidate is not model
                    ]
                    # END ZETA IDENTITY-SAFE MODEL REMOVAL
"""

MODELS_REQUIRED_BLOCKS = {
    "exact/shorthand lookups": MODELS_LOOKUP_REPLACEMENT,
    "direct exact lookup": MODELS_DIRECT_LOOKUP_REPLACEMENT,
    "identity-safe removal": MODELS_REMOVAL_REPLACEMENT,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compile_python(path: Path) -> None:
    compile(path.read_text(encoding="utf-8"), str(path), "exec")


def load_frontend_overlay():
    """Load the sibling helper without relying on the caller's ``sys.path``."""

    path = Path(__file__).resolve().with_name("frontend_overlay.py")
    spec = importlib.util.spec_from_file_location("zeta_frontend_overlay", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load frontend overlay helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"Expected one {label} anchor, found {count}")
    return source.replace(old, new, 1)


def transform_desktop_update_router(source: str) -> str:
    """Add or validate the public desktop-update router integration."""

    if DESKTOP_UPDATE_ROUTER_MARKER in source:
        if (
            source.count(DESKTOP_UPDATE_ROUTER_MARKER) != 1
            or source.count(DESKTOP_UPDATE_ROUTER_END_MARKER) != 1
            or source.count(DESKTOP_UPDATE_IMPORT_LINE) != 1
            or source.count(DESKTOP_UPDATE_INCLUDE_LINE) != 1
        ):
            raise RuntimeError(
                "Desktop-update router integration is duplicated or incomplete"
            )
        required = {
            "desktop-update router import": DESKTOP_UPDATE_IMPORT_REPLACEMENT,
            "desktop-update router include": DESKTOP_UPDATE_INCLUDE_REPLACEMENT,
        }
        missing = [
            label for label, block in required.items() if source.count(block) != 1
        ]
        if missing:
            raise RuntimeError(
                "Desktop-update router marker exists but the installation is "
                "incomplete: " + ", ".join(missing)
            )
        spa_mount = source.find(
            "SPAStaticFiles(directory=FRONTEND_BUILD_DIR, html=True)"
        )
        router_include = source.find(DESKTOP_UPDATE_INCLUDE_REPLACEMENT)
        if spa_mount >= 0 and router_include > spa_mount:
            raise RuntimeError(
                "Desktop-update router must be registered before the root SPA mount"
            )
        return source

    if DESKTOP_UPDATE_ROUTER_END_MARKER in source or "zeta_desktop_updates" in source:
        raise RuntimeError("Partial Zeta desktop-update router installation detected")

    source = replace_once(
        source,
        IMPORT_REPLACEMENT,
        DESKTOP_UPDATE_IMPORT_REPLACEMENT,
        "desktop-update router import",
    )
    source = replace_once(
        source,
        INCLUDE_REPLACEMENT,
        DESKTOP_UPDATE_INCLUDE_REPLACEMENT,
        "desktop-update router include",
    )
    return transform_desktop_update_router(source)


def transform_main(source: str, source_hash: str) -> str:
    installed = ROUTER_MARKER in source and STATUS_MARKER in source
    if installed:
        catalogue_imports = (
            source.count(IMPORT_REPLACEMENT)
            + source.count(DESKTOP_UPDATE_IMPORT_REPLACEMENT)
        )
        if catalogue_imports != 1 or source.count(INCLUDE_REPLACEMENT) != 1:
            raise RuntimeError(
                "Catalogue markers exist but the installation is incomplete: "
                "router import/include"
            )
        if source.count(STATUS_APPEND) == 1:
            transformed = source
        elif source.count(OLD_STATUS_APPEND) == 1:
            transformed = source.replace(OLD_STATUS_APPEND, STATUS_APPEND, 1)
        else:
            raise RuntimeError(
                "Catalogue markers exist but the status overlay is unknown or incomplete"
            )
        return transform_desktop_update_router(transformed)

    if (
        ROUTER_MARKER in source
        or STATUS_MARKER in source
        or DESKTOP_UPDATE_ROUTER_MARKER in source
        or "zeta_desktop_updates" in source
    ):
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
    source = source[:status_start] + status_block + source[status_end:]
    return transform_desktop_update_router(source)


def transform_openai(source: str, source_hash: str) -> str:
    """Install fail-closed enablement gates without altering raw discovery."""

    if OPENAI_ENABLEMENT_MARKER in source:
        missing = [
            label
            for label, block in OPENAI_REQUIRED_BLOCKS.items()
            if source.count(block) != 1
        ]
        if missing:
            raise RuntimeError(
                "Model-enablement markers exist but installation is incomplete: "
                + ", ".join(missing)
            )
        return deepseek_routing_patch.transform(source)

    if "# BEGIN ZETA MODEL ENABLEMENT" in source:
        raise RuntimeError("Partial Zeta model-enablement installation detected")
    if source_hash != EXPECTED_OPENAI_SHA256:
        raise RuntimeError(
            "OpenWebUI openai.py differs from the reviewed base; refusing to patch "
            f"(expected {EXPECTED_OPENAI_SHA256}, found {source_hash})"
        )

    source = replace_once(
        source,
        OPENAI_IMPORT_ANCHOR,
        OPENAI_IMPORT_REPLACEMENT,
        "OpenAI enablement imports",
    )
    source = replace_once(
        source,
        OPENAI_HELPER_ANCHOR,
        OPENAI_HELPERS + OPENAI_HELPER_ANCHOR,
        "OpenAI helper",
    )
    for label, anchor, replacement in (
        ("OpenAI model-list return", OPENAI_MODELS_RETURN_ANCHOR, OPENAI_MODELS_RETURN_REPLACEMENT),
        ("OpenAI chat gate", OPENAI_CHAT_ANCHOR, OPENAI_CHAT_REPLACEMENT),
        ("OpenAI embeddings gate", OPENAI_EMBEDDINGS_ANCHOR, OPENAI_EMBEDDINGS_REPLACEMENT),
        ("OpenAI Responses gate", OPENAI_RESPONSES_ACCESS_ANCHOR, OPENAI_RESPONSES_ACCESS_REPLACEMENT),
        ("OpenAI token-count gate", OPENAI_TOKEN_COUNT_ANCHOR, OPENAI_TOKEN_COUNT_REPLACEMENT),
        ("OpenAI generic proxy gate", OPENAI_PROXY_ANCHOR, OPENAI_PROXY_REPLACEMENT),
        ("OpenAI speech gate", OPENAI_SPEECH_ANCHOR, OPENAI_SPEECH_REPLACEMENT),
    ):
        source = replace_once(source, anchor, replacement, label)
    return deepseek_routing_patch.transform(source)


def transform_models(source: str, source_hash: str) -> str:
    """Separate exact overrides from Ollama shorthand and remove safely."""

    markers = (MODELS_LOOKUP_MARKER, MODELS_REMOVAL_MARKER)
    present = [marker in source for marker in markers]
    if all(present):
        missing = [
            label
            for label, block in MODELS_REQUIRED_BLOCKS.items()
            if source.count(block) != 1
        ]
        if missing:
            raise RuntimeError(
                "Model-merge markers exist but installation is incomplete: "
                + ", ".join(missing)
            )
        return source

    if any(present):
        raise RuntimeError("Partial Zeta model-merge installation detected")
    if source_hash not in EXPECTED_MODELS_SHA256:
        expected = ", ".join(sorted(EXPECTED_MODELS_SHA256))
        raise RuntimeError(
            "OpenWebUI utils/models.py differs from the reviewed bases; refusing "
            f"to patch (expected one of {expected}, found {source_hash})"
        )

    source = replace_once(
        source,
        MODELS_LOOKUP_ANCHOR,
        MODELS_LOOKUP_REPLACEMENT,
        "model lookup",
    )
    source = replace_once(
        source,
        MODELS_DIRECT_LOOKUP_ANCHOR,
        MODELS_DIRECT_LOOKUP_REPLACEMENT,
        "direct exact model lookup",
    )
    if source.count(MODELS_ORIGINAL_REMOVAL) == 1:
        removal = MODELS_ORIGINAL_REMOVAL
    elif source.count(MODELS_HOTFIX_REMOVAL) == 1:
        removal = MODELS_HOTFIX_REMOVAL
    else:
        raise RuntimeError("Unable to locate the reviewed disabled-model removal")
    return replace_once(
        source,
        removal,
        MODELS_REMOVAL_REPLACEMENT,
        "identity-safe model removal",
    )


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
    openwebui_root = backend.parent
    main_path = backend / "open_webui" / "main.py"
    if not main_path.is_file():
        raise RuntimeError(f"OpenWebUI main.py not found beneath {backend}")
    openai_path = backend / "open_webui" / "routers" / "openai.py"
    if not openai_path.is_file():
        raise RuntimeError(f"OpenWebUI openai.py not found beneath {backend}")
    models_path = backend / "open_webui" / "utils" / "models.py"
    if not models_path.is_file():
        raise RuntimeError(f"OpenWebUI utils/models.py not found beneath {backend}")
    model_editor_path = (
        openwebui_root
        / "src"
        / "lib"
        / "components"
        / "workspace"
        / "Models"
        / "ModelEditor.svelte"
    )
    if not model_editor_path.is_file():
        raise RuntimeError(
            f"OpenWebUI ModelEditor.svelte not found beneath {openwebui_root}"
        )

    overlay_directory = Path(__file__).resolve().parent
    backend_overlay_root = overlay_directory / "files" / "open_webui"
    backend_sources = {
        backend_overlay_root / "utils" / "zeta_model_catalog.py": (
            backend / "open_webui" / "utils" / "zeta_model_catalog.py"
        ),
        backend_overlay_root / "utils" / "zeta_desktop_updates.py": (
            backend / "open_webui" / "utils" / "zeta_desktop_updates.py"
        ),
        backend_overlay_root / "routers" / "zeta_model_catalog.py": (
            backend / "open_webui" / "routers" / "zeta_model_catalog.py"
        ),
        backend_overlay_root / "routers" / "zeta_desktop_updates.py": (
            backend / "open_webui" / "routers" / "zeta_desktop_updates.py"
        ),
    }
    for source in backend_sources:
        compile_python(source)
    compile_python(overlay_directory / "publish_desktop_release.py")
    frontend_overlay = load_frontend_overlay()
    frontend_sources = frontend_overlay.frontend_sources(
        overlay_directory, openwebui_root
    )
    sources = {**backend_sources, **frontend_sources}

    original = main_path.read_text(encoding="utf-8")
    transformed = transform_main(original, sha256(main_path))
    compile(transformed, str(main_path), "exec")
    original_openai = openai_path.read_text(encoding="utf-8")
    transformed_openai = transform_openai(original_openai, sha256(openai_path))
    compile(transformed_openai, str(openai_path), "exec")
    original_models = models_path.read_text(encoding="utf-8")
    transformed_models = transform_models(original_models, sha256(models_path))
    compile(transformed_models, str(models_path), "exec")
    original_model_editor = model_editor_path.read_text(encoding="utf-8")
    transformed_model_editor = frontend_overlay.transform_model_editor(
        original_model_editor,
        sha256(model_editor_path),
    )

    targets_changed = (
        transformed != original
        or transformed_openai != original_openai
        or transformed_models != original_models
        or transformed_model_editor != original_model_editor
        or any(
            not target.exists() or source.read_bytes() != target.read_bytes()
            for source, target in sources.items()
        )
    )
    if check_only or not targets_changed:
        return None, targets_changed

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backend / ".zeta-backups" / f"model-catalog-{timestamp}"
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(main_path, backup / "main.py")
    backup_openai = backup / openai_path.relative_to(backend)
    backup_openai.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(openai_path, backup_openai)
    backup_models = backup / models_path.relative_to(backend)
    backup_models.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(models_path, backup_models)
    backup_model_editor = (
        backup / "_frontend" / model_editor_path.relative_to(openwebui_root)
    )
    backup_model_editor.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(model_editor_path, backup_model_editor)
    for _, target in sources.items():
        if target.exists():
            if target.is_relative_to(backend):
                relative = target.relative_to(backend)
            elif target.is_relative_to(openwebui_root):
                relative = Path("_frontend") / target.relative_to(openwebui_root)
            else:
                raise RuntimeError(f"Overlay target is outside OpenWebUI: {target}")
            backup_target = backup / relative
            backup_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup_target)

    atomic_write(main_path, transformed.encode("utf-8"), main_path.stat().st_mode)
    atomic_write(
        openai_path,
        transformed_openai.encode("utf-8"),
        openai_path.stat().st_mode,
    )
    atomic_write(
        models_path,
        transformed_models.encode("utf-8"),
        models_path.stat().st_mode,
    )
    atomic_write(
        model_editor_path,
        transformed_model_editor.encode("utf-8"),
        model_editor_path.stat().st_mode,
    )
    for source, target in sources.items():
        mode = target.stat().st_mode if target.exists() else source.stat().st_mode
        atomic_write(target, source.read_bytes(), mode)

    compile_python(main_path)
    compile_python(openai_path)
    compile_python(models_path)
    for target in backend_sources.values():
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
