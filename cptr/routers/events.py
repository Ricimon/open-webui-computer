"""Unified system events WebSocket: fs watching + port scanning.

Replaces the old watch.py with a single multiplexed event stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import sys
import threading
from pathlib import Path
from typing import Optional, NamedTuple

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers.polling import PollingObserver

from cptr.utils.terminal import manager
from cptr.utils.config import check_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["events"])

# ── Filesystem watcher ────────────────────────────────────────────


# Max directories watched per connection. Each watched directory gets its
# own emitter threads, so this bounds the thread count. The frontend only
# watches directories the UI is actually showing (current directory plus
# expanded tree nodes), never whole subtrees.
MAX_WATCH_PATHS_PER_CONNECTION = 256


def _create_observer():
    if platform.system() == "Darwin":
        # Prefer the native FSEvents observer on macOS – it fires only on
        # real filesystem changes instead of polling (which generates
        # constant false-positive "modified" events from stat calls).
        try:
            from watchdog.observers.fsevents import FSEventsObserver

            return FSEventsObserver()
        except ImportError:
            # If the C-extension isn't available, fall back to polling
            # with a longer interval to reduce noise.
            return PollingObserver(timeout=3)
    return Observer()


_shared_observer: Optional[Observer] = None
_shared_observer_lock = threading.Lock()


def _get_shared_observer() -> Observer:
    """Return a single observer shared by all (non-recursive) watches."""
    global _shared_observer
    with _shared_observer_lock:
        if _shared_observer is None:
            observer = _create_observer()
            observer.daemon = True
            observer.start()
            _shared_observer = observer
        return _shared_observer


class _ChangeCollector(FileSystemEventHandler):
    """Collects fs events into a set for debounced delivery."""

    def __init__(self) -> None:
        self._changes: set[str] = set()
        self._lock = threading.Lock()

    # Paths that should never trigger a user-visible refresh
    _IGNORED_SEGMENTS = {".git", "__pycache__", ".DS_Store", "node_modules"}

    def _record(self, event: FileSystemEvent) -> None:
        path = event.src_path
        # Skip noisy internal paths that shouldn't trigger file browser refreshes
        parts = Path(path).parts
        if any(seg in self._IGNORED_SEGMENTS for seg in parts):
            return
        with self._lock:
            self._changes.add(str(Path(path).parent))
            self._changes.add(path)

    def on_created(self, event: FileSystemEvent) -> None:
        self._record(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._record(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        # Ignore directory-modified events – these fire on any child
        # change (which we already capture via on_created/on_deleted)
        # and on metadata-only updates like atime from stat calls.
        if event.is_directory:
            return
        self._record(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._record(event)
        if hasattr(event, "dest_path"):
            with self._lock:
                self._changes.add(str(Path(event.dest_path).parent))
                self._changes.add(event.dest_path)

    def drain(self) -> list[str]:
        with self._lock:
            paths = list(self._changes)
            self._changes.clear()
        return paths


class _WatchEntry(NamedTuple):
    handler: _ChangeCollector
    watch: object
    subscribers: set[asyncio.Queue[list[str]]]


_watch_registry: dict[str, _WatchEntry] = {}
_watch_registry_lock = asyncio.Lock()


async def _subscribe_to_path(path: str) -> tuple[str, asyncio.Queue[list[str]]]:
    """Subscribe this connection to a shared, non-recursive directory watch.

    Watches are per-directory (non-recursive): the frontend lazily
    subscribes to directories as they become visible in the UI, so the
    watch cost stays proportional to the visible tree instead of the
    whole workspace.
    """
    resolved = str(Path(path).resolve())
    queue: asyncio.Queue[list[str]] = asyncio.Queue(maxsize=32)

    async with _watch_registry_lock:
        entry = _watch_registry.get(resolved)
        if entry is None:
            observer = _get_shared_observer()
            handler = _ChangeCollector()
            watch = observer.schedule(handler, resolved, recursive=False)
            entry = _WatchEntry(handler, watch, set())
            _watch_registry[resolved] = entry
        entry.subscribers.add(queue)

    return resolved, queue


async def _unsubscribe_from_path(path: str, queue: asyncio.Queue[list[str]]) -> None:
    """Remove a filesystem watch subscription (the shared observer keeps running)."""
    async with _watch_registry_lock:
        entry = _watch_registry.get(path)
        if entry is None:
            return

        entry.subscribers.discard(queue)
        if entry.subscribers:
            return

        _watch_registry.pop(path, None)
        try:
            _get_shared_observer().unschedule(entry.watch)
        except Exception:
            pass


async def _dispatch_fs_changes() -> None:
    """Fan out debounced filesystem changes from shared watchers."""
    while True:
        await asyncio.sleep(1.0)

        async with _watch_registry_lock:
            entries = list(_watch_registry.items())

        for path, entry in entries:
            paths = entry.handler.drain()
            if not paths:
                continue

            if sys.platform == "win32":
                paths = [p.replace("\\", "/") for p in paths]

            for queue in list(entry.subscribers):
                try:
                    queue.put_nowait(paths)
                except asyncio.QueueFull:
                    logger.debug("Dropped fs_change event for slow subscriber on %s", path)


_fs_dispatch_task: Optional[asyncio.Task] = None


async def _ensure_fs_dispatcher() -> None:
    global _fs_dispatch_task
    if _fs_dispatch_task is None or _fs_dispatch_task.done():
        _fs_dispatch_task = asyncio.create_task(_dispatch_fs_changes())


async def _queue_reader(queue: asyncio.Queue[list[str]], out: asyncio.Queue) -> None:
    """Forward one watched path's change batches to the connection out-queue."""
    while True:
        paths = await queue.get()
        await out.put(paths)


async def _fs_watcher_loop(ws: WebSocket, initial_path: str, path_holder: dict) -> None:
    """Watch filesystem and push fs_change events.

    The set of watched paths is maintained lazily per connection: the
    initial path is watched on connect, and the client sends ``watch_paths``
    messages to grow/shrink the set (e.g. directories the file browser has
    expanded). Each watched path is a single directory, never a recursive
    tree, so watch cost stays proportional to what the UI is showing.
    """
    out: asyncio.Queue = asyncio.Queue()
    queues: dict[str, asyncio.Queue[list[str]]] = {}
    readers: dict[str, asyncio.Task] = {}

    def _start_reader(path: str, queue: asyncio.Queue[list[str]]) -> None:
        readers[path] = asyncio.create_task(_queue_reader(queue, out))

    async def _stop_reader(path: str) -> None:
        task = readers.pop(path, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _set_watched(paths: list[str]) -> None:
        desired: dict[str, None] = {}
        for raw in paths:
            try:
                resolved = str(Path(raw).resolve())
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved not in desired and Path(resolved).is_dir():
                desired[resolved] = None

        for path in desired:
            if path not in queues:
                try:
                    resolved, queue = await _subscribe_to_path(path)
                    queues[resolved] = queue
                    _start_reader(resolved, queue)
                except Exception as e:
                    logger.warning(f"Failed to watch {path}: {e}")

        for path in [p for p in queues if p not in desired]:
            queue = queues.pop(path)
            await _stop_reader(path)
            await _unsubscribe_from_path(path, queue)

    try:
        await _ensure_fs_dispatcher()
        await _set_watched([initial_path])
        if not queues:
            logger.warning(f"Failed to watch {initial_path}")
            return
    except Exception as e:
        logger.warning(f"Failed to watch {initial_path}: {e}")
        return

    try:
        while True:
            # Apply a new watch set (from the receive loop)
            new_paths = path_holder.get("pending")
            if new_paths is not None:
                del path_holder["pending"]
                await _set_watched(new_paths)

            try:
                paths = await asyncio.wait_for(out.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            # Merge any already-queued batches into one event
            merged = list(paths)
            while True:
                try:
                    merged.extend(out.get_nowait())
                except asyncio.QueueEmpty:
                    break

            try:
                await ws.send_json({"type": "fs_change", "paths": merged})
            except Exception:
                break
    finally:
        for path in list(queues):
            queue = queues.pop(path)
            await _stop_reader(path)
            await _unsubscribe_from_path(path, queue)


# ── Port scanning ─────────────────────────────────────────────────

# System processes to ignore
_IGNORED_PROCESSES = {
    "sshd",
    "cupsd",
    "mDNSResponder",
    "rapportd",
    "systemd-resolve",
    "avahi-daemon",
    "dnsmasq",
    "launchd",
    "systemd",
    "ntpd",
    "bluetoothd",
    "AirPlayXPCHelper",
    "ControlCenter",
}

# Ports that are almost certainly system services
_SYSTEM_PORTS = {22, 53, 80, 443, 631, 5353}


async def _get_ppid(pid: int) -> int:
    """Get parent PID. Cross-platform."""
    try:
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                "wmic", "process", "where", f"ProcessId={pid}",
                "get", "ParentProcessId", "/value",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            for line in stdout.decode(errors="replace").splitlines():
                if line.startswith("ParentProcessId="):
                    return int(line.split("=", 1)[1])
            return 0
        elif sys.platform == "linux":
            def _read_ppid():
                with open(f"/proc/{pid}/stat") as f:
                    return int(f.read().split()[3])
            return await asyncio.to_thread(_read_ppid)
        else:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-o", "ppid=", "-p", str(pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
            return int(stdout.decode(errors="replace").strip())
    except Exception:
        return 0


async def _find_session_for_pid(pid: int) -> Optional[str]:
    """Walk up the process tree to find which terminal session spawned this PID."""
    current = pid
    visited: set[int] = set()
    while current > 1 and current not in visited:
        visited.add(current)
        for session in manager._sessions.values():
            # Unix: match by child PID
            if session._pid == current:
                return session.session_id
            # Windows: winpty process has a .pid attribute
            if session._process is not None:
                try:
                    if getattr(session._process, "pid", None) == current:
                        return session.session_id
                except Exception:
                    pass
        current = await _get_ppid(current)
    return None


async def _get_process_name(pid: int) -> str:
    """Get process name from PID."""
    try:
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                "tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            line = stdout.decode(errors="replace").strip()
            if line and line.startswith('"'):
                return line.split('"')[1]
            return "unknown"
        elif sys.platform == "linux":
            def _read_comm():
                with open(f"/proc/{pid}/comm") as f:
                    return f.read().strip()
            return await asyncio.to_thread(_read_comm)
        else:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-o", "comm=", "-p", str(pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
            name = stdout.decode(errors="replace").strip()
            # macOS returns full path, extract basename
            return os.path.basename(name) if name else "unknown"
    except Exception:
        return "unknown"


async def _scan_ports_darwin() -> list[dict]:
    """Scan listening ports on macOS using lsof."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "lsof", "-iTCP", "-sTCP:LISTEN", "-nP", "-F", "pcn",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            return []

        ports = []
        current_pid = 0
        current_process = ""
        for line in stdout.decode(errors="replace").splitlines():
            if line.startswith("p"):
                current_pid = int(line[1:])
            elif line.startswith("c"):
                current_process = line[1:]
            elif line.startswith("n"):
                # Format: n*:PORT or nhost:PORT
                addr = line[1:]
                if ":" in addr:
                    port_str = addr.rsplit(":", 1)[1]
                    try:
                        port = int(port_str)
                        ports.append(
                            {
                                "port": port,
                                "pid": current_pid,
                                "process": current_process,
                            }
                        )
                    except ValueError:
                        pass
        return ports
    except Exception as e:
        logger.warning(f"Port scan failed: {e}")
        return []


async def _scan_ports_linux() -> list[dict]:
    """Scan listening ports on Linux using /proc/net/tcp."""
    ports = []
    try:
        def _read_proc_net():
            with open("/proc/net/tcp") as f:
                return f.readlines()[1:]  # skip header

        lines = await asyncio.to_thread(_read_proc_net)
        for line in lines:
            parts = line.split()
            if parts[3] == "0A":  # LISTEN state
                local = parts[1]
                port = int(local.split(":")[1], 16)
                inode = int(parts[9])
                # Find PID for this inode
                pid = await _inode_to_pid(inode)
                process = await _get_process_name(pid) if pid else "unknown"
                ports.append({"port": port, "pid": pid or 0, "process": process})
    except Exception as e:
        logger.warning(f"Port scan failed: {e}")
    return ports


async def _inode_to_pid(inode: int) -> Optional[int]:
    """Map a socket inode to a PID on Linux."""
    def _scan():
        try:
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    fd_dir = f"/proc/{entry}/fd"
                    for fd in os.listdir(fd_dir):
                        try:
                            link = os.readlink(f"{fd_dir}/{fd}")
                            if f"socket:[{inode}]" in link:
                                return int(entry)
                        except (OSError, ValueError):
                            continue
                except (OSError, PermissionError):
                    continue
        except Exception:
            pass
        return None
    return await asyncio.to_thread(_scan)


async def _scan_ports_windows() -> list[dict]:
    """Scan listening ports on Windows using netstat."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "netstat", "-ano", "-p", "TCP",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        ports = []
        for line in stdout.decode(errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 5 and "LISTENING" in parts:
                local = parts[1]
                if ":" in local:
                    port_str = local.rsplit(":", 1)[1]
                    try:
                        port = int(port_str)
                        pid = int(parts[-1])
                        ports.append(
                            {
                                "port": port,
                                "pid": pid,
                                "process": await _get_process_name(pid),
                            }
                        )
                    except ValueError:
                        pass
        return ports
    except Exception as e:
        logger.warning(f"Port scan failed: {e}")
        return []


async def _scan_ports() -> list[dict]:
    """Scan listening ports. Cross-platform."""
    system = platform.system()
    if system == "Darwin":
        raw = await _scan_ports_darwin()
    elif system == "Linux":
        raw = await _scan_ports_linux()
    elif system == "Windows":
        raw = await _scan_ports_windows()
    else:
        return []

    # Get our own PID to filter ourselves out
    our_pid = os.getpid()

    # Deduplicate by port and filter
    seen: set[int] = set()
    filtered = []
    for entry in raw:
        port = entry["port"]
        if port in seen:
            continue
        seen.add(port)

        # Skip system ports and our own process
        if port in _SYSTEM_PORTS:
            continue
        if entry["pid"] == our_pid:
            continue
        if entry["process"] in _IGNORED_PROCESSES:
            continue

        # Session attribution: only include ports spawned by our terminals
        if entry["pid"]:
            session_id = await _find_session_for_pid(entry["pid"])
            if session_id:
                entry["session_id"] = session_id
                filtered.append(entry)
            # else: not from a cptr terminal, skip
        # pid=0 or no pid: skip

    return filtered


async def _port_scanner_loop(ws: WebSocket) -> None:
    """Periodically scan ports and push add/remove events."""
    known: dict[int, dict] = {}  # port -> info

    while True:
        await asyncio.sleep(3)

        try:
            current_ports = {p["port"]: p for p in await _scan_ports()}
        except Exception as e:
            logger.warning(f"Port scan error: {e}")
            continue

        # Detect new ports
        for port, info in current_ports.items():
            if port not in known:
                try:
                    await ws.send_json(
                        {
                            "type": "port_added",
                            "port": info["port"],
                            "pid": info["pid"],
                            "process": info["process"],
                            "session_id": info.get("session_id"),
                        }
                    )
                except Exception:
                    return

        # Detect removed ports
        for port in list(known.keys()):
            if port not in current_ports:
                try:
                    await ws.send_json(
                        {
                            "type": "port_removed",
                            "port": port,
                        }
                    )
                except Exception:
                    return

        known = current_ports


# ── Receive loop ──────────────────────────────────────────────────


async def _receive_loop(ws: WebSocket, path_holder: dict) -> None:
    """Handle incoming messages from the client."""
    try:
        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type")
            if msg_type == "watch_paths":
                paths = msg.get("paths")
                if isinstance(paths, list):
                    path_holder["pending"] = [
                        str(p) for p in paths if p
                    ][:MAX_WATCH_PATHS_PER_CONNECTION]
            elif msg_type == "watch_path":
                # Legacy single-path form: replaces the watch set
                new_path = msg.get("path")
                if new_path:
                    path_holder["pending"] = [str(new_path)]
    except (WebSocketDisconnect, Exception):
        pass


# ── WebSocket endpoint ────────────────────────────────────────────


@router.websocket("/ws")
async def events_ws(
    websocket: WebSocket, path: str = Query("/", description="Initial fs watch path")
):
    """Unified system events WebSocket.

    Pushes:
      - {"type": "fs_change", "paths": [...]}
      - {"type": "port_added", "port": N, "pid": N, "process": "...", "session_id": "..." | null}
      - {"type": "port_removed", "port": N}

    Receives:
      - {"type": "watch_paths", "paths": [...]}: set fs watch directories
      - {"type": "watch_path", "path": "..."}: (legacy) set a single fs watch directory
    """
    target = Path(path).resolve()
    if not target.is_dir():
        await websocket.close(code=4000, reason="Not a directory")
        return

    # Auth check for WebSocket
    client_host = websocket.client.host if websocket.client else "127.0.0.1"
    token = websocket.cookies.get("cptr_session") or websocket.query_params.get("token")
    auth = check_access(client_host=client_host, jwt_token=token)
    if auth is None:
        await websocket.close(code=4001, reason="unauthorized")
        return

    await websocket.accept()
    logger.info(f"Events WS connected, watching {target}")

    path_holder: dict = {}

    fs_task = asyncio.create_task(_fs_watcher_loop(websocket, str(target), path_holder))
    port_task = asyncio.create_task(_port_scanner_loop(websocket))
    recv_task = asyncio.create_task(_receive_loop(websocket, path_holder))

    try:
        done, pending = await asyncio.wait(
            [fs_task, port_task, recv_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        logger.error(f"Events WS error: {e}")
    finally:
        logger.info("Events WS disconnected")
