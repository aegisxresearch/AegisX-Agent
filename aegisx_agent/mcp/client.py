"""MCP stdio client: JSON-RPC 2.0 handshake, discovery, and tool calls.

Built on the official ``mcp`` SDK's ``stdio_client`` + ``ClientSession``.

Two constraints shape this module:

1. A session must live on **one event loop** for its whole lifetime, but
   AegisX runs a fresh loop per turn (chat, scheduler ticks, tests). The
   connection is therefore owned by a dedicated thread with its own loop and
   every public method bridges results back to the caller's loop.
2. The SDK's contexts use anyio cancel scopes, which must be entered **and**
   exited by the same task. A single host task on the owner loop enters the
   contexts, serves operations from a queue, and exits its own contexts on
   close — no other task ever touches them.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

from aegisx_agent.tools.base import ToolResult, ToolStatus

try:  # pragma: no cover - exercised implicitly whenever mcp is installed
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - the degraded path when mcp is absent
    MCP_AVAILABLE = False


class MCPClientError(RuntimeError):
    """Raised when an MCP server cannot be reached or speaks an incompatible protocol."""


class _OwnerLoop:
    """A daemon thread running a private event loop for connection lifetimes."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="aegisx-mcp-owner", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro: Any) -> Any:
        """Schedule ``coro`` on the owner loop; returns a concurrent Future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def submit_task_wait(self, task: Any, timeout: float) -> None:
        """Wait (blocking this thread) for a concurrent Future to finish."""
        try:
            task.result(timeout=timeout)
        except BaseException:  # noqa: BLE001 - best-effort teardown wait
            pass

    def stop(self) -> None:
        """Stop the loop and join its thread."""
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


@dataclass
class MCPServerInfo:
    """Identity reported by a server during the MCP handshake."""

    name: str
    version: str = ""


@dataclass
class MCPToolSpec:
    """One tool advertised by a server, already normalized for AegisX.

    The registry-facing tool name is derived from the bridged plugin manifest
    (``plugin_mcp_<server>_<tool>``) — this spec stays protocol-shaped.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    server_id: str
    title: str = ""


_CLOSE = object()  # sentinel telling the host task to exit its contexts


class _Host:
    """Single task owning the SDK contexts for one server connection.

    Enters ``stdio_client`` + ``ClientSession`` exactly once, serves queued
    operations, and on the close sentinel exits the contexts *in this same
    task* — the only task allowed to, per anyio's cancel-scope rules.
    """

    def __init__(self, params: Any, server_id: str) -> None:
        self.params = params
        self.server_id = server_id
        self.commands: asyncio.Queue[Any] = asyncio.Queue()
        self.server = MCPServerInfo(name="", version="")
        self.closed = False
        self.session: Any = None
        # Created inside ``run()`` — i.e. on the owner loop. A future made in
        # the caller's thread would fire callbacks into a dead loop.
        self._ready: asyncio.Future[tuple[Any, Any]] | None = None

    async def run(self) -> None:
        self._ready = asyncio.get_running_loop().create_future()
        try:
            async with stdio_client(self.params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    handshake = await session.initialize()
                    # mcp 1.x calls it ``serverInfo``; 2.x went snake_case.
                    server_info = getattr(handshake, "server_info", None) or getattr(
                        handshake, "serverInfo", None
                    )
                    self.server = MCPServerInfo(
                        name=str(getattr(server_info, "name", "") or ""),
                        version=str(getattr(server_info, "version", "") or ""),
                    )
                    self.session = session
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_result((session, handshake))
                    await self._serve(session)
        except BaseException as error:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(error)
            else:
                # Handshake succeeded but the session died afterwards; report
                # it on any operations still queued instead of hanging them.
                self._fail_pending(f"MCP server '{self.server_id}' connection ended: {error!r}")
        finally:
            self.closed = True
            self._fail_pending(f"MCP server '{self.server_id}' is disconnected")

    async def _serve(self, session: Any) -> None:
        while True:
            item = await self.commands.get()
            if item is _CLOSE:
                return
            future, operation, arguments = item
            if future.done():
                continue
            try:
                if operation == "list_tools":
                    future.set_result(await self._list_tools(session))
                elif operation == "call_tool":
                    future.set_result(await self._call_tool(session, arguments))
                else:  # pragma: no cover - internal invariant
                    raise MCPClientError(f"Unknown MCP client operation: {operation}")
            except BaseException as error:  # noqa: BLE001 - forwarded to the awaiting caller
                future.set_exception(error)

    async def _list_tools(self, session: Any) -> list[MCPToolSpec]:
        listing = await session.list_tools()
        return [
            MCPToolSpec(
                name=tool.name,
                description=tool.description or "",
                parameters=dict(tool.input_schema or {}),
                server_id=self.server_id,
                title=str(getattr(tool, "title", "") or ""),
            )
            for tool in listing.tools
        ]

    async def _call_tool(self, session: Any, arguments: dict[str, Any]) -> ToolResult:
        result = await session.call_tool(arguments["name"], arguments["arguments"])
        blocks: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text is not None:
                blocks.append(text)
        if result.is_error:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="\n".join(blocks),
                error="\n".join(blocks) or "MCP server reported an error",
            )
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output="\n".join(blocks),
            metadata={"server": self.server.name, "mcp_tool": arguments["name"]},
        )

    def _fail_pending(self, message: str) -> None:
        while True:
            try:
                item = self.commands.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item is _CLOSE:
                continue
            future, _operation, _arguments = item
            if not future.done():
                future.set_exception(MCPClientError(message))

    async def wait_ready(self) -> tuple[Any, Any]:
        """Resolve with the session once the handshake finishes; raise on failure."""
        if self._ready is None:  # pragma: no cover - run() always sets it first
            raise MCPClientError(f"MCP server '{self.server_id}' never started")
        return await self._ready


class MCPToolClient:
    """Async client for one MCP server process (stdio transport).

    The session lives on a private owner loop inside a host task; callers
    from any event loop bridge through :meth:`_call_owner`. Concurrent calls
    serialize on the host task instead of racing inside the SDK session.
    """

    def __init__(self, server_id: str, config: dict[str, Any]) -> None:
        self.server_id = server_id
        self.config = dict(config)
        self._owner: _OwnerLoop | None = None
        self._host: _Host | None = None
        # concurrent.futures.Future returned by run_coroutine_threadsafe()
        self._task: Any = None

    @property
    def connected(self) -> bool:
        """Whether a live session currently exists."""
        return self._host is not None and not self._host.closed

    @property
    def server_info(self) -> MCPServerInfo | None:
        """Identity reported during the handshake, once connected."""
        return self._host.server if self._host and not self._host.closed else None

    @property
    def server_version(self) -> str:
        """Version string reported during the handshake (``''`` if unknown)."""
        info = self.server_info
        return info.version if info else ""

    # ------------------------------------------------------------------ #
    # Public API (callable from any event loop)
    # ------------------------------------------------------------------ #

    async def list_tools(self) -> list[MCPToolSpec]:
        """Discover the tools this server advertises."""
        specs: list[MCPToolSpec] = await self._call_owner("list_tools")
        for spec in specs:
            # ``parameters`` always has type=object; a sloppy server is
            # normalized here rather than crashing manifest validation later.
            parameters = spec.parameters if isinstance(spec.parameters, dict) else {}
            if parameters.get("type") != "object":
                parameters = {"type": "object", "properties": {}}
            spec.parameters = parameters
            # A whitespace-only description passes the protocol but not the
            # plugin manifest's ``strip()`` check — normalize both here.
            spec.description = (spec.description or "").strip() or "No description provided"
        return specs

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute one tool on the server."""
        result: ToolResult = await self._call_owner("call_tool", name=name, arguments=arguments)
        return result

    async def close(self) -> None:
        """Shut the session down, terminate the server process, stop the loop."""
        host, owner, task = self._host, self._owner, self._task
        self._host = None
        self._owner = None
        self._task = None
        if host is None or owner is None or task is None:
            return
        try:
            await asyncio.wrap_future(
                owner.submit(self._request_close(host, task))
            )
        finally:
            owner.stop()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _request_close(self, host: _Host, task: asyncio.Future[None]) -> None:
        """Ask the host task to exit its contexts, then wait for it."""
        if not host.closed:
            host.commands.put_nowait(_CLOSE)
            run_task = asyncio.wrap_future(task)  # bound to this (owner) loop
            try:
                await asyncio.wait_for(asyncio.shield(run_task), timeout=10)
            except TimeoutError:  # pragma: no cover - defensive
                run_task.cancel()

    async def _call_owner(self, operation: str, **arguments: Any) -> Any:
        owner, host = self._ensure_host()
        return await asyncio.wrap_future(owner.submit(_enqueue(host, operation, arguments)))

    def _ensure_host(self) -> tuple[_OwnerLoop, _Host]:
        if self._host is not None and not self._host.closed:
            return self._owner, self._host  # type: ignore[return-value]
        if not MCP_AVAILABLE:
            raise MCPClientError(
                "The official 'mcp' package is not installed; "
                "install it with: pip install 'mcp>=2.0'"
            )
        command = str(self.config.get("command", "")).strip()
        if not command:
            raise MCPClientError(
                f"MCP server '{self.server_id}' has no 'command' configured"
            )
        args = [str(argument) for argument in self.config.get("args", [])]
        env = dict(self.config.get("env", {}))
        params = StdioServerParameters(command=command, args=args, env=env or None)

        self._owner = _OwnerLoop()
        owner = self._owner
        host = _Host(params, self.server_id)
        self._host = host
        task = asyncio.run_coroutine_threadsafe(host.run(), owner.loop)
        self._task = task
        # Block this (caller) thread until the handshake resolves; failures
        # propagate as exceptions here, where the CLI/manager can report them.
        handshake = asyncio.run_coroutine_threadsafe(host.wait_ready(), owner.loop)
        try:
            handshake.result(timeout=30)
        except BaseException as error:
            self._host = None
            self._task = None
            host.closed = True
            # Let the host task finish unwinding its context exits BEFORE the
            # owner loop stops, or the subprocess transport is left for the
            # garbage collector to close against a dead loop (ResourceWarning
            # noise: "Event loop is closed").
            try:
                task.result(timeout=5)
            except BaseException:  # noqa: BLE001 - secondary failure, ignored
                pass
            owner.stop()
            self._owner = None
            raise MCPClientError(
                f"Failed to start MCP server '{self.server_id}': {error}"
            ) from error
        return self._owner, host


async def _enqueue(host: _Host, operation: str, arguments: dict[str, Any]) -> Any:
    """Owner-loop coroutine: queue one operation and await its reply."""
    if host.closed:
        raise MCPClientError(f"MCP server '{host.server_id}' is disconnected")
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    host.commands.put_nowait((future, operation, arguments))
    return await future


__all__ = [
    "MCP_AVAILABLE",
    "MCPClientError",
    "MCPServerInfo",
    "MCPToolClient",
    "MCPToolSpec",
]
