"""Hash-guarded source integration for the Zeta OpenWebUI catalogue editor."""

from __future__ import annotations

import hashlib
from pathlib import Path


EXPECTED_MODEL_EDITOR_SHA256 = (
    "52918b452adeed33431b253bf73ea30da751b6a0e0efb653408e029436055d41"
)
IMPORT_MARKER = "// BEGIN ZETA DESKTOP CATALOGUE IMPORTS"
STATE_MARKER = "// BEGIN ZETA DESKTOP CATALOGUE STATE"
SUBMIT_MARKER = "// BEGIN ZETA DESKTOP CATALOGUE SUBMIT"
LOAD_MARKER = "// BEGIN ZETA DESKTOP CATALOGUE LOAD"
UI_MARKER = "<!-- BEGIN ZETA DESKTOP CATALOGUE UI -->"

IMPORT_ANCHOR = "\timport { updateModelAccessGrants } from '$lib/apis/models';\n"
IMPORT_BLOCK = """\timport { updateModelAccessGrants } from '$lib/apis/models';
	// BEGIN ZETA DESKTOP CATALOGUE IMPORTS
	import ZetaDesktopCatalogue from './ZetaDesktopCatalogue.svelte';
	import {
		createZetaCatalogueDraft,
		mergeZetaCatalogueDraft,
		validateZetaCatalogueDraft,
		type ZetaCatalogueDraft
	} from '$lib/utils/zetaModelCatalog';
	// END ZETA DESKTOP CATALOGUE IMPORTS
"""

STATE_ANCHOR = "\tlet tts = { voice: '' };\n"
STATE_BLOCK = """\tlet tts = { voice: '' };

	// BEGIN ZETA DESKTOP CATALOGUE STATE
	let zetaCatalogueDraft: ZetaCatalogueDraft = createZetaCatalogueDraft({});
	let zetaCatalogueChangedFields: string[] = [];
	// END ZETA DESKTOP CATALOGUE STATE
"""

SUBMIT_ANCHOR = """\t\tif (knowledge.some((item) => item.status === 'uploading')) {
"""
SUBMIT_BLOCK = """\t\t// BEGIN ZETA DESKTOP CATALOGUE SUBMIT
		if (zetaCatalogueChangedFields.length > 0) {
			const catalogueErrors = validateZetaCatalogueDraft(zetaCatalogueDraft);
			if (catalogueErrors.length > 0) {
				toast.error($i18n.t(catalogueErrors[0]));
				loading = false;
				return;
			}
		}
		// END ZETA DESKTOP CATALOGUE SUBMIT

		if (knowledge.some((item) => item.status === 'uploading')) {
"""

MERGE_ANCHOR = """\t\tinfo.params.system = system.trim() === '' ? null : system;
"""
MERGE_BLOCK = """\t\tif (zetaCatalogueChangedFields.length > 0) {
			info.meta = mergeZetaCatalogueDraft(
				info.meta,
				zetaCatalogueDraft,
				zetaCatalogueChangedFields
			) as typeof info.meta;
		}

		info.params.system = system.trim() === '' ? null : system;
"""

LOAD_ANCHOR = """\t\t\tconsole.log(model);
	\t}

	\tloaded = true;
"""
LOAD_BLOCK = """\t\t\tconsole.log(model);
		}

		// BEGIN ZETA DESKTOP CATALOGUE LOAD
		zetaCatalogueDraft = createZetaCatalogueDraft(info.meta);
		zetaCatalogueChangedFields = [];
		// END ZETA DESKTOP CATALOGUE LOAD

		loaded = true;
"""

UI_ANCHOR = """\t\t\t\t\t<div class="my-2">
	\t\t\t\t\t<div class="flex w-full justify-between">
	\t\t\t\t\t\t<div class=" self-center text-xs font-medium text-gray-500">
	\t\t\t\t\t\t\t{$i18n.t('Model Params')}
"""
UI_BLOCK = """\t\t\t\t\t<!-- BEGIN ZETA DESKTOP CATALOGUE UI -->
					{#if !preset}
						<ZetaDesktopCatalogue
							bind:draft={zetaCatalogueDraft}
							bind:changedFields={zetaCatalogueChangedFields}
						/>
					{/if}
					<!-- END ZETA DESKTOP CATALOGUE UI -->

					<div class="my-2">
						<div class="flex w-full justify-between">
							<div class=" self-center text-xs font-medium text-gray-500">
								{$i18n.t('Model Params')}
"""


def sha256_bytes(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"Expected one {label} anchor, found {count}")
    return source.replace(old, new, 1)


def transform_model_editor(source: str, source_hash: str) -> str:
    markers = (IMPORT_MARKER, STATE_MARKER, SUBMIT_MARKER, LOAD_MARKER, UI_MARKER)
    present = [marker in source for marker in markers]
    if all(present):
        required = (IMPORT_BLOCK, STATE_BLOCK, SUBMIT_BLOCK, MERGE_BLOCK, LOAD_BLOCK, UI_BLOCK)
        if any(source.count(block) != 1 for block in required):
            raise RuntimeError("Zeta frontend markers exist but the integration is incomplete")
        return source
    if any(present):
        raise RuntimeError("Partial Zeta desktop catalogue editor integration detected")
    if source_hash != EXPECTED_MODEL_EDITOR_SHA256:
        raise RuntimeError(
            "OpenWebUI ModelEditor.svelte differs from the reviewed base; refusing "
            f"to patch (expected {EXPECTED_MODEL_EDITOR_SHA256}, found {source_hash})"
        )

    source = replace_once(source, IMPORT_ANCHOR, IMPORT_BLOCK, "catalogue import")
    source = replace_once(source, STATE_ANCHOR, STATE_BLOCK, "catalogue state")
    source = replace_once(source, SUBMIT_ANCHOR, SUBMIT_BLOCK, "catalogue validation")
    source = replace_once(source, MERGE_ANCHOR, MERGE_BLOCK, "catalogue merge")
    source = replace_once(source, LOAD_ANCHOR, LOAD_BLOCK, "catalogue load")
    return replace_once(source, UI_ANCHOR, UI_BLOCK, "catalogue UI")


def frontend_sources(overlay_directory: Path, openwebui_root: Path) -> dict[Path, Path]:
    source_root = overlay_directory / "files" / "frontend"
    return {
        source_root / "zetaModelCatalog.ts": (
            openwebui_root / "src" / "lib" / "utils" / "zetaModelCatalog.ts"
        ),
        source_root / "zetaModelCatalog.test.ts": (
            openwebui_root / "src" / "lib" / "utils" / "zetaModelCatalog.test.ts"
        ),
        source_root / "ZetaDesktopCatalogue.svelte": (
            openwebui_root
            / "src"
            / "lib"
            / "components"
            / "workspace"
            / "Models"
            / "ZetaDesktopCatalogue.svelte"
        ),
    }
