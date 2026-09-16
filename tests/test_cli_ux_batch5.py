"""Batch-5: prompt_toolkit input fallback and streaming markdown blocks."""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

from aegisx_agent.cli import interactive as inter


class _FakeTTY:
    """A stdout stand-in that claims to be a terminal."""

    def __init__(self) -> None:
        self.buffer = io.StringIO()

    def write(self, text: str) -> None:
        self.buffer.write(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


def _printer_with_tty(monkeypatch: Any) -> tuple[inter.StreamLinePrinter, _FakeTTY]:
    fake = _FakeTTY()
    monkeypatch.setattr(inter.sys, "stdout", fake)
    return inter.StreamLinePrinter(), fake


def test_plain_prose_streams_raw(monkeypatch: Any) -> None:
    printer, fake = _printer_with_tty(monkeypatch)

    printer.write("hello ")
    printer.write("world\n")

    # Prose streams raw; a trailing partial line is held back until a
    # newline arrives (so a split ``` can never leak), and finish() flushes it.
    assert fake.buffer.getvalue() == "🤖 AegisX: hello world\n"
    printer.write("tail without newline")
    printer.finish()
    assert fake.buffer.getvalue().endswith("tail without newline\n")


def test_code_fence_renders_and_continues(monkeypatch: Any) -> None:
    printer, fake = _printer_with_tty(monkeypatch)
    printed: list[str] = []

    def fake_print(block: str) -> None:
        printed.append(block)

    printer._flush_markdown = fake_print  # type: ignore[method-assign]

    printer.write("before\n```python\nprint(1)\n```\nafter")
    printer.finish()

    assert any("```python" in block or "print(1)" in block for block in printed)
    assert "after" in fake.buffer.getvalue()
    # No fence remains in the raw stream.
    assert "```" not in fake.buffer.getvalue()


def test_unterminated_fence_flushed_on_finish(monkeypatch: Any) -> None:
    printer, fake = _printer_with_tty(monkeypatch)
    printed: list[str] = []

    printer._flush_markdown = printed.append  # type: ignore[method-assign]

    printer.write("answer\n```js\nconsole.log(2)")
    printer.finish()

    assert any("console.log(2)" in block for block in printed)
    assert printer._buffer == ""


def test_status_lines_bypass_markdown_buffer(monkeypatch: Any) -> None:
    printer, fake = _printer_with_tty(monkeypatch)

    printer.write("thinking\n")
    printer.write("\n✎ editor → file.py\n")

    out = fake.buffer.getvalue()
    assert "editor" in out
    assert "\x1b[2m" in out  # dimmed on a TTY


def test_input_function_returns_none_without_tty(monkeypatch: Any) -> None:
    fake_stdin = SimpleNamespace(isatty=lambda: False)
    monkeypatch.setattr(inter.sys, "stdin", fake_stdin)

    assert inter._input_function() is None
