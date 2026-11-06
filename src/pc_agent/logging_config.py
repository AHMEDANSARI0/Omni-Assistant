"""Small logging setup plus secret redaction for anything user-facing."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(api[_-]?key\s*[=:]\s*)\S+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"(bearer\s+)\S+", re.IGNORECASE), r"\1<redacted>"),
    (
        re.compile(
            r"((?:GEMINI|OPENROUTER|OPENAI)_API_KEY\s*[=:]\s*)\S+", re.IGNORECASE
        ),
        r"\1<redacted>",
    ),
    # Bare provider keys that reach a log line or a stored error without an
    # ``api_key=`` label. Kept deliberately narrow so ordinary words such as
    # "task-" or "risk-" are never redacted.
    (
        re.compile(
            r"\b(?:sk|pk|rk)[-_](?:live|test|proj|ant|or)?[-_]?[A-Za-z0-9]{12,}",
            re.IGNORECASE,
        ),
        "<redacted>",
    ),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "<redacted>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "<redacted>"),
    (re.compile(r"\bxai-[A-Za-z0-9]{16,}\b"), "<redacted>"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{16,}\b"), "<redacted>"),
)


def redact_text(text: str) -> str:
    result = text
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


class JsonFormatter(logging.Formatter):
    """One JSON object per log record (structured logging, Phase 3).

    Messages pass through :func:`redact_text` so structured logs never
    carry API keys or bearer tokens. ``extra={...}`` fields with the
    ``ctx_`` prefix are included verbatim as structured context.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key.startswith("ctx_"):
                entry[key[4:]] = value if isinstance(value, (str, int, float, bool)) else str(value)
        if record.exc_info:
            entry["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=True, sort_keys=True)


_TEXT_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def configure_logging(
    level: str = "INFO",
    *,
    stream: TextIO | None = None,
    log_format: str = "text",
    log_file: Path | None = None,
) -> logging.Logger:
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
    use_json = log_format.strip().lower() == "json"

    def _formatter() -> logging.Formatter:
        return JsonFormatter() if use_json else logging.Formatter(_TEXT_FORMAT)

    stream_handlers = [
        handler
        for handler in logger.handlers
        if getattr(handler, "_pc_agent_handler", False)
        and not getattr(handler, "_pc_agent_file_handler", False)
    ]
    if not stream_handlers:
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler._pc_agent_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(_formatter())
        logger.addHandler(handler)
    else:
        for existing in stream_handlers:
            existing.setFormatter(_formatter())
    if log_file is not None:
        target = str(Path(log_file).expanduser())
        if not any(
            getattr(handler, "_pc_agent_file_handler", False)
            and getattr(handler, "_pc_agent_log_file", "") == target
            for handler in logger.handlers
        ):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                target, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler._pc_agent_handler = True  # type: ignore[attr-defined]
            file_handler._pc_agent_file_handler = True  # type: ignore[attr-defined]
            file_handler._pc_agent_log_file = target  # type: ignore[attr-defined]
            file_handler.setFormatter(JsonFormatter() if use_json else logging.Formatter(_TEXT_FORMAT))
            logger.addHandler(file_handler)
    logger.propagate = False
    return logger
