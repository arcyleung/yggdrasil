"""Yggdrasil MCP server entrypoint (FastMCP stdio)."""
from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from yggdrasil.mcp.app_context import AppContext
from yggdrasil.mcp.tools import register_tools

logger = logging.getLogger(__name__)


def build_server(ctx: AppContext | None = None) -> FastMCP:
    context = ctx or AppContext.from_config()
    mcp = FastMCP("yggdrasil")
    register_tools(mcp, context)
    return mcp


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    mcp = build_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
