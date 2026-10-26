<script lang="ts">
	import { onMount } from 'svelte';
	import type { ToolApprovalMode, ReasoningLevel } from '$lib/apis/chat';
	import { tooltip } from '$lib/tooltip';
	import { t } from '$lib/i18n';
	import Icon from '../Icon.svelte';
	import ToggleSwitch from '../common/ToggleSwitch.svelte';

	interface Props {
		onfiles: (files: FileList) => void;
		oncapture?: (file: File) => void;
		toolApprovalMode?: ToolApprovalMode;
		planMode?: boolean;
		requestParams?: Record<string, unknown>;
		reasoningLevel?: ReasoningLevel;
		compactKeepPercent?: number | null;
		compactTokenThreshold?: number | null;
		compactTokenThresholdConfig?: number | null;
		onchange?: () => void;
		ontoolapprovalchange?: (mode: ToolApprovalMode) => void;
	}
	let {
		onfiles,
		oncapture,
		toolApprovalMode = $bindable('auto'),
		planMode = $bindable(false),
		requestParams = $bindable({}),
		reasoningLevel = $bindable('default'),
		compactKeepPercent = $bindable<number | null>(null),
		compactTokenThreshold = $bindable<number | null>(null),
		compactTokenThresholdConfig = null,
		onchange,
		ontoolapprovalchange
	}: Props = $props();

	let open = $state(false);
	let tab = $state<'' | 'tools' | 'request_params' | 'reasoning' | 'compaction'>('');
	let btnEl: HTMLButtonElement | undefined = $state();
	let menuEl: HTMLDivElement | undefined = $state();
	let inputEl: HTMLInputElement | undefined = $state();
	let cameraInputEl: HTMLInputElement | undefined = $state();
	let pos = $state<{ x: number; bottom: number }>({ x: -9999, bottom: -9999 });
	let ready = $state(false);

	const modes: { value: ToolApprovalMode; label: string; desc: string }[] = $derived([
		{ value: 'ask', label: $t('plusMenu.askApproval'), desc: $t('plusMenu.askApprovalDesc') },
		{ value: 'auto', label: $t('plusMenu.autoApprove'), desc: $t('plusMenu.autoApproveDesc') },
		{ value: 'full', label: $t('plusMenu.fullAccess'), desc: $t('plusMenu.fullAccessDesc') }
	]);

	const currentModeLabel = $derived(
		modes.find((m) => m.value === toolApprovalMode)?.label ?? $t('plusMenu.toolPermissions')
	);

	// ── Reasoning level state ────────────────────────
	const reasoningLevels: { value: ReasoningLevel; label: string }[] = $derived([
		{ value: 'default', label: $t('plusMenu.reasoningDefault') },
		{ value: 'off', label: $t('plusMenu.reasoningOff') },
		{ value: 'low', label: $t('plusMenu.reasoningLow') },
		{ value: 'medium', label: $t('plusMenu.reasoningMedium') },
		{ value: 'high', label: $t('plusMenu.reasoningHigh') },
		{ value: 'xhigh', label: $t('plusMenu.reasoningXHigh') }
	]);

	const currentReasoningLabel = $derived(
		reasoningLevels.find((l) => l.value === reasoningLevel)?.label ?? $t('plusMenu.reasoning')
	);

	function selectReasoning(level: ReasoningLevel) {
		reasoningLevel = level;
		onchange?.();
	}

	// ── Compaction keep-percent (per chat) ────────────────────────
	// Bound to the chat's saved "keep last N%" for auto-compaction. null
	// (empty) means the default 40, shown as placeholder while the field is
	// empty. For type="number", Svelte's bind:value holds number | null.
	const COMPACT_KEEP_DEFAULT = 40;
	const COMPACT_KEEP_MIN = 0;
	const COMPACT_KEEP_MAX = 100;
	const COMPACT_KEEP_STEP = 10;

	// Clamp on blur: while typing the field holds the raw text, and the value
	// is committed (clamped back into range) when focus leaves the input.
	// onchange persists the value for this chat.
	function clampCompactKeep() {
		if (compactKeepPercent === null) return;
		const before = compactKeepPercent;
		compactKeepPercent = Math.min(COMPACT_KEEP_MAX, Math.max(COMPACT_KEEP_MIN, compactKeepPercent));
		if (compactKeepPercent !== before) onchange?.();
	}

	// Custom stepper: the native spinner would step from 0 while the field is
	// empty, so it's hidden and the value is driven directly — an empty field
	// acts as the 40 default (the placeholder), so the first press steps from 40.
	// Each committed step persists the value for this chat.
	function stepCompactKeep(dir: 1 | -1) {
		const base = compactKeepPercent === null ? COMPACT_KEEP_DEFAULT : compactKeepPercent;
		const next = Math.min(
			COMPACT_KEEP_MAX,
			Math.max(COMPACT_KEEP_MIN, base + dir * COMPACT_KEEP_STEP)
		);
		if (next === compactKeepPercent) return;
		compactKeepPercent = next;
		onchange?.();
	}

	// Arrow keys keep stepping the value now that the native spinner is hidden.
	function handleCompactKeepKeydown(e: KeyboardEvent) {
		if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
			e.preventDefault();
			stepCompactKeep(e.key === 'ArrowUp' ? 1 : -1);
		}
	}

	// ── Compaction token threshold (per chat) ─────────────────────
	// Bound to the chat's saved compaction threshold override. null (empty)
	// means "use the configured threshold" — the effective value from model /
	// global config, passed in as compactTokenThresholdConfig and shown as the
	// placeholder. The default below mirrors CHAT_COMPACT_TOKEN_THRESHOLD in
	// cptr/env.py, used only when no context usage has been computed yet.
	const COMPACT_TOKEN_THRESHOLD_DEFAULT = 80000;
	const COMPACT_TOKEN_THRESHOLD_MIN = 0;
	const COMPACT_TOKEN_THRESHOLD_STEP = 10000;

	function clampCompactTokenThreshold() {
		if (compactTokenThreshold === null) return;
		const before = compactTokenThreshold;
		compactTokenThreshold = Math.max(COMPACT_TOKEN_THRESHOLD_MIN, compactTokenThreshold);
		if (compactTokenThreshold !== before) onchange?.();
	}

	function stepCompactTokenThreshold(dir: 1 | -1) {
		const base =
			compactTokenThreshold ??
			compactTokenThresholdConfig ??
			COMPACT_TOKEN_THRESHOLD_DEFAULT;
		const next = Math.max(COMPACT_TOKEN_THRESHOLD_MIN, base + dir * COMPACT_TOKEN_THRESHOLD_STEP);
		if (next === compactTokenThreshold) return;
		compactTokenThreshold = next;
		onchange?.();
	}

	function handleCompactTokenThresholdKeydown(e: KeyboardEvent) {
		if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
			e.preventDefault();
			stepCompactTokenThreshold(e.key === 'ArrowUp' ? 1 : -1);
		}
	}

	// ── Request params state ────────────────────────
	let paramRows = $state<Array<{ key: string; value: string }>>(
		Object.entries(requestParams).map(([key, value]) => ({
			key,
			value: typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)
		}))
	);

	const paramCount = $derived(paramRows.filter((r) => r.key.trim()).length);

	const canAddParam = $derived(
		paramRows.length === 0 || paramRows.every((r) => r.key.trim() || r.value.trim())
	);

	function addParamRow() {
		paramRows = [...paramRows, { key: '', value: '' }];
	}

	function removeParamRow(index: number) {
		paramRows = paramRows.filter((_, i) => i !== index);
		syncParams();
	}

	function syncParams() {
		const result: Record<string, unknown> = {};
		for (const { key, value } of paramRows) {
			if (!key.trim()) continue;
			try {
				result[key.trim()] = JSON.parse(value);
			} catch {
				result[key.trim()] = value;
			}
		}
		requestParams = result;
		onchange?.();
	}

	$effect(() => {
		paramRows = Object.entries(requestParams).map(([key, value]) => ({
			key,
			value: typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)
		}));
	});

	function toggle() {
		open = !open;
		if (open) {
			tab = '';
			requestAnimationFrame(updatePosition);
		}
	}

	function updatePosition() {
		if (!btnEl || !menuEl) return;
		const rect = btnEl.getBoundingClientRect();
		const mw = menuEl.offsetWidth;
		const vh = window.innerHeight;
		let x = rect.left;
		if (x + mw > window.innerWidth - 8) x = window.innerWidth - mw - 8;
		if (x < 8) x = 8;
		pos = { x, bottom: vh - rect.top + 4 };
		ready = true;
	}

	function selectMode(mode: ToolApprovalMode) {
		toolApprovalMode = mode;
		if (ontoolapprovalchange) ontoolapprovalchange(mode);
		else onchange?.();
	}

	function triggerUpload() {
		open = false;
		inputEl?.click();
	}

	function handleFileChange() {
		if (inputEl?.files && inputEl.files.length > 0) {
			onfiles(inputEl.files);
			inputEl.value = '';
		}
	}

	function handleCameraChange() {
		if (cameraInputEl?.files && cameraInputEl.files.length > 0) {
			// Camera input returns image files — pass them through oncapture or onfiles
			for (const file of Array.from(cameraInputEl.files)) {
				if (oncapture) oncapture(file);
			}
			cameraInputEl.value = '';
		}
	}

	function isMobileDevice(): boolean {
		if (typeof navigator === 'undefined') return false;
		const ua = navigator.userAgent || (navigator as any).vendor || (window as any).opera || '';
		return /android|iphone|ipad|ipod|windows phone/i.test(ua);
	}

	async function captureHandler() {
		open = false;
		if (isMobileDevice()) {
			// On mobile, open the camera
			cameraInputEl?.click();
			return;
		}
		// Desktop: use screen capture API
		try {
			const mediaStream = await navigator.mediaDevices.getDisplayMedia({
				video: { cursor: 'never' } as any,
				audio: false
			});
			const video = document.createElement('video');
			video.srcObject = mediaStream;
			await video.play();

			const canvas = document.createElement('canvas');
			canvas.width = video.videoWidth;
			canvas.height = video.videoHeight;
			const ctx = canvas.getContext('2d');
			if (ctx) ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

			mediaStream.getTracks().forEach((t) => t.stop());
			window.focus();

			const blob = await new Promise<Blob | null>((res) => canvas.toBlob(res, 'image/png'));
			if (blob) {
				const file = new File([blob], `screen-capture-${Date.now()}.png`, { type: 'image/png' });
				if (oncapture) oncapture(file);
			}
			video.srcObject = null;
		} catch (err) {
			console.error('Error capturing screen:', err);
		}
	}

	function close() {
		open = false;
		ready = false;
		tab = '';
	}

	$effect(() => {
		if (!open) {
			ready = false;
			tab = '';
			return;
		}
		// Keep menu anchored on resize/scroll while open
		window.addEventListener('resize', updatePosition);
		window.addEventListener('scroll', updatePosition, true);
		return () => {
			window.removeEventListener('resize', updatePosition);
			window.removeEventListener('scroll', updatePosition, true);
		};
	});
</script>

<input
	bind:this={inputEl}
	type="file"
	multiple
	accept="image/*,.pdf,.txt,.md,.csv,.json,.xml,.yaml,.yml,.html,.css,.js,.ts,.py,.go,.rs,.java,.c,.cpp,.h,.rb,.sh"
	class="hidden"
	onchange={handleFileChange}
/>

<!-- Hidden camera input for mobile capture -->
<input
	bind:this={cameraInputEl}
	type="file"
	accept="image/*"
	capture="environment"
	class="hidden"
	onchange={handleCameraChange}
/>

<button
	bind:this={btnEl}
	type="button"
	class="flex items-center justify-center w-6 h-6 rounded-full text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 transition-colors duration-100 cursor-pointer"
	onclick={toggle}
	aria-expanded={open}
	aria-haspopup="menu"
>
	<svg
		class="size-3.5 transition-transform duration-150"
		class:rotate-45={open}
		viewBox="0 0 24 24"
		fill="none"
		stroke="currentColor"
		stroke-width="2"
		stroke-linecap="round"
		stroke-linejoin="round"
	>
		<line x1="12" y1="5" x2="12" y2="19" />
		<line x1="5" y1="12" x2="19" y2="12" />
	</svg>
</button>

{#if open}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="fixed inset-0 z-[100]" onclick={close}></div>

	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		bind:this={menuEl}
		class="app-theme app-surface fixed z-[101] w-56 rounded-xl border shadow-xl p-0.5 overflow-hidden"
		style="left: {pos.x}px; bottom: {pos.bottom}px; opacity: {ready ? 1 : 0}; pointer-events: {ready
			? 'auto'
			: 'none'};"
		onclick={(e) => e.stopPropagation()}
	>
		{#if tab === ''}
			<!-- Main menu -->
			<div class="plus-menu-slide-in-left">
				<button
					class="flex items-center gap-2 w-full h-7 px-2 rounded-xl text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-900 dark:hover:text-white transition-colors duration-75"
					onclick={triggerUpload}
				>
					<svg
						class="size-3.5 shrink-0"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path
							d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"
						/>
					</svg>
					<span class="flex-1 text-left truncate">{$t('plusMenu.attachFiles')}</span>
				</button>

				<button
					class="flex items-center gap-2 w-full h-7 px-2 rounded-xl text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-900 dark:hover:text-white transition-colors duration-75"
					onclick={captureHandler}
				>
					<svg
						class="size-3.5 shrink-0"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path
							d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"
						/>
						<circle cx="12" cy="13" r="4" />
					</svg>
					<span class="flex-1 text-left truncate">{$t('plusMenu.capture')}</span>
				</button>

				<div class="app-divider h-px mx-1 my-0.5"></div>

				<!-- Plan mode toggle -->
				<button
					class="flex items-center gap-2 w-full h-7 px-2 rounded-xl text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-900 dark:hover:text-white transition-colors duration-75"
					onclick={() => {
						planMode = !planMode;
						onchange?.();
					}}
				>
					<svg
						class="size-3.5 shrink-0"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
						<polyline points="14 2 14 8 20 8" />
						<line x1="9" y1="13" x2="15" y2="13" />
						<line x1="9" y1="17" x2="15" y2="17" />
					</svg>
					<span class="flex-1 text-left truncate">{$t('plusMenu.planMode')}</span>
					<ToggleSwitch
						value={planMode}
						onchange={(v) => {
							planMode = v;
							onchange?.();
						}}
					/>
				</button>

				<div class="app-divider h-px mx-1 my-0.5"></div>

				<button
					class="flex items-center gap-2 w-full h-7 px-2 rounded-xl text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-900 dark:hover:text-white transition-colors duration-75"
					onclick={() => (tab = 'tools')}
				>
					<Icon name="shield" size={14} />
					<span class="flex-1 text-left truncate">{$t('plusMenu.toolPermissions')}</span>
					<span class="text-[0.625rem] text-gray-400 dark:text-gray-500 truncate max-w-16"
						>{currentModeLabel}</span
					>
					<Icon name="chevron-right" size={12} class="shrink-0 text-gray-400 dark:text-gray-500" />
				</button>

				<div class="app-divider h-px mx-1 my-0.5"></div>

				<button
					class="flex items-center gap-2 w-full h-7 px-2 rounded-xl text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-900 dark:hover:text-white transition-colors duration-75"
					onclick={() => (tab = 'request_params')}
				>
					<svg
						class="size-3.5 shrink-0"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<circle cx="12" cy="12" r="3" />
						<path
							d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"
						/>
					</svg>
					<span class="flex-1 text-left truncate">{$t('plusMenu.parameters')}</span>
					{#if paramCount > 0}
						<span class="text-[0.625rem] text-gray-400 dark:text-gray-500">{paramCount}</span>
					{/if}
					<Icon name="chevron-right" size={12} class="shrink-0 text-gray-400 dark:text-gray-500" />
				</button>

				<div class="app-divider h-px mx-1 my-0.5"></div>

				<button
					class="flex items-center gap-2 w-full h-7 px-2 rounded-xl text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-900 dark:hover:text-white transition-colors duration-75"
					onclick={() => (tab = 'reasoning')}
				>
					<Icon name="brain" size={14} />
					<span class="flex-1 text-left truncate">{$t('plusMenu.reasoning')}</span>
					<span class="text-[0.625rem] text-gray-400 dark:text-gray-500 truncate max-w-16"
						>{currentReasoningLabel}</span
					>
					<Icon name="chevron-right" size={12} class="shrink-0 text-gray-400 dark:text-gray-500" />
				</button>

				<div class="app-divider h-px mx-1 my-0.5"></div>

				<button
					class="flex items-center gap-2 w-full h-7 px-2 rounded-xl text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-900 dark:hover:text-white transition-colors duration-75"
					onclick={() => (tab = 'compaction')}
				>
					<Icon name="archive" size={14} />
					<span class="flex-1 text-left truncate">{$t('plusMenu.compaction')}</span>
					<Icon name="chevron-right" size={12} class="shrink-0 text-gray-400 dark:text-gray-500" />
				</button>
			</div>
		{:else if tab === 'tools'}
			<!-- Tool permissions submenu -->
			<div class="plus-menu-slide-in-right">
				<button
					class="flex items-center gap-2 w-full h-7 px-2 rounded-xl text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-900 dark:hover:text-white transition-colors duration-75"
					onclick={() => (tab = '')}
				>
					<Icon name="chevron-left" size={12} />
					<span class="flex-1 text-left font-medium">{$t('plusMenu.toolPermissions')}</span>
				</button>

				<div class="app-divider h-px mx-1 my-0.5"></div>

				{#each modes as mode}
					<button
						class="flex items-center gap-2 w-full h-7 px-2 rounded-xl text-xs transition-colors duration-75
							{toolApprovalMode === mode.value
							? 'text-gray-900 dark:text-white bg-gray-50 dark:bg-white/5'
							: 'text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-900 dark:hover:text-white'}"
						onclick={() => selectMode(mode.value)}
						use:tooltip={{ content: mode.desc, placement: 'right' }}
					>
						<span class="flex-1 text-left truncate">{mode.label}</span>
						{#if toolApprovalMode === mode.value}
							<svg
								class="w-3 h-3 shrink-0 text-gray-400 dark:text-gray-500"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2.5"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<polyline points="20 6 9 17 4 12" />
							</svg>
						{/if}
					</button>
				{/each}
			</div>
		{:else if tab === 'request_params'}
			<!-- Request params submenu -->
			<div class="plus-menu-slide-in-right">
				<button
					class="flex items-center gap-2 w-full h-7 px-2 rounded-xl text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-900 dark:hover:text-white transition-colors duration-75"
					onclick={() => (tab = '')}
				>
					<Icon name="chevron-left" size={12} />
					<span class="flex-1 text-left font-medium">{$t('plusMenu.parameters')}</span>
				</button>

				<div class="app-divider h-px mx-1 my-0.5"></div>

				{#if paramRows.length === 0}
					<p
						class="px-2 h-7 flex items-center justify-center text-[0.6875rem] text-gray-400 dark:text-gray-500"
					>
						{$t('plusMenu.noParams')}
					</p>
				{:else}
					<div class="px-0.5 py-0.5 flex flex-col max-h-48 overflow-y-auto">
						{#each paramRows as row, i}
							<div class="group/row flex items-center gap-1 px-1.5 h-6">
								<input
									type="text"
									placeholder={$t('plusMenu.paramKey')}
									bind:value={row.key}
									onblur={syncParams}
									autocomplete="off"
									spellcheck="false"
									class="w-[4.5rem] shrink-0 bg-transparent text-[0.6875rem] font-mono text-gray-600 dark:text-gray-400 placeholder:text-gray-300 dark:placeholder:text-gray-600 outline-none"
								/>
								<input
									type="text"
									placeholder={$t('plusMenu.paramValue')}
									bind:value={row.value}
									onblur={syncParams}
									autocomplete="off"
									spellcheck="false"
									class="flex-1 min-w-0 bg-transparent text-[0.6875rem] font-mono text-gray-600 dark:text-gray-400 placeholder:text-gray-300 dark:placeholder:text-gray-600 outline-none"
								/>
								<button
									type="button"
									onclick={() => removeParamRow(i)}
									class="shrink-0 text-gray-300 dark:text-gray-700 opacity-0 group-hover/row:opacity-100 hover:text-gray-500 dark:hover:text-gray-400 transition-colors duration-75"
									aria-label={$t('plusMenu.removeParam')}
								>
									<Icon name="xmark" size={8} />
								</button>
							</div>
						{/each}
					</div>
				{/if}

				<button
					class="flex items-center gap-2 w-full h-7 px-2 rounded-xl text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-900 dark:hover:text-white transition-colors duration-75 disabled:opacity-30 disabled:pointer-events-none"
					onclick={addParamRow}
					disabled={!canAddParam}
				>
					<Icon name="plus" size={12} />
					<span class="flex-1 text-left">{$t('plusMenu.addParam')}</span>
				</button>
			</div>
		{:else if tab === 'reasoning'}
			<!-- Reasoning level submenu -->
			<div class="plus-menu-slide-in-right">
				<button
					class="flex items-center gap-2 w-full h-7 px-2 rounded-xl text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-900 dark:hover:text-white transition-colors duration-75"
					onclick={() => (tab = '')}
				>
					<Icon name="chevron-left" size={12} />
					<span class="flex-1 text-left font-medium">{$t('plusMenu.reasoning')}</span>
				</button>

				<div class="app-divider h-px mx-1 my-0.5"></div>

				{#each reasoningLevels as level}
					<button
						class="flex items-center gap-2 w-full h-7 px-2 rounded-xl text-xs transition-colors duration-75
							{reasoningLevel === level.value
							? 'text-gray-900 dark:text-white bg-gray-50 dark:bg-white/5'
							: 'text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-900 dark:hover:text-white'}"
						onclick={() => selectReasoning(level.value)}
					>
						<span class="flex-1 text-left truncate">{level.label}</span>
						{#if reasoningLevel === level.value}
							<svg
								class="w-3 h-3 shrink-0 text-gray-400 dark:text-gray-500"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2.5"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<polyline points="20 6 9 17 4 12" />
							</svg>
						{/if}
					</button>
				{/each}
			</div>
		{:else if tab === 'compaction'}
			<!-- Compaction submenu -->
			<div class="plus-menu-slide-in-right">
				<button
					class="flex items-center gap-2 w-full h-7 px-2 rounded-xl text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-900 dark:hover:text-white transition-colors duration-75"
					onclick={() => (tab = '')}
				>
					<Icon name="chevron-left" size={12} />
					<span class="flex-1 text-left font-medium">{$t('plusMenu.compaction')}</span>
				</button>

				<div class="app-divider h-px mx-1 my-0.5"></div>

				<div class="flex items-center gap-2 w-full h-7 px-2">
					<span class="flex-1 text-left text-xs text-gray-500 dark:text-gray-400"
						>{$t('plusMenu.compactThreshold')}</span
					>
					<input
						type="number"
						min={COMPACT_TOKEN_THRESHOLD_MIN}
						step={COMPACT_TOKEN_THRESHOLD_STEP}
						inputmode="numeric"
						placeholder={String(
							compactTokenThresholdConfig ?? COMPACT_TOKEN_THRESHOLD_DEFAULT
						)}
						autocomplete="off"
						bind:value={compactTokenThreshold}
						onblur={clampCompactTokenThreshold}
						onkeydown={handleCompactTokenThresholdKeydown}
						class="compact-keep w-16 shrink-0 rounded bg-gray-50 dark:bg-white/5 px-1 py-0.5 text-center text-xs font-mono text-gray-700 dark:text-gray-300 placeholder:text-gray-300 dark:placeholder:text-gray-600 outline-none"
					/>
					<span class="flex flex-col shrink-0">
						<button
							type="button"
							aria-label="Increase compaction threshold"
							onclick={() => stepCompactTokenThreshold(1)}
							class="flex items-center justify-center w-[18px] h-[13px] rounded text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-white/10 transition-colors duration-100 cursor-pointer"
						>
							<Icon name="chevron-up" size={10} />
						</button>
						<button
							type="button"
							aria-label="Decrease compaction threshold"
							onclick={() => stepCompactTokenThreshold(-1)}
							class="flex items-center justify-center w-[18px] h-[13px] rounded text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-white/10 transition-colors duration-100 cursor-pointer"
						>
							<Icon name="chevron-down" size={10} />
						</button>
					</span>
					<span class="w-10 text-left text-xs text-gray-400 dark:text-gray-500">
						{$t('plusMenu.compactTokens')}
					</span>
				</div>

				<div class="flex items-center gap-2 w-full h-7 px-2">
					<span class="flex-1 text-left text-xs text-gray-500 dark:text-gray-400"
						>{$t('plusMenu.compactKeep')}</span
					>
					<input
						type="number"
						min={COMPACT_KEEP_MIN}
						max={COMPACT_KEEP_MAX}
						step={COMPACT_KEEP_STEP}
						inputmode="numeric"
						placeholder="40"
						autocomplete="off"
						bind:value={compactKeepPercent}
						onblur={clampCompactKeep}
						onkeydown={handleCompactKeepKeydown}
						class="compact-keep w-16 shrink-0 rounded bg-gray-50 dark:bg-white/5 px-1 py-0.5 text-center text-xs font-mono text-gray-700 dark:text-gray-300 placeholder:text-gray-300 dark:placeholder:text-gray-600 outline-none"
					/>
					<span class="flex flex-col shrink-0">
						<button
							type="button"
							aria-label="Increase keep percent"
							onclick={() => stepCompactKeep(1)}
							class="flex items-center justify-center w-[18px] h-[13px] rounded text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-white/10 transition-colors duration-100 cursor-pointer"
						>
							<Icon name="chevron-up" size={10} />
						</button>
						<button
							type="button"
							aria-label="Decrease keep percent"
							onclick={() => stepCompactKeep(-1)}
							class="flex items-center justify-center w-[18px] h-[13px] rounded text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-white/10 transition-colors duration-100 cursor-pointer"
						>
							<Icon name="chevron-down" size={10} />
						</button>
					</span>
					<span class="w-10 text-left text-xs text-gray-400 dark:text-gray-500">%</span>
				</div>
			</div>
		{/if}
	</div>
{/if}

<style>
	@reference "../../../app.css";

	.plus-menu-slide-in-left {
		animation: slideFromLeft 0.15s ease-out;
	}

	.plus-menu-slide-in-right {
		animation: slideFromRight 0.15s ease-out;
	}

	@keyframes slideFromLeft {
		from {
			opacity: 0;
			transform: translateX(-0.75rem);
		}
		to {
			opacity: 1;
			transform: translateX(0);
		}
	}

	@keyframes slideFromRight {
		from {
			opacity: 0;
			transform: translateX(0.75rem);
		}
		to {
			opacity: 1;
			transform: translateX(0);
		}
	}

	.compact-keep::-webkit-outer-spin-button,
	.compact-keep::-webkit-inner-spin-button {
		-webkit-appearance: none;
		margin: 0;
	}

	.compact-keep {
		appearance: textfield;
		-moz-appearance: textfield;
	}
</style>
