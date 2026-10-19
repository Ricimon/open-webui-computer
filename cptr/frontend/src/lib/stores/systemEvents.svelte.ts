/**
 * Unified system events store.
 *
 * Single WebSocket to /api/events/ws that multiplexes:
 * - fs_change events (for FileBrowser auto-refresh)
 * - port_added / port_removed events (for port notifications)
 *
 * Replaces the old fsWatch store.
 *
 * Filesystem watches are lazy: the server only watches the exact set of
 * directories requested via `watch_paths` (one directory each, never a
 * recursive tree). `connect()` sets the primary directory; components can
 * additionally watch directories they display (e.g. expanded tree nodes)
 * via `addWatch()` / `removeWatch()`.
 */

export interface PortInfo {
	port: number;
	pid: number;
	process: string;
	session_id: string | null;
}

// Bound for lazily-added watches (the server enforces the same cap).
const MAX_EXTRA_WATCHES = 256;

let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let sendTimer: ReturnType<typeof setTimeout> | null = null;
let primaryPath: string | null = null;
const extraWatchRefs = new Map<string, number>();

// ── FS events ────────────────────────────────────────────────────
let _fsTick = $state(0);
let _fsChangedPaths = $state<string[]>([]);

// ── Port events ──────────────────────────────────────────────────
let _ports = $state<PortInfo[]>([]);
let _newPorts = $state<PortInfo[]>([]);
let _dismissedPorts = new Set<number>();

function desiredWatchPaths(): string[] {
	const set = new Set<string>(extraWatchRefs.keys());
	if (primaryPath) set.add(primaryPath);
	return [...set];
}

function sendWatchPaths() {
	if (!ws || ws.readyState !== WebSocket.OPEN) return;
	ws.send(JSON.stringify({ type: 'watch_paths', paths: desiredWatchPaths() }));
}

function scheduleSendWatchPaths() {
	if (sendTimer) return;
	sendTimer = setTimeout(() => {
		sendTimer = null;
		sendWatchPaths();
	}, 100);
}

function connect(watchPath: string) {
	primaryPath = watchPath;

	if (ws && ws.readyState <= WebSocket.OPEN) {
		// Already connected, just update the watch set
		scheduleSendWatchPaths();
		return;
	}

	const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
	const url = `${protocol}//${location.host}/api/events/ws?path=${encodeURIComponent(watchPath)}`;

	ws = new WebSocket(url);

	ws.onopen = () => {
		// (Re-)send the full watch set after (re)connecting so lazily
		// added directories are resubscribed.
		sendWatchPaths();
	};

	ws.onmessage = (event) => {
		try {
			const msg = JSON.parse(event.data);

			if (msg.type === 'fs_change') {
				_fsChangedPaths = msg.paths ?? [];
				_fsTick++;
			} else if (msg.type === 'port_added') {
				const info: PortInfo = {
					port: msg.port,
					pid: msg.pid,
					process: msg.process,
					session_id: msg.session_id ?? null
				};
				// Add to active ports
				_ports = [..._ports.filter((p) => p.port !== info.port), info];
				// Add to new ports (for notification) unless dismissed
				if (!_dismissedPorts.has(info.port)) {
					_newPorts = [..._newPorts.filter((p) => p.port !== info.port), info];
				}
			} else if (msg.type === 'port_removed') {
				_ports = _ports.filter((p) => p.port !== msg.port);
				_newPorts = _newPorts.filter((p) => p.port !== msg.port);
				_dismissedPorts.delete(msg.port);
			}
		} catch {}
	};

	ws.onclose = () => {
		ws = null;
		if (reconnectTimer) clearTimeout(reconnectTimer);
		reconnectTimer = setTimeout(() => {
			if (primaryPath) connect(primaryPath);
		}, 2000);
	};

	ws.onerror = () => {
		ws?.close();
	};
}

function disconnect() {
	if (reconnectTimer) {
		clearTimeout(reconnectTimer);
		reconnectTimer = null;
	}
	if (sendTimer) {
		clearTimeout(sendTimer);
		sendTimer = null;
	}
	if (ws) {
		ws.onclose = null;
		ws.close();
		ws = null;
	}
	primaryPath = null;
	extraWatchRefs.clear();
}

/**
 * Add a lazily-watched directory (refcounted). Typical use: watch the
 * directories the file browser has expanded. Only takes effect while a
 * connection is open — the set is resent on connect.
 */
function addWatch(path: string) {
	if (!path || path === primaryPath) return;
	const refs = extraWatchRefs.get(path) ?? 0;
	if (refs === 0 && extraWatchRefs.size >= MAX_EXTRA_WATCHES) return;
	extraWatchRefs.set(path, refs + 1);
	scheduleSendWatchPaths();
}

/** Remove one reference to a lazily-watched directory. */
function removeWatch(path: string) {
	const refs = extraWatchRefs.get(path);
	if (!refs) return;
	if (refs <= 1) extraWatchRefs.delete(path);
	else extraWatchRefs.set(path, refs - 1);
	scheduleSendWatchPaths();
}

function isRelevantFsChange(targetPath: string): boolean {
	return _fsChangedPaths.some((p) => p === targetPath || p.startsWith(targetPath + '/'));
}

function dismissPort(port: number) {
	_dismissedPorts.add(port);
	_newPorts = _newPorts.filter((p) => p.port !== port);
}

export const systemEvents = {
	connect,
	disconnect,
	addWatch,
	removeWatch,

	// FS
	get fsTick() {
		return _fsTick;
	},
	get fsChangedPaths() {
		return _fsChangedPaths;
	},
	isRelevantFsChange,

	// Ports
	get ports() {
		return _ports;
	},
	get newPorts() {
		return _newPorts;
	},
	dismissPort
};
