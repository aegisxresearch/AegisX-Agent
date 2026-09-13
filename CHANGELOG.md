# Changelog

All notable changes to AegisX-Agent are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`aegisx plan` crashed on every invocation.** The planning prompt template
  contains literal JSON braces, and `str.format` raised `KeyError` on them
  before any LLM call was made. Placeholders are now substituted by exact
  string replacement.
- **Plan parsing degraded safely.** A malformed LLM response (a `steps` value
  that is not a list, or non-dict step entries) raised `AttributeError`/
  `TypeError` instead of falling back to a single reasoning step. All
  malformed shapes now degrade gracefully.
- **Scheduler: weekly tasks no longer hot-loop the daemon.** If a weekly task's
  scheduled time had already passed on its target day, `calculate_next_run()`
  returned that past time as the next run. The scheduler saw the task as due on
  every poll, retried it in a tight loop, and each failure compounded the
  problem. The next run is now pushed a full week forward. Locked in by a
  regression test asserting the recomputed slot lands 6–8 days out on the same
  weekday.

### Added

- **Coverage gate in CI.** `pytest --cov` enforces a `fail_under` threshold from
  `pyproject.toml` (currently 61%), so coverage cannot silently regress.
- **Dependabot + CodeQL.** Weekly dependency updates (pip, GitHub Actions) and
  CodeQL's `security-extended` analysis on every push/PR plus a weekly scan.
- **`aegisx ingest` / `aegisx search`.** The CLI surface for the RAG engine the
  README already documented — file/directory ingestion and scored search,
  including clean errors when ChromaDB is not installed.
- **`/permissions` documentation.** The README security section now shows the
  real output of the permission commands (verified against the live CLI, not
  hand-written), including the gated tool list and audit-log location.
- **Versioned git hooks.** Secret-scanning pre-push guard moved to `.githooks/`
  with `core.hooksPath`; it now also works on first pushes from a fresh clone
  and catches dash-prefixed patterns (PEM keys) that grep used to swallow.

### Changed

- **One LLM request per streamed turn.** `chat_stream` no longer runs a
  non-streaming probe before streaming: tool calls are parsed from the stream
  itself. A tool-less turn drops from 2 requests to 1; a tool turn from 3 to 2.
- **Lint-clean CI.** The ruff gate runs strict on `src` and `tests`.

## [0.1.0]

Initial release: streaming chat with tool calling, permission gate with audit
log, planning (ReAct), RAG (ChromaDB-backed, optional), skills, scheduler,
code workspace tools, web search/scraper, and a CLI with `chat`, `plan`, `run`,
`schedule`, `ingest`, and `search` commands.
