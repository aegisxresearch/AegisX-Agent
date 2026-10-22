"""Allow ``python -m aegisx_agent.cli`` to launch the Typer application."""

from aegisx_agent.cli.main import app

if __name__ == "__main__":
    # Click derives the program name from argv, which for ``-m`` is the module
    # path. The wrapper the installer writes launches this module, so without
    # this every usage and error line would read "python -m aegisx_agent.cli".
    app(prog_name="aegisx")
