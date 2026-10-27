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
    resolved_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(resolved_level)
    # Corrupt PDFs are an expected, reportable input in folder merges. pypdf's
    # WARNING lines duplicate our own skipped-file reason and make a successful
    # run look broken, so keep them out of normal CLI output. DEBUG still shows
    # the parser's detailed diagnostics when a developer asks for them.
    logging.getLogger("pypdf").setLevel(
        logging.DEBUG if resolved_level == logging.DEBUG else logging.ERROR
    )
    if not any(getattr(handler, "_pc_agent_handler", False) for handler in logger.handlers):
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler._pc_agent_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
        logger.addHandler(handler)
    logger.propagate = False
    return logger
