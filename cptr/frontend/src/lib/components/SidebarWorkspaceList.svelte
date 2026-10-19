<script lang="ts">
	import { goto } from '$app/navigation';
	import {
		workspaceList,
		removeWorkspace,
		reorderWorkspaces,
		sidebarOpen,
		activeTab,
		currentWorkspace,
		setActiveTab,
		type Tab
	} from '$lib/stores';
	import { chatEnabled, chatStatuses, updateChatStatuses, type ChatStatus } from '$lib/stores/chat';
	import { socketStore } from '$lib/stores/socket.svelte';
	import {
		deleteChat as apiDeleteChat,
		getChats,
		updateChatTitle,
		type ChatInfo
	} from '$lib/apis/chat';
	import { t } from '$lib/i18n';
	import { tooltip } from '$lib/tooltip';
	import Sortable from 'sortablejs';
	import { onDestroy, onMount } from 'svelte';
	import ChatItem from './common/ChatItem.svelte';
	import DropdownMenu from './DropdownMenu.svelte';
	import Icon from './Icon.svelte';

	interface Props {
		onaddworkspace: () => void;
	}

	let { onaddworkspace }: Props = $props();
	let wsMenuPath = $state<string | null>(null);
	let wsMenuAnchor = $state<HTMLElement | null>(null);
	let chatMenu = $state<{ chatId: string; wsPath: string; anchor: HTMLElement } | null>(null);
	let wsListEl: HTMLDivElement | undefined = $state();
	let sortable: Sortable | null = null;
	let unbindSocketListener: (() => void) | null = null;
	let workspacesExpanded = $state(true);

	/** Workspace paths the user explicitly collapsed; the active workspace starts expanded. */
	let collapsedWorkspaces = $state<Set<string>>(new Set());
	let wsChatsCache = $state<Map<string, ChatInfo[]>>(new Map());
	let wsChatsHasMore = $state<Map<string, boolean>>(new Map());
	let wsChatsLoading = $state<Set<string>>(new Set());
	let currentPath = $derived($currentWorkspace?.path ?? null);
	let currentChatId = $derived($activeTab?.type === 'chat' ? $activeTab.path : null);
	const WS_CHATS_PAGE_SIZE = 5;

	// ── Chat tree for the active workspace ──────────────────────────────
	// Chats with an open tab are always listed first (in tab order, one row
	// per chat) and are never truncated; only the fetched server list is
	// paginated through "Show more".

	const openChatTabEntries = $derived.by(() => {
		const ws = $currentWorkspace;
		if (!ws) return [];
		const seen = new Set<string>();
		const entries: { tab: Tab; groupId: string }[] = [];
		for (const group of ws.groups) {
			for (const tab of group.tabs) {
				if (tab.type !== 'chat' || !tab.path || seen.has(tab.path)) continue;
				seen.add(tab.path);
				entries.push({ tab, groupId: group.id });
			}
		}
		return entries;
	});

	const openChatIds = $derived(new Set(openChatTabEntries.map((entry) => entry.tab.path as string)));

	const serverChatById = $derived.by(() => {
		const map = new Map<string, ChatInfo>();
		for (const chat of wsChatsCache.get(currentPath ?? '') ?? []) map.set(chat.id, chat);
		return map;
	});

	const closedChats = $derived.by(() => {
		const loaded = wsChatsCache.get(currentPath ?? '');
		if (!loaded) return [];
		return loaded.filter((chat) => !openChatIds.has(chat.id));
	});

	function chatInfoForTab(tab: Tab, status: ChatStatus | undefined): ChatInfo {
		const id = tab.path as string;
		const server = serverChatById.get(id);
		const isPending = id.startsWith('new-') || id.startsWith('pending-');
		return {
			id,
			title: server?.title ?? (isPending ? $t('bar.newChat') : tab.label || 'Chat'),
			summary: null,
			folder: '',
			meta: null,
			current_message_id: null,
			created_at: server?.created_at ?? 0,
			updated_at: server?.updated_at ?? status?.updatedAt ?? 0,
			last_read_at: server?.last_read_at ?? status?.lastReadAt ?? null,
			is_active: server?.is_active ?? status?.active ?? false
		};
	}

	type TreeRow =
		| { key: string; kind: 'open'; chat: ChatInfo; tab: Tab; groupId: string }
		| { key: string; kind: 'closed'; chat: ChatInfo }
		| { key: string; kind: 'loading' }
		| { key: string; kind: 'more' };

	const treeRows = $derived.by(() => {
		const rows: TreeRow[] = openChatTabEntries.map((entry) => {
			const status = $chatStatuses.get(entry.tab.path as string);
			return {
				key: `open-${entry.tab.id}`,
				kind: 'open' as const,
				chat: chatInfoForTab(entry.tab, status),
				tab: entry.tab,
				groupId: entry.groupId
			};
		});
		for (const chat of closedChats) rows.push({ key: `closed-${chat.id}`, kind: 'closed', chat });
		const path = currentPath;
		if (path && !wsChatsCache.has(path)) {
			rows.push({ key: 'loading', kind: 'loading' });
		} else if (path && wsChatsHasMore.get(path)) {
			rows.push({ key: 'more', kind: 'more' });
		}
		return rows;
	});

	// The active workspace's dropdown is open by default: fetch (or refresh)
	// its chat list whenever the active workspace changes.
	let lastFetchedPath: string | null = null;
	$effect(() => {
		const path = currentPath;
		if (!path || !$chatEnabled) return;
		if (path === lastFetchedPath) return;
		lastFetchedPath = path;
		reloadWorkspaceChats(path);
	});

	function toggleActiveWorkspaceExpand(path: string) {
		const next = new Set(collapsedWorkspaces);
		if (next.has(path)) next.delete(path);
		else next.add(path);
		collapsedWorkspaces = next;
	}

	async function fetchWorkspaceChats(path: string, append = false, limit = WS_CHATS_PAGE_SIZE) {
		if (wsChatsLoading.has(path)) return;
		wsChatsLoading = new Set([...wsChatsLoading, path]);
		try {
			const existing = wsChatsCache.get(path) ?? [];
			const data = await getChats(
				path,
				append ? WS_CHATS_PAGE_SIZE : limit,
				append ? existing.length : 0,
				'updated_at',
				'desc'
			);
			wsChatsCache = new Map([
				...wsChatsCache,
				[
					path,
					append
						? [...existing, ...(data.chats || [])].sort(
								(a, b) =>
									Number(
										!b.is_active && (b.last_read_at === null || b.updated_at > b.last_read_at)
									) -
										Number(
											!a.is_active && (a.last_read_at === null || a.updated_at > a.last_read_at)
										) || b.updated_at - a.updated_at
							)
						: data.chats || []
				]
			]);
			updateChatStatuses(data.chats || [], path);
			wsChatsHasMore = new Map([...wsChatsHasMore, [path, data.has_more]]);
		} catch {
			wsChatsCache = new Map([...wsChatsCache, [path, []]]);
			wsChatsHasMore = new Map([...wsChatsHasMore, [path, false]]);
		} finally {
			const next = new Set(wsChatsLoading);
			next.delete(path);
			wsChatsLoading = next;
		}
	}

	function reloadWorkspaceChats(path: string) {
		const loadedCount = wsChatsCache.get(path)?.length ?? WS_CHATS_PAGE_SIZE;
		void fetchWorkspaceChats(path, false, Math.max(loadedCount, WS_CHATS_PAGE_SIZE));
	}

	function closeMobileSidebar() {
		if (typeof window !== 'undefined' && window.innerWidth < 768) sidebarOpen.set(false);
	}

	function openWorkspace(e: MouseEvent, path: string) {
		if (e.metaKey || e.ctrlKey) return;
		e.preventDefault();
		goto(`/?workspace=${encodeURIComponent(path)}`);
		closeMobileSidebar();
	}

	function openChat(chatId: string, wsPath: string) {
		goto(`/?workspace=${encodeURIComponent(wsPath)}&chatId=${encodeURIComponent(chatId)}`);
		closeMobileSidebar();
	}

	function newChat(wsPath: string) {
		goto(`/?workspace=${encodeURIComponent(wsPath)}&chatId`);
		closeMobileSidebar();
	}

	function openWsMenu(e: MouseEvent, path: string) {
		e.stopPropagation();
		e.preventDefault();
		closeChatMenu();
		wsMenuAnchor = e.currentTarget as HTMLElement;
		wsMenuPath = path;
	}

	function closeWsMenu() {
		wsMenuPath = null;
		wsMenuAnchor = null;
	}

	function openChatMenu(e: MouseEvent, chatId: string, wsPath: string) {
		e.stopPropagation();
		closeWsMenu();
		chatMenu = { chatId, wsPath, anchor: e.currentTarget as HTMLElement };
	}

	function closeChatMenu() {
		chatMenu = null;
	}

	async function handleRemoveWorkspace(path: string) {
		closeWsMenu();
		await removeWorkspace(path);
		if (currentPath === path) goto('/');
	}

	async function handleDeleteChat() {
		if (!chatMenu) return;
		const { chatId, wsPath } = chatMenu;
		closeChatMenu();
		await apiDeleteChat(chatId);
		const chats = wsChatsCache.get(wsPath) ?? [];
		wsChatsCache = new Map([...wsChatsCache, [wsPath, chats.filter((chat) => chat.id !== chatId)]]);
		if (currentPath === wsPath && currentChatId === chatId) {
			goto(`/?workspace=${encodeURIComponent(wsPath)}`);
		}
	}

	async function handleRenameChat() {
		if (!chatMenu) return;
		const { chatId, wsPath } = chatMenu;
		const chat = (wsChatsCache.get(wsPath) ?? []).find((item) => item.id === chatId);
		const title = window.prompt($t('files.rename'), chat?.title)?.trim();
		if (!title || title === chat?.title) return;
		await updateChatTitle(chatId, title);
		const chats = wsChatsCache.get(wsPath) ?? [];
		wsChatsCache = new Map([
			...wsChatsCache,
			[wsPath, chats.map((item) => (item.id === chatId ? { ...item, title } : item))]
		]);
	}

	function copyChatPath() {
		if (!chatMenu) return;
		const { chatId, wsPath } = chatMenu;
		const chat = (wsChatsCache.get(wsPath) ?? []).find((item) => item.id === chatId);
		if (!chat) return;
		navigator.clipboard.writeText(
			`${wsPath.replace(/\/$/, '')}/.cptr/chats/${chat.folder ? `${chat.folder}/` : ''}${chat.id}.json`
		);
	}

	function handleChatEvent(data: {
		type?: string;
		chat_id: string;
		done?: boolean;
		title?: string;
		delta?: string;
		workspace?: string;
		active?: boolean;
		updated_at?: number;
		last_read_at?: number;
		workspace_unread_count?: number;
	}) {
		if (
			!data.title &&
			typeof data.active !== 'boolean' &&
			typeof data.updated_at !== 'number' &&
			typeof data.last_read_at !== 'number' &&
			typeof data.workspace_unread_count !== 'number'
		) {
			return;
		}
		const unreadCount = data.workspace_unread_count;
		if (data.workspace && typeof unreadCount === 'number') {
			workspaceList.update((workspaces) =>
				workspaces.map((workspace) =>
					workspace.path === data.workspace
						? { ...workspace, unread_count: unreadCount }
						: workspace
				)
			);
		}

		let known = false;
		const shouldReorder =
			typeof data.updated_at === 'number' ||
			typeof data.last_read_at === 'number' ||
			typeof data.active === 'boolean';

		wsChatsCache = new Map(
			[...wsChatsCache].map(([path, chats]) => {
				const nextChats = chats.map((chat) => {
					if (chat.id !== data.chat_id) return chat;
					known = true;
					return {
						...chat,
						...(data.title ? { title: data.title } : {}),
						...(typeof data.updated_at === 'number' ? { updated_at: data.updated_at } : {}),
						...(typeof data.last_read_at === 'number' ? { last_read_at: data.last_read_at } : {}),
						...(typeof data.active === 'boolean' ? { is_active: data.active } : {})
					};
				});
				return [
					path,
					shouldReorder
						? nextChats.sort(
								(a, b) =>
									Number(
										!b.is_active && (b.last_read_at === null || b.updated_at > b.last_read_at)
									) -
										Number(
											!a.is_active && (a.last_read_at === null || a.updated_at > a.last_read_at)
										) || b.updated_at - a.updated_at
							)
						: nextChats
				] as [string, ChatInfo[]];
			})
		);

		// A chat created in another session is not yet in this sidebar's page.
		// Refresh only that expanded workspace; all known rows update in place.
		if (!known && data.workspace && data.workspace === currentPath) {
			reloadWorkspaceChats(data.workspace);
		} else if (known && typeof data.last_read_at === 'number' && data.workspace === currentPath) {
			reloadWorkspaceChats(data.workspace);
		}
	}

	function isTouchDevice(): boolean {
		return (
			typeof window !== 'undefined' && ('ontouchstart' in window || navigator.maxTouchPoints > 0)
		);
	}

	onMount(() => {
		if (wsListEl && !isTouchDevice()) {
			sortable = Sortable.create(wsListEl, {
				animation: 150,
				ghostClass: 'opacity-30',
				dragClass: 'cursor-grabbing',
				direction: 'vertical',
				onEnd: (evt) => {
					if (evt.oldIndex != null && evt.newIndex != null && evt.oldIndex !== evt.newIndex) {
						reorderWorkspaces(evt.oldIndex, evt.newIndex);
					}
				}
			});
		}

		unbindSocketListener = socketStore.on('events:chat', handleChatEvent);
	});

	onDestroy(() => {
		sortable?.destroy();
		unbindSocketListener?.();
		unbindSocketListener = null;
	});
</script>

<div class="flex items-center justify-between h-8 pl-3.5 pr-1.5 shrink-0">
	<button
		class="group flex flex-1 h-full items-center gap-1 text-left text-xs text-gray-400 hover:text-gray-500 dark:text-gray-500 dark:hover:text-gray-400 transition-colors duration-100"
		onclick={() => (workspacesExpanded = !workspacesExpanded)}
		aria-expanded={workspacesExpanded}
		aria-controls="workspace-list"
	>
		<span>{$t('sidebar.workspaces')}</span>
		<span
			class="flex opacity-0 group-hover:opacity-100 transition-all duration-100"
			style="transform: rotate({workspacesExpanded ? '90deg' : '0deg'})"
		>
			<Icon name="chevron-right" size={11} />
		</span>
	</button>
	<button
		class="flex items-center justify-center w-7 h-7 rounded-lg text-gray-300 hover:text-gray-500 dark:text-gray-600 dark:hover:text-gray-400 transition-colors duration-100"
		onclick={onaddworkspace}
		aria-label={$t('sidebar.addWorkspace')}
		use:tooltip={$t('sidebar.addWorkspace')}
	>
		<Icon name="plus" size={14} />
	</button>
</div>

<div
	id="workspace-list"
	bind:this={wsListEl}
	class="flex-1 overflow-y-auto px-1.5"
	class:invisible={!workspacesExpanded}
>
	{#each $workspaceList as ws (ws.path)}
		{@const isExpanded = $chatEnabled && ws.path === currentPath && !collapsedWorkspaces.has(ws.path)}
		<div class="ws-item">
			<div
				class="group flex items-center gap-1 w-full h-7 px-2 rounded-lg text-xs font-medium transition-colors duration-100
				{ws.path === currentPath
					? 'bg-gray-200/50 text-gray-900 dark:bg-white/8 dark:text-white'
					: 'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'}"
			>
				<a
					href="/?workspace={encodeURIComponent(ws.path)}"
					class="flex items-center gap-1.5 flex-1 min-w-0 no-underline text-inherit"
					onclick={(e) => openWorkspace(e, ws.path)}
				>
					{#if $chatEnabled && ws.path === currentPath}
						<span
							class="ws-icon-toggle shrink-0"
							role="button"
							tabindex="-1"
							onclick={(e) => {
								e.stopPropagation();
								e.preventDefault();
								toggleActiveWorkspaceExpand(ws.path);
							}}
							aria-label={isExpanded ? $t('sidebar.collapse') : $t('sidebar.expand')}
						>
							<span class="ws-icon-folder"><Icon name="folder" size={14} /></span>
							<span
								class="ws-icon-chevron"
								style="transform: rotate({isExpanded ? '90deg' : '0deg'})"
							>
								<Icon name="chevron-right" size={11} />
							</span>
						</span>
					{:else}
						<Icon name="folder" size={14} />
					{/if}
					<span class="min-w-0 truncate text-left">{ws.name}</span>
					{#if ws.unread_count > 0}
						<span
							class="inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-md bg-sky-500/10 px-1 text-[0.625rem] font-semibold text-sky-600 dark:bg-sky-400/10 dark:text-sky-300"
						>
							{new Intl.NumberFormat(undefined, {
								notation: 'compact',
								compactDisplay: 'short'
							}).format(ws.unread_count)}
						</span>
					{/if}
				</a>
				<span
					class="flex items-center justify-center w-4 h-4 shrink-0 text-gray-400 opacity-0 group-hover:opacity-100 hover:text-gray-600 dark:hover:text-gray-300 transition-all duration-75"
					role="button"
					tabindex="-1"
					onclick={(e) => openWsMenu(e, ws.path)}
					aria-label={$t('sidebar.workspaceOptions')}
				>
					<Icon name="three-dots" size={11} />
				</span>
				{#if $chatEnabled}
					<span
						class="flex items-center justify-center w-4 h-4 shrink-0 text-gray-400 opacity-0 group-hover:opacity-100 hover:text-gray-600 dark:hover:text-gray-300 transition-all duration-75"
						role="button"
						tabindex="-1"
						onclick={() => newChat(ws.path)}
						aria-label={$t('bar.newChat')}
						use:tooltip={$t('bar.newChat')}
					>
						<Icon name="pencil" size={11} />
					</span>
				{/if}
			</div>

			{#if isExpanded}
				<div class="ws-chats">
					{#each treeRows as row, i (row.key)}
						<div
							class="ws-tree-row"
							class:ws-tree-row--chat={row.kind === 'open' || row.kind === 'closed'}
							class:ws-tree-row--last={i === treeRows.length - 1}
						>
							{#if row.kind === 'open'}
								<ChatItem
									chat={row.chat}
									isSelected={row.chat.id === currentChatId}
									onclick={() => setActiveTab(row.tab.id, row.groupId)}
								/>
							{:else if row.kind === 'closed'}
								<ChatItem
									chat={row.chat}
									isSelected={row.chat.id === currentChatId}
									onclick={() => openChat(row.chat.id, ws.path)}
									onmenu={(e) => openChatMenu(e, row.chat.id, ws.path)}
								/>
							{:else if row.kind === 'loading'}
								<div class="ws-chat-loading">
									<span class="ws-chat-loading-dot"></span>
									<span class="ws-chat-loading-dot"></span>
									<span class="ws-chat-loading-dot"></span>
								</div>
							{:else}
								<button
									class="ws-chat-show-more"
									disabled={wsChatsLoading.has(ws.path)}
									onclick={() => fetchWorkspaceChats(ws.path, true)}
								>
									{$t('sidebar.showMore')}
								</button>
							{/if}
						</div>
					{/each}
				</div>
			{/if}
		</div>
	{/each}

	{#if $workspaceList.length === 0}
		<div class="flex flex-col items-center justify-center py-12">
			<p class="text-xs text-gray-400 dark:text-gray-600">{$t('sidebar.noWorkspaces')}</p>
		</div>
	{/if}
</div>

{#if wsMenuPath && wsMenuAnchor}
	<DropdownMenu
		anchor={wsMenuAnchor}
		items={[
			{
				label: $t('sidebar.remove'),
				icon: 'xmark',
				onclick: () => handleRemoveWorkspace(wsMenuPath!)
			}
		]}
		onclose={closeWsMenu}
	/>
{/if}

{#if chatMenu}
	<DropdownMenu
		anchor={chatMenu.anchor}
		align="end"
		items={[
			{
				label: $t('files.copyPath'),
				icon: 'copy',
				onclick: copyChatPath
			},
			{
				label: $t('files.rename'),
				icon: 'pencil',
				onclick: handleRenameChat
			},
			{
				label: $t('chat.history.delete'),
				icon: 'trash',
				onclick: handleDeleteChat
			}
		]}
		onclose={closeChatMenu}
	/>
{/if}

<style>
	@reference "../../app.css";

	.ws-item {
		margin-bottom: 0.125rem;
	}

	.ws-icon-toggle {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 0.875rem;
		height: 0.875rem;
		cursor: pointer;
	}

	.ws-icon-folder {
		display: flex;
		transition: opacity 0.1s;
	}

	.ws-icon-chevron {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		opacity: 0;
		transition:
			opacity 0.1s,
			transform 0.1s;
		color: var(--app-fg-subtle);
	}

	:global(.dark) .ws-icon-chevron {
		color: var(--app-fg-muted);
	}

	.ws-icon-toggle:hover .ws-icon-folder {
		opacity: 0;
	}

	.ws-icon-toggle:hover .ws-icon-chevron {
		opacity: 1;
	}

	.ws-chats {
		margin-top: 0;
		padding-bottom: 0.25rem;
	}

	.ws-tree-row {
		position: relative;
		padding-left: 1.75rem;
	}

	.ws-tree-row::before {
		content: '';
		position: absolute;
		left: 0.9375rem;
		top: 0;
		bottom: 0;
		width: 1px;
		background: color-mix(in oklab, var(--app-fg) 20%, transparent);
	}

	.ws-tree-row--last::before {
		bottom: 50%;
	}

	.ws-tree-row--chat::after {
		content: '';
		position: absolute;
		left: 0.9375rem;
		top: 50%;
		width: 0.6875rem;
		height: 1px;
		background: color-mix(in oklab, var(--app-fg) 20%, transparent);
	}

	.ws-chat-show-more {
		display: flex;
		align-items: center;
		width: 100%;
		height: 1.75rem;
		padding: 0;
		border: none;
		background: none;
		cursor: pointer;
		font-size: 0.6875rem;
		color: var(--app-fg-subtle);
		text-align: left;
		transition: color 0.1s;
	}

	.ws-chat-show-more:hover {
		color: var(--app-fg);
	}

	.ws-chat-loading {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		height: 1.75rem;
	}

	.ws-chat-loading-dot {
		width: 0.25rem;
		height: 0.25rem;
		border-radius: 50%;
		background: var(--app-fg-subtle);
		animation: dotPulse 1s ease-in-out infinite;
	}

	:global(.dark) .ws-chat-loading-dot {
		background: var(--app-fg-muted);
	}

	.ws-chat-loading-dot:nth-child(2) {
		animation-delay: 0.15s;
	}

	.ws-chat-loading-dot:nth-child(3) {
		animation-delay: 0.3s;
	}

	@keyframes dotPulse {
		0%,
		100% {
			opacity: 0.3;
		}
		50% {
			opacity: 1;
		}
	}
</style>
