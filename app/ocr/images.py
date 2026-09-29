"""Image decoding, pre-processing and presentation helpers (Pillow based)."""

from __future__ import annotations

import base64
import io
import logging

from PIL import Image, ImageOps, UnidentifiedImageError

from ..exceptions import CorruptFileError

logger = logging.getLogger(__name__)

#: Pillow formats accepted beyond the literal JPG/PNG extensions, so that a
#: mislabelled but perfectly readable JPEG is not rejected.
READABLE_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "BMP", "TIFF", "WEBP", "GIF", "JPEG2000"})


def configure_pillow_limits(max_pixels: int | None) -> None:
    """Apply the decompression-bomb guard process wide.

    Called once while the application is built (instead of per request) because
    ``Image.MAX_IMAGE_PIXELS`` is global state and would not be thread safe.
    Pillow evaluates it inside ``Image.open``, i.e. before any pixels are
    decoded, which is what makes it a useful first line of defence.
    """
    Image.MAX_IMAGE_PIXELS = max_pixels


def looks_like_image(data: bytes) -> bool:
    """Return ``True`` when *data* is a raster image Pillow can decode."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            return (image.format or "").upper() in READABLE_IMAGE_FORMATS
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def load_image(data: bytes, max_pixels: int | None = None) -> Image.Image:
    """Decode *data* into an upright RGB image.

    A two-pass ``verify()``/reopen dance catches truncated or corrupted downloads
    before Tesseract has to deal with them. *max_pixels* additionally rejects
    "decompression bombs" - small files that expand into enormous bitmaps.
    """
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
    except UnidentifiedImageError as exc:
        raise CorruptFileError("The uploaded file is not a readable image.") from exc
    except Image.DecompressionBombError as exc:
        raise CorruptFileError("The image has too many pixels to process safely.") from exc
    except OSError as exc:
        raise CorruptFileError(f"The image is damaged or incomplete ({exc}).") from exc

    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if max_pixels and width * height > max_pixels:
                raise CorruptFileError(
                    f"The image is {width} x {height} pixels, which is beyond the "
                    f"{max_pixels:,} pixel limit for a single upload."
                )
            # exif_transpose() returns a detached copy, so it stays valid after close().
            return ImageOps.exif_transpose(image).convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise CorruptFileError(f"The image could not be decoded ({exc}).") from exc


def preprocess_for_ocr(
    image: Image.Image,
    *,
    min_target_px: int = 1800,
    max_upscale: float = 3.0,
) -> Image.Image:
    """Return the variant of *image* that gives Tesseract the best chance.

    Steps: grayscale conversion (removes colour noise) plus a contrast stretch,
    and a LANCZOS upscale for images that are too small - Tesseract expects
    roughly 300 DPI text and does poorly on tiny screenshots.
    """
    work = image if image.mode == "RGB" else image.convert("RGB")

    longest_edge = max(work.size)
    if min_target_px > 0 and 0 < longest_edge < min_target_px:
        factor = min(max_upscale, min_target_px / longest_edge)
        if factor > 1.05:
            new_size = (max(1, round(work.width * factor)), max(1, round(work.height * factor)))
            work = work.resize(new_size, Image.Resampling.LANCZOS)
            logger.debug("Upscaled image %s -> %s for OCR", image.size, new_size)

    grayscale = work if work.mode == "L" else work.convert("L")
    return ImageOps.autocontrast(grayscale)


def downscale(image: Image.Image, max_px: int) -> Image.Image:
    """Shrink *image* so its longest edge is at most *max_px* pixels."""
    longest_edge = max(image.size)
    if max_px <= 0 or longest_edge <= max_px:
        return image
    factor = max_px / longest_edge
    return image.resize(
        (max(1, round(image.width * factor)), max(1, round(image.height * factor))),
        Image.Resampling.LANCZOS,
    )


def to_data_uri(image: Image.Image, *, max_px: int = 360) -> str:
    """Encode *image* as a base64 PNG data URI for inline previews."""
    preview = downscale(image.convert("RGB"), max_px)
    buffer = io.BytesIO()
    preview.save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
