# 🗺️ Codebase Map — AegisX Agent

> Peta navigasi untuk memahami proyek dalam ~2 menit. Diverifikasi langsung ke kode.

## Apa ini?

**AegisX Agent** (`aegisx-agent` v0.1.0) — agentic AI CLI berbahasa Python (~29.000 LOC, 114 file) yang mendukung multi-provider LLM (OpenAI, Anthropic, Ollama, Groq, custom OpenAI-compatible), tool calling, RAG, memory 4-lapis, scheduler, plugins, MCP, subagent delegation, dan **mode cyber** (asesmen keamanan untuk target yang diotorisasi). Entry point: `aegisx` = `aegisx_agent.cli:app` (Typer).

**Stack kunci:** Python ≥3.10, pydantic-settings (config), httpx (semua HTTP ke LLM — SDK `openai`/`anthropic` dihapus dari deps, tidak pernah dipakai), rich+typer+prompt_toolkit (CLI; pt untuk autocomplete & riwayat input), ChromaDB (RAG), SQLite (scheduler/daemon/sessions), tiktoken, pytest+mypy strict+ruff (dev).

## Entry Points

| Jalur | File |
|---|---|
| CLI chat (default) | `cli/main.py` → `main()` callback → `_run_chat()` (interactive.py) |
| Onboarding | `aegisx init` → `cli/main.py:init()` — provider/model/key + AGENTS.md template |
| One-shot task | `aegisx run "task" [--json]` → `cli/main.py:run()` → `agent.chat()` |
| Plan & execute | `aegisx plan` → `agent.plan_and_execute(confirm=…)` — plan-then-confirm |
| Asesmen keamanan | `aegisx cyber scan\|sast\|audit\|swarm\|agent\|report\|capabilities` → `cli/commands/cyber.py` → `cyber/scanner.py` atau `cyber/agent.py` |
| Slash cyber | `/cyber …` → `cli/interactive.py` case `/cyber` → `_handle_cyber_command` (handler yang sama dengan CLI) |
| Update install | `aegisx update` → fetch + reset + reinstall (pip/uv) |
| Programmatic | `aegisx_agent/__init__.py` → `AegisXAgent` |
| Launcher | `./aegisx` (bash) → set `PYTHONPATH`, **tanpa `cd`** — cwd pengguna dipertahankan agar deteksi project benar |

## Alur Data Utama (satu turn chat)

```
user input (cli/interactive.py:_run_chat)
  input via prompt_toolkit (slash autocomplete + riwayat) bila TTY, fallback rich Prompt
→ AegisXAgent.chat()/chat_stream() (core/agent.py) — add ke ConversationMemory, build system prompt
→ _build_system_prompt(): persona + tools + RAG + project context (project.py)
   + prompt memory + skill summaries + user model + permission policy
   + (hanya kalau AEGISX_CYBER_ENABLED) instruksi cyber + playbook pentest
→ AgenticLoop.run/run_streaming (core/loop.py)
   → LLM call via UsageTracker → provider (llm/*.py, httpx)
   → iterasi 0 & permintaan berbentuk tugas → tool_choice="required" (AEGISX_FORCE_TOOLS=auto|always|never);
     server menolak (400)? → dicatat di _rejects_required_tool_choice dan diulang dengan "auto"
   → jawaban teks tanpa tool yang menyuruh user paste kode → satu retry paksa tool (jalur non-streaming)
   → error HTTP? → _raise_with_server_message: body pesan server ikut naik (quota/model/key)
   → HTTP 200 tanpa "choices", atau envelope error di stream? → dianggap gagal upstream: retry,
     lalu LLMProviderError berisi body (bukan KeyError 'choices')
   → budget guard: AEGISX_MAX_TOKENS_PER_TURN tercapai → berhenti anggun (default off)
   → ada tool_calls? → on_tool_start(name, args) → ToolRegistry.execute (tools/registry.py)
      → PermissionGate.check (security/permissions.py)  ← fail-closed, audit ke JSONL
      → Tool.execute(**args) → ToolResult → on_tool_result(name, success)
   → hasil dikirim balik ke LLM sebagai message role=tool (by id, bukan name)
   → loop sampai max_iterations=15 atau LLM tidak minta tool lagi
   → setiap 3 iterasi: self-reflection (_self_reflect, JSON verdict)
   → tool gagal? → _recover_from_error (LLM sarankan tool alternatif)
→ respons final → memory + session store + auto-skill capture
→ UI: StreamLinePrinter — status tool dim sebelum eksekusi, prose streaming live,
   fenced code blocks dirender rich Markdown; footer HUD turn (token · tool · detik)
```

## Module Boundaries (expose → consume)

| Modul | Expose | Consume dari |
|---|---|---|
| `core/` | `AegisXAgent` (orchestrator), `AgenticLoop`, `AgentConfig`, `RAGAPI/MemoryAPI/SchedulerAPI` (mixin) | cli, scheduler |
| `llm/` | `LLMProvider` ABC, `Message/ToolCall/LLMResponse`, factory, `autodetect` (probing Ollama lokal) | core, observability (wrapper) |
| `tools/` | `Tool` ABC + `ToolRisk`, `ToolRegistry` (satu-satunya jalur eksekusi, gate ditekan di sini), `FunctionTool`, tool-tool konkret | core, plugins, subagent |
| `security/` | `PermissionGate` (evaluate murni + check async, **scoped allow**: `editor`→`<dir>/**`, `shell`→`<program>*` via `allow_scoped`), `AuditLog` (JSONL, redact kredensial) | core, tools, cli, plugins |
| `memory/` | `ConversationMemory`/`LongTermMemory` (store.py), 4-lapis advanced (PromptMemory, SessionStore SQLite+FTS5, UserModel) | core |
| `rag/` | `RAGEngine` (ChromaDB lazy-init, chunking) | core, tools/rag_search |
| `scheduler/` | `Scheduler` (SQLite, checkpoint, backoff, recovery), `ScheduledTask`, cron parser 5-field lengkap | core, cli/commands |
| `skills/` | `SkillManager` (markdown di ~/.aegisx/skills), `Skill`, capture otomatis, **export/import** file & URL portabel (`import_skill_url`, gist→raw) | core, tools/skill_tool, cli |
| `planning/` | `PlanBuilder`, `ExecutionPlan` (ReAct) | core |
| `plugins/` | `PluginRegistry` (+ `origin_of` untuk **hot-reload** `/plugin reload`), `PluginManifest` (versi + qualified name `plugin_<id>_<tool>`), builtin: browser (SSRF guard), github, database (SELECT-only) | core, cli |
| `mcp/` | `MCPManager` (config ~/.aegisx/mcp_servers.json), `MCPToolClient` (stdio JSON-RPC di thread+loop sendiri — anyio cancel scope butuh satu task), `MCPToolBridge` (risk CAUTION default, eskalasi via kata "dangerous/destructive"), `catalog.py` (katalog server terkenal) | core, cli |
| `cyber/` | `ScopeGuard`+`ScopePolicy` (satu-satunya batas jaringan), `Prober` (satu-satunya jalur request: scope, budget, rate, bukti), `Finding`/`ScanResult`, `scanner.py`, `swarm.py` (grant kapabilitas per jalur), `agent.py` (survey→plan→execute→escalate→verify), `playbooks.py`, `sast.py`, `cve.py`, `report.py`, tool `cyber_*` (satu `CyberSession` bersama) | core/agent, cli |
| `observability/` | `UsageTracker` (wraps LLMProvider, JSONL per call), `track_delegation` (contextvar per asyncio-task) | core, cli, subagent |
| `cli/` | Typer app + 27 slash commands, prompt_toolkit input + fallback, `StreamLinePrinter` (markdown streaming), approval dengan **diff preview + bell + scoped `s`**, wizard `init`, `mcp wizard/search/doctor/connect --all`, `skills list/show/export/import/search`, `/resume`, `/undo` | — (top layer) |

## Sistem Keamanan (desain paling khas)

1. **Permission gate fail-closed** (`security/permissions.py`): risk dideklarasikan tool sendiri, dievaluasi **per call** (`risk_for(arguments)` — mis. `file_ops read`=safe, `delete`=dangerous; `run_tests` custom command=dangerous). Mode: `allow-all`/`ask`/`read-only`. Unattended (scheduler) + dangerous = **ditolak**, bukan disunyikan. Persetujuan interaktif menawarkan: `y` sekali, `a` selalu (per tool), `s` **ter-scope** (folder/perintah saja).
2. **Audit log** (`security/audit.py`): JSONL append-only, redact nilai kredensial (apikey/token/secret/authorization…), best-effort (disk penuh tidak mematikan agent).
3. **Subagent** (`tools/subagent.py`): child dapat tool whitelist dari parent, share PermissionGate yang sama (bukan bypass), step budget = `max_iterations` child (struktural), depth cap 2, timeout wall-clock, toolset hanya mengecil turun generasi. `spawn_subagent` tidak pernah diberikan by name.
4. **Registry = satu pintu**: tidak ada jalur eksekusi tool yang melewati gate; `SystemExit` pun ditangkap.
5. **MCP/plugins tidak jadi pintu samping**: semua lewat registry + gate; MCP tool default CAUTION, eskalasi otomatis via deskripsi.
6. **Scope guard cyber terpisah dari permission gate**: gate menjawab "bolehkah manusia menyetujui aksi ini", scope guard menjawab "bolehkah host ini dihubungi sama sekali". `Prober` memanggil `ensure_allowed` **sebelum** setiap request dan setiap hop redirect; loopback/privat/link-local/metadata ditolak kecuali di-opt-in; budget global + per-host, rate limit, konkurensi, dan deadline ditegakkan di titik yang sama. Model tidak bisa memperlebar scope: rencana hanya memilih dari `CAPABILITIES`, dan `ToolGrant` per jalur membatasi check yang boleh jalan.

## Dependency Graph

```
cli → core → {llm, tools, security, memory, rag, skills, planning,
              plugins, mcp, scheduler, observability, personas, project, cyber}
cyber/agent → cyber/{scope, prober, recon, swarm, store, checks, playbooks}
cyber/tools → cyber/{scanner, sast, cve, report} (import di dalam method, bukan top-level)
cyber/playbooks → skills (to_skill) — hanya untuk instalasi eksplisit ke pustaka skill
tools/subagent → core/loop (child AgenticLoop) + observability
tools/registry → security (TYPE_CHECKING only — hindari import runtime)
plugins → tools/base; mcp → plugins + tools
scheduler → core (agent_factory membuat agent interaktif=false)
```
Arah bersih (top-down), tidak ada siklus runtime yang bermasalah. Shims back-compat: `aegisx_agent/config.py` dan `aegisx_agent/agent_loop.py` (di-omit dari coverage).

## Mode Cyber (mesin asesmen)

| Lapisan | File | Peran |
|---|---|---|
| Batas | `cyber/scope.py` | `ScopePolicy` (allowlist host/path/port, budget, deadline, private opt-in) + `ScopeGuard.check/ensure_allowed/throttle/record_request` |
| Jaringan | `cyber/prober.py` | `Prober.request`: cek scope → throttle → httpx → rekam `Response` (status, header multi-value, body di-cap 512 KB, redirect manual yang tetap dicek scope) |
| Recon | `cyber/recon.py` | `Crawler` (same-origin, bounded; **mengantre URL absolut**, bukan href relatif), `fingerprint`, `PortScanner`, `SubdomainEnumerator` |
| Check | `cyber/checks/*.py` | Satu pertanyaan sempit per check; `CheckContext.fetch` satu-satunya akses; `run_checks` menahan check yang crash |
| Orkestrasi | `cyber/scanner.py` | recon → cheap checks → deep checks (hanya bila ada parameter) → `FindingStore` |
| Paralel | `cyber/swarm.py` | `LineOfAttack` + `ToolGrant` (subset kapabilitas), store bersama, koroborasi menaikkan confidence |
| Otonom | `cyber/agent.py` | survey → plan (katalog kapabilitas) → execute → **escalate** (gelombang-2 dari temuan, boleh menolak) → verify (alasan wajib, retensi minimum, severity terlindungi) → report |
| Pengetahuan | `cyber/playbooks.py`, `cyber/cve.py`, `cyber/sast.py` | Prosedur bawaan (dikirim lewat system prompt), advisory OSV + fallback offline, aturan secret/sink/misconfig + parser manifest |
| Keluaran | `cyber/report.py` | SARIF 2.1.0, Markdown, HTML mandiri; `ReportOptions` menyaring severity |

Invarian yang dijaga test: temuan punya fingerprint stabil (kategori+target+endpoint+judul), severity tidak pernah turun saat merge, penolakan scope selalu menyebut alasannya, dan laporan tidak bisa dikosongkan oleh review model.

**Dua pintu, dua kontrak.** `aegisx cyber …` dan `/cyber …` menggerakkan mesin secara langsung: selalu tersedia, dan jalur deterministiknya (`scan`, `sast`, `audit`, `swarm`, `report`) tidak butuh API key. Yang opt-in adalah mode cyber *percakapan* — `AEGISX_CYBER_ENABLED=true` mendaftarkan tool `cyber_*` di registry dan menyuntik playbook ke system prompt. Matikan secara bawaan, karena prompt itu menambah ±2.000 token doktrin ke **setiap** turn dan tool cyber ke setiap daftar tool; sesi biasa tidak seharusnya membayar biaya itu.

## Test Suite (64 file, gate: coverage ≥77%, mypy strict)

- `tests/fake_llm.py` — HTTP server in-process yang bicara wire format OpenAI/Anthropic asli (SSE, 429 retry) → e2e tanpa API key.
- `tests/support.py`, `conftest.py` — helper & sys.path shim ke root repo (paket flat di root, tanpa editable install).
- `tests/mcp_compat.py` — probe kompatibilitas SDK MCP: test yang spawn `mcp_demo_server.py` (API 2.x) di-skip dengan alasan jelas bila yang ter-install bukan mcp 2.x, alih-alih gagal dengan `McpError: Connection closed` yang menyesatkan. Jalankan suite lewat `.venv/bin/python -m pytest` (venv dari `uv venv --python 3.11 .venv && uv pip install -e ".[dev,mcp]"`).
- Cakupan: loop (tool-call id matching, failure containment, parallel, budget guard), gate + fail-closed + scoped allow + audit redaction, scheduler/cron/daemon, skill library + export/import, konversi wire-format provider + pesan error server, MCP client/manager/bridge/wizard, subagent unit+e2e, observability, semua CLI command, UX batch (diff preview, HUD, /resume, /undo, printer markdown).
- `tests/test_cyber_core.py` (model/scope/store/prober/playbook), `test_cyber_checks.py` (setiap check vs target berskrip), `test_cyber_engine.py` (recon, swarm, agen 2 gelombang, scanner), `test_cyber_analysis.py` (SAST, advisory, tool, reporter), `test_cyber_cli.py` (perintah `cyber` + `/cyber`) — total 154 test.

## Konvensi

- Docstring "mengapa bukan apa"; config via env `AEGISX_*` (pydantic-settings, `.env`).
- Tools: subclass `Tool`, schema JSON OpenAI-style; risk eksplisit; registry satu pintu.
- Persistensi di `~/.aegisx/`: config.json, audit.log, usage.jsonl, daemon.db, scheduler/, skills/, sessions/, vectorstore/, mcp_servers.json.
- Data sementara: `aegisx` file launcher di root; `aegisx/` (dir) berisi launcher serupa; `.githooks/` (commit filter, secret guard) diaktifkan via `git config core.hooksPath .githooks`.

## Catatan / Mismatch & Pertanyaan Terbuka

1. ~~conftest.py menambah `src/` ke sys.path~~ — **DIPERBAIKI**: shim menunjuk root repo.
2. ~~SDK `openai`/`anthropic` di deps tak terpakai~~ — **DIHAPUS** (commit 927f3a5).
3. ~~launcher `cd $ROOT` merusak deteksi project~~ — **DIPERBAIKI** (cae4cc9): `PYTHONPATH` saja.
4. ~~error LLM cuma "400 Bad Request"~~ — **DIPERBAIKI** (1aa345c): pesan body server ikut naik.
5. `AGENTS.md` di root adalah prompt "Freebuff Superpower Ultra" — **bukan** bagian dari runtime AegisX; hanya instruksi workspace saat agent berjalan di repo ini.
6. ~~Blok komentar duplikat "SLASH COMMANDS" di `cli/interactive.py`~~ — **DIHAPUS** (Batch A).
7. ~~FTS index sesi tidak ikut ter-trim saat `/undo`/`trim_session`~~ — **DIPERBAIKI** (Batch A): `SessionStore.trim_session` kini juga menghapus baris FTS; bug lama yang menyimpan pesan *terbaru* (bukan *tertua*) juga diluruskan ke `ORDER BY id ASC`.
8. ~~Crawler mengantre `href` relatif~~ — **DIPERBAIKI**: `_enqueue` dulu mengembalikan `bool` dan pemanggil mengantre link apa adanya, sehingga `Prober` menerima URL tanpa skema dan crawl berhenti setelah halaman pertama. Kini `_enqueue` mengembalikan URL absolut yang sudah dinormalisasi.
9. ~~`FingerprintRule.version_group` diketik `str` tapi dipakai sebagai nama grup regex~~ — **DIPERBAIKI**: `match.group("1")` selalu `IndexError` yang ditelan `except`, jadi semua versi teknologi kosong dan `match_technology_advisories` tidak pernah cocok. Field kini `int`.
10. ~~Playbook dipasang ke pustaka skill pengguna saat startup~~ — **DIPERBAIKI**: itu mencampur doktrin dengan skill yang dipelajari dan mengotori `~/.aegisx/skills`. Playbook kini dikirim lewat system prompt sesi cyber; `install_playbooks()` hanya dipakai bila operator memintanya.
11. ~~Default persona `pentester` dan `cyber_enabled=true`~~ — **DIPERBAIKI**: keduanya kembali netral (`default` / `false`). Alasannya: `cyber_enabled=true` menyuntik playbook (±2.077 token cl100k) ke system prompt *setiap* turn, dan persona `pentester` mewarnai sesi biasa yang mungkin cuma bertanya soal Python dengan doktrin offensif. Tidak ada fungsi yang hilang: `cli/commands/cyber.py` membangun `ScanConfig`/`CyberAgentConfig` sendiri dan tidak pernah membaca `cyber_enabled`, jadi seluruh perintah cyber tetap utuh. Persona `pentester` tetap ada — tinggal diminta (`aegisx --persona pentester`).

## Batch A — Polishing (fitur kecil, langsung terasa)

| Fitur | Lokasi |
|---|---|
| `/resume` tanpa argumen menampilkan tabel preview (jumlah pesan, terakhir aktif, pesan pembuka) | `memory/advanced.py: SessionStore.get_session_previews`, `cli/interactive.py` case `/resume` |
| `aegisx mcp search <term> --add` → wizard langsung ke template katalog | `cli/commands/mcp.py: _mcp_wizard(agent, preset=…)`, `cli/main.py: mcp_search(--add)` |
| Filter tools per risk: `aegisx tools --risk dangerous`, `/tools dangerous` | `cli/commands/permissions.py: _print_tools_table(risk_filter=…)` |
| FTS trim saat `/undo` + `get_session_previews` | `memory/advanced.py` |

## Batch B — Ekosistem (berbagi skill + MCP massal)

| Fitur | Lokasi |
|---|---|
| Impor skill dari **file, link gist, atau URL mentah** — halaman gist & blob GitHub ditulis ulang ke URL raw | `skills/manager.py: normalize_skill_url, import_skill_text, import_skill_url(fetch=…)`, `cli/commands/skills.py: _fetch_text` |
| Grup CLI `aegisx skills list\|show\|export\|import\|search` (URL lewat `search` = impor) | `cli/main.py: skills_app` |
| `/skills list\|show\|export\|import\|search` + `help` memakai handler yang sama dengan CLI | `cli/commands/skills.py: _handle_skills_command` |
| `aegisx mcp connect --all` / `/mcp connect --all` — dial semua server sekaligus, **satu gagal tidak menghentikan sisanya**, ringkasan per server + hint `mcp doctor` | `mcp/manager.py: connect_all`, `cli/main.py: _connect_every_server`, `cli/commands/mcp.py: _connect_all` |
