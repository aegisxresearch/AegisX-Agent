# 🗺️ Codebase Map — AegisX Agent

> Peta navigasi untuk memahami proyek dalam ~2 menit. Diverifikasi langsung ke kode.

## Apa ini?

**AegisX Agent** (`aegisx-agent` v0.1.0) — agentic AI CLI berbahasa Python (~14.000 LOC, 155 file) yang mendukung multi-provider LLM (OpenAI, Anthropic, Ollama, Groq, custom OpenAI-compatible), tool calling, RAG, memory 4-lapis, scheduler, plugins, MCP, dan subagent delegation. Entry point: `aegisx` = `aegisx_agent.cli:app` (Typer).

**Stack kunci:** Python ≥3.10, pydantic-settings (config), httpx (semua HTTP ke LLM — SDK `openai`/`anthropic` ada di deps tapi **tidak dipakai langsung**), rich+typer (CLI), ChromaDB (RAG), SQLite (scheduler/daemon/sessions), tiktoken, pytest+mypy strict+ruff (dev).

## Entry Points

| Jalur | File |
|---|---|
| CLI chat (default) | `cli/main.py` → `main()` callback → `_run_chat()` |
| One-shot task | `aegisx run "task"` → `cli/main.py:run()` → `agent.chat()` |
| Plan & execute | `aegisx plan` → `agent.plan_and_execute()` |
| Programmatic | `aegisx_agent/__init__.py` → `AegisXAgent` |
| Launcher | `./aegisx` (bash) → `python -m aegisx_agent.cli` |

## Alur Data Utama (satu turn chat)

```
user input (cli/interactive.py:_run_chat)
→ AegisXAgent.chat() (core/agent.py)          — add ke ConversationMemory, build system prompt
→ _build_system_prompt(): persona + tools + RAG + project context (project.py)
   + prompt memory + skill summaries + user model + permission policy
→ AgenticLoop.run/run_streaming (core/loop.py)
   → LLM call via UsageTracker → provider (llm/*.py, httpx)
   → ada tool_calls? → ToolRegistry.execute (tools/registry.py)
      → PermissionGate.check (security/permissions.py)  ← fail-closed, audit ke JSONL
      → Tool.execute(**args) → ToolResult
   → hasil dikirim balik ke LLM sebagai message role=tool (by id, bukan name)
   → loop sampai max_iterations=15 atau LLM tidak minta tool lagi
   → setiap 3 iterasi: self-reflection (_self_reflect, JSON verdict)
   → tool gagal? → _recover_from_error (LLM sarankan tool alternatif)
→ respons final → memory + session store + auto-skill capture
```

## Module Boundaries (expose → consume)

| Modul | Expose | Consume dari |
|---|---|---|
| `core/` | `AegisXAgent` (orchestrator), `AgenticLoop`, `AgentConfig`, `RAGAPI/MemoryAPI/SchedulerAPI` (mixin) | cli, scheduler |
| `llm/` | `LLMProvider` ABC, `Message/ToolCall/LLMResponse`, factory, `autodetect` (probing Ollama lokal) | core, observability (wrapper) |
| `tools/` | `Tool` ABC + `ToolRisk`, `ToolRegistry` (satu-satunya jalur eksekusi, gate ditekan di sini), `FunctionTool`, tool-tool konkret | core, plugins, subagent |
| `security/` | `PermissionGate` (evaluate murni + check async), `AuditLog` (JSONL, redact kredensial) | core, tools, cli, plugins |
| `memory/` | `ConversationMemory`/`LongTermMemory` (store.py), 4-lapis advanced (PromptMemory, SessionStore SQLite+FTS5, UserModel) | core |
| `rag/` | `RAGEngine` (ChromaDB lazy-init, chunking) | core, tools/rag_search |
| `scheduler/` | `Scheduler` (SQLite, checkpoint, backoff, recovery), `ScheduledTask`, cron parser 5-field lengkap | core, cli/commands |
| `skills/` | `SkillManager` (markdown di ~/.aegisx/skills), `Skill`, capture otomatis | core, tools/skill_tool |
| `planning/` | `PlanBuilder`, `ExecutionPlan` (ReAct) | core |
| `plugins/` | `PluginRegistry`, `PluginManifest` (versi + qualified name `plugin_<id>_<tool>`), builtin: browser (SSRF guard), github, database (SELECT-only) | core, cli |
| `mcp/` | `MCPManager` (config ~/.aegisx/mcp_servers.json), `MCPToolClient` (stdio JSON-RPC di thread+loop sendiri — anyio cancel scope butuh satu task), `MCPToolBridge` (risk CAUTION default, eskalasi via kata "dangerous/destructive") | core, cli |
| `observability/` | `UsageTracker` (wraps LLMProvider, JSONL per call), `track_delegation` (contextvar per asyncio-task) | core, cli, subagent |
| `cli/` | Typer app + 22 slash commands, animated progress, setup wizard, autodetect Ollama | — (top layer) |

## Sistem Keamanan (desain paling khas)

1. **Permission gate fail-closed** (`security/permissions.py`): risk dideklarasikan tool sendiri, dievaluasi **per call** (`risk_for(arguments)` — mis. `file_ops read`=safe, `delete`=dangerous; `run_tests` custom command=-dangerous). Mode: `allow-all`/`ask`/`read-only`.无人 attended (scheduler) + dangerous = **ditolak**, bukan disunyikan.
2. **Audit log** (`security/audit.py`): JSONL append-only, redact nilai kredensial (apikey/token/secret/authorization…), best-effort (disk penuh tidak mematikan agent).
3. **Subagent** (`tools/subagent.py`): child dapat tool whitelist dari parent, share PermissionGate yang sama (bukan bypass), step budget = `max_iterations` child (struktural), depth cap 2, timeout wall-clock, toolset hanya mengecil turun generasi. `spawn_subagent` tidak pernah diberikan by name.
4. **Registry = satu pintu**: tidak ada jalur eksekusi tool yang melewati gate; `SystemExit` pun ditangkap.
5. **MCP/plugins tidak jadi pintu samping**: semua lewat registry + gate; MCP tool default CAUTION, eskalasi otomatis via deskripsi.

## Dependency Graph

```
cli → core → {llm, tools, security, memory, rag, skills, planning,
              plugins, mcp, scheduler, observability, personas, project}
tools/subagent → core/loop (child AgenticLoop) + observability
tools/registry → security (TYPE_CHECKING only — hindari import runtime)
plugins → tools/base; mcp → plugins + tools
scheduler → core (agent_factory membuat agent interaktif=false)
```
Arah bersih (top-down), tidak ada siklus runtime yang bermasalah. Shims back-compat: `aegisx_agent/config.py` dan `aegisx_agent/agent_loop.py` (di-omit dari coverage).

## Test Suite (50 file, gate: coverage ≥77%, mypy strict)

- `tests/fake_llm.py` — HTTP server in-process yang bicara wire format OpenAI/Anthropic asli (SSE, 429 retry) → e2e tanpa API key.
- `tests/support.py`, `conftest.py` — helper & sys.path shim ke root repo (paket flat di root, tanpa editable install).
- `tests/mcp_compat.py` — probe kompatibilitas SDK MCP: test yang spawn `mcp_demo_server.py` (API 2.x) di-skip dengan alasan jelas bila yang ter-install bukan mcp 2.x, alih-alih gagal dengan `McpError: Connection closed` yang menyesatkan. Jalankan suite lewat `.venv/bin/python -m pytest` (venv dari `uv venv --python 3.11 .venv && uv pip install -e ".[dev,mcp]"`).
- Cakupan: loop (tool-call id matching, failure containment, parallel), gate + fail-closed + audit redaction, scheduler/cron/daemon, skill library, konversi wire-format provider, MCP client/manager/bridge, subagent unit+e2e, observability, semua CLI command.

## Konvensi

- Docstring "mengapa bukan apa"; config via env `AEGISX_*` (pydantic-settings, `.env`).
- Tools: subclass `Tool`, schema JSON OpenAI-style; risk eksplisit; registry satu pintu.
- Persistensi di `~/.aegisx/`: config.json, audit.log, usage.jsonl, daemon.db, scheduler/, skills/, sessions/, vectorstore/, mcp_servers.json.
- Data sementara: `aegisx` file launcher di root; `aegisx/` (dir) berisi launcher serupa; `.githooks/` (commit filter, secret guard) diaktifkan via `git config core.hooksPath .githooks`.

## Catatan / Mismatch & Pertanyaan Terbuka

1. ~~conftest.py menambah `src/` ke sys.path~~ — **SUDAH DIPERBAIKI**: shim sekarang menunjuk root repo (lokasi paket flat yang benar).
2. **`openai` & `anthropic` SDK di dependencies tidak pernah di-import** — semua via httpx; bisa dibersihkan.
3. `AGENTS.md` di root adalah prompt "Freebuff Superpower Ultra" (roster sub-agent/skill markdown) — **bukan** bagian dari runtime AegisX; hanya dibaca sebagai instruksi workspace oleh `project.py` saat agent berjalan di repo ini.
4. `cli/interactive.py` punya blok komentar duplikat (copas kecil, kosmetik).
5. README klaim "mypy strict sama seperti CI" — `pyproject.toml` memang `strict = true`; CI config ada di `.github/` (belum diverifikasi isinya).
