"""Local desktop GUI for the Self-Healing PC Agent.

The package intentionally does not import the Tkinter application at package
import time.  Headless code and tests can import :mod:`pc_agent.gui.controller`
without requiring a display; only :mod:`pc_agent.gui.app` opens a window.
"""

__all__: list[str] = []
