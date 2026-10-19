/**
 * Centralized git status store.
 *
 * Single source of truth – replaces scattered getGitStatus() calls
 * across layout, GitBar, GitView, and FileEditor.
 *
 * Components read from this store; mutations (commit, stage, etc.)
 * call refresh() to update everyone at once.
 */

import { fetchJSON } from '$lib/apis';

export interface GitFile {
	path: string;
	status: string;
	staged: boolean;
	unstaged?: boolean;
	staged_status?: string;
	unstaged_status?: string;
	binary?: boolean;
	additions?: number;
	deletions?: number;
}

export interface GitStatus {
	is_repo: boolean;
	branch: string;
	upstream?: string;
	remote_url?: string;
	ahead: number;
	behind: number;
	files: GitFile[];
}

let _status = $state<GitStatus | null>(null);
let _loading = $state(false);
let _currentRoot = '';
let _fetchPromise: Promise<void> | null = null;
let _fetchSeq = 0;
let _pendingForceRefresh = false;

async function _doFetch(root: string, seq: number) {
	if (!root) {
		_status = null;
		return;
	}
	_loading = true;
	try {
		const data = (await fetchJSON(`/api/git/status?root=${encodeURIComponent(root)}`)) as GitStatus;
		if (root === _currentRoot && seq === _fetchSeq) {
			_status = data;
		}
	} catch {
		// ignore errors; keep existing state
		if (root === _currentRoot && seq === _fetchSeq) {
			_status = null;
		}
	} finally {
		if (seq === _fetchSeq) {
			_loading = false;
			_fetchPromise = null;
			// If a forced refresh was requested while this fetch was in
			// flight, run exactly one follow-up fetch (no stacked
			// pipelines of overlapping requests).
			if (_pendingForceRefresh && root === _currentRoot) {
				_pendingForceRefresh = false;
				_fetchPromise = _doFetch(root, ++_fetchSeq);
			}
		}
	}
}

export const gitStatusStore = {
	get status() {
		return _status;
	},
	get loading() {
		return _loading;
	},
	get isRepo() {
		return _status?.is_repo ?? false;
	},

	/** Set the workspace root and fetch status. */
	setRoot(root: string) {
		if (root === _currentRoot) return;
		_currentRoot = root;
		_status = null;
		_fetchPromise = null;
		_pendingForceRefresh = false;
		this.refresh({ force: true });
	},

	/** Clear all state (no workspace). */
	clear() {
		_currentRoot = '';
		_status = null;
		_loading = false;
		_fetchPromise = null;
		_fetchSeq++;
		_pendingForceRefresh = false;
	},

	/**
	 * Refresh git status from the server.
	 * Concurrent calls are coalesced into a single in-flight fetch; a
	 * forced refresh while a fetch is in flight schedules exactly one
	 * follow-up fetch (instead of stacking overlapping request
	 * pipelines).
	 */
	refresh({ force = false }: { force?: boolean } = {}) {
		if (!_currentRoot) return;
		if (force && _fetchPromise) {
			_pendingForceRefresh = true;
			return _fetchPromise;
		}
		if (!_fetchPromise) {
			_fetchPromise = _doFetch(_currentRoot, ++_fetchSeq);
		}
		return _fetchPromise;
	}
};
