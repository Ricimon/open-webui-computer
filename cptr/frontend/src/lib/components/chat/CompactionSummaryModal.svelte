<script lang="ts">
	import Modal from '../Modal.svelte';
	import Icon from '../Icon.svelte';
	import MarkdownRenderer from '../markdown/MarkdownRenderer.svelte';
	import { t } from '$lib/i18n';

	interface Props {
		summary: string;
		onclose: () => void;
	}

	let { summary, onclose }: Props = $props();
</script>

<Modal {onclose} class="w-[min(92vw,32rem)] flex flex-col max-h-[80vh]">
	<div class="flex items-center justify-between gap-3 px-5 py-3.5 border-b border-gray-200 dark:border-white/6">
		<div class="flex items-center gap-2 min-w-0">
			<Icon name="archive" size={15} class="shrink-0 text-sky-500 dark:text-sky-400" />
			<h2 class="text-sm font-medium truncate">{$t('chat.compactionSummary')}</h2>
		</div>
		<button
			type="button"
			aria-label={$t('common.close')}
			class="shrink-0 rounded-lg p-1.5 text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-white/6 transition-colors duration-100"
			onclick={onclose}
		>
			<Icon name="xmark" size={14} />
		</button>
	</div>
	<div class="overflow-y-auto min-h-0 px-5 py-4">
		<MarkdownRenderer content={summary} />
	</div>
</Modal>
