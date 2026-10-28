"""Bounded local image inspection and transformation primitives.

Phase 15 deliberately keeps the native image surface small and verifiable:
Pillow may decode only a short list of common still-image formats, file and
pixel budgets are enforced before pixel data is trusted, outputs are encoded
through a temporary file and hard-linked into place so an existing file can
never be overwritten, and embedded metadata is not copied to outputs.

Semantic image understanding lives in the provider layer.  This module only
prepares a bounded payload for that separate, explicitly approved operation.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageOps, UnidentifiedImageError

from .errors import ToolError

MAX_IMAGE_FILE_BYTES = 20_000_000
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_DIMENSION = 10_000
MAX_VISION_IMAGE_BYTES = 20_000_000

SUPPORTED_INPUT_FORMATS = frozenset({"PNG", "JPEG", "GIF", "BMP", "WEBP", "TIFF"})
_FORMAT_BY_EXTENSION = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
    ".bmp": "BMP",
    ".tif": "TIFF",
    ".tiff": "TIFF",
}
_MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "WEBP": "image/webp",
    "TIFF": "image/tiff",
}
_VISION_PASSTHROUGH_FORMATS = frozenset({"PNG", "JPEG", "WEBP", "GIF"})

# Pillow's own decompression-bomb guard becomes a hard error at our cap.
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


@dataclass(frozen=True, slots=True)
class ImageInfo:
    format: str
    mime_type: str
    width: int
    height: int
    mode: str
    frame_count: int
    is_animated: bool
    has_alpha: bool
    size_bytes: int
    sha256: str
    average_color: str
    dominant_colors: tuple[str, ...]
    average_hash: str
    exif_orientation: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "mode": self.mode,
            "frame_count": self.frame_count,
            "is_animated": self.is_animated,
            "has_alpha": self.has_alpha,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "average_color": self.average_color,
            "dominant_colors": list(self.dominant_colors),
            "average_hash": self.average_hash,
            "exif_orientation": self.exif_orientation,
        }


@dataclass(frozen=True, slots=True)
class ImageComparison:
    first: ImageInfo
    second: ImageInfo
    byte_identical: bool
    pixel_identical: bool
    same_dimensions: bool
    average_hash_distance: int
    mean_absolute_error: float
    similarity_percent: float


@dataclass(frozen=True, slots=True)
class VisionImagePayload:
    data: bytes
    mime_type: str
    info: ImageInfo
    converted_or_resized: bool


@dataclass(frozen=True, slots=True)
class _LoadedImage:
    image: Image.Image
    format: str
    frame_count: int
    exif_orientation: int | None


def output_format_for_path(path: Path) -> str:
    image_format = _FORMAT_BY_EXTENSION.get(path.suffix.lower())
    if image_format is None:
        supported = ", ".join(sorted(_FORMAT_BY_EXTENSION))
        raise ToolError(
            f"Unsupported image output extension {path.suffix!r}; use one of: {supported}."
        )
    return image_format


def inspect_image(path: Path, *, include_color_stats: bool = True) -> ImageInfo:
    size_bytes, digest = _file_size_and_hash(path)
    loaded = _load_image(path)
    image = loaded.image
    try:
        has_alpha = _has_alpha(image)
        if include_color_stats:
            average_color, dominant_colors, average_hash = _pixel_stats(image)
        else:
            average_color, dominant_colors, average_hash = "#000000", (), _average_hash(image)
        return ImageInfo(
            format=loaded.format,
            mime_type=_MIME_BY_FORMAT[loaded.format],
            width=image.width,
            height=image.height,
            mode=image.mode,
            frame_count=loaded.frame_count,
            is_animated=loaded.frame_count > 1,
            has_alpha=has_alpha,
            size_bytes=size_bytes,
            sha256=digest,
            average_color=average_color,
            dominant_colors=dominant_colors,
            average_hash=average_hash,
            exif_orientation=loaded.exif_orientation,
        )
    finally:
        image.close()


def convert_image(source: Path, destination: Path, *, quality: int = 90) -> ImageInfo:
    _validate_quality(quality)
    output_format_for_path(destination)
    loaded = _load_image(source)
    try:
        _save_new_image(_normalize_for_processing(loaded.image), destination, quality=quality)
    finally:
        loaded.image.close()
    return inspect_image(destination)


def transform_image(
    source: Path,
    destination: Path,
    *,
    width: int | None = None,
    height: int | None = None,
    max_dimension: int | None = None,
    crop_box: tuple[int, int, int, int] | None = None,
    rotate_degrees: float = 0.0,
    flip: str = "none",
    grayscale: bool = False,
    quality: int = 90,
) -> tuple[ImageInfo, tuple[str, ...]]:
    _validate_quality(quality)
    output_format_for_path(destination)
    normalized_flip = flip.strip().lower()
    if normalized_flip not in {"none", "horizontal", "vertical", "both"}:
        raise ToolError("Image flip must be none, horizontal, vertical or both.")
    if rotate_degrees < -360 or rotate_degrees > 360:
        raise ToolError("Image rotate_degrees must be between -360 and 360.")

    operations: list[str] = []
    loaded = _load_image(source)
    image = _normalize_for_processing(loaded.image)
    loaded.image.close()
    try:
        if crop_box is not None:
            image = _crop(image, crop_box)
            operations.append("crop")
        if width is not None or height is not None:
            image = _resize(image, width=width, height=height)
            operations.append("resize")
        if max_dimension is not None:
            before = image.size
            image = _thumbnail(image, max_dimension)
            if image.size != before:
                operations.append("thumbnail")
        if rotate_degrees:
            image = _rotate(image, rotate_degrees)
            operations.append("rotate")
        if normalized_flip != "none":
            image = _flip(image, normalized_flip)
            operations.append(f"flip-{normalized_flip}")
        if grayscale:
            converted = image.convert("L")
            image.close()
            image = converted
            operations.append("grayscale")
        if not operations:
            raise ToolError(
                "Choose at least one image transformation: resize, thumbnail, crop, "
                "rotate, flip or grayscale."
            )
        _save_new_image(image, destination, quality=quality)
    finally:
        image.close()
    return inspect_image(destination), tuple(operations)


def compare_images(first_path: Path, second_path: Path) -> ImageComparison:
    first_info = inspect_image(first_path)
    second_info = inspect_image(second_path)
    first_loaded = _load_image(first_path)
    second_loaded = _load_image(second_path)
    try:
        first = _normalize_for_processing(first_loaded.image).convert("RGBA")
        second = _normalize_for_processing(second_loaded.image).convert("RGBA")
        try:
            same_dimensions = first.size == second.size
            pixel_identical = False
            if same_dimensions:
                difference = ImageChops.difference(first, second)
                try:
                    pixel_identical = all(
                        low == 0 and high == 0 for low, high in difference.getextrema()
                    )
                finally:
                    difference.close()

            compare_width = min(first.width, second.width, 512)
            compare_height = min(first.height, second.height, 512)
            first_small = first.resize(
                (compare_width, compare_height), Image.Resampling.LANCZOS
            )
            second_small = second.resize(
                (compare_width, compare_height), Image.Resampling.LANCZOS
            )
            try:
                diff = ImageChops.difference(first_small, second_small)
                try:
                    histogram = diff.histogram()
                finally:
                    diff.close()
            finally:
                first_small.close()
                second_small.close()
            # RGBA histogram is four consecutive 256-bin histograms.
            weighted = sum(
                (index % 256) * count for index, count in enumerate(histogram)
            )
            samples = compare_width * compare_height * 4
            mean_error = weighted / max(1, samples)
            similarity = max(0.0, 100.0 * (1.0 - mean_error / 255.0))
            hash_distance = _hash_distance(
                first_info.average_hash, second_info.average_hash
            )
            return ImageComparison(
                first=first_info,
                second=second_info,
                byte_identical=first_info.sha256 == second_info.sha256,
                pixel_identical=pixel_identical,
                same_dimensions=same_dimensions,
                average_hash_distance=hash_distance,
                mean_absolute_error=round(mean_error, 6),
                similarity_percent=round(similarity, 4),
            )
        finally:
            first.close()
            second.close()
    finally:
        first_loaded.image.close()
        second_loaded.image.close()


def prepare_vision_image(path: Path, *, max_bytes: int) -> VisionImagePayload:
    if max_bytes < 1_024 or max_bytes > MAX_VISION_IMAGE_BYTES:
        raise ToolError("Vision image byte limit must be between 1024 and 20000000.")
    info = inspect_image(path, include_color_stats=False)
    if info.format in _VISION_PASSTHROUGH_FORMATS and info.size_bytes <= max_bytes:
        return VisionImagePayload(
            data=path.read_bytes(),
            mime_type=info.mime_type,
            info=info,
            converted_or_resized=False,
        )

    loaded = _load_image(path)
    image = _normalize_for_processing(loaded.image)
    loaded.image.close()
    try:
        for max_dimension, quality in ((2048, 88), (1280, 82), (800, 76)):
            candidate = _thumbnail(image.copy(), max_dimension)
            try:
                encoded, mime_type = _encode_vision_candidate(candidate, quality=quality)
            finally:
                candidate.close()
            if len(encoded) <= max_bytes:
                return VisionImagePayload(
                    data=encoded,
                    mime_type=mime_type,
                    info=info,
                    converted_or_resized=True,
                )
    finally:
        image.close()
    raise ToolError(
        "The image could not be reduced below the configured vision byte limit; "
        "create a smaller copy with image.transform first."
    )


def _file_size_and_hash(path: Path) -> tuple[int, str]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ToolError(f"Image file not found: {path.name}") from exc
    if size <= 0:
        raise ToolError("Image file is empty.")
    if size > MAX_IMAGE_FILE_BYTES:
        raise ToolError(
            f"Image file exceeds the local {MAX_IMAGE_FILE_BYTES}-byte safety limit."
        )
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ToolError(f"Could not read image file: {path.name}") from exc
    return size, digest.hexdigest()


def _load_image(path: Path) -> _LoadedImage:
    _file_size_and_hash(path)
    try:
        with Image.open(path) as probe:
            detected_format = str(probe.format or "").upper()
            if detected_format not in SUPPORTED_INPUT_FORMATS:
                raise ToolError(
                    "Unsupported image format "
                    f"{detected_format or 'unknown'}; supported inputs are PNG, JPEG, "
                    "GIF, BMP, WebP and TIFF."
                )
            _validate_dimensions(probe.width, probe.height)
            probe.verify()

        with Image.open(path) as source:
            detected_format = str(source.format or "").upper()
            frame_count = int(getattr(source, "n_frames", 1) or 1)
            source.seek(0)
            orientation = _exif_orientation(source)
            transposed = ImageOps.exif_transpose(source)
            transposed.load()
            image = transposed.copy()
            transposed.close()
            _validate_dimensions(image.width, image.height)
            return _LoadedImage(
                image=image,
                format=detected_format,
                frame_count=max(1, frame_count),
                exif_orientation=orientation,
            )
    except ToolError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise ToolError(f"Could not decode image {path.name}: {exc}") from exc


def _validate_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ToolError("Image dimensions are invalid.")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise ToolError(f"Image dimensions exceed the {MAX_IMAGE_DIMENSION}px safety limit.")
    if width * height > MAX_IMAGE_PIXELS:
        raise ToolError(f"Image exceeds the {MAX_IMAGE_PIXELS}-pixel safety limit.")


def _validate_quality(quality: int) -> None:
    if not isinstance(quality, int) or isinstance(quality, bool) or not 1 <= quality <= 100:
        raise ToolError("Image quality must be an integer between 1 and 100.")


def _exif_orientation(image: Image.Image) -> int | None:
    try:
        value = image.getexif().get(274)
    except (AttributeError, OSError, ValueError):
        return None
    return int(value) if isinstance(value, int) and 1 <= value <= 8 else None


def _has_alpha(image: Image.Image) -> bool:
    if image.mode in {"RGBA", "LA", "PA"}:
        return True
    return image.mode == "P" and "transparency" in image.info


def _normalize_for_processing(image: Image.Image) -> Image.Image:
    if image.mode == "P":
        return image.convert("RGBA" if _has_alpha(image) else "RGB")
    if image.mode in {"RGB", "RGBA", "L", "LA"}:
        return image.copy()
    if image.mode == "CMYK":
        return image.convert("RGB")
    return image.convert("RGBA" if _has_alpha(image) else "RGB")


def _pixel_values(image: Image.Image) -> list[Any]:
    flattened = getattr(image, "get_flattened_data", None)
    if callable(flattened):
        return list(flattened())
    return list(image.getdata())  # Pillow < 12 compatibility


def _pixel_stats(image: Image.Image) -> tuple[str, tuple[str, ...], str]:
    sample = _normalize_for_processing(image).convert("RGBA")
    try:
        sample.thumbnail((128, 128), Image.Resampling.LANCZOS)
        pixels = [
            (int(red), int(green), int(blue), int(alpha))
            for red, green, blue, alpha in _pixel_values(sample)
            if int(alpha) >= 16
        ]
        if not pixels:
            return "#000000", (), _average_hash(image)
        average = tuple(
            round(sum(pixel[channel] for pixel in pixels) / len(pixels))
            for channel in range(3)
        )
        quantized = Counter(
            (
                min(255, (red // 32) * 32 + 16),
                min(255, (green // 32) * 32 + 16),
                min(255, (blue // 32) * 32 + 16),
            )
            for red, green, blue, _alpha in pixels
        )
        dominant = tuple(
            _hex_color(color)
            for color, _count in sorted(
                quantized.items(), key=lambda item: (-item[1], item[0])
            )[:5]
        )
        return _hex_color(average), dominant, _average_hash(image)
    finally:
        sample.close()


def _average_hash(image: Image.Image) -> str:
    grayscale = _normalize_for_processing(image).convert("L")
    try:
        small = grayscale.resize((8, 8), Image.Resampling.LANCZOS)
        try:
            values = _pixel_values(small)
        finally:
            small.close()
    finally:
        grayscale.close()
    average = sum(values) / len(values)
    bits = 0
    for value in values:
        bits = (bits << 1) | int(value >= average)
    return f"{bits:016x}"


def _hash_distance(first: str, second: str) -> int:
    try:
        return (int(first, 16) ^ int(second, 16)).bit_count()
    except ValueError:
        return 64


def _hex_color(values: tuple[int, ...]) -> str:
    red, green, blue = (max(0, min(255, int(value))) for value in values[:3])
    return f"#{red:02x}{green:02x}{blue:02x}"


def _crop(image: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    left, top, right, bottom = box
    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise ToolError("Image crop bounds must describe a positive rectangle.")
    if right > image.width or bottom > image.height:
        raise ToolError("Image crop bounds must stay inside the source image.")
    cropped = image.crop(box)
    image.close()
    _validate_dimensions(cropped.width, cropped.height)
    return cropped


def _resize(
    image: Image.Image, *, width: int | None, height: int | None
) -> Image.Image:
    if width is not None and (
        not isinstance(width, int) or isinstance(width, bool) or not 1 <= width <= MAX_IMAGE_DIMENSION
    ):
        raise ToolError(f"Image width must be between 1 and {MAX_IMAGE_DIMENSION}.")
    if height is not None and (
        not isinstance(height, int)
        or isinstance(height, bool)
        or not 1 <= height <= MAX_IMAGE_DIMENSION
    ):
        raise ToolError(f"Image height must be between 1 and {MAX_IMAGE_DIMENSION}.")
    if width is None:
        assert height is not None
        width = max(1, round(image.width * height / image.height))
    if height is None:
        height = max(1, round(image.height * width / image.width))
    _validate_dimensions(width, height)
    resized = image.resize((width, height), Image.Resampling.LANCZOS)
    image.close()
    return resized


def _thumbnail(image: Image.Image, max_dimension: int) -> Image.Image:
    if (
        not isinstance(max_dimension, int)
        or isinstance(max_dimension, bool)
        or not 1 <= max_dimension <= MAX_IMAGE_DIMENSION
    ):
        raise ToolError(
            f"Image max_dimension must be between 1 and {MAX_IMAGE_DIMENSION}."
        )
    copy = image.copy()
    image.close()
    copy.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    _validate_dimensions(copy.width, copy.height)
    return copy


def _rotate(image: Image.Image, degrees_clockwise: float) -> Image.Image:
    normalized = float(degrees_clockwise) % 360.0
    if normalized == 0:
        return image
    transpose_by_angle = {
        90.0: Image.Transpose.ROTATE_270,  # Pillow transpose names are counter-clockwise.
        180.0: Image.Transpose.ROTATE_180,
        270.0: Image.Transpose.ROTATE_90,
    }
    if normalized in transpose_by_angle:
        rotated = image.transpose(transpose_by_angle[normalized])
    else:
        fill = (0, 0, 0, 0) if _has_alpha(image) else (0, 0, 0)
        rotated = image.rotate(
            -normalized,
            resample=Image.Resampling.BICUBIC,
            expand=True,
            fillcolor=fill,  # type: ignore[arg-type]
        )
    image.close()
    _validate_dimensions(rotated.width, rotated.height)
    return rotated


def _flip(image: Image.Image, flip: str) -> Image.Image:
    result = image
    if flip in {"horizontal", "both"}:
        flipped = result.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        result.close()
        result = flipped
    if flip in {"vertical", "both"}:
        flipped = result.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        result.close()
        result = flipped
    return result


def _save_new_image(image: Image.Image, destination: Path, *, quality: int) -> None:
    if destination.exists():
        raise ToolError(
            "Image output already exists. This tool never overwrites; choose a new path."
        )
    parent = destination.parent
    if not parent.is_dir():
        raise ToolError(f"Image output folder not found: {parent}")
    output_format = output_format_for_path(destination)
    prepared = _prepare_for_output(image, output_format)
    temp_path: Path | None = None
    try:
        descriptor, raw_temp = tempfile.mkstemp(prefix=".image-", dir=parent)
        os.close(descriptor)
        temp_path = Path(raw_temp)
        save_options: dict[str, Any] = {}
        if output_format == "JPEG":
            save_options = {"quality": quality, "optimize": True, "progressive": False}
        elif output_format == "WEBP":
            save_options = {"quality": quality, "method": 4}
        elif output_format == "PNG":
            save_options = {"optimize": True}
        elif output_format == "TIFF":
            save_options = {"compression": "tiff_lzw"}
        prepared.save(temp_path, format=output_format, **save_options)
        # Hard-link is atomic and fails if the destination appeared meanwhile.
        os.link(temp_path, destination)
    except FileExistsError as exc:
        raise ToolError(
            "Image output already exists. This tool never overwrites; choose a new path."
        ) from exc
    except OSError as exc:
        raise ToolError(f"Could not write image {destination.name}: {exc}") from exc
    finally:
        prepared.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _prepare_for_output(image: Image.Image, output_format: str) -> Image.Image:
    normalized = _normalize_for_processing(image)
    if output_format in {"JPEG", "BMP"} and _has_alpha(normalized):
        rgba = normalized.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        rgba.close()
        normalized.close()
        flattened = background.convert("RGB")
        background.close()
        return flattened
    if output_format == "BMP" and normalized.mode == "LA":
        converted = normalized.convert("L")
        normalized.close()
        return converted
    return normalized


def _encode_vision_candidate(image: Image.Image, *, quality: int) -> tuple[bytes, str]:
    buffer = BytesIO()
    if _has_alpha(image):
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue(), "image/png"
    rgb = image.convert("RGB") if image.mode != "RGB" else image
    try:
        rgb.save(buffer, format="JPEG", quality=quality, optimize=True)
    finally:
        if rgb is not image:
            rgb.close()
    return buffer.getvalue(), "image/jpeg"
