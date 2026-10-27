"""Reviewed offline starting templates for common missing capabilities."""

from __future__ import annotations

from typing import Any

_WORD_COUNT = {
    "tool_id": "custom.text_word_count",
    "name": "Text word count",
    "version": "1.0.0",
    "description": "Count words, characters and non-space characters in text.",
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string", "maxLength": 100000}},
        "required": ["text"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "words": {"type": "integer", "minimum": 0},
            "characters": {"type": "integer", "minimum": 0},
            "characters_no_spaces": {"type": "integer", "minimum": 0},
        },
        "required": ["words", "characters", "characters_no_spaces"],
    },
    "source_code": '''import re


def run(arguments):
    text = arguments["text"]
    words = re.findall(r"\\S+", text)
    return {
        "words": len(words),
        "characters": len(text),
        "characters_no_spaces": len(re.sub(r"\\s+", "", text)),
    }
''',
    "test_cases": [
        {
            "name": "two words",
            "arguments": {"text": "hello world"},
            "expected_output": {
                "words": 2,
                "characters": 11,
                "characters_no_spaces": 10,
            },
        },
        {
            "name": "empty text",
            "arguments": {"text": ""},
            "expected_output": {
                "words": 0,
                "characters": 0,
                "characters_no_spaces": 0,
            },
        },
    ],
    "dependencies": ["re"],
    "timeout_seconds": 5,
    "permissions": [],
    "network_required": False,
}

_SLUGIFY = {
    "tool_id": "custom.text_slugify",
    "name": "Text slugify",
    "version": "1.0.0",
    "description": "Convert text to a lowercase hyphen-separated slug.",
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string", "maxLength": 10000}},
        "required": ["text"],
    },
    "output_schema": {
        "type": "object",
        "properties": {"slug": {"type": "string"}},
        "required": ["slug"],
    },
    "source_code": '''import re
import unicodedata


def run(arguments):
    normalized = unicodedata.normalize("NFKD", arguments["text"])
    ascii_text = "".join(
        character for character in normalized if not unicodedata.combining(character)
    ).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return {"slug": slug}
''',
    "test_cases": [
        {
            "name": "sentence",
            "arguments": {"text": "Hello, World!"},
            "expected_output": {"slug": "hello-world"},
        },
        {
            "name": "accented text",
            "arguments": {"text": "Café Menu"},
            "expected_output": {"slug": "cafe-menu"},
        },
    ],
    "dependencies": ["re", "unicodedata"],
    "timeout_seconds": 5,
    "permissions": [],
    "network_required": False,
}

_PERCENTAGE = {
    "tool_id": "custom.math_percentage",
    "name": "Percentage calculator",
    "version": "1.0.0",
    "description": "Calculate part divided by total as a percentage.",
    "input_schema": {
        "type": "object",
        "properties": {
            "part": {"type": "number"},
            "total": {"type": "number"},
            "precision": {"type": "integer", "minimum": 0, "maximum": 10},
        },
        "required": ["part", "total"],
    },
    "output_schema": {
        "type": "object",
        "properties": {"percentage": {"type": "number"}},
        "required": ["percentage"],
    },
    "source_code": '''def run(arguments):
    total = arguments["total"]
    if total == 0:
        raise ValueError("total must not be zero")
    precision = arguments.get("precision", 2)
    return {"percentage": round((arguments["part"] / total) * 100, precision)}
''',
    "test_cases": [
        {
            "name": "one quarter",
            "arguments": {"part": 25, "total": 100},
            "expected_output": {"percentage": 25.0},
        },
        {
            "name": "one third rounded",
            "arguments": {"part": 1, "total": 3, "precision": 2},
            "expected_output": {"percentage": 33.33},
        },
    ],
    "dependencies": [],
    "timeout_seconds": 5,
    "permissions": [],
    "network_required": False,
}

_TEMPERATURE = {
    "tool_id": "custom.unit_temperature_convert",
    "name": "Temperature converter",
    "version": "1.0.0",
    "description": "Convert a temperature between Celsius and Fahrenheit.",
    "input_schema": {
        "type": "object",
        "properties": {
            "value": {"type": "number"},
            "from_unit": {"type": "string", "maxLength": 1},
            "to_unit": {"type": "string", "maxLength": 1},
        },
        "required": ["value", "from_unit", "to_unit"],
    },
    "output_schema": {
        "type": "object",
        "properties": {"value": {"type": "number"}},
        "required": ["value"],
    },
    "source_code": '''def run(arguments):
    value = arguments["value"]
    source = arguments["from_unit"].upper()
    target = arguments["to_unit"].upper()
    if source not in ("C", "F") or target not in ("C", "F"):
        raise ValueError("Units must be C or F")
    if source == target:
        return {"value": value}
    if source == "C":
        return {"value": round((value * 9 / 5) + 32, 4)}
    return {"value": round((value - 32) * 5 / 9, 4)}
''',
    "test_cases": [
        {
            "name": "freezing Celsius",
            "arguments": {"value": 0, "from_unit": "C", "to_unit": "F"},
            "expected_output": {"value": 32.0},
        },
        {
            "name": "boiling Fahrenheit",
            "arguments": {"value": 212, "from_unit": "F", "to_unit": "C"},
            "expected_output": {"value": 100.0},
        },
    ],
    "dependencies": [],
    "timeout_seconds": 5,
    "permissions": [],
    "network_required": False,
}

TEMPLATES: dict[str, dict[str, Any]] = {
    "word_count": _WORD_COUNT,
    "slugify": _SLUGIFY,
    "percentage": _PERCENTAGE,
    "temperature": _TEMPERATURE,
}

_SIGNALS = (
    ("word_count", ("word count", "count words", "words count", "character count")),
    ("slugify", ("slug", "slugify", "url safe text")),
    ("percentage", ("percentage", "percent", "%")),
    (
        "temperature",
        ("temperature", "celsius", "fahrenheit", "centigrade"),
    ),
)


def suggest_template(requirement: str) -> str | None:
    lowered = requirement.lower()
    for template_id, signals in _SIGNALS:
        if any(signal in lowered for signal in signals):
            return template_id
    return None


def get_template(template_id: str) -> dict[str, Any]:
    import copy

    try:
        return copy.deepcopy(TEMPLATES[template_id])
    except KeyError as exc:
        available = ", ".join(sorted(TEMPLATES))
        raise ValueError(f"Unknown tool template '{template_id}'. Available: {available}") from exc
