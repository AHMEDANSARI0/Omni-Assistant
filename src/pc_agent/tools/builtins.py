"""Assembly point for the approved built-in tool set."""

from __future__ import annotations

from .browser_tools import register_browser_tools
from .command_tools import register_command_tools
from .desktop_automation_tools import register_desktop_automation_tools
from .desktop_tools import register_desktop_tools
from .file_tools import register_file_tools
from .git_tools import register_git_tools
from .image_tools import register_image_tools
from .office_tools import register_office_tools
from .pdf_tools import register_pdf_tools
from .registry import ToolRegistry
from .web_tools import register_web_tools
from .workspace_tools import register_workspace_tools


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_workspace_tools(registry)
    register_file_tools(registry)
    register_pdf_tools(registry)
    register_office_tools(registry)
    register_web_tools(registry)
    register_desktop_tools(registry)
    register_desktop_automation_tools(registry)
    register_git_tools(registry)
    register_command_tools(registry)
    register_browser_tools(registry)
    register_image_tools(registry)
    return registry
