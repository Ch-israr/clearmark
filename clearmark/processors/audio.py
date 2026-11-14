"""Audio cleaning for ClearMark.

- WAV: keep only the `fmt ` and `data` chunks; every other RIFF chunk
  (LIST/INFO, bext, JUNK, cue, and anything else that can carry
  metadata) is dropped.
- MP3: strip ID3v2 and ID3v1 tags with mutagen, keeping the audio frames.
- M4A: an ISO BMFF container, so the same C2PA box walk as video
  applies (see processors/video.py).
"""

import struct

from mutagen.id3 import ID3
from mutagen.mp3 import MP3

from .video import find_c2pa_boxes, strip_c2pa_boxes


class ProcessingError(Exception):
    """The file could not be cleaned; the job must fail and refund."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _clean_wav(src, dst):
    try:
        with open(src, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise ProcessingError("UNREADABLE_FILE", f"This file could not be read: {exc}") from exc
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ProcessingError(
            "UNREADABLE_FILE",
            "This file does not look like a WAV file.",
        )
    kept = [b"RIFF", b"\x00\x00\x00\x00", b"WAVE"]
    dropped = []
    pos = 12
    fmt_seen = False
    data_seen = False
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        chunk_size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body_start = pos + 8
        body_end = body_start + chunk_size
        if body_end > len(data):
            break
        if chunk_id in (b"fmt ", b"data"):
            kept.append(data[pos:body_end + (chunk_size % 2)])
            if chunk_id == b"fmt ":
                fmt_seen = True
            else:
                data_seen = True
        else:
            dropped.append(chunk_id.decode("latin-1", errors="replace"))
        pos = body_end + (chunk_size % 2)
    if not fmt_seen or not data_seen:
        raise ProcessingError(
            "UNREADABLE_FILE",
            "This WAV file is missing its format or audio data.",
        )
    body = b"".join(kept[3:])
    riff_size = 4 + len(body)
    out = bytearray()
    out += b"RIFF"
    out += struct.pack("<I", riff_size)
    out += b"WAVE"
    out += body
    with open(dst, "wb") as handle:
        handle.write(bytes(out))
    return dropped


def _clean_mp3(src, dst):
    try:
        MP3(src)
    except Exception as exc:
        raise ProcessingError(
            "UNREADABLE_FILE",
            f"This MP3 file could not be opened: {exc}",
        ) from exc
    removed = []
    try:
        tags = ID3(src)
        removed = [str(key) for key in tags.keys()]
        tags.delete()
    except Exception:
        removed = []
    with open(src, "rb") as handle:
        data = bytearray(handle.read())
    # ID3v1 lives in the last 128 bytes and starts with "TAG".
    if len(data) >= 128 and data[-128:-125] == b"TAG":
        del data[-128:]
        removed.append("ID3v1")
    with open(dst, "wb") as handle:
        handle.write(bytes(data))
    if data[:3] == b"ID3":
        raise ProcessingError(
            "CLEANING_FAILED",
            "ID3 tags were still present after cleaning, so no download was produced.",
        )
    return removed


def clean_audio(src, dst, media_ext):
    """Clean one audio file. Returns a details dict for the result card."""
    ext = media_ext.lower()
    if ext == "wav":
        dropped = _clean_wav(src, dst)
        return {
            "removed": [f"Extra WAV chunks ({', '.join(dropped)})"] if dropped else [],
            "note": (
                "Non-audio WAV chunks were removed; the audio data itself "
                "was copied unchanged."
                if dropped else
                "No extra metadata chunks were found in this WAV file; "
                "the audio data was copied unchanged."
            ),
        }
    if ext == "mp3":
        removed = _clean_mp3(src, dst)
        return {
            "removed": ["ID3 tags"] if removed else [],
            "note": (
                "ID3 tags were removed; the audio frames were copied unchanged."
                if removed else
                "No ID3 tags were found in this file; the audio frames "
                "were copied unchanged."
            ),
        }
    if ext == "m4a":
        try:
            with open(src, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise ProcessingError(
                "UNREADABLE_FILE", f"This file could not be read: {exc}"
            ) from exc
        found = find_c2pa_boxes(data)
        cleaned, count = strip_c2pa_boxes(data)
        if find_c2pa_boxes(cleaned):
            raise ProcessingError(
                "CLEANING_FAILED",
                "C2PA boxes were still present after cleaning, so no download was produced.",
            )
        with open(dst, "wb") as handle:
            handle.write(cleaned)
        return {
            "removed": [f"C2PA boxes ({count})"] if count else [],
            "note": (
                "C2PA boxes were removed without re-encoding."
                if count else
                "No C2PA boxes were found in this file. Nothing was changed; "
                "the original file was returned as-is."
            ),
        }
    raise ProcessingError(
        "UNSUPPORTED_FORMAT",
        "Only MP3, WAV and M4A audio files are supported.",
    )
