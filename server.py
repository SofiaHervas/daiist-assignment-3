"""
Assignment 3: MCP server. Your editor and agent.py start it; by hand: `uv run server.py`.

- search(query): returns chunks with source and location.
- add_document(path): runs the file through index.py's pipeline, embeds it
  and stores it.

Load index/ at startup; never re-embed the corpus. Print nothing to stdout
(it carries the protocol); log to stderr. Docstrings become the tool
descriptions the model reads.
"""

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("corpus", log_level="WARNING")


@mcp.tool()
def search(query: str) -> str:
    """TODO: what the corpus is, when to use this tool, what it returns."""
    raise NotImplementedError


@mcp.tool()
def add_document(path: str) -> str:
    """TODO: what this tool adds to the index and what it returns."""
    raise NotImplementedError


if __name__ == "__main__":
    mcp.run()  # stdio
