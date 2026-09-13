# AegisX Agent CLI Architecture

The CLI package lives at `aegisx_agent/cli/`. It is deliberately split so each
module holds one concern; the Typer application object is shared, not duplicated.

## Layout

```
aegisx_agent/cli/
├── __init__.py        # Re-exports the public surface (app + patched symbols)
├── __main__.py        # python -m aegisx_agent.cli entry
├── app.py             # Shared Typer app, Rich console, theme (single source)
├── main.py            # Config plumbing (_get_config/_get_agent), typer entry points,
│                      #   re-exports for tests; defines _agent + CONFIG_FILE
├── interactive.py     # AnimatedProgress, COMMANDS menu, _handle_slash_command,
│                      #   _run_chat, _chat_with_animation, workspace banner
└── commands/
    ├── permissions.py # /permissions handler + tools table
    ├── code.py        # /code, /git, /test handlers
    └── schedule.py    # /schedule handler, flag parsing, schedule rendering
```

## Rules

1. **One shared app object.** `app`, `console`, and `THEME` are created in
   `app.py`. Every other module imports them from there — never construct a
   second Typer app or console.
2. **Entry points live in `main.py`.** Only `main.py` registers `@app.command`
   handlers (plus the `schedule` sub-app). This keeps registration in one
   place; command *logic* lives in `commands/` and `interactive.py`.
3. **Patched symbols live in `main.py`.** `_agent` and `CONFIG_FILE` are module
   globals of `main.py` because the test suite monkeypatches them.
4. **Back-compat re-exports.** `main.py` re-exports handler names that tests
   access as `cli._handle_slash_command`, `cli._split_flags`, etc. Marked with
   `# noqa: F401 — re-exported for tests`; do not let autofix remove them.
5. **Slash handler contract.** A handler takes `(args: str, agent)` and prints
   via the shared console. It never constructs an agent itself.

## Data flow of a chat turn

```
stdin → main.chat() → _read_piped_prompt() → _run_chat()/_chat_with_animation()
      → AegisXAgent.chat()/chat_stream() → AgenticLoop.run_streaming()
      → tool calls via agent.tools.execute() → permission gate on dangerous tools
```

A `/command` typed in the chat loop is dispatched by
`interactive._handle_slash_command` and never reaches the LLM.
