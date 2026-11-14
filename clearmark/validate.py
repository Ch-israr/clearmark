"""Server-side validation for ClearMark uploads.

These limits are authoritative: the browser mirrors them for fast
feedback, but every upload is re-checked here before anything is saved
or any credit is reserved.
"""

import os
import re
import struct
import subprocess
import wave

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_IMAGE_MEGAPIXELS = 50
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_VIDEO_SECONDS = 30
MAX_AUDIO_BYTES = 30 * 1024 * 1024
MAX_AUDIO_SECONDS = 300
MAX_BATCH_FILES = 10
MAX_BATCH_BYTES = 200 * 1024 * 1024
MIN_FREE_DISK_BYTES = int(os.environ.get("CLEARMARK_MIN_FREE_DISK_MB", "100")) * 1024 * 1024

IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
VIDEO_EXTENSIONS = {"mp4", "mov", "m4v"}
AUDIO_EXTENSIONS = {"mp3", "wav", "m4a"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ValidationError(Exception):
    """A user-facing rejection with a short machine code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def classify(filename):
    """Return 'image', 'video' or 'audio' for a supported filename.

    GIF is rejected explicitly: cleaning an animated image would destroy
    the animation.
    """
    name = (filename or "").strip().lower()
    if name.endswith(".gif"):
        raise ValidationError(
            "ANIMATED_NOT_SUPPORTED",
            "Animated images (GIF) are not supported — cleaning would break the animation.",
        )
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    raise ValidationError(
        "UNSUPPORTED_FORMAT",
        "That format is not supported. Use JPEG, PNG or WebP images,"
        " MP4, MOV or M4V video, or MP3, WAV or M4A audio.",
    )


def check_size(media, size_bytes):
    """Enforce the per-file size limit for the media type."""
    if media == "image" and size_bytes > MAX_IMAGE_BYTES:
        raise ValidationError(
            "FILE_TOO_LARGE",
            "Images must be under 50 MB.",
        )
    if media == "video" and size_bytes > MAX_VIDEO_BYTES:
        raise ValidationError(
            "FILE_TOO_LARGE",
            "Videos must be under 100 MB.",
        )
    if media == "audio" and size_bytes > MAX_AUDIO_BYTES:
        raise ValidationError(
            "FILE_TOO_LARGE",
            "Audio files must be under 30 MB.",
        )


def check_batch(file_count, total_bytes):
    """Enforce the image batch limits (10 files, 200 MB total)."""
    if file_count > MAX_BATCH_FILES:
        raise ValidationError(
            "BATCH_TOO_LARGE",
            f"Up to {MAX_BATCH_FILES} images can be processed at once.",
        )
    if total_bytes > MAX_BATCH_BYTES:
        raise ValidationError(
            "BATCH_TOO_LARGE",
            "A batch must be under 200 MB in total.",
        )


def check_image_dimensions(width, height):
    if width * height > MAX_IMAGE_MEGAPIXELS * 1_000_000:
        raise ValidationError(
            "IMAGE_TOO_LARGE",
            "Images must be under 50 megapixels.",
        )


def check_duration(media, seconds):
    if seconds is None:
        return
    if media == "video" and seconds > MAX_VIDEO_SECONDS:
        raise ValidationError(
            "DURATION_TOO_LONG",
            "Videos up to 30 seconds are supported.",
        )
    if media == "audio" and seconds > MAX_AUDIO_SECONDS:
        raise ValidationError(
            "DURATION_TOO_LONG",
            "Audio up to 300 seconds (5 minutes) is supported.",
        )


def valid_email(email):
    return bool(email) and bool(_EMAIL_RE.match(email.strip()))


def _mvhd_duration(path):
    """Read the duration from an MP4/MOV/M4A mvhd box without ffmpeg."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(1024 * 1024)
    except OSError:
        return None
    pos = 0
    while pos + 8 <= len(data):
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        box_type = data[pos + 4:pos + 8]
        if size < 8:
            break
        if box_type == b"moov":
            inner = pos + 8
            end = pos + size
            while inner + 8 <= end and inner + 8 <= len(data):
                inner_size = struct.unpack(">I", data[inner:inner + 4])[0]
                inner_type = data[inner + 4:inner + 8]
                if inner_size < 8:
                    break
                if inner_type == b"mvhd":
                    body = data[inner + 8:inner + inner_size]
                    if len(body) < 4:
                        return None
                    version = body[0]
                    if version == 1 and len(body) >= 28:
                        timescale, duration = struct.unpack(">II", body[20:28])
                    elif len(body) >= 20:
                        timescale, duration = struct.unpack(">II", body[12:20])
                    else:
                        return None
                    if timescale == 0:
                        return None
                    return duration / timescale
                inner += inner_size
            return None
        pos += size
    return None


def media_duration(path, media):
    """Return the duration in seconds of a video or audio file, or None
    when it cannot be determined. Prefers ffprobe when it is installed,
    then falls back to format-specific parsing."""
    suffix = path.suffix.lower()
    if suffix == ".wav":
        try:
            with wave.open(str(path), "rb") as wav:
                frames = wav.getnframes()
                rate = wav.getframerate()
                if rate:
                    return frames / rate
        except (wave.Error, OSError):
            return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        value = result.stdout.strip()
        if value:
            return float(value)
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        pass
    if media == "video" or suffix == ".m4a":
        return _mvhd_duration(path)
    if suffix == ".mp3":
        try:
            from mutagen.mp3 import MP3
            return MP3(str(path)).info.length
        except Exception:
            return None
    return None


def format_bytes(size):
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"
