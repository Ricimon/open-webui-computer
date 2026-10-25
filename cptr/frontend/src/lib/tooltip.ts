import tippy, { type Instance, type Props as TippyProps } from 'tippy.js';
import 'tippy.js/dist/tippy.css';
import { mount, unmount } from 'svelte';
import MarkdownRenderer from '$lib/components/markdown/MarkdownRenderer.svelte';

/**
 * Tooltip params for a compaction indicator. With a summary, renders the
 * base label, a bold heading, and the summary as markdown via
 * MarkdownRenderer (no raw HTML is injected). `base` and `heading` are
 * trusted locale strings; `detail` (the summary) is untrusted and is only
 * ever rendered through the markdown component.
 */
export function summaryTooltip(
	base: string,
	heading: string,
	detail?: string | null
): string | (Partial<TippyProps> & { _cleanup?: () => void }) {
	if (!detail) return base;

	const wrap = document.createElement('div');
	const baseEl = document.createElement('div');
	baseEl.textContent = base;
	wrap.appendChild(baseEl);
	const headEl = document.createElement('b');
	headEl.textContent = heading;
	wrap.appendChild(headEl);
	const mdHost = document.createElement('div');
	wrap.appendChild(mdHost);

	const exported = mount(MarkdownRenderer, { target: mdHost, props: { content: detail } });
	let disposed = false;
	const cleanup = () => {
		if (!disposed) {
			disposed = true;
			unmount(exported);
		}
	};

	return {
		content: wrap,
		// Size to the longest line of text, capped within the viewport.
		maxWidth: 'calc(100vw - 40px)',
		_cleanup: cleanup
	};
}

type TooltipParams =
	| string
	| (Partial<TippyProps> & { _cleanup?: () => void })
	| null
	| undefined
	| false;

/**
 * Svelte action for tippy.js tooltips.
 * Usage: <button use:tooltip={'New terminal'}>
 *    or: <button use:tooltip={{ content: 'New terminal', placement: 'bottom' }}>
 */
export function tooltip(node: HTMLElement, params: TooltipParams) {
	type Parsed = { opts: Partial<TippyProps>; cleanup?: () => void };

	function parse(value: TooltipParams): Parsed | null {
		if (!value) return null;
		const raw: Partial<TippyProps> & { _cleanup?: () => void } =
			typeof value === 'string' ? { content: value } : value;
		if (!raw.content) return null;
		const { _cleanup, ...opts } = raw;
		return { opts, cleanup: _cleanup };
	}

	function create(opts: Partial<TippyProps>) {
		return tippy(node, {
			arrow: false,
			delay: [400, 0],
			duration: [100, 75],
			placement: 'bottom',
			theme: 'cptr',
			touch: false,
			...opts
		});
	}

	let instance: Instance | null = null;
	let cleanup: (() => void) | undefined;
	const runCleanup = () => {
		const fn = cleanup;
		cleanup = undefined;
		fn?.();
	};

	const initial = parse(params);
	if (initial) {
		instance = create(initial.opts);
		cleanup = initial.cleanup;
	}

	return {
		update(newParams: TooltipParams) {
			const next = parse(newParams);
			if (!next) {
				runCleanup();
				instance?.destroy();
				instance = null;
				return;
			}
			if (instance) {
				runCleanup();
				instance.setProps(next.opts);
			} else {
				instance = create(next.opts);
			}
			cleanup = next.cleanup;
		},
		destroy() {
			runCleanup();
			instance?.destroy();
			instance = null;
		}
	};
}
