"""Backwards-compatible launcher for the unified web application."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> None:
    from jobsearch_mcp_server.webapp import main as web_main

    web_main()


if __name__ == "__main__":
    main()
