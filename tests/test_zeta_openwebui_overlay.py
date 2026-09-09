import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


INSTALLER_PATH = (
    Path(__file__).parents[1] / "deploy" / "zeta-openwebui" / "install.py"
)
SPEC = importlib.util.spec_from_file_location("zeta_openwebui_installer", INSTALLER_PATH)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(installer)
frontend = installer.load_frontend_overlay()


MINIMAL_MAIN = """from open_webui.routers import (
    memories,
    models,
    knowledge,
)
app.include_router(models.router, prefix='/api/v1/models', tags=['models'])

@app.get('/api/v1/models/status')
async def get_model_runtime_status(
    request: Request,
    user=Depends(get_verified_user),
):
    data = []
    seen_model_ids = set()
    return JSONResponse(
        content={'data': data}
    )


@app.get('/api/models')
@app.get('/api/v1/models')
async def get_models():
    pass
"""

MINIMAL_OPENAI = """import json
from typing import Optional
from fastapi import HTTPException
from open_webui.internal.db import get_session

from open_webui.models.models import Models

async def get_filtered_models(models, user, db=None):
    return models.get('data', [])

async def get_models(request, url_idx=None, user=None):
    models = {'data': []}
    if user.role == 'user' and not BYPASS_MODEL_ACCESS_CONTROL:
        models['data'] = await get_filtered_models(models, user)

    return models

async def generate_chat_completion(request, form_data, user=None):
    model_id = form_data.get('model')
    model_info = Models.get_model_by_id(model_id)
    return model_info

async def embeddings(request, form_data, user):
    model_id = form_data.get('model')
    # Check if model is already in app state cache to avoid expensive get_all_models() call
    models = request.app.state.OPENAI_MODELS
    return models

async def _require_responses_model_access(model: Optional[dict], user: UserModel) -> None:
    \"\"\"Apply the same model visibility rules used by the OpenAI model list.\"\"\"
    if model is None:
        raise HTTPException(status_code=404, detail='Model not found')

    if user.role == 'user' and not BYPASS_MODEL_ACCESS_CONTROL:
        allowed_models = await get_filtered_models({'data': [model]}, user)
        if not allowed_models.get('data'):
            raise HTTPException(status_code=403, detail='Model not found')

async def get_context_token_count(request, user=None):
    payload = await request.json()
    model_id = payload.get('model') if isinstance(payload, dict) else None
    messages = payload.get('messages') if isinstance(payload, dict) else None
    if not model_id or not isinstance(messages, list):
        raise HTTPException(status_code=400, detail='Model and messages are required')

    models = request.app.state.OPENAI_MODELS
    return models

async def proxy(path, request, user=None):
    payload = await request.json()
    idx = 0
    model_id = payload.get('model') if isinstance(payload, dict) else None
    if model_id:
        models = request.app.state.OPENAI_MODELS
    return idx

async def speech(request, user=None):
    try:
        body = await request.body()
        name = hashlib.sha256(body).hexdigest()
        return name
    except Exception:
        raise
"""

MINIMAL_MODEL_EDITOR = (
    "<script lang=\"ts\">\n"
    + frontend.IMPORT_ANCHOR
    + "\n"
    + frontend.STATE_ANCHOR
    + "\nconst submitHandler = async () => {\n"
    + frontend.SUBMIT_ANCHOR
    + "}\n"
    + frontend.MERGE_ANCHOR
    + "};\n"
    + "onMount(() => {\nif (model) {\n"
    + frontend.LOAD_ANCHOR
    + "});\n</script>\n"
    + frontend.UI_ANCHOR
    + "\n\t\t\t\t\t\t\t</div>\n\t\t\t\t\t\t</div>\n\t\t\t\t\t</div>\n"
)


def create_install_tree(directory: str):
    root = Path(directory) / "openwebui"
    backend = root / "backend"
    main_path = backend / "open_webui" / "main.py"
    openai_path = backend / "open_webui" / "routers" / "openai.py"
    model_editor_path = (
        root / "src" / "lib" / "components" / "workspace" / "Models" / "ModelEditor.svelte"
    )
    main_path.parent.mkdir(parents=True)
    openai_path.parent.mkdir(parents=True)
    model_editor_path.parent.mkdir(parents=True)
    main_path.write_text(MINIMAL_MAIN)
    openai_path.write_text(MINIMAL_OPENAI)
    model_editor_path.write_text(MINIMAL_MODEL_EDITOR)
    return root, backend, main_path, openai_path, model_editor_path


class TestOverlayInstaller(unittest.TestCase):
    def test_transform_is_guarded_and_idempotent(self):
        digest = hashlib.sha256(MINIMAL_MAIN.encode()).hexdigest()
        with patch.object(installer, "EXPECTED_MAIN_SHA256", digest):
            transformed = installer.transform_main(MINIMAL_MAIN, digest)
            repeated = installer.transform_main(
                transformed, hashlib.sha256(transformed.encode()).hexdigest()
            )

        self.assertEqual(transformed, repeated)
        self.assertIn(installer.ROUTER_MARKER, transformed)
        self.assertIn(installer.STATUS_MARKER, transformed)
        compile(transformed, "main.py", "exec")

    def test_current_installed_status_block_upgrades_safely(self):
        digest = hashlib.sha256(MINIMAL_MAIN.encode()).hexdigest()
        with patch.object(installer, "EXPECTED_MAIN_SHA256", digest):
            current = installer.transform_main(MINIMAL_MAIN, digest)
        current = current.replace(installer.STATUS_APPEND, installer.OLD_STATUS_APPEND)

        upgraded = installer.transform_main(
            current, hashlib.sha256(current.encode()).hexdigest()
        )

        self.assertIn(installer.STATUS_APPEND, upgraded)
        self.assertNotIn(installer.OLD_STATUS_APPEND, upgraded)
        compile(upgraded, "main.py", "exec")

    def test_openai_transform_is_guarded_complete_and_idempotent(self):
        digest = hashlib.sha256(MINIMAL_OPENAI.encode()).hexdigest()
        with patch.object(installer, "EXPECTED_OPENAI_SHA256", digest):
            transformed = installer.transform_openai(MINIMAL_OPENAI, digest)
            repeated = installer.transform_openai(
                transformed, hashlib.sha256(transformed.encode()).hexdigest()
            )

        self.assertEqual(transformed, repeated)
        self.assertIn(installer.OPENAI_ENABLEMENT_MARKER, transformed)
        self.assertIn("_filter_disabled_registered_models(models)", transformed)
        self.assertIn("_require_registered_model_enabled(model_id)", transformed)
        self.assertIn("if not allowed_models:", transformed)
        compile(transformed, "openai.py", "exec")

    def test_unknown_main_source_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "differs from the reviewed base"):
            installer.transform_main(MINIMAL_MAIN, "0" * 64)

    def test_unknown_openai_source_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "differs from the reviewed base"):
            installer.transform_openai(MINIMAL_OPENAI, "0" * 64)

    def test_partial_or_corrupted_installed_overlay_is_refused(self):
        digest = hashlib.sha256(MINIMAL_MAIN.encode()).hexdigest()
        with patch.object(installer, "EXPECTED_MAIN_SHA256", digest):
            transformed = installer.transform_main(MINIMAL_MAIN, digest)
        corrupted = transformed.replace(
            "app.include_router(zeta_model_catalog.router, prefix='/api/v1/models', tags=['models'])\n",
            "",
        )

        with self.assertRaisesRegex(RuntimeError, "installation is incomplete"):
            installer.transform_main(
                corrupted, hashlib.sha256(corrupted.encode()).hexdigest()
            )

    def test_partial_or_corrupted_openai_overlay_is_refused(self):
        digest = hashlib.sha256(MINIMAL_OPENAI.encode()).hexdigest()
        with patch.object(installer, "EXPECTED_OPENAI_SHA256", digest):
            transformed = installer.transform_openai(MINIMAL_OPENAI, digest)
        corrupted = transformed.replace(
            "    _require_registered_model_enabled(model_id)\n"
            "    # END ZETA MODEL ENABLEMENT CHAT GATE\n",
            "",
            1,
        )

        with self.assertRaisesRegex(RuntimeError, "installation is incomplete"):
            installer.transform_openai(
                corrupted, hashlib.sha256(corrupted.encode()).hexdigest()
            )

    def test_check_mode_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root, backend, main_path, openai_path, model_editor_path = create_install_tree(
                directory
            )
            digest = hashlib.sha256(MINIMAL_MAIN.encode()).hexdigest()
            openai_digest = hashlib.sha256(MINIMAL_OPENAI.encode()).hexdigest()
            model_editor_digest = hashlib.sha256(MINIMAL_MODEL_EDITOR.encode()).hexdigest()
            before = main_path.read_bytes()
            openai_before = openai_path.read_bytes()
            model_editor_before = model_editor_path.read_bytes()

            with patch.object(installer, "EXPECTED_MAIN_SHA256", digest), patch.object(
                installer, "EXPECTED_OPENAI_SHA256", openai_digest
            ), patch.object(
                frontend, "EXPECTED_MODEL_EDITOR_SHA256", model_editor_digest
            ), patch.object(
                installer, "load_frontend_overlay", return_value=frontend
            ):
                backup, changed = installer.install(backend, check_only=True)

            self.assertIsNone(backup)
            self.assertTrue(changed)
            self.assertEqual(main_path.read_bytes(), before)
            self.assertEqual(openai_path.read_bytes(), openai_before)
            self.assertEqual(model_editor_path.read_bytes(), model_editor_before)
            self.assertFalse((root / "src" / "lib" / "utils" / "zetaModelCatalog.ts").exists())
            self.assertFalse((backend / ".zeta-backups").exists())

    def test_install_patches_and_backs_up_openai_and_existing_status_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root, backend, main_path, openai_path, model_editor_path = create_install_tree(
                directory
            )
            digest = hashlib.sha256(MINIMAL_MAIN.encode()).hexdigest()
            openai_digest = hashlib.sha256(MINIMAL_OPENAI.encode()).hexdigest()
            model_editor_digest = hashlib.sha256(MINIMAL_MODEL_EDITOR.encode()).hexdigest()

            with patch.object(installer, "EXPECTED_MAIN_SHA256", digest), patch.object(
                installer, "EXPECTED_OPENAI_SHA256", openai_digest
            ), patch.object(
                frontend, "EXPECTED_MODEL_EDITOR_SHA256", model_editor_digest
            ), patch.object(
                installer, "load_frontend_overlay", return_value=frontend
            ):
                backup, changed = installer.install(backend, check_only=False)

            transformed = main_path.read_text()
            self.assertTrue(changed)
            self.assertIsNotNone(backup)
            self.assertIn(installer.OPENAI_ENABLEMENT_MARKER, openai_path.read_text())
            self.assertIn("seen_model_ids = set()", transformed)
            self.assertIn("content={'data': data}", transformed)
            self.assertIn(installer.STATUS_APPEND, transformed)
            self.assertIn(frontend.UI_MARKER, model_editor_path.read_text())
            self.assertTrue((root / "src" / "lib" / "utils" / "zetaModelCatalog.ts").is_file())
            self.assertEqual((backup / "main.py").read_text(), MINIMAL_MAIN)
            self.assertEqual(
                (backup / "open_webui" / "routers" / "openai.py").read_text(),
                MINIMAL_OPENAI,
            )
            self.assertEqual(
                (
                    backup
                    / "_frontend"
                    / "src"
                    / "lib"
                    / "components"
                    / "workspace"
                    / "Models"
                    / "ModelEditor.svelte"
                ).read_text(),
                MINIMAL_MODEL_EDITOR,
            )

            with patch.object(installer, "load_frontend_overlay", return_value=frontend):
                repeated_backup, repeated_changed = installer.install(backend, check_only=False)
            self.assertIsNone(repeated_backup)
            self.assertFalse(repeated_changed)


if __name__ == "__main__":
    unittest.main()
