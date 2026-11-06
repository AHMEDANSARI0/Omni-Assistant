"""Local computer vision: deterministic, offline screen understanding (Phase 11).

Everything here runs on the machine with Pillow only - no model download, no
network call, no invented description. What it produces is measurable image
facts: dimensions, coarse brightness grid, dominant colours, an ink/edge
density estimate per cell (a cheap "is there text/UI here" signal) and
bounded template matching.

The richer "describe what you see" path stays on the existing gated provider
vision route (`PC_AGENT_ALLOW_VISION`), which is a separate, approved action.
This module deliberately cannot talk to a model.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ToolError

# Bounds that keep a single analysis cheap and predictable.
MAX_SOURCE_BYTES = 20_000_000
MAX_PIXELS = 40_000_000
MAX_GRID = 12
TEMPLATE_SCALES = (1.0, 0.75, 0.5)
MAX_TEMPLATE_PIXELS = 250_000
MAX_SEARCH_STEPS = 40_000
WORK_WIDTH = 640
TEMPLATE_SAMPLES = 96
MAX_OFFSETS_PER_AXIS = 96
MIN_TEMPLATE_CONTRAST = 8.0
VERIFY_TOLERANCE = 24.0
CANDIDATE_LIMIT = 16
MIN_TEMPLATE_SCORE = 0.90
INK_BUSY_THRESHOLD = 0.10
INK_WORK_CELL_WIDTH = 128
INK_EDGE_CUTOFF = 40


@dataclass(frozen=True, slots=True)
class ImageFacts:
    """Measured properties of one image, never a guess."""

    width: int
    height: int
    mode: str
    format: str
    size_bytes: int
    brightness: float
    contrast: float
    dominant_colors: tuple[tuple[int, int, int], ...]
    grid_rows: int
    grid_columns: int
    grid_brightness: tuple[tuple[float, ...], ...]
    grid_ink: tuple[tuple[float, ...], ...]
    busy_cells: int
    # True when at least one grid cell reached the ink-density threshold,
    # i.e. some part of the image carries dense high-contrast detail (text,
    # icons, table rules). It is an estimate, never OCR.
    text_likely: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "mode": self.mode,
            "format": self.format,
            "size_bytes": self.size_bytes,
            "brightness": round(self.brightness, 2),
            "contrast": round(self.contrast, 2),
            "dominant_colors": [
                {"r": red, "g": green, "b": blue} for red, green, blue in self.dominant_colors
            ],
            "grid": {
                "rows": self.grid_rows,
                "columns": self.grid_columns,
                "brightness": [
                    [round(value, 1) for value in row] for row in self.grid_brightness
                ],
                "ink": [[round(value, 3) for value in row] for row in self.grid_ink],
            },
            "busy_cells": self.busy_cells,
            "text_likely": self.text_likely,
        }


@dataclass(frozen=True, slots=True)
class TemplateMatch:
    x: int
    y: int
    width: int
    height: int
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "score": round(self.score, 4),
        }


def _pixel_values(image: Any) -> list[Any]:
    """Pixel values without the deprecated Pillow accessor.

    Pillow 12 deprecates ``Image.getdata`` in favour of
    ``get_flattened_data``; older Pillow releases only have the former.
    """
    getter = getattr(image, "get_flattened_data", None)
    if getter is not None:
        return list(getter())
    return list(image.getdata())


def _open_image(data: bytes) -> tuple[Any, str]:
    from PIL import Image

    if not data:
        raise ToolError("The image is empty.")
    if len(data) > MAX_SOURCE_BYTES:
        raise ToolError(
            f"The image is larger than {MAX_SOURCE_BYTES // 1_000_000} MB."
        )
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise ToolError(f"The image could not be decoded ({type(exc).__name__}).") from exc
    image_format = str(image.format or "").upper()
    if image.width * image.height > MAX_PIXELS:
        raise ToolError("The image has too many pixels to analyse safely.")
    return image, image_format


def analyse_image(data: bytes, *, grid: int = 4) -> ImageFacts:
    """Measure brightness, contrast, dominant colours and cell ink density."""

    image, image_format = _open_image(data)
    rows = max(1, min(int(grid), MAX_GRID))
    columns = rows
    rgb = image.convert("RGB")
    small = rgb.copy()
    small.thumbnail((256, 256))

    pixels = _pixel_values(small)
    total = len(pixels) or 1
    brightness = sum((r * 299 + g * 587 + b * 114) / 1000 for r, g, b in pixels) / total
    variance = (
        sum(
            (((r * 299 + g * 587 + b * 114) / 1000) - brightness) ** 2
            for r, g, b in pixels
        )
        / total
    )
    contrast = math.sqrt(variance)

    quantized: dict[tuple[int, int, int], int] = {}
    for red, green, blue in pixels:
        key = (red // 32 * 32, green // 32 * 32, blue // 32 * 32)
        quantized[key] = quantized.get(key, 0) + 1
    dominant = tuple(
        colour
        for colour, _count in sorted(
            quantized.items(), key=lambda item: item[1], reverse=True
        )[:5]
    )

    grey = rgb.convert("L")
    cell_w = max(1, grey.width // columns)
    cell_h = max(1, grey.height // rows)
    grid_brightness: list[tuple[float, ...]] = []
    grid_ink: list[tuple[float, ...]] = []
    busy = 0
    for row in range(rows):
        brightness_row: list[float] = []
        ink_row: list[float] = []
        for column in range(columns):
            box = (
                column * cell_w,
                row * cell_h,
                min(grey.width, (column + 1) * cell_w),
                min(grey.height, (row + 1) * cell_h),
            )
            cell = grey.crop(box)
            if cell.width == 0 or cell.height == 0:
                brightness_row.append(0.0)
                ink_row.append(0.0)
                continue
            values = _pixel_values(cell)
            mean = sum(values) / len(values)
            edges = _edge_density(cell)
            brightness_row.append(mean)
            ink_row.append(edges)
            if edges >= INK_BUSY_THRESHOLD:
                busy += 1
        grid_brightness.append(tuple(brightness_row))
        grid_ink.append(tuple(ink_row))

    return ImageFacts(
        width=image.width,
        height=image.height,
        mode=str(image.mode),
        format=image_format,
        size_bytes=len(data),
        brightness=brightness,
        contrast=contrast,
        dominant_colors=dominant,
        grid_rows=rows,
        grid_columns=columns,
        grid_brightness=tuple(grid_brightness),
        grid_ink=tuple(grid_ink),
        busy_cells=busy,
        text_likely=busy > 0,
    )


def _edge_density(cell: Any) -> float:
    """Share of edge pixels in one grid cell.

    A cheap but honest proxy for "ink on a background" (text, icons, table
    rules): the cell is reduced to a fixed working width and filtered with a
    gradient kernel, both in Pillow's C code, so the number is resolution
    stable (the same page reports a similar density at 1366x768 and at
    2560x1440) and cheap. It is not OCR, and callers always report it as an
    estimate.
    """
    from PIL import ImageFilter

    grey = cell if cell.mode == "L" else cell.convert("L")
    if grey.width > INK_WORK_CELL_WIDTH:
        scale = INK_WORK_CELL_WIDTH / grey.width
        grey = grey.resize(
            (INK_WORK_CELL_WIDTH, max(2, int(grey.height * scale))),
            ImageFilter.Resampling.BILINEAR if hasattr(ImageFilter, "Resampling") else 1,
        )
    histogram = grey.filter(ImageFilter.FIND_EDGES).histogram()
    total = sum(histogram) or 1
    return sum(histogram[INK_EDGE_CUTOFF:]) / total


def _match_score(
    screen_pixels: bytes,
    screen_width: int,
    template_pixels: bytes,
    template_width: int,
    origin_x: int,
    origin_y: int,
    tolerance: float,
) -> float:
    """Share of template pixels that match the screen at one offset."""
    total = len(template_pixels)
    if not total:
        return 0.0
    matched = 0
    for index in range(total):
        actual = screen_pixels[
            (origin_y + index // template_width) * screen_width
            + origin_x
            + index % template_width
        ]
        if abs(actual - template_pixels[index]) <= tolerance:
            matched += 1
    return matched / total


def _select_candidates(
    found: list[tuple[float, int, int]],
    *,
    gap: int,
    limit: int,
) -> list[tuple[float, int, int]]:
    """Keep the strongest peaks that are not neighbours of a stronger one."""
    chosen: list[tuple[float, int, int]] = []
    for score, origin_x, origin_y in sorted(found, reverse=True):
        if all(
            abs(origin_x - other_x) > gap or abs(origin_y - other_y) > gap
            for _score, other_x, other_y in chosen
        ):
            chosen.append((score, origin_x, origin_y))
        if len(chosen) >= limit:
            break
    return chosen


def _refine_and_verify(
    *,
    screen_full_pixels: bytes,
    screen_full_width: int,
    screen_full_height: int,
    template_grey: Any,
    factor: int,
    start_x: int,
    start_y: int,
    radius: int,
) -> tuple[float, int, int, int, int]:
    """Nail a candidate down to the pixel and score every pixel of it.

    Reduced-resolution offsets can be a pixel or two away from the true
    position, which matters for repeating content, so the candidate is refined
    on the full-size screenshot before it is scored. Both the refinement and
    the final score use the full-size screenshot; the returned score is the
    share of template pixels that really agree.
    """
    width = max(4, min(template_grey.width, screen_full_width))
    height = max(4, min(template_grey.height, screen_full_height))
    scaled = template_grey.resize((width, height))
    scaled_pixels = scaled.tobytes()

    max_x = max(0, screen_full_width - width)
    max_y = max(0, screen_full_height - height)
    best_x = min(max(0, start_x), max_x)
    best_y = min(max(0, start_y), max_y)

    stride = max(1, (width * height) // TEMPLATE_SAMPLES)
    samples = [
        (index % width, index // width, scaled_pixels[index])
        for index in range(0, width * height, stride)
    ] or [(0, 0, scaled_pixels[0])]
    best_score = -1.0
    for offset_y in range(max(0, best_y - radius), min(max_y, best_y + radius) + 1):
        for offset_x in range(max(0, best_x - radius), min(max_x, best_x + radius) + 1):
            matched = 0
            for sample_x, sample_y, expected in samples:
                actual = screen_full_pixels[
                    (offset_y + sample_y) * screen_full_width + offset_x + sample_x
                ]
                if abs(actual - expected) <= VERIFY_TOLERANCE:
                    matched += 1
            score = matched / len(samples)
            if score > best_score:
                best_score = score
                refined_x, refined_y = offset_x, offset_y
    final = _match_score(
        screen_full_pixels,
        screen_full_width,
        scaled_pixels,
        width,
        refined_x,
        refined_y,
        VERIFY_TOLERANCE,
    )
    return (final, refined_x, refined_y, width, height)


def locate_template(
    screen: bytes,
    template: bytes,
    *,
    threshold: float = MIN_TEMPLATE_SCORE,
    max_matches: int = 5,
) -> dict[str, Any]:
    """Find a template inside a screenshot with a bounded, verified search.

    Searching is a two-stage design, because a single sampled comparison is
    easily fooled by repeating content (rows of text, table rules):

    1. A coarse scan compares a fixed number of sampled template pixels at
       offsets on a reduced copy of the screenshot, and keeps the strongest
       non-neighbouring peaks. Cheap, and only used to *nominate* positions.
    2. Every nominated position is then re-scored with **all** template pixels,
       first on the reduced copy and then, for the winner, on the full-size
       screenshot. Only a position that survives full-pixel verification at or
       above ``threshold`` is reported as a match, so the reported score is a
       real measurement rather than a sampled guess.

    The screenshot is reduced so its width is at most :data:`WORK_WIDTH`, the
    offset count is bounded by :data:`MAX_SEARCH_STEPS`, and the template is
    capped by :data:`MAX_TEMPLATE_PIXELS`, so a 4K screenshot cannot turn this
    into a minutes-long loop. A template with almost no variation (a flat
    colour patch) is refused: it would "match" everywhere.
    """
    screen_image, _ = _open_image(screen)
    template_image, _ = _open_image(template)
    if template_image.width * template_image.height > MAX_TEMPLATE_PIXELS:
        raise ToolError(
            f"The template is larger than {MAX_TEMPLATE_PIXELS} pixels; crop it smaller."
        )
    if (
        template_image.width > screen_image.width
        or template_image.height > screen_image.height
    ):
        raise ToolError("The template is larger than the screenshot.")

    template_grey = template_image.convert("L")
    template_values = _pixel_values(template_grey)
    template_mean = sum(template_values) / max(1, len(template_values))
    template_variance = (
        sum((value - template_mean) ** 2 for value in template_values)
        / max(1, len(template_values))
    )
    if math.sqrt(template_variance) < MIN_TEMPLATE_CONTRAST:
        raise ToolError(
            "The template is almost a single flat colour, so any match would be "
            "meaningless. Crop a template that contains visible detail."
        )

    factor = max(1, screen_image.width // WORK_WIDTH)
    screen_full = screen_image.convert("L")
    screen_full_pixels = screen_full.tobytes()
    screen_full_width = screen_full.width
    screen_grey = screen_full
    if factor > 1:
        screen_grey = screen_full.resize(
            (max(1, screen_image.width // factor), max(1, screen_image.height // factor))
        )
    screen_pixels = screen_grey.tobytes()
    screen_width, screen_height = screen_grey.size
    reduced_width = max(2, round(template_image.width / factor))
    reduced_height = max(2, round(template_image.height / factor))
    tolerance = max(10.0, 60.0 * (1.0 - min(1.0, threshold)))

    steps = 0
    verified: list[tuple[float, int, int, int, int, float]] = []
    for scale in TEMPLATE_SCALES:
        width = max(4, int(reduced_width * scale))
        height = max(4, int(reduced_height * scale))
        if width > screen_width or height > screen_height:
            continue
        scaled = template_grey.resize((width, height))
        template_pixels = scaled.tobytes()
        stride = max(1, (width * height) // TEMPLATE_SAMPLES)
        samples = [
            (index % width, index // width, template_pixels[index])
            for index in range(0, width * height, stride)
        ]
        if not samples:
            continue
        span_x = screen_width - width
        span_y = screen_height - height
        step_x = max(1, span_x // MAX_OFFSETS_PER_AXIS)
        step_y = max(1, span_y // MAX_OFFSETS_PER_AXIS)
        nominations: list[tuple[float, int, int]] = []
        nomination_floor = max(0.5, threshold - 0.25)
        stop = False
        for origin_y in range(0, span_y + 1, step_y):
            if stop:
                break
            for origin_x in range(0, span_x + 1, step_x):
                steps += 1
                if steps > MAX_SEARCH_STEPS:
                    stop = True
                    break
                matched = 0
                for offset_x, offset_y, expected in samples:
                    actual = screen_pixels[
                        (origin_y + offset_y) * screen_width + origin_x + offset_x
                    ]
                    if abs(actual - expected) <= tolerance:
                        matched += 1
                score = matched / len(samples)
                if score >= nomination_floor:
                    nominations.append((score, origin_x, origin_y))
        if nominations:
            gap = max(4, min(width, height) // 4)
            shortlist = _select_candidates(
                nominations, gap=gap, limit=CANDIDATE_LIMIT
            )
            for _score, origin_x, origin_y in shortlist:
                coarse = _match_score(
                    screen_pixels,
                    screen_width,
                    template_pixels,
                    width,
                    origin_x,
                    origin_y,
                    tolerance,
                )
                steps += len(template_pixels)
                if coarse < threshold * 0.9:
                    continue
                verified.append(
                    _refine_and_verify(
                        screen_full_pixels=screen_full_pixels,
                        screen_full_width=screen_full_width,
                        screen_full_height=screen_full.height,
                        template_grey=template_grey,
                        factor=factor,
                        start_x=origin_x * factor,
                        start_y=origin_y * factor,
                        radius=max(2, factor + 1),
                    )
                    + (scale,)
                )
        if steps > MAX_SEARCH_STEPS:
            break

    screen_area = screen_full.width * screen_full.height
    accepted: list[TemplateMatch] = []
    for score, x, y, width, height, _scale in sorted(
        verified, key=lambda item: item[0], reverse=True
    ):
        if score < threshold or width * height >= screen_area:
            continue
        accepted.append(
            TemplateMatch(x=x, y=y, width=width, height=height, score=score)
        )

    best_scale = 0.0
    if accepted and verified:
        best_scale = max(verified, key=lambda item: item[0])[5]
    return {
        "found": bool(accepted),
        "score": round(accepted[0].score, 4) if accepted else 0.0,
        "threshold": round(threshold, 4),
        "scale": round(best_scale, 2),
        "matches": [match.to_dict() for match in accepted[: max(1, max_matches)]],
        "search_steps": steps,
        "screen": {"width": screen_image.width, "height": screen_image.height},
        "template": {
            "width": template_image.width,
            "height": template_image.height,
        },
        "reduction_factor": factor,
        "method": "verified-correlation-v3",
    }


def image_crop(data: bytes, *, box: tuple[int, int, int, int]) -> bytes:
    """Return a PNG crop for a validated, non-empty box."""
    image, _ = _open_image(data)
    left, top, right, bottom = box
    if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
        raise ToolError(
            f"The crop box must fit inside the image ({image.width}x{image.height})."
        )
    cropped = image.crop(box)
    buffer = io.BytesIO()
    cropped.save(buffer, format="PNG")
    return buffer.getvalue()


def read_image_file(path: Path, *, max_bytes: int = MAX_SOURCE_BYTES) -> bytes:
    """Read an image file with a hard size cap and no path surprises."""
    if not path.is_file():
        raise ToolError(f"Image not found: {path.name}")
    size = path.stat().st_size
    if size > max_bytes:
        raise ToolError(f"The image is larger than {max_bytes // 1_000_000} MB.")
    return path.read_bytes()


def region_hint(facts: ImageFacts) -> str:
    """Describe where the busiest cells are, in plain language."""
    if not facts.busy_cells:
        return "no dense region detected (flat or mostly empty image)"
    rows = facts.grid_rows
    columns = facts.grid_columns
    hottest = (0, 0)
    hottest_value = -1.0
    for row in range(rows):
        for column in range(columns):
            value = facts.grid_ink[row][column]
            if value > hottest_value:
                hottest_value = value
                hottest = (row, column)
    vertical = ["top", "middle", "bottom"]
    horizontal = ["left", "centre", "right"]
    row_band = vertical[min(2, int(hottest[0] * 3 / max(1, rows)))]
    column_band = horizontal[min(2, int(hottest[1] * 3 / max(1, columns)))]
    return (
        f"densest cell is {row_band}-{column_band} "
        f"(ink {hottest_value:.2f}); {facts.busy_cells} of {rows * columns} cells look busy"
    )


__all__ = [
    "INK_BUSY_THRESHOLD",
    "MAX_GRID",
    "MAX_PIXELS",
    "MAX_SOURCE_BYTES",
    "MIN_TEMPLATE_CONTRAST",
    "MIN_TEMPLATE_SCORE",
    "ImageFacts",
    "TemplateMatch",
    "analyse_image",
    "image_crop",
    "locate_template",
    "read_image_file",
    "region_hint",
]
