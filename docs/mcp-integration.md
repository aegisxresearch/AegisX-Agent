# MCP Integration Guide

AegisX can consume tools from any [MCP (Model Context Protocol)](https://modelcontextprotocol.io)
server. A server's tools are bridged into the agent as **permission-gated plugin tools** —
the same validation, manifest contract, and permission gate as every other tool. There is
no side door: connecting a server gives the model *visibility* of its tools, never *authority*
over the gate.

## Installation

```bash
pip install "aegisx-agent[mcp]"     # installs the official `mcp` SDK (v2)
```

MCP support is optional; the rest of AegisX works without it.

## Quick start

### 1. Declare servers in `~/.aegisx/mcp_servers.json`

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/workspace"]
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "ghp_..."}
    }
  }
}
```

The format matches Claude Desktop's `mcpServers`, so existing configs can be copied.

### 2. Connect from the CLI or chat

```bash
# Typer sub-app
aegisx mcp list                          # configured servers + state
aegisx mcp connect filesystem            # connect and register its tools
aegisx mcp disconnect filesystem
aegisx mcp add weather curlie --risk safe
aegisx mcp remove weather
```

```
# Slash commands inside aegisx chat
/mcp list
/mcp connect filesystem
/mcp disconnect filesystem
/mcp add weather python -m my_server --risk caution
/mcp remove weather
```

Tools arrive named `plugin_mcp_<server>_<tool>` (collision-free across servers) and appear
in `/tools`, `/plugin list`, and the permission-gate audit log like any other tool.

### 3. One-shot connections without a config file

```bash
aegisx mcp connect demo --config servers.json
```

The file may be a full `{"mcpServers": {...}}` document (Claude Desktop style) or a single
server config object. The same works in chat: `/mcp connect demo servers.json`.

> **Tip:** when an `add` argument starts with `-` (like `-m`), insert `--` before it so the
> CLI does not mistake it for an option: `aegisx mcp add demo -- python3 -m my_server`.
> Inside chat, `/mcp add demo python3 -m my_server` needs no separator.

## Server config reference

| Key                | Type           | Default    | Meaning                                                          |
|--------------------|----------------|------------|------------------------------------------------------------------|
| `command`          | str            | *required* | Executable that speaks MCP over stdio                            |
| `args`             | list[str]      | `[]`       | Arguments for the command                                        |
| `env`              | dict           | `{}`       | Extra environment variables (secrets stay in the config file)    |
| `risk`             | str            | `caution`  | Default risk for every tool of this server                       |
| `tool_risks`       | dict[str, str] | `{}`       | Per-tool risk overrides, e.g. `{"delete_all": "dangerous"}`      |
| `requires_approval`| bool           | `false`    | Escalate every tool of this server to *ask-first*                |
| `version`          | str            | handshake  | Manifest version; overrides the version reported in the handshake|

Unknown keys are rejected when saving or connecting — a typo fails loudly instead of being
silently ignored.

## Security model

1. **External code is never `safe`.** MCP tools talk to a process the agent does not
   control, so the default risk is `caution`. Descriptions containing *dangerous*,
   *destructive*, or *irreversible* escalate a tool to `dangerous` automatically.
2. **Read-only mode refuses MCP tools.** Their effective risk is at least `caution`, so
   unattended read-only sessions never execute them. Lowering an individual tool to `safe`
   requires an explicit `tool_risks` entry *and* still leaves the gate in charge.
3. **The gate stays authoritative.** Every call passes through `PermissionGate.check` —
   denylists, allowlists, `--permission-mode`, and the audit log all apply unchanged.
4. **Crash isolation.** A server is a separate process. If it dies, its calls return
   errors to the model; the agent loop recovers. Unloading (`/plugin unload mcp_<server>`
   or `/mcp disconnect`) removes every tool the server brought.
5. **Explicit loading.** Nothing connects at import time. Connections happen when you run
   `aegisx mcp connect` or `/mcp connect` — the same policy as Python plugins.

## Lifecycle notes

- Sessions live on a dedicated owner thread, so a connection survives across the agent's
  per-turn event loops (chat, scheduler ticks) and works from tests.
- Concurrent tool calls serialize on one connection — the MCP session is not raced.
- `Ctrl-C`-safe: `close_all()` tears down every session at agent shutdown.
- A server that fails its handshake (missing binary, bad protocol) surfaces a clean
  `MCP connect failed: ...` message; no half-registered tools are left behind.

## Testing your server

`tests/mcp_demo_server.py` in the repository is a minimal MCP server used by the test
suite (54 tests) and handy for manual checks:

```bash
python tests/mcp_demo_server.py          # exposes echo, explode, shouty_echo
```

```python
# Library usage
await agent.connect_mcp_server("demo", {"command": sys.executable,
                                        "args": ["tests/mcp_demo_server.py"]})
names = agent.list_mcp_servers()            # {"demo": {..., "connected": True}}
await agent.disconnect_mcp_server("demo")
```
