"""FastMCP server exposing legacy-reverse-mcp tools."""

from __future__ import annotations

from fastmcp import FastMCP

mcp = FastMCP("legacy-reverse-mcp")


@mcp.tool()
def scan_repository(repo_path: str, force: bool = False) -> dict:
    raise NotImplementedError


@mcp.tool()
def get_project_overview() -> dict:
    raise NotImplementedError


@mcp.tool()
def find_code_areas(query: str) -> dict:
    raise NotImplementedError


@mcp.tool()
def get_module_map() -> dict:
    raise NotImplementedError


@mcp.tool()
def list_endpoints() -> dict:
    raise NotImplementedError


@mcp.tool()
def trace_endpoint(endpoint_id: int) -> dict:
    raise NotImplementedError


@mcp.tool()
def explain_class(fqn: str) -> dict:
    raise NotImplementedError


@mcp.tool()
def get_change_impact(symbol: str) -> dict:
    raise NotImplementedError


@mcp.tool()
def generate_context_pack(task: str, max_tokens: int = 4000) -> dict:
    raise NotImplementedError


if __name__ == "__main__":
    mcp.run()
