# 🤖 AegisX Agent

**Super-powered Agentic AI** — Support ANY LLM provider, with tool calling, RAG, memory, planning, and customizable personas.

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🔌 **Multi-Provider LLM** | OpenAI, Anthropic, Ollama (local), Groq, or ANY OpenAI-compatible endpoint |
| 🔧 **Tool Calling** | Web search, code execution, file ops, shell, calculator, datetime |
| 📚 **RAG** | Ingest documents, search knowledge base with ChromaDB |
| 🧠 **Memory** | Conversation history + long-term fact storage |
| 📋 **Planning** | Multi-step reasoning with ReAct pattern |
| 🎭 **Personas** | Built-in personas + custom persona creation |
| 🖥️ **Rich CLI** | Beautiful terminal interface with streaming |
| 📁 **Workspace aware** | Knows the folder it runs in: stack, git state, `AGENTS.md` instructions |
| 🔐 **Permission gate** | Every tool call is classified by risk and gated before it runs |
| ⏰ **Scheduler** | Cron-style tasks that run unattended, with checkpoints, resume, exponential backoff, loop detection, and an audit log |
| 🧩 **Plugins** | Explicitly loaded, versioned tools with JSON schemas and permission policies |

## 🚀 Quick Start

### Install

```bash
pip install -e .
```

### Run it in a project

```bash
cd my-project

# Interactive chat — the agent reads this folder, its git state, and AGENTS.md
aegisx

# One-shot: run a task and exit (scriptable)
aegisx run "add tests for the scheduler"
echo "why is CI failing?" | aegisx run
```

No configuration is needed if an **Ollama** server is already running: AegisX
finds it, picks an installed chat model, and starts. Otherwise set a provider
explicitly (below).

### Set up your LLM provider

```bash
# Option 1: OpenAI
export AEGISX_OPENAI_API_KEY="sk-..."

# Option 2: Anthropic
export AEGISX_LLM_PROVIDER=anthropic
export AEGISX_ANTHROPIC_API_KEY="sk-ant-..."

# Option 3: Ollama (local, free!)
export AEGISX_LLM_PROVIDER=ollama
export AEGISX_OLLAMA_MODEL=llama3.1

# Option 4: Groq (fast inference)
export AEGISX_LLM_PROVIDER=groq
export AEGISX_GROQ_API_KEY="gsk_..."

# Option 5: ANY custom OpenAI-compatible endpoint
export AEGISX_LLM_PROVIDER=custom
export AEGISX_CUSTOM_BASE_URL="https://api.together.xyz/v1"
export AEGISX_CUSTOM_API_KEY="your-key"
export AEGISX_CUSTOM_MODEL="meta-llama/Llama-3-70b-chat-hf"
```

### What it sees when it starts

```
  📁 my-project (/home/you/code/my-project)
  Python • git main, 3 changed • 412 files
  📜 AGENTS.md loaded as instructions
  🔌 ollama • 🧠 llama3.1 • 🔧 15 tools • 💡 2 skills • 🔐 ask • 🎭 default
```

The first turn already knows the working directory, the language, whether the
tree is dirty, and any `AGENTS.md` / `CLAUDE.md` instructions — so it does not
have to spend a tool call working that out. Turn it off with
`AEGISX_PROJECT_CONTEXT_ENABLED=false`.

### Chat options

```bash
aegisx --provider ollama --model llama3.1
aegisx -p openai -m gpt-4o
aegisx -p custom --url https://api.together.xyz/v1 -k your-key -m meta-llama/Llama-3-70b-chat-hf
```

## 📖 Commands

```bash
aegisx                    # Start interactive chat in the current folder
aegisx chat               # Same as above
aegisx run "task"         # One-shot: do it, print the answer, exit
aegisx run < task.md      # Task read from stdin (pipeline friendly)
aegisx plan "goal"        # Plan and execute a multi-step goal
aegisx ingest ./docs/     # Ingest documents into knowledge base
aegisx search "query"     # Search the knowledge base
aegisx personas           # List available personas
aegisx tools              # List available tools and their risk level
aegisx config-info        # Show current configuration

# Safety
aegisx chat --permission-mode read-only   # Only let read-only tools run
aegisx chat --permission-mode allow-all   # Gate nothing (still audited)

# Scheduled tasks (run automatically)
aegisx schedule add nightly "summarise my inbox" --interval 1h
aegisx schedule add report "write the weekly report" --daily 09:00
aegisx schedule list      # Tasks, next run, last status
aegisx schedule run       # Execute due tasks now, then keep checking until Ctrl+C
aegisx schedule run --once  # Fire everything due and exit
aegisx schedule logs <id> # Run history for one task
aegisx schedule cancel <id> # Pause a task now (interrupts an active run)
aegisx schedule resume <id> # Resume a paused task from its checkpoint
aegisx schedule checkpoint <id> # Show a task's latest persisted checkpoint
aegisx schedule remove <id>

# Plugins (explicitly loaded, permission-aware)
aegisx plugin list         # Loaded plugins + the gate's verdict for each
aegisx plugin load ./my_plugin.py   # Load from a Python file
aegisx plugin load my_pkg.plugins   # Load from an importable module
aegisx plugin unload demo  # Remove a loaded plugin
```

## 🔌 Supported Providers

| Provider | Setup | Free? |
|----------|-------|-------|
| **OpenAI** | `AEGISX_OPENAI_API_KEY` | ❌ (paid) |
| **Anthropic** | `AEGISX_ANTHROPIC_API_KEY` | ❌ (paid) |
| **Ollama** | Install ollama, pull a model | ✅ (local) |
| **Groq** | `AEGISX_GROQ_API_KEY` | ✅ (free tier) |
| **Custom** | `AEGISX_CUSTOM_BASE_URL` | Varies |

### Custom Provider Examples

Works with ANY OpenAI-compatible API:

```bash
# Together AI
aegisx -p custom -u https://api.together.xyz/v1 -k $TOGETHER_KEY -m meta-llama/Llama-3-70b-chat-hf

# OpenRouter
aegisx -p custom -u https://openrouter.ai/api/v1 -k $OPENROUTER_KEY -m anthropic/claude-3.5-sonnet

# Local LM Studio
aegisx -p custom -u http://localhost:1234/v1 -m local-model

# vLLM
aegisx -p custom -u http://localhost:8000/v1 -m model-name

# Text Generation WebUI
aegisx -p custom -u http://localhost:5000/v1 -m model-name
```

## 🎭 Personas

```bash
# List personas
aegisx personas

# Use a persona
aegisx --persona coder
aegisx --persona researcher

# Create custom persona
# Save to ~/.aegisx/personas/my_persona.txt
```

Built-in personas: `default`, `coder`, `researcher`, `analyst`, `creative`, `hacker`, `scientist`

## 🔧 Tools

| Tool | Risk | Description |
|------|------|-------------|
| `web_search` | safe | Search the internet via DuckDuckGo |
| `execute_code` | dangerous | Run Python in this process (no sandbox) |
| `file_ops` | varies | Read, write, list, search, delete files |
| `shell` | dangerous | Execute shell commands (opt-in) |
| `calculator` | safe | Evaluate math expressions |
| `datetime` | safe | Date/time utilities |
| `rag_search` | safe | Search document knowledge base |
| `skill` | safe | List, search, and load reusable skills |
| `api_call` | varies | Call any REST endpoint |
| `db_query` | varies | Query SQLite / PostgreSQL |
| `web_scrape` | safe | Scrape and extract page content |
| `codebase` | safe | Explore project structure and code |
| `code_edit` | caution | Edit files with diff preview |
| `git` | varies | Git status / diff / commit / branch |
| `run_tests` | varies | Auto-detect and run the test suite |

## 🔐 Permissions

Every tool call passes one gate before it touches the system. Tools declare the
risk themselves, and it is evaluated **per call** — `file_ops read` is safe,
`file_ops delete` is not.

| Risk | Examples | What `ask` mode does |
|------|----------|----------------------|
| `safe` | read a file, search, calculate, `git status` | runs |
| `caution` | write or edit a file, POST/PUT, run auto-detected tests | runs, and is audited |
| `dangerous` | `execute_code`, `shell`, delete files, `git commit`, `DELETE` | asks first |

| Mode | Behaviour |
|------|-----------|
| `ask` (default) | safe and caution calls run; dangerous calls ask |
| `read-only` | only safe calls run |
| `allow-all` | nothing is gated (every call is still audited) |

```bash
aegisx chat --permission-mode read-only
AEGISX_PERMISSION_MODE=read-only aegisx chat
AEGISX_ALLOWED_TOOLS=execute_code,run_tests aegisx schedule run   # explicit opt-in
```

The flag works on `chat`, `plan`, and `schedule run`. In `ask` mode the
approval prompt offers **always allow**, which adds the tool to a per-session
allow-list without touching your saved config.

### Managing permissions from chat

```
/permissions                                  # policy, gated tools, 5 recent audit decisions
/permissions mode <allow-all|ask|read-only>   # switch mode (saved to config)
/permissions allow <tool>                     # never prompt for this tool again
/permissions deny <tool>                      # block it — even safe tools, even in allow-all
/permissions reset <tool>                     # clear its allow/deny override
```

Example session (real output):

```
You: /permissions mode read-only
✅ Permission mode: read-only

You: /permissions deny execute_code
✅ deny: execute_code

You: /permissions
         🔐 Tool Permissions          
┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Setting             ┃ Value        ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ Mode                │ read-only    │
│ Runtime             │ interactive  │
│ Allowed (no prompt) │ —            │
│ Denied              │ execute_code │
└─────────────────────┴──────────────┘
Audit log: /tmp/aegisx-demo/audit.log
Gated tools: execute_code (dangerous), code_edit (caution), run_tests (caution)
Usage: /permissions | /permissions mode <mode> | /permissions allow|deny|reset <tool>
```

`/permissions allow no_such_tool` is rejected with the list of valid tool
names, and the audit log path is printed with the policy so you can inspect
decisions afterwards.

**Unattended runs fail closed.** `aegisx schedule run` has nobody to answer a
prompt, so in `ask` mode a dangerous tool is *denied* rather than approved
silently. Opt in explicitly with `AEGISX_ALLOWED_TOOLS` or
`--permission-mode allow-all`.

Every decision is appended to `~/.aegisx/audit.log` as JSONL — tool, risk,
verdict, who decided — with credential-shaped arguments redacted.

## 📚 RAG (Document Ingestion)

```bash
# Ingest a file
aegisx ingest ./document.pdf

# Ingest a directory
aegisx ingest ./docs/

# Search knowledge base
aegisx search "what is the API rate limit?"
```

Supported formats: `.txt`, `.md`, `.py`, `.js`, `.ts`, `.json`, `.yaml`, `.toml`, `.pdf`

Requires ChromaDB (`pip install chromadb`) — everything else the agent needs
is installed by default.

## 🧪 Tests

```bash
uv venv --python 3.11 .venv
uv pip install -e ".[dev]"
.venv/bin/python -m pytest --cov=aegisx_agent   # enforced coverage gate
```

The suite covers the agentic loop (tool-call id matching, failure containment,
parallel batches), the tool registry, the permission gate and its fail-closed
paths, the audit log and its redaction, the scheduler, the skill library,
provider wire-format conversion, and end-to-end runs against a fake
OpenAI/Anthropic-compatible HTTP server.

## 🪝 Git hooks

Security hooks (commit-message filters, pre-push secret guard) are versioned
in `.githooks/` and activated per clone with:

```bash
git config core.hooksPath .githooks
```

## ⚙️ Configuration

All settings can be configured via environment variables (prefix `AEGISX_`):

```env
# .env file
AEGISX_LLM_PROVIDER=custom
AEGISX_CUSTOM_BASE_URL=https://api.together.xyz/v1
AEGISX_CUSTOM_API_KEY=your-key
AEGISX_CUSTOM_MODEL=meta-llama/Llama-3-70b-chat-hf
AEGISX_TEMPERATURE=0.7
AEGISX_MAX_ITERATIONS=15
AEGISX_MEMORY_ENABLED=true
AEGISX_RAG_ENABLED=true
AEGISX_SHELL_ENABLED=false
AEGISX_PROJECT_CONTEXT_ENABLED=true    # cwd, stack, git, AGENTS.md in the prompt

# Permissions
AEGISX_PERMISSION_MODE=ask           # allow-all | ask | read-only
AEGISX_ALLOWED_TOOLS=                # e.g. execute_code,run_tests
AEGISX_DENIED_TOOLS=                 # e.g. shell
AEGISX_AUDIT_LOG_ENABLED=true
```

## 🧩 Plugins and autonomous tasks

Versioned tool plugins can be loaded explicitly from a module or Python path;
they never execute merely because AegisX is imported. Every plugin remains
behind the normal tool registry and permission gate. Scheduler tasks persist
checkpoints, recover interrupted runs, support cancellation/resume, use
exponential retry backoff, and pause after repeated identical failures.

See [plugins and autonomous tasks](docs/plugins-and-autonomous-tasks.md) for the
Python API and lifecycle details. The CLI mirrors it: `aegisx plugin
list|load|unload` shows each plugin with its risk and the gate's verdict under
the current mode, and `aegisx schedule cancel|resume|checkpoint` controls
tasks without leaving the terminal.

## 🏗️ Architecture

```text
aegisx_agent/                 # flat package at the repo root (no src/)
├── core/                     # Agent runtime
│   ├── agent.py              # AegisXAgent orchestrator and chat/planning
│   ├── rag_api.py            # RAG ingestion and search API
│   ├── memory_api.py         # Conversation, fact, and session memory API
│   ├── scheduler_api.py      # Scheduled-task and scheduler-daemon API
│   ├── loop.py               # Agentic tool-use loop (streaming + retry)
│   └── config.py             # Configuration (pydantic-settings)
├── cli/                      # Rich terminal CLI
│   ├── app.py                # Shared Typer app + Rich console (single source)
│   ├── main.py               # Config plumbing + typer entry points
│   ├── interactive.py        # Chat loop, animated progress, slash dispatch
│   ├── commands/             # Slash handlers: permissions, code, schedule
│   └── __init__.py           # Re-exports for programmatic use
├── plugins/                  # Versioned tool manifests and explicit loader
├── security/                 # Permission gate + audit log
├── llm/                      # Multi-provider LLM support
│   ├── base.py               # Base abstractions
│   ├── factory.py            # Provider factory
│   ├── openai_provider.py
│   ├── anthropic_provider.py
│   ├── ollama_provider.py
│   ├── groq_provider.py
│   └── custom_provider.py    # ANY OpenAI-compatible endpoint
├── tools/                    # Tool system
│   ├── base.py               # Tool abstractions
│   ├── registry.py           # Tool registry
│   ├── coding/               # Git, editor, test runner, codebase search
│   ├── web_search.py
│   ├── web_scraper.py
│   ├── code_executor.py
│   ├── file_ops.py
│   ├── shell.py
│   ├── calculator.py
│   ├── db_query.py
│   ├── api_caller.py
│   ├── datetime_tool.py
│   └── rag_search.py
├── memory/                   # Memory system
│   ├── store.py              # Conversation + long-term memory
│   └── advanced.py           # Prompt memory, sessions, user model
├── rag/                      # RAG system
│   └── engine.py             # ChromaDB vector store
├── scheduler/                # Persistent autonomous task engine
│   ├── task.py               # Task model, checkpoint, retry/cancel state
│   └── engine.py             # Resume, backoff, loop detection, daemon
├── skills/                   # Skill capture + management
├── planning/                 # Planning system
│   └── react.py              # ReAct reasoning loop
├── personas/                # Persona system
│   └── loader.py             # Custom persona loader
├── project.py                # Project context detection
├── config.py                 # Back-compat re-export of core.config
└── agent_loop.py             # Back-compat re-export of core.loop
```
