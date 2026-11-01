"""Connect to Titan's MCP tool server and hand its tools to LangGraph."""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

SRC_DIR = Path(__file__).resolve().parent.parent

CONNECTIONS = {
    "titan": {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-m", "tools.server"],
        "cwd": str(SRC_DIR),
    }
}


@asynccontextmanager
async def titan_tools():
    """Yield LangChain tools backed by one long-lived MCP server session."""
    client = MultiServerMCPClient(CONNECTIONS)
    async with client.session("titan") as session:
        yield await load_mcp_tools(session)
