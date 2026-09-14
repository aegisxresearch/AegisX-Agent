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

The scheduler remains fail-closed for unattended permission prompts. Plugins and
scheduled tasks therefore share the same permission and audit infrastructure.
