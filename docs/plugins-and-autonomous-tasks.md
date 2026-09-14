# AegisX plugins and autonomous tasks

## Versioned tool plugins

Plugins are explicit Python extensions. Importing `aegisx_agent` never scans or
executes plugin directories. A plugin is loaded only when the application calls
`load_plugin_module()` or `load_plugin_path()`.

```python
from aegisx_agent.core import AegisXAgent
from aegisx_agent.plugins import PluginManifest, PluginPermissionPolicy, define_plugin

manifest = PluginManifest(
    plugin_id="weather",
    version="1.0.0",
    tool_name="lookup",
    description="Look up the weather for a city.",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
    permission=PluginPermissionPolicy(requires_approval=True),
)

@define_plugin(manifest)
def lookup(city: str) -> str:
    return f"Weather for {city}: unavailable"

agent = AegisXAgent(config)
agent.register_plugin(lookup)
```

The plugin becomes the qualified tool `plugin_weather_lookup`. Its JSON schema
is sent to the model and its execution still passes through the normal
`ToolRegistry` and `PermissionGate`. A plugin policy may make a call stricter,
but can never bypass the global gate.

Supported manifest rules:

- `plugin_id`: lowercase stable identifier;
- `version`: semantic version such as `1.0.0`;
- `api_version`: currently `1`;
- `parameters`: object-shaped JSON Schema;
- `risk`: `safe`, `caution`, or `dangerous`;
- `permission`: optional approval/read-only restrictions.

The same plugin ID cannot be replaced by another version until it is unloaded:

```python
agent.load_plugin_path("./plugins/weather.py")
agent.unload_plugin("weather")
```

## Autonomous scheduler controls

Scheduler state is persisted in SQLite and older databases are migrated in
place. New task state includes a checkpoint, retry count, retry budget, and a
cancellation flag.

```python
task = agent.add_scheduled_task(
    "nightly report", "write the report", "interval", "1h", max_retries=4
)
task_id = task["id"]

agent.request_scheduler_cancel(task_id)
agent.resume_scheduled_task(task_id)
checkpoint = agent.get_scheduler_checkpoint(task_id)
```

Lifecycle guarantees:

- a task writes a `running` checkpoint before invoking the agent;
- a process restart changes stranded `running` tasks to `pending` and records
  `resumed_after_restart`;
- cancellation interrupts an active task and persists `paused` state;
- failures retry no more than `max_retries` with exponential backoff;
- repeated identical failures are detected by fingerprint and pause the task;
- paused tasks resume explicitly, never silently.

## CLI surface

The terminal mirrors both APIs. Plugin output is permission-aware: each row
shows the plugin's risk and what the gate would do under the current mode
(`allowed`, `asks first`, or `denied`).

```bash
# Plugins
aegisx plugin list                 # loaded plugins + gate verdicts
aegisx plugin load ./my_plugin.py  # load from a Python file
aegisx plugin load my_pkg.plugins  # load from an importable module
aegisx plugin unload demo          # unload by plugin_id

# Scheduled tasks
aegisx schedule cancel <task-id>     # interrupt an active run, pause the task
aegisx schedule resume <task-id>     # resume from the persisted checkpoint
aegisx schedule checkpoint <task-id> # inspect the latest checkpoint
```

Inside the chat loop the same operations are available as `/plugin ...` and
`/schedule cancel|resume|checkpoint ...`.

## Built-in plugins

Three opt-in plugins ship inside the package under
`aegisx_agent/plugins/builtin/`. They are ordinary plugins — versioned
manifests, JSON Schemas, gate-checked — just bundled:

| Module | Tools | Behaviour |
|---|---|---|
| `builtin.browser` | `read_page`, `http_get` | Fetch pages as readable text or raw JSON. Refuses private/loopback hosts unless `AEGISX_ALLOW_PRIVATE_HTTP=1` (SSRF guard). |
| `builtin.github` | `gh_api`, `list_issues`, `get_file` | GitHub REST calls with optional `AEGISX_GITHUB_TOKEN`. |
| `builtin.database` | `sql_query` | Read-only `SELECT`/`WITH` against a SQLite file (`AEGISX_DB_PATH` or per-call), row-capped, keyword denylist. |

```python
agent.load_plugin_module("aegisx_agent.plugins.builtin.browser")
agent.load_plugin_module("aegisx_agent.plugins.builtin.github")
agent.load_plugin_module("aegisx_agent.plugins.builtin.database")
agent.load_plugin_module("aegisx_agent.plugins.builtin.all")  # convenience: all three
```

All three declare risk `caution`: permitted in `ask`/`allow-all` modes (with a
prompt in the former), refused in `read-only`. The `all` convenience module
registers every bundled plugin at once.

The scheduler remains fail-closed for unattended permission prompts. Plugins and
scheduled tasks therefore share the same permission and audit infrastructure.
