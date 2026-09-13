"""Allow ``python -m aegisx_agent.cli`` to launch the Typer application."""

from aegisx_agent.cli.main import app

if __name__ == "__main__":
    app()
