"""Manage long-lived stdio MCP server processes.

Stdio MCP servers are spawned as subprocesses and kept alive across
tool calls (unlike HTTP MCP which reconnects each time). This singleton
tracks active connections and handles lifecycle management.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from cptr.utils.mcp.client import MCPClient

logger = logging.getLogger(__name__)


class StdioMCPManager:
    """Singleton that manages long-lived stdio MCP connections."""

    def __init__(self):
        self._instances: dict[str, MCPClient] = {}  # server_id → live client
        self._connecting: dict[str, asyncio.Future] = {}  # server_id → in-flight connect

    async def get_client(
        self,
        server_id: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> MCPClient:
        """Get an existing client or spawn a new stdio MCP process.

        Concurrent first-connects for the same server are coalesced into a
        single spawn; additional callers await the in-flight connection.

        Args:
            server_id: Unique identifier for the server config.
            command: The executable to run.
            args: Command line arguments.
            env: Optional environment variables.
            cwd: Optional working directory.

        Returns:
            A connected MCPClient with an active session.
        """
        if server_id in self._instances:
            client = self._instances[server_id]
            if client.session is not None:
                return client
            # Session dead — clean up and reconnect
            logger.info("[mcp-stdio] Process for '%s' died, reconnecting", server_id)
            await self._safe_disconnect(client)
            del self._instances[server_id]

        pending = self._connecting.get(server_id)
        if pending is not None and not pending.done():
            return await asyncio.shield(pending)

        pending = asyncio.get_running_loop().create_future()
        self._connecting[server_id] = pending
        try:
            client = MCPClient()
            await client.connect_stdio(command, args, env, cwd)
        except BaseException as exc:
            self._connecting.pop(server_id, None)
            if isinstance(exc, asyncio.CancelledError):
                pending.cancel()
            else:
                pending.set_exception(exc)
                # Mark retrieved so a failure with no other awaiters does not
                # trigger an "exception was never retrieved" warning at GC.
                pending.exception()
            raise

        self._instances[server_id] = client
        pending.set_result(client)
        self._connecting.pop(server_id, None)
        logger.info("[mcp-stdio] Spawned process for '%s'", server_id)
        return client

    async def disconnect(self, server_id: str) -> None:
        """Disconnect and kill a specific server's process."""
        client = self._instances.pop(server_id, None)
        if client:
            await self._safe_disconnect(client)
            logger.info("[mcp-stdio] Disconnected '%s'", server_id)

    async def disconnect_all(self) -> None:
        """Shut down all stdio server processes (called on app shutdown)."""
        ids = list(self._instances.keys())
        for sid in ids:
            await self.disconnect(sid)
        logger.info("[mcp-stdio] All stdio servers disconnected")

    def list_active(self) -> list[str]:
        """Return IDs of servers with active connections."""
        return [
            sid for sid, client in self._instances.items()
            if client.session is not None
        ]

    async def _safe_disconnect(self, client: MCPClient) -> None:
        """Disconnect a client, swallowing errors."""
        try:
            await client.disconnect()
        except Exception:
            logger.debug("[mcp-stdio] Error during disconnect", exc_info=True)


# Module-level singleton
stdio_manager = StdioMCPManager()
