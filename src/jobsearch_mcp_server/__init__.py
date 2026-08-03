"""智职引擎 package.

Heavy optional integrations are imported lazily so the local web application
can start even when MCP, Selenium, Qdrant, or the OpenAI SDK are not installed.
"""

__version__ = "1.3.0"


def main() -> None:
    """Start the MCP server (backwards-compatible package entry point)."""
    from .server import main as server_main

    server_main()


__all__ = ["__version__", "main"]
