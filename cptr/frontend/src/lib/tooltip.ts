import tippy, { type Instance, type Props as TippyProps } from 'tippy.js';
import 'tippy.js/dist/tippy.css';

type TooltipParams = string | Partial<TippyProps> | null | undefined | false;

/**
 * Svelte action for tippy.js tooltips.
 * Usage: <button use:tooltip={'New terminal'}>
 *    or: <button use:tooltip={{ content: 'New terminal', placement: 'bottom' }}>
 */
export function tooltip(node: HTMLElement, params: TooltipParams) {
	function optsFor(value: TooltipParams): Partial<TippyProps> | null {
		if (!value) return null;
		const opts: Partial<TippyProps> = typeof value === 'string' ? { content: value } : value;
		return opts.content ? opts : null;
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
	const opts = optsFor(params);
	if (opts) instance = create(opts);

	return {
		update(newParams: TooltipParams) {
			const newOpts = optsFor(newParams);
			if (!newOpts) {
				instance?.destroy();
				instance = null;
				return;
			}
			if (instance) instance.setProps(newOpts);
			else instance = create(newOpts);
		},
		destroy() {
			instance?.destroy();
		}
	};
}
