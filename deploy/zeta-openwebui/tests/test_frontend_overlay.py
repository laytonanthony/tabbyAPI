import hashlib
import importlib.util
from pathlib import Path
import unittest


OVERLAY_PATH = Path(__file__).parents[1] / "frontend_overlay.py"
SPEC = importlib.util.spec_from_file_location("zeta_frontend_overlay", OVERLAY_PATH)
frontend = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(frontend)

RUNTIME_MODEL_EDITOR = Path(
    "/media/anthony/UltraGPT/Zeta WEBUI/Zeta WEBUI/"
    "src/lib/components/workspace/Models/ModelEditor.svelte"
)


class TestFrontendOverlay(unittest.TestCase):
    @unittest.skipUnless(RUNTIME_MODEL_EDITOR.is_file(), "reviewed OpenWebUI source unavailable")
    def test_reviewed_model_editor_transforms_and_is_idempotent(self):
        source = RUNTIME_MODEL_EDITOR.read_text(encoding="utf-8")
        digest = hashlib.sha256(source.encode()).hexdigest()
        already_installed = all(
            marker in source
            for marker in (
                frontend.IMPORT_MARKER,
                frontend.STATE_MARKER,
                frontend.SUBMIT_MARKER,
                frontend.LOAD_MARKER,
                frontend.UI_MARKER,
            )
        )
        if not already_installed:
            self.assertEqual(digest, frontend.EXPECTED_MODEL_EDITOR_SHA256)

        transformed = frontend.transform_model_editor(source, digest)
        repeated = frontend.transform_model_editor(
            transformed, hashlib.sha256(transformed.encode()).hexdigest()
        )

        self.assertEqual(transformed, repeated)
        self.assertIn("<ZetaDesktopCatalogue", transformed)
        self.assertIn("mergeZetaCatalogueDraft", transformed)
        self.assertIn("validateZetaCatalogueDraft", transformed)

    def test_unknown_source_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "differs from the reviewed base"):
            frontend.transform_model_editor("<script></script>", "0" * 64)

    @unittest.skipUnless(RUNTIME_MODEL_EDITOR.is_file(), "reviewed OpenWebUI source unavailable")
    def test_partial_install_is_refused(self):
        source = RUNTIME_MODEL_EDITOR.read_text(encoding="utf-8")
        if frontend.STATE_MARKER in source:
            partial = source.replace(frontend.STATE_BLOCK, frontend.STATE_ANCHOR, 1)
        else:
            partial = source.replace(
                frontend.IMPORT_ANCHOR,
                frontend.IMPORT_BLOCK,
                1,
            )
        with self.assertRaisesRegex(RuntimeError, "Partial Zeta"):
            frontend.transform_model_editor(partial, hashlib.sha256(partial.encode()).hexdigest())

    def test_frontend_source_mapping_stays_inside_openwebui_src(self):
        root = Path("/tmp/openwebui")
        sources = frontend.frontend_sources(OVERLAY_PATH.parent, root)
        self.assertEqual(len(sources), 3)
        self.assertTrue(all(source.is_file() for source in sources))
        self.assertTrue(
            all(str(target).startswith(str(root / "src")) for target in sources.values())
        )


if __name__ == "__main__":
    unittest.main()
