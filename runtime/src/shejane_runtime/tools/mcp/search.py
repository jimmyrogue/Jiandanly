"""Compact provider-independent search over discovered MCP tools."""

from __future__ import annotations

import re
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import Any

from langchain_core.tools import BaseTool
from langchain_core.tools import tool as langchain_tool

MCP_TOOL_SEARCH_NAME = "mcp.search_tools"
MCP_TOOL_SEARCH_RESULT_KIND = "mcp_tool_search_results"
MCP_TOOL_SEARCH_THRESHOLD = 12
MCP_TOOL_SEARCH_DESCRIPTION_CHARS = 512
MCP_TOOL_SEARCH_QUERY_CHARS = 512


def make_mcp_tool_search(tools: Sequence[BaseTool]) -> BaseTool:
    """Expose a compact, provider-independent MCP tool directory."""
    directory = tuple(
        {
            "name": item.name,
            "description": (item.description or "").strip()[:MCP_TOOL_SEARCH_DESCRIPTION_CHARS],
        }
        for item in tools
    )

    @langchain_tool(MCP_TOOL_SEARCH_NAME)
    def search_tools(query: str, limit: int = 5) -> dict[str, Any]:
        """Search available MCP integrations by capability before using one."""
        bounded_query = query.strip()[:MCP_TOOL_SEARCH_QUERY_CHARS]
        normalized_query = bounded_query.lower()
        bounded_limit = max(1, min(int(limit), 8))
        ranked = sorted(
            directory,
            key=lambda item: (
                _mcp_tool_search_score(normalized_query, item),
                item["name"],
            ),
            reverse=True,
        )
        return {
            "kind": MCP_TOOL_SEARCH_RESULT_KIND,
            "query": bounded_query,
            "tools": list(ranked[:bounded_limit]),
        }

    return search_tools


def _mcp_tool_search_score(query: str, item: dict[str, str]) -> float:
    if not query:
        return 0
    name = item["name"].lower()
    description = item["description"].lower()
    corpus = f"{name} {description}"
    query_tokens = set(re.findall(r"[\w.-]+", query))
    corpus_tokens = set(re.findall(r"[\w.-]+", corpus))
    score = 8.0 if query in name else 3.0 if query in description else 0.0
    score += 2.0 * len(query_tokens & corpus_tokens)
    score += SequenceMatcher(None, query, name).ratio()
    return score
