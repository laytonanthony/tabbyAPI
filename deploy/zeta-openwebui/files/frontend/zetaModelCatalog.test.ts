import { describe, expect, it } from 'vitest';

import {
	createZetaCatalogueDraft,
	mergeZetaCatalogueDraft,
	validateZetaCatalogueDraft
} from './zetaModelCatalog';

describe('Zeta desktop catalogue editor helpers', () => {
	it('uses conservative form defaults without creating metadata', () => {
		const meta = { description: 'normal OpenWebUI metadata' };
		const draft = createZetaCatalogueDraft(meta);

		expect(draft).toMatchObject({
			enabled: false,
			displayName: '',
			priority: '',
			contextWindow: '',
			visibility: 'list',
			defaultReasoningLevel: 'medium',
			supportedReasoningLevels: ['medium'],
			inputModalities: ['text']
		});
		expect(mergeZetaCatalogueDraft(meta, draft, [])).toEqual(meta);
		expect(mergeZetaCatalogueDraft(meta, draft, [])).not.toHaveProperty('zeta_catalog');
	});

	it('normalises selector order and keeps text input mandatory', () => {
		const draft = createZetaCatalogueDraft({
			zeta_catalog: {
				default_reasoning_level: 'high',
				supported_reasoning_levels: ['xhigh', 'invalid', 'high', 'low', 'high'],
				input_modalities: ['audio', 'invalid', 'image']
			}
		});

		expect(draft.supportedReasoningLevels).toEqual(['low', 'high', 'xhigh']);
		expect(draft.defaultReasoningLevel).toBe('high');
		expect(draft.inputModalities).toEqual(['text', 'image', 'audio']);
	});

	it('validates public text, numeric bounds, and reasoning consistency', () => {
		const draft = createZetaCatalogueDraft({});
		draft.displayName = 'provider at http://localhost:9000';
		draft.priority = '-1';
		draft.contextWindow = '1000';
		draft.supportedReasoningLevels = ['low'];
		draft.defaultReasoningLevel = 'high';

		const errors = validateZetaCatalogueDraft(draft);
		expect(errors).toHaveLength(4);
		expect(errors.join(' ')).toContain('cannot contain URLs');
		expect(errors.join(' ')).toContain('Priority');
		expect(errors.join(' ')).toContain('Context window');
		expect(errors.join(' ')).toContain('Default reasoning level');
	});

	it('merges only touched fields and preserves unknown metadata', () => {
		const meta = {
			hidden: true,
			zeta_catalog: {
				enabled: true,
				future_schema_field: { retain: true },
				capabilities: { tools: false, future_capability: 'retain' }
			}
		};
		const draft = createZetaCatalogueDraft(meta);
		draft.displayName = '  Zeta   Qwen  ';
		draft.capabilities.tools = true;

		const merged = mergeZetaCatalogueDraft(meta, draft, ['display_name', 'capabilities.tools']);

		expect(merged).toEqual({
			hidden: true,
			zeta_catalog: {
				enabled: true,
				display_name: 'Zeta Qwen',
				future_schema_field: { retain: true },
				capabilities: {
					tools: true,
					future_capability: 'retain'
				}
			}
		});
	});

	it('clears optional overrides without disturbing retention', () => {
		const meta = {
			zeta_catalog: {
				enabled: true,
				display_name: 'Old name',
				priority: 20
			}
		};
		const draft = createZetaCatalogueDraft(meta);
		draft.displayName = '';
		draft.priority = '';

		const merged = mergeZetaCatalogueDraft(meta, draft, ['display_name', 'priority']);
		expect(merged).toEqual({ zeta_catalog: { enabled: true } });
	});

	it('handles Svelte number-input coercion and an emptied numeric input', () => {
		const draft = createZetaCatalogueDraft({});
		draft.priority = 25;
		draft.contextWindow = undefined;

		expect(validateZetaCatalogueDraft(draft)).toEqual([]);
		expect(
			mergeZetaCatalogueDraft({ zeta_catalog: { context_window: 32768 } }, draft, [
				'priority',
				'context_window'
			])
		).toEqual({ zeta_catalog: { priority: 25 } });
	});
});
