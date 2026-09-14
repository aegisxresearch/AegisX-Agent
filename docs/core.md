# AegisX core architecture

The runtime package lives at `aegisx_agent/core/`. `AegisXAgent` remains the
stable public façade, while domain-specific public APIs are implemented in
small mixin modules. This keeps the orchestration code focused without
changing the existing Python or CLI API.

## Layout

```text
aegisx_agent/core/
├── __init__.py        # Public core exports
├── agent.py           # AegisXAgent lifecycle, chat, planning, tools, personas
├── rag_api.py         # RAG ingestion and knowledge-base search
├── memory_api.py      # Facts, conversation clearing, sessions, preferences
├── scheduler_api.py   # Scheduled task CRUD, execution, and daemon control
├── loop.py            # Agentic reasoning/tool-use loop
└── config.py          # Pydantic settings and provider configuration
```

## Responsibilities

### `agent.py`

`AegisXAgent` owns construction and coordination: configuration validation,
provider setup, permission-gated tool registration, prompt assembly, chat,
streaming, planning, personas, skills, and lifecycle wiring. It delegates the
three domain APIs below to its mixins.

### `rag_api.py`

`RAGAPI` exposes `ingest_document`, `ingest_text`, and `search_knowledge`. The
methods validate that RAG is enabled and then delegate to the lazily configured
`RAGEngine`.

### `memory_api.py`

`MemoryAPI` exposes long-term facts (`remember`, `recall`), conversation
cleanup (`clear_memory`), session search/statistics, and learned preferences.
It delegates persistence to the memory objects initialized by the agent.

### `scheduler_api.py`

`SchedulerAPI` exposes scheduled-task creation, listing, toggling, deletion,
log retrieval, immediate execution, due-task execution, and background-loop
startup. It delegates scheduling state and execution to `Scheduler`.

## Compatibility contract

```python
from aegisx_agent import AegisXAgent, AgentConfig
from aegisx_agent.core import AegisXAgent

agent = AegisXAgent(AgentConfig(...))
await agent.ingest_text("document text")
agent.remember("preferences", "Use concise answers")
tasks = agent.list_scheduled_tasks()
```

The façade and all existing method names are unchanged. The mixins are
implementation modules, not a second agent type; callers should normally
construct `AegisXAgent` and use its methods. Legacy imports from
`aegisx_agent.config` and `aegisx_agent.agent_loop` remain supported by their
compatibility shims.

## Dependency direction

```text
AegisXAgent
├── RAGAPI       → initialized _rag_engine → rag.engine.RAGEngine
├── MemoryAPI    → initialized memory stores → memory.store/advanced
└── SchedulerAPI → initialized scheduler   → scheduler.engine.Scheduler
```

The API mixins do not construct providers, read environment variables, or
create independent stores. Initialization remains centralized in
`AegisXAgent`, preventing duplicate state and preserving the existing
permission, persistence, and scheduler wiring.
