# AegisX Agent

**An autonomous pentest and bug-hunting agent for authorised security work.**

Point it at a target you are allowed to test. It maps the attack surface, decides
which lines of attack are worth running, proves what it can, throws away what it
cannot, and writes a report a developer can act on — in SARIF, Markdown, or a
self-contained HTML file.

The engine, not the model, decides what is true. The model only decides what
order to work in and what counts as proof.

> **Authorisation first.** Only assess systems you own or have written
> permission to test. The scope guard refuses anything you did not name, but no
> tool can grant you permission you do not have.

## Why the design is shaped this way

- **Scope is enforced at the network boundary, not by prompt.** A single guard
  sits in front of every request — before it is sent, not after. The model
  cannot widen it, and neither can a redirect or a host discovered mid-scan.
- **Findings need evidence.** Every one carries the request line, the response
  status, and the body snippet that proved it.
- **Severity and confidence are separate axes.** "Confirmed critical" and
  "probable medium" are different statements, and the report never collapses
  them into one number.
- **A check that finds nothing is cheap to skip.** Each check is narrow, so the
  expensive ones only run where the target earns them.
- **A write verb is never tested by writing.** `PUT`, `DELETE`, and `PATCH` are
  refused before they reach the network; the check reads the server's `Allow`
  header instead, and reports an advertisement as probable, not confirmed.
- **Discipline over volume.** A finding the reader cannot reproduce is a
  finding the reader will not fix.

## Quick start

### Install

One command — installs from GitHub, sets up a virtualenv, and puts `aegisx` on
your PATH:

```bash
curl -fsSL https://raw.githubusercontent.com/aegisxresearch/AegisX-Agent/main/installer.sh | bash
```

Options: `--ref <branch-or-tag>`, `--dir <path>`, `--bin <path>`, `--with-mcp`
(the MCP extra), `--dev` (test tools); `--uninstall` removes it again. Run
`installer.sh --help` for the full list. Re-running the installer updates an
existing install, and it cleans up a stale `alias aegisx=...` or dead `PATH`
line left by an older manual install (only provably dead lines; every edited
file gets a `.bak-aegisx-*` backup).

Working from a checkout instead:

```bash
pip install -e ".[dev]"
```

### Assess something

No API key is needed for the deterministic engine:

```bash
# What can this engine actually do?
aegisx cyber capabilities

# Full deterministic assessment: recon, checks, findings, reports
aegisx cyber scan https://staging.example.com --report-dir ./out

# Static analysis of a source tree: committed secrets, sinks, misconfiguration
aegisx cyber sast ./

# Dependencies against OSV, with an offline fallback
aegisx cyber audit ./
```

Add a model and the same engine becomes an agent that plans its own attack:

```bash
export AEGISX_OPENAI_API_KEY="sk-..."          # or Anthropic, Groq, Ollama, or any compatible endpoint
aegisx cyber agent https://staging.example.com --report-dir ./out
```

Without a provider, `cyber agent` still runs: it falls back to the engine's
default plan and skips the review stage, and says so rather than pretending the
run was complete.

## The assessment pipeline

```text
survey   map the target with no model in the loop: endpoints, parameters,
         technologies, robots.txt, and the framework versions in use
plan     the model chooses lines of attack from a fixed capability catalogue
run      the swarm executes them concurrently, each line granted only the
         capabilities it was given
escalate the first wave's findings are fed back; the model may plan one
         narrower follow-up wave, or decline when nothing is worth following
verify   the model may dismiss a false positive, with a stated reason, under a
         retention floor and a protected severity (info/low are never dismissed)
report   SARIF 2.1.0, Markdown, and a self-contained HTML file
```

Two properties fall out of that shape:

- A plan can only name a capability the engine implements, so it can never
  invent a technique that does not exist.
- A dismissal is recorded as an INFO finding rather than deleted, so the
  review is visible and can be challenged.

## Commands

```bash
# The six assessment verbs
aegisx cyber scan <url>          # deterministic pipeline: recon → checks → findings
aegisx cyber sast [path]         # static analysis: secrets, sinks, misconfiguration
aegisx cyber audit [path]        # dependency advisories (OSV + offline fallback)
aegisx cyber swarm <url>         # several lines of attack at once, no model
aegisx cyber agent <url>         # autonomous: survey → plan → execute → verify
aegisx cyber report <file.json>  # re-render saved findings into another format
aegisx cyber capabilities        # the capabilities the engine can actually run
```

Flags on `scan`:

| Flag | Default | Meaning |
|------|---------|---------|
| `--deep/--no-deep` | `--deep` | Include the injection checks (which also need the target to earn them) |
| `--max-requests` | `2000` | Total request budget |
| `--rate` | `10.0` | Requests per second |
| `--concurrency` | `6` | Concurrent requests |
| `--timeout` | `1800` | Deadline in seconds |
| `--report-dir` | unset | Write reports into this directory |
| `--report-format` | `all` | `sarif`, `markdown`, `html`, or `all` |
| `--min-severity` | `info` | Lowest severity to show |
| `--allow-private` | off | Permit loopback and private addresses (own network only) |
| `--json` | off | Print the result as JSON on stdout; progress goes to stderr, so it pipes cleanly |

`swarm` and `agent` take `--max-requests`, `--concurrency`, `--timeout`,
`--report-dir`, `--json`; `agent` additionally takes `--verify/--no-verify`.

### Inside a chat session

`aegisx` in a project folder starts an interactive session that already knows
the directory, the stack, the git state, and any `AGENTS.md` instructions. The
same engine is reachable as a slash command there, and both entry points call
the same implementation functions, so they cannot drift apart:

```text
/cyber scan <url>        /cyber status
/cyber sast [path]       /cyber scope <url>
/cyber audit [path]      /cyber capabilities
/cyber swarm <url>       /cyber help
/cyber agent <url>
```

Conversational cyber mode — the four assessment tools registered as tools the
model can call by itself — is opt-in with `AEGISX_CYBER_ENABLED=true`. It is off
by default because it adds those tools and roughly two thousand tokens of
doctrine to every prompt. The commands above work either way. Nothing is
scanned until a target is named in the request.

## What the engine checks

Each check is one narrow question asked of a target. The default scan runs the
cheap ones on every target, and the injection checks only where the target
earns them.

| Family | Checks | Runs |
|--------|--------|------|
| Transport & headers | missing/weak security headers, cookie attributes, TLS protocol and certificate, CORS with credentials | always |
| Configuration | dangerous read methods (`TRACE`, `PROPFIND`), write methods advertised in `Allow`, open redirects, verbose error pages, directory listing | always |
| Exposure | sensitive files (`.env`, `.git`, backups, source maps), admin surface, cloud storage listing | always / swarm |
| Access control | object-level authorisation, dangerous methods on write endpoints | deep / swarm |
| Identity | token decoding (`alg: none`, missing `exp`, claims in the payload), session identifiers | swarm |
| API surface | GraphQL introspection, mass assignment, debug endpoints | swarm |
| Injection | SQL (boolean pair + error signatures), XSS, template injection, path traversal, SSRF, XXE, command injection, NoSQL, CRLF, host-header | deep / swarm |
| Static analysis | committed secrets, dangerous sinks, weak cryptography, framework misconfiguration, manifests | `cyber sast` |
| Dependencies | OSV advisories with a bundled offline fallback | `cyber audit` |

Which capability a given wave may use is a grant, not a suggestion: a line of
attack that was not granted `inject` cannot run an injection check, even if the
model asks for it.

## Scope guarantees

Enforced at the network boundary, before any request is sent:

- only the hosts you named (plus any `extra_hosts` you configured) are
  contacted; hosts discovered during the scan are never added automatically;
- literal IPs in loopback, private, link-local, multicast, and cloud-metadata
  ranges are refused unless you pass `--allow-private` /
  `AEGISX_CYBER_ALLOW_PRIVATE_ADDRESSES=true`;
- a redirect is followed only if the *next* host is also in scope;
- a request budget, a per-host budget, a rate limit, a concurrency cap, and a
  deadline apply to every check;
- every refusal is recorded with its reason as part of the audit trail.

Every request is also checked against a verb allowlist. `GET`, `HEAD`,
`OPTIONS`, `POST`, `TRACE`, and `PROPFIND` may be sent. `PUT`, `DELETE`,
`PATCH`, and `CONNECT` are refused *before* they reach the network, so no check
can test a write verb by performing the write. Write methods are reported from
the server's own `Allow` header instead, as a probable finding rather than a
confirmed one. A caller building the policy directly can narrow the allowlist
further (`ScopePolicy.allowed_methods`), but the engine never widens it past
the read-only set on its own.

## Shipped playbooks

A playbook is a procedure, not a capability: the engine decides what is true,
the playbook decides what order to work in and what counts as proof. They ship
with the engine so a run starts from the same procedure every time — an
assessment whose method depends on what the model happened to remember is not
reproducible, and a report that cannot be reproduced is not evidence.

`recon-survey`, `injection-hunt`, `authn-review`, `authz-review`, `api-abuse`,
`exposure-hunt`, `supply-chain-review`, `business-logic`, `cloud-posture`,
`finding-discipline`.

They are rendered into the system prompt when conversational cyber mode is on
(`AEGISX_CYBER_ENABLED=true`), not installed into the skill library — the
library is for procedures the agent learned or you wrote, and mixing shipped
doctrine into it made the two indistinguishable. Your own skills sit alongside
them and are never overwritten by an upgrade.

## Reports

`--report-dir ./out` writes:

| File | Format |
|------|--------|
| `aegisx-report.sarif` | SARIF 2.1.0 — what GitHub code scanning and most CI pipelines consume |
| `aegisx-report.md` | Markdown for a ticket or a merge request |
| `aegisx-report.html` | A self-contained file, no network access needed to read it |

`aegisx cyber report <findings.json> --dir out --format sarif` re-renders saved
findings without re-running the assessment, and `--min-severity` filters what
reaches every format. The input is the findings document `--json` prints, so
`aegisx cyber scan <url> --json > findings.json` then `aegisx cyber report
findings.json` is the supported round-trip.

## Configuration

The deterministic commands ignore the LLM settings entirely.

```env
# Scope and budgets
AEGISX_CYBER_DEFAULT_TARGETS=           # comma-separated hosts, or empty to name one per request
AEGISX_CYBER_MAX_REQUESTS=2000
AEGISX_CYBER_REQUESTS_PER_SECOND=10
AEGISX_CYBER_MAX_CONCURRENCY=6
AEGISX_CYBER_TIMEOUT_SECONDS=1800
AEGISX_CYBER_ALLOW_PRIVATE_ADDRESSES=false   # loopback/private targets: own network only

# Conversational cyber mode (the commands always work; this turns on the chat tools)
AEGISX_CYBER_ENABLED=false              # true registers cyber_scan/sast/cve/report as model tools
AEGISX_PERSONA=default                  # switch to `pentester` for the offensive-security doctrine

# The model the agent plans and reviews with
AEGISX_LLM_PROVIDER=openai              # openai | anthropic | ollama | groq | custom
AEGISX_OPENAI_API_KEY=sk-...
```

## Supported LLM providers

| Provider | Setup | Free? |
|----------|-------|-------|
| **OpenAI** | `AEGISX_OPENAI_API_KEY` | no |
| **Anthropic** | `AEGISX_LLM_PROVIDER=anthropic` + `AEGISX_ANTHROPIC_API_KEY` | no |
| **Ollama** | install ollama, pull a model (`AEGISX_LLM_PROVIDER=ollama`) | yes, local |
| **Groq** | `AEGISX_GROQ_API_KEY` | free tier |
| **Custom** | `AEGISX_CUSTOM_BASE_URL` / `_API_KEY` / `_MODEL` | varies |

Any OpenAI-compatible endpoint works — Together, OpenRouter, LM Studio, vLLM,
Text Generation WebUI:

```bash
aegisx -p custom -u https://openrouter.ai/api/v1 -k $OPENROUTER_KEY -m anthropic/claude-3.5-sonnet
```

If an Ollama server is already running and nothing else is configured, AegisX
finds it, picks an installed chat model, and starts.

## Personas

```bash
aegisx personas                     # list what is available
aegisx --persona pentester          # offensive-security doctrine
aegisx --persona coder              # switch to coding mode
```

Built-in: `default`, `pentester`, `hacker`, `coder`, `researcher`, `analyst`,
`creative`, `scientist`. `default` is the pentest and bug-hunting assistant and
is what a plain session gets; `pentester` adds the full offensive-security
doctrine; `coder` switches to engineering. Custom personas are files in
`~/.aegisx/personas/`.

## The platform underneath

The agent is not only a scanner driver. These are the parts a real engagement
needs, and each one stays behind the same permission gate.

### Tools

| Tool | Risk | Description |
|------|------|-------------|
| `cyber_scan` | dangerous | Scoped assessment of an authorised target |
| `cyber_sast` | caution | Static analysis of a source tree |
| `cyber_cve` | safe | Dependency advisory lookup |
| `cyber_report` | safe | Render findings as SARIF / Markdown / HTML |
| `web_search` | safe | Search the internet |
| `web_scrape` | safe | Fetch and extract page content |
| `codebase` | safe | Explore project structure and code |
| `code_edit` | caution | Edit files with a diff preview |
| `git` | varies | Status, diff, commit, branch |
| `run_tests` | varies | Detect and run the project's tests |
| `api_call` | varies | Call any REST endpoint |
| `db_query` | varies | Read-only SQLite / PostgreSQL |
| `shell` | dangerous | Execute shell commands (opt-in) |
| `execute_code` | dangerous | Run Python in this process (no sandbox) |
| `file_ops` | varies | Read, write, list, search, delete files |
| `calculator`, `datetime` | safe | Math and time utilities |
| `rag_search`, `skill` | safe | Knowledge base and skill library |

### Permissions

Every tool call passes one gate, and risk is evaluated **per call** —
`file_ops read` is safe, `file_ops delete` is not.

| Risk | Examples | What `ask` mode does |
|------|----------|----------------------|
| `safe` | read a file, search, `git status` | runs |
| `caution` | write a file, POST/PUT, run tests | runs, and is audited |
| `dangerous` | `execute_code`, `shell`, delete, `git commit`, `cyber_scan` | asks first |

| Mode | Behaviour |
|------|-----------|
| `ask` (default) | safe and caution run; dangerous asks |
| `read-only` | only safe calls run |
| `allow-all` | nothing is gated (every call is still audited) |

```bash
aegisx chat --permission-mode read-only
AEGISX_ALLOWED_TOOLS=execute_code,run_tests aegisx schedule run
```

In chat, `/permissions` shows the policy, the gated tools, and the last five
decisions, and `/permissions allow|deny|reset <tool>` edits it. Every decision
is appended to `~/.aegisx/audit.log` as JSONL — tool, risk, verdict, who
decided — with credential-shaped arguments redacted.

**Unattended runs fail closed.** `aegisx schedule run` has nobody to answer a
prompt, so in `ask` mode a dangerous tool is denied rather than approved
silently.

### Memory, RAG, and skills

```bash
aegisx ingest ./docs/            # ingest into the knowledge base (ChromaDB)
aegisx search "rate limit"       # search it
aegisx skills list               # learned procedures, portable between machines
aegisx skills export "injection-hunt" hunt.md
aegisx skills import https://gist.github.com/user/abc123
```

Supported ingestion formats: `.txt`, `.md`, `.py`, `.js`, `.ts`, `.json`,
`.yaml`, `.toml`, `.pdf`.

### Scheduled and unattended runs

```bash
aegisx schedule add nightly "re-check the staging surface" --daily 09:00
aegisx schedule run --once       # fire everything due and exit
aegisx daemon add pulse "check CI status" --every 15m
aegisx daemon run                # tasks live in SQLite and survive restarts
```

Scheduler tasks persist checkpoints, recover interrupted runs, support
cancel/resume, use exponential retry backoff, and pause after repeated
identical failures. Cron syntax supports ranges, steps, and names.

### Plugins, MCP, and subagents

Plugins load explicitly, never on import, and stay behind the gate:

```python
agent.load_plugin_module("aegisx_agent.plugins.builtin.browser")   # read_page, http_get
agent.load_plugin_module("aegisx_agent.plugins.builtin.github")    # gh_api, list_issues, get_file
agent.load_plugin_module("aegisx_agent.plugins.builtin.database")  # sql_query (read-only SELECT)
```

MCP servers (any external tool catalogue) use the same pipeline:

```bash
aegisx mcp wizard                 # pick, fill, test-connect
aegisx mcp connect --all          # → plugin_mcp_<server>_<tool>
aegisx mcp doctor files           # why won't it connect?
```

Subagents get their own history, a narrower tool set, and a hard step budget;
every child call passes the **same** gate as the parent, so delegation is never
a bypass. `aegisx usage --delegations` prices each one separately.

See [plugins and autonomous tasks](docs/plugins-and-autonomous-tasks.md) and
[MCP integration](docs/mcp-integration.md).

### Observability

```bash
aegisx usage               # token/cost summary across recent runs
aegisx usage --today
aegisx usage --delegations # subagent cost, one row per delegation
aegisx audit --denied      # only the gate's refusals
```

Every LLM call records one JSONL line in `~/.aegisx/usage.jsonl`, because the
tracker wraps the provider itself — so chat, plan execution, scheduled runs, and
daemon runs are all measured.

## Architecture

```text
aegisx_agent/
├── cyber/                    # The assessment engine — the core of this project
│   ├── scope.py              # Scope guard: allowlist, budgets, deadline, private-address refusal
│   ├── prober.py             # The only network path: scoped, rate-limited, evidence-capturing
│   ├── recon.py              # Crawler, fingerprinting, port and subdomain discovery
│   ├── checks/               # One narrow check per module (headers, injection, authz, …)
│   ├── swarm.py              # Parallel lines of attack with per-line capability grants
│   ├── agent.py              # Autonomous loop: survey → plan → execute → escalate → verify
│   ├── playbooks.py          # Shipped pentest procedures
│   ├── sast.py               # Static analysis: secrets, sinks, misconfiguration, manifests
│   ├── cve.py                # OSV advisories with a bundled offline fallback
│   ├── report.py             # SARIF 2.1.0, Markdown, self-contained HTML
│   └── tools.py              # cyber_scan / cyber_sast / cyber_cve / cyber_report
├── core/                     # Agent runtime (loop, orchestrator, config)
├── cli/                      # Typer app, chat loop, slash commands
├── security/                 # Permission gate + audit log
├── llm/                      # Multi-provider support
├── tools/                    # Tool registry and builtins
├── memory/                   # Conversation, facts, sessions
├── rag/                      # ChromaDB vector store
├── scheduler/                # Persistent task engine
├── skills/                   # Skill capture and library
├── plugins/                  # Versioned manifests and explicit loader
├── mcp/                      # MCP client and server catalogue
├── personas/                 # Persona loader
└── planning/                 # ReAct planning
```

## Development

```bash
uv venv --python 3.11 .venv
uv pip install -e ".[dev]"
.venv/bin/python -m pytest --cov=aegisx_agent   # enforced coverage gate
.venv/bin/python -m mypy aegisx_agent           # strict typecheck (same gate as CI)
.venv/bin/ruff check aegisx_agent tests         # lint (same rules as CI)
```

The cyber suite (`tests/test_cyber_*.py`) drives every check against a scripted
target, and covers the scope guard's refusals, the crawler's same-origin rule,
the swarm's capability grants and corroboration, the agent's two-wave escalation
and bounded review, static analysis and advisory matching, the reporters, and
the CLI commands.

### Git hooks

Security hooks (commit-message filters, pre-push secret guard) are versioned in
`.githooks/` and activated per clone:

```bash
git config core.hooksPath .githooks
```

## Licence

MIT.
