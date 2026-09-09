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

    def test_unknown_main_source_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "differs from the reviewed base"):
            installer.transform_main(MINIMAL_MAIN, "0" * 64)

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

    def test_check_mode_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = Path(directory)
            main_path = backend / "open_webui" / "main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text(MINIMAL_MAIN)
            digest = hashlib.sha256(MINIMAL_MAIN.encode()).hexdigest()
            before = main_path.read_bytes()

            with patch.object(installer, "EXPECTED_MAIN_SHA256", digest):
                backup, changed = installer.install(backend, check_only=True)

            self.assertIsNone(backup)
            self.assertTrue(changed)
            self.assertEqual(main_path.read_bytes(), before)
            self.assertFalse((backend / ".zeta-backups").exists())

    def test_install_preserves_openai_router_and_existing_status_body(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = Path(directory)
            main_path = backend / "open_webui" / "main.py"
            openai_path = backend / "open_webui" / "routers" / "openai.py"
            main_path.parent.mkdir(parents=True)
            openai_path.parent.mkdir(parents=True)
            main_path.write_text(MINIMAL_MAIN)
            openai_source = b"# /openai/models and Responses routing are untouched\n"
            openai_path.write_bytes(openai_source)
            digest = hashlib.sha256(MINIMAL_MAIN.encode()).hexdigest()

            with patch.object(installer, "EXPECTED_MAIN_SHA256", digest):
                backup, changed = installer.install(backend, check_only=False)

            transformed = main_path.read_text()
            self.assertTrue(changed)
            self.assertIsNotNone(backup)
            self.assertEqual(openai_path.read_bytes(), openai_source)
            self.assertIn("seen_model_ids = set()", transformed)
            self.assertIn("content={'data': data}", transformed)
            self.assertIn(installer.STATUS_APPEND, transformed)
            self.assertEqual((backup / "main.py").read_text(), MINIMAL_MAIN)


if __name__ == "__main__":
    unittest.main()
