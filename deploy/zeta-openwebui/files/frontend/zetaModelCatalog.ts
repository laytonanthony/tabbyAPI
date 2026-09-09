export const ZETA_REASONING_LEVELS = ['low', 'medium', 'high', 'xhigh'] as const;
export const ZETA_INPUT_MODALITIES = ['text', 'image', 'audio'] as const;
export const ZETA_CAPABILITIES = [
	'tools',
	'parallel_tools',
	'images',
	'web_search',
	'reasoning'
] as const;

export type ZetaReasoningLevel = (typeof ZETA_REASONING_LEVELS)[number];
export type ZetaInputModality = (typeof ZETA_INPUT_MODALITIES)[number];
export type ZetaCapability = (typeof ZETA_CAPABILITIES)[number];
export type ZetaCatalogueVisibility = 'list' | 'hidden';
export type ZetaNumericDraft = string | number | undefined;

export type ZetaCatalogueDraft = {
	enabled: boolean;
	displayName: string;
	description: string;
	priority: ZetaNumericDraft;
	visibility: ZetaCatalogueVisibility;
	contextWindow: ZetaNumericDraft;
	defaultReasoningLevel: ZetaReasoningLevel;
	supportedReasoningLevels: ZetaReasoningLevel[];
	inputModalities: ZetaInputModality[];
	capabilities: Record<ZetaCapability, boolean>;
};

export const ZETA_CATALOGUE_LIMITS = {
	priority: { min: 0, max: 1_000_000 },
	contextWindow: { min: 1_024, max: 4_194_304 },
	displayName: 200,
	description: 2_000
} as const;

const isRecord = (value: unknown): value is Record<string, unknown> =>
	typeof value === 'object' && value !== null && !Array.isArray(value);

const orderedSelection = <T extends string>(value: unknown, allowed: readonly T[]): T[] => {
	if (!Array.isArray(value)) return [];
	const selected = new Set(value.filter((item): item is T => allowed.includes(item as T)));
	return allowed.filter((item) => selected.has(item));
};

const integerDraft = (value: unknown, minimum: number, maximum: number): string =>
	typeof value === 'number' && Number.isInteger(value) && value >= minimum && value <= maximum
		? String(value)
		: '';

const publicText = (value: string): string => value.trim().replace(/\s+/g, ' ');
const numericText = (value: ZetaNumericDraft): string =>
	typeof value === 'number' && Number.isFinite(value)
		? String(value)
		: typeof value === 'string'
			? value
			: '';

const UNSAFE_PUBLIC_TEXT =
	/(?:[a-z][a-z0-9+.-]*:\/\/|\blocalhost\b|\b(?:authorization|api[_ -]?key|access[_ -]?token|secret)\s*[:=]|\b(?:sk-|AIza|gh[pousr]_|xox[baprs]-|AKIA)[A-Za-z0-9_-]{8,})/i;

export const createZetaCatalogueDraft = (meta: unknown): ZetaCatalogueDraft => {
	const metadata = isRecord(meta) ? meta : {};
	const raw = isRecord(metadata.zeta_catalog) ? metadata.zeta_catalog : {};
	const rawCapabilities = isRecord(raw.capabilities) ? raw.capabilities : {};

	let supportedReasoningLevels = orderedSelection(
		raw.supported_reasoning_levels,
		ZETA_REASONING_LEVELS
	);
	if (supportedReasoningLevels.length === 0) supportedReasoningLevels = ['medium'];

	const requestedDefault = raw.default_reasoning_level;
	const defaultReasoningLevel = supportedReasoningLevels.includes(
		requestedDefault as ZetaReasoningLevel
	)
		? (requestedDefault as ZetaReasoningLevel)
		: supportedReasoningLevels.includes('medium')
			? 'medium'
			: supportedReasoningLevels[0];

	const configuredModalities = orderedSelection(raw.input_modalities, ZETA_INPUT_MODALITIES);
	const inputModalities = ZETA_INPUT_MODALITIES.filter(
		(modality) => modality === 'text' || configuredModalities.includes(modality)
	);

	return {
		enabled: raw.enabled === true,
		displayName: typeof raw.display_name === 'string' ? raw.display_name : '',
		description: typeof raw.description === 'string' ? raw.description : '',
		priority: integerDraft(
			raw.priority,
			ZETA_CATALOGUE_LIMITS.priority.min,
			ZETA_CATALOGUE_LIMITS.priority.max
		),
		visibility: raw.visibility === 'hidden' ? 'hidden' : 'list',
		contextWindow: integerDraft(
			raw.context_window,
			ZETA_CATALOGUE_LIMITS.contextWindow.min,
			ZETA_CATALOGUE_LIMITS.contextWindow.max
		),
		defaultReasoningLevel,
		supportedReasoningLevels,
		inputModalities,
		capabilities: Object.fromEntries(
			ZETA_CAPABILITIES.map((capability) => [capability, rawCapabilities[capability] === true])
		) as Record<ZetaCapability, boolean>
	};
};

const optionalIntegerError = (
	value: ZetaNumericDraft,
	label: string,
	minimum: number,
	maximum: number
): string | null => {
	const text = numericText(value).trim();
	if (text === '') return null;
	const number = Number(text);
	return Number.isInteger(number) && number >= minimum && number <= maximum
		? null
		: `${label} must be a whole number from ${minimum.toLocaleString()} to ${maximum.toLocaleString()}.`;
};

export const validateZetaCatalogueDraft = (draft: ZetaCatalogueDraft): string[] => {
	const errors: string[] = [];
	const displayName = publicText(draft.displayName);
	const description = publicText(draft.description);

	if (displayName.length > ZETA_CATALOGUE_LIMITS.displayName) {
		errors.push(
			`Desktop display name must be ${ZETA_CATALOGUE_LIMITS.displayName} characters or fewer.`
		);
	}
	if (description.length > ZETA_CATALOGUE_LIMITS.description) {
		errors.push(
			`Desktop description must be ${ZETA_CATALOGUE_LIMITS.description.toLocaleString()} characters or fewer.`
		);
	}
	if (UNSAFE_PUBLIC_TEXT.test(displayName) || UNSAFE_PUBLIC_TEXT.test(description)) {
		errors.push(
			'Desktop name and description cannot contain URLs, credentials, or internal addresses.'
		);
	}

	const priorityError = optionalIntegerError(
		draft.priority,
		'Priority',
		ZETA_CATALOGUE_LIMITS.priority.min,
		ZETA_CATALOGUE_LIMITS.priority.max
	);
	if (priorityError) errors.push(priorityError);

	const contextError = optionalIntegerError(
		draft.contextWindow,
		'Context window',
		ZETA_CATALOGUE_LIMITS.contextWindow.min,
		ZETA_CATALOGUE_LIMITS.contextWindow.max
	);
	if (contextError) errors.push(contextError);

	if (draft.supportedReasoningLevels.length === 0) {
		errors.push('Select at least one supported reasoning level.');
	} else if (!draft.supportedReasoningLevels.includes(draft.defaultReasoningLevel)) {
		errors.push('Default reasoning level must also be selected as supported.');
	}
	if (!draft.inputModalities.includes('text')) {
		errors.push('Text must remain an input modality.');
	}

	return errors;
};

const setOrDeleteText = (target: Record<string, unknown>, key: string, value: string) => {
	const cleaned = publicText(value);
	if (cleaned) target[key] = cleaned;
	else delete target[key];
};

const setOrDeleteInteger = (
	target: Record<string, unknown>,
	key: string,
	value: ZetaNumericDraft
) => {
	const text = numericText(value).trim();
	if (text === '') delete target[key];
	else target[key] = Number(text);
};

/**
 * Merge only fields the operator touched. Unknown and untouched catalogue
 * metadata survives normal OpenWebUI model edits byte-for-byte at the value
 * level, while deliberately cleared optional fields return to server defaults.
 */
export const mergeZetaCatalogueDraft = (
	meta: unknown,
	draft: ZetaCatalogueDraft,
	changedFields: readonly string[]
): Record<string, unknown> => {
	const metadata = isRecord(meta) ? { ...meta } : {};
	if (changedFields.length === 0) return metadata;

	const existing = isRecord(metadata.zeta_catalog) ? metadata.zeta_catalog : {};
	const catalogue: Record<string, unknown> = { ...existing };
	const changed = new Set(changedFields);

	if (changed.has('enabled')) catalogue.enabled = draft.enabled;
	if (changed.has('display_name')) setOrDeleteText(catalogue, 'display_name', draft.displayName);
	if (changed.has('description')) setOrDeleteText(catalogue, 'description', draft.description);
	if (changed.has('priority')) setOrDeleteInteger(catalogue, 'priority', draft.priority);
	if (changed.has('visibility')) catalogue.visibility = draft.visibility;
	if (changed.has('context_window')) {
		setOrDeleteInteger(catalogue, 'context_window', draft.contextWindow);
	}
	if (changed.has('default_reasoning_level')) {
		catalogue.default_reasoning_level = draft.defaultReasoningLevel;
	}
	if (changed.has('supported_reasoning_levels')) {
		catalogue.supported_reasoning_levels = [...draft.supportedReasoningLevels];
	}
	if (changed.has('input_modalities')) catalogue.input_modalities = [...draft.inputModalities];

	const changedCapabilities = ZETA_CAPABILITIES.filter((capability) =>
		changed.has(`capabilities.${capability}`)
	);
	if (changedCapabilities.length > 0) {
		const existingCapabilities = isRecord(catalogue.capabilities) ? catalogue.capabilities : {};
		catalogue.capabilities = {
			...existingCapabilities,
			...Object.fromEntries(
				changedCapabilities.map((capability) => [capability, draft.capabilities[capability]])
			)
		};
	}

	metadata.zeta_catalog = catalogue;
	return metadata;
};
