<script lang="ts">
	import { getContext } from 'svelte';

	import Checkbox from '$lib/components/common/Checkbox.svelte';
	import Textarea from '$lib/components/common/Textarea.svelte';
	import {
		ZETA_CAPABILITIES,
		ZETA_INPUT_MODALITIES,
		ZETA_REASONING_LEVELS,
		type ZetaCatalogueDraft,
		type ZetaCapability,
		type ZetaInputModality,
		type ZetaReasoningLevel
	} from '$lib/utils/zetaModelCatalog';

	const i18n: any = getContext('i18n');

	export let draft: ZetaCatalogueDraft;
	export let changedFields: string[] = [];

	const capabilityLabels: Record<ZetaCapability, string> = {
		tools: 'Tools',
		parallel_tools: 'Parallel tools',
		images: 'Image input',
		web_search: 'Native web search',
		reasoning: 'Reasoning controls'
	};

	const modalityLabels: Record<ZetaInputModality, string> = {
		text: 'Text',
		image: 'Image',
		audio: 'Audio'
	};

	const markChanged = (field: string) => {
		if (!changedFields.includes(field)) changedFields = [...changedFields, field];
	};

	const setReasoningLevel = (level: ZetaReasoningLevel, enabled: boolean) => {
		const selected = new Set(draft.supportedReasoningLevels);
		if (enabled) selected.add(level);
		else selected.delete(level);

		draft.supportedReasoningLevels = ZETA_REASONING_LEVELS.filter((item) => selected.has(item));
		markChanged('supported_reasoning_levels');

		if (
			draft.supportedReasoningLevels.length > 0 &&
			!draft.supportedReasoningLevels.includes(draft.defaultReasoningLevel)
		) {
			draft.defaultReasoningLevel = draft.supportedReasoningLevels.includes('medium')
				? 'medium'
				: draft.supportedReasoningLevels[0];
			markChanged('default_reasoning_level');
		}
	};

	const setModality = (modality: ZetaInputModality, enabled: boolean) => {
		if (modality === 'text') return;
		const selected = new Set(draft.inputModalities);
		if (enabled) selected.add(modality);
		else selected.delete(modality);
		selected.add('text');
		draft.inputModalities = ZETA_INPUT_MODALITIES.filter((item) => selected.has(item));
		markChanged('input_modalities');
	};
</script>

<section class="my-4 rounded-xl border border-gray-100 p-4 dark:border-gray-800">
	<div class="mb-3">
		<h3 class="text-sm font-medium">{$i18n.t('Zeta Desktop Catalogue')}</h3>
		<p class="mt-1 text-xs leading-5 text-gray-500">
			{$i18n.t(
				'These public details control how this model appears in the Zeta/Codex model picker.'
			)}
		</p>
		<p class="mt-1 text-xs leading-5 text-gray-500">
			{$i18n.t(
				'The normal Enabled switch is authoritative: disabling the model removes it from Zeta. Offline retention only keeps an enabled model listed when its provider is unavailable.'
			)}
		</p>
	</div>

	<div class="mb-4 flex items-start gap-2">
		<Checkbox
			state={draft.enabled ? 'checked' : 'unchecked'}
			on:change={(event) => {
				draft.enabled = event.detail === 'checked';
				markChanged('enabled');
			}}
		/>
		<div class="-mt-0.5">
			<div class="text-sm">
				{$i18n.t('Keep in catalogue while provider is offline')}
			</div>
			<div class="text-xs text-gray-500">
				{$i18n.t('This is not the model Enabled/Disabled control.')}
			</div>
		</div>
	</div>

	<div class="grid grid-cols-1 gap-4 md:grid-cols-2">
		<label class="block">
			<span class="mb-1 block text-xs font-medium text-gray-500">
				{$i18n.t('Desktop display name')}
			</span>
			<input
				class="w-full rounded-lg border border-gray-100 bg-transparent px-3 py-2 text-sm outline-hidden dark:border-gray-800"
				maxlength="200"
				placeholder={$i18n.t('Uses the normal model name when blank')}
				bind:value={draft.displayName}
				on:input={() => markChanged('display_name')}
			/>
		</label>

		<label class="block">
			<span class="mb-1 block text-xs font-medium text-gray-500">{$i18n.t('Visibility')}</span>
			<select
				class="w-full rounded-lg border border-gray-100 bg-transparent px-3 py-2 text-sm outline-hidden dark:border-gray-800"
				bind:value={draft.visibility}
				on:change={() => markChanged('visibility')}
			>
				<option value="list">{$i18n.t('Listed')}</option>
				<option value="hidden">{$i18n.t('Hidden')}</option>
			</select>
		</label>

		<label class="block">
			<span class="mb-1 block text-xs font-medium text-gray-500">
				{$i18n.t('Priority')}
			</span>
			<input
				class="w-full rounded-lg border border-gray-100 bg-transparent px-3 py-2 text-sm outline-hidden dark:border-gray-800"
				type="number"
				min="0"
				max="1000000"
				step="1"
				placeholder={$i18n.t('Uses model order when blank')}
				value={draft.priority ?? ''}
				on:input={(event) => {
					draft.priority = event.currentTarget.value;
					markChanged('priority');
				}}
			/>
			<span class="mt-1 block text-xs text-gray-500">{$i18n.t('Lower values appear first.')}</span>
		</label>

		<label class="block">
			<span class="mb-1 block text-xs font-medium text-gray-500">
				{$i18n.t('Context window (tokens)')}
			</span>
			<input
				class="w-full rounded-lg border border-gray-100 bg-transparent px-3 py-2 text-sm outline-hidden dark:border-gray-800"
				type="number"
				min="1024"
				max="4194304"
				step="1"
				placeholder={$i18n.t('Uses Context label or 32768 when blank')}
				value={draft.contextWindow ?? ''}
				on:input={(event) => {
					draft.contextWindow = event.currentTarget.value;
					markChanged('context_window');
				}}
			/>
		</label>
	</div>

	<label class="mt-4 block">
		<span class="mb-1 block text-xs font-medium text-gray-500">
			{$i18n.t('Desktop description')}
		</span>
		<Textarea
			className="w-full rounded-lg border border-gray-100 bg-transparent px-3 py-2 text-sm outline-hidden resize-none dark:border-gray-800"
			rows={3}
			placeholder={$i18n.t('Optional public description for the desktop model picker')}
			bind:value={draft.description}
			onInput={() => markChanged('description')}
		/>
		<span class="mt-1 block text-xs text-gray-500">
			{$i18n.t('Do not include provider URLs, internal addresses, API keys, or other secrets.')}
		</span>
	</label>

	<div class="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
		<div>
			<div class="mb-2 text-xs font-medium text-gray-500">
				{$i18n.t('Supported reasoning levels')}
			</div>
			<div class="flex flex-wrap gap-x-4 gap-y-2">
				{#each ZETA_REASONING_LEVELS as level}
					<label class="flex items-center gap-2 text-sm capitalize">
						<Checkbox
							state={draft.supportedReasoningLevels.includes(level) ? 'checked' : 'unchecked'}
							on:change={(event) => setReasoningLevel(level, event.detail === 'checked')}
						/>
						<span>{level}</span>
					</label>
				{/each}
			</div>
		</div>

		<label class="block">
			<span class="mb-1 block text-xs font-medium text-gray-500">
				{$i18n.t('Default reasoning level')}
			</span>
			<select
				class="w-full rounded-lg border border-gray-100 bg-transparent px-3 py-2 text-sm outline-hidden dark:border-gray-800"
				bind:value={draft.defaultReasoningLevel}
				on:change={() => markChanged('default_reasoning_level')}
				disabled={draft.supportedReasoningLevels.length === 0}
			>
				{#each draft.supportedReasoningLevels as level}
					<option value={level}>{level}</option>
				{/each}
			</select>
		</label>
	</div>

	<div class="mt-4">
		<div class="mb-2 text-xs font-medium text-gray-500">
			{$i18n.t('Input modalities')}
		</div>
		<div class="flex flex-wrap gap-x-4 gap-y-2">
			{#each ZETA_INPUT_MODALITIES as modality}
				<label class="flex items-center gap-2 text-sm">
					<Checkbox
						state={draft.inputModalities.includes(modality) ? 'checked' : 'unchecked'}
						disabled={modality === 'text'}
						on:change={(event) => setModality(modality, event.detail === 'checked')}
					/>
					<span>{modalityLabels[modality]}</span>
				</label>
			{/each}
		</div>
	</div>

	<div class="mt-4">
		<div class="mb-2 text-xs font-medium text-gray-500">
			{$i18n.t('Desktop capabilities')}
		</div>
		<div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
			{#each ZETA_CAPABILITIES as capability}
				<label class="flex items-center gap-2 text-sm">
					<Checkbox
						state={draft.capabilities[capability] ? 'checked' : 'unchecked'}
						on:change={(event) => {
							draft.capabilities[capability] = event.detail === 'checked';
							markChanged(`capabilities.${capability}`);
						}}
					/>
					<span>{capabilityLabels[capability]}</span>
				</label>
			{/each}
		</div>
	</div>

	{#if changedFields.length > 0}
		<div class="mt-4 text-xs text-amber-600 dark:text-amber-400">
			{$i18n.t('Catalogue changes will be applied when you save the model.')}
		</div>
	{/if}
</section>
