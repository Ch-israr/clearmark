"""Image cleaning for ClearMark.

Two real steps, both server-side:

1. Re-encode with Pillow. Saving a fresh image drops EXIF, XMP, C2PA
   (JUMBF) and ICC metadata, because none of it is copied to the output.
2. A subtle best-effort disruption pass aimed at the SynthID signal: a
   faint high-frequency perturbation plus a very light blur, applied
   before the re-encode. This is the same class of technique the original
   site describes in its FAQ. It is best-effort and NOT a guarantee —
   nobody can verify a SynthID signal is gone, because Google's detector
   is not public.

The output is then re-checked: if any known marker survives, the job
fails instead of offering a bad download.
"""

import numpy as np
from PIL import Image, ImageFilter

from ..validate import ValidationError, check_image_dimensions

# Raw byte markers that must never appear in a cleaned image.
FORBIDDEN_MARKERS = (
    b"jumbf",
    b"c2pa",
    b"C2PA",
    b"Exif\x00\x00",
    b"http://ns.adobe.com/xap/1.0/",
    b"http://ns.adobe.com/xmp/extension/",
)

_NOISE_STRENGTH = 1.6
_BLUR_RADIUS = 0.4


class ProcessingError(Exception):
    """The file could not be cleaned; the job must fail and refund."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _disrupt_pixels(image):
    """Apply the faint high-frequency perturbation to an RGB(A) image."""
    array = np.asarray(image).astype(np.float32)
    has_alpha = array.shape[-1] == 4
    rgb = array[:, :, :3] if has_alpha else array
    rng = np.random.default_rng(20261114)
    noise = rng.normal(0.0, _NOISE_STRENGTH, rgb.shape).astype(np.float32)
    # Keep only the high-frequency part of the noise so flat areas stay
    # visually untouched: subtract a blurred copy of the noise itself.
    noise_image = Image.fromarray(
        np.clip(noise + 128.0, 0, 255).astype(np.uint8)
    )
    low = np.asarray(
        noise_image.filter(ImageFilter.GaussianBlur(radius=1.2))
    ).astype(np.float32) - 128.0
    noise = noise - low
    rgb = np.clip(rgb + noise, 0, 255)
    if has_alpha:
        rgb = np.dstack([rgb, array[:, :, 3:4]])
    result = Image.fromarray(rgb.astype(np.uint8), mode=image.mode)
    return result.filter(ImageFilter.GaussianBlur(radius=_BLUR_RADIUS)) \
        if _BLUR_RADIUS else result


def _markers_found(path):
    with open(path, "rb") as handle:
        data = handle.read()
    return [m for m in FORBIDDEN_MARKERS if m in data]


def inspect_image(path):
    """List the metadata markers present in a file before cleaning."""
    found = []
    try:
        with Image.open(path) as image:
            image.load()
            if image.format == "JPEG":
                exif = image.getexif()
                if exif and len(exif):
                    found.append("EXIF")
            info = image.info or {}
            if "exif" in info:
                found.append("EXIF")
            if "icc_profile" in info:
                found.append("ICC profile")
            if "dpi" in info:
                found.append("DPI record")
            text_keys = [k for k in info if isinstance(k, str)]
            if any("xmp" in k.lower() for k in text_keys):
                found.append("XMP")
    except Exception:
        pass
    raw = _markers_found(path)
    if b"jumbf" in raw or b"c2pa" in raw or b"C2PA" in raw:
        found.append("C2PA (JUMBF)")
    if b"http://ns.adobe.com/xap/1.0/" in raw:
        found.append("XMP")
    return sorted(set(found))


def clean_image(src, dst):
    """Clean one image file. Returns a details dict for the result card.

    Raises ProcessingError when the file cannot be decoded, is animated,
    or is over the megapixel limit.
    """
    try:
        with Image.open(src) as image:
            image.load()
            fmt = (image.format or "").upper()
            width, height = image.size
            frames = getattr(image, "n_frames", 1)
    except Exception as exc:
        raise ProcessingError(
            "UNREADABLE_FILE",
            f"This image could not be opened: {exc}",
        ) from exc
    if fmt not in ("JPEG", "PNG", "WEBP"):
        raise ProcessingError(
            "UNSUPPORTED_FORMAT",
            "Only JPEG, PNG and WebP images are supported.",
        )
    if frames > 1:
        raise ProcessingError(
            "ANIMATED_NOT_SUPPORTED",
            "Animated images are not supported — cleaning would break the animation.",
        )
    try:
        check_image_dimensions(width, height)
    except ValidationError as exc:
        raise ProcessingError("IMAGE_TOO_LARGE", exc.message) from exc

    before = inspect_image(src)

    with Image.open(src) as image:
        image.load()
        if image.mode not in ("RGB", "RGBA", "L", "LA"):
            image = image.convert("RGBA" if "A" in image.mode else "RGB")
        if image.mode in ("L", "LA"):
            image = image.convert("RGBA" if image.mode == "LA" else "RGB")
        disrupted = _disrupt_pixels(image)
        save_kwargs = {}
        if fmt == "JPEG":
            save_kwargs = {"quality": 95, "optimize": True}
            if disrupted.mode == "RGBA":
                disrupted = disrupted.convert("RGB")
        elif fmt == "WEBP":
            save_kwargs = {"quality": 95, "method": 6}
        elif fmt == "PNG":
            save_kwargs = {"optimize": True}
        disrupted.save(dst, format=fmt, **save_kwargs)

    remaining = _markers_found(dst)
    if remaining:
        raise ProcessingError(
            "CLEANING_FAILED",
            "Markers were still present after cleaning, so no download was produced.",
        )
    return {
        "removed": before,
        "note": (
            "C2PA, EXIF and XMP metadata were removed by re-encoding. "
            "A subtle disruption pass was applied to the pixels; SynthID "
            "removal is best-effort and not guaranteed."
        ),
    }
