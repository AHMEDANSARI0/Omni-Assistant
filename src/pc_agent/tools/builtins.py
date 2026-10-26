"""Assembly point for the approved built-in tool set."""

from __future__ import annotations

from .pdf_tools import register_pdf_tools
from .registry import ToolRegistry
from .workspace_tools import register_workspace_tools


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_workspace_tools(registry)
    register_pdf_tools(registry)
    return registry
