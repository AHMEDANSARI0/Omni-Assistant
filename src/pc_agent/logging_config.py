"""Small logging setup plus secret redaction for anything user-facing."""

from __future__ import annotations

import logging
import re
import sys
from typing import TextIO

_SECRET_PATTERNS = (
    re.compile(r"(api[_-]?key\s*[=:]\s*)\S+", re.IGNORECASE),
    re.compile(r"(bearer\s+)\S+", re.IGNORECASE),
    re.compile(r"((?:GEMINI|OPENROUTER|OPENAI)_API_KEY\s*[=:]\s*)\S+", re.IGNORECASE),
)


def redact_text(text: str) -> str:
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(r"\1<redacted>", result)
    return result


def configure_logging(level: str = "INFO", *, stream: TextIO | None = None) -> logging.Logger:
    logger = logging.getLogger("pc_agent")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not any(getattr(handler, "_pc_agent_handler", False) for handler in logger.handlers):
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler._pc_agent_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
        logger.addHandler(handler)
    logger.propagate = False
    return logger
