"""ISO BMFF box walking for ClearMark (MP4, MOV, M4A, M4V).

C2PA / Content Credentials live in `uuid` boxes whose user type is the
JUMBF UUID d8fec3d6-1b0e-483c-9297-5828877ec481. This module rewrites
those boxes in place as `free` boxes with a zeroed payload, so the file
keeps exactly the same size and layout and no other box has to move.

Container boxes are walked recursively; `meta` boxes carry a 4-byte
version/flags field before their children, which the walker accounts
for. Boxes with size 0 (to end of file) or size 1 (64-bit largesize)
are handled.
"""

import struct

JUMBF_UUID = bytes.fromhex("d8fec3d61b0e483c92975828877ec481")
CONTAINER_TYPES = {
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf",
    b"udta", b"meta", b"moof", b"traf", b"mfra", b"ilst",
}

# Extra safety margin: never trust a declared size that runs past the
# end of the data, and never walk a box smaller than its own header.
_MIN_BOX = 8


class ProcessingError(Exception):
    """The file could not be cleaned; the job must fail and refund."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _box_size(data, pos):
    """Return (total_size, header_size) for the box at pos, or None."""
    if pos + _MIN_BOX > len(data):
        return None
    size = struct.unpack(">I", data[pos:pos + 4])[0]
    header = 8
    if size == 1:
        if pos + 16 > len(data):
            return None
        size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
        header = 16
    elif size == 0:
        size = len(data) - pos
    if size < header or pos + size > len(data):
        return None
    return size, header


def _walk(data, start, end, found, rewrite):
    """Walk boxes in data[start:end]. found collects (offset, box_type)
    for C2PA uuid boxes; when rewrite is True they are neutralised."""
    pos = start
    while pos + _MIN_BOX <= end:
        parsed = _box_size(data, pos)
        if parsed is None:
            break
        size, header = parsed
        box_type = bytes(data[pos + 4:pos + 8])
        body_start = pos + header
        body_end = pos + size
        if box_type == b"uuid" and body_start + 16 <= body_end:
            usertype = data[body_start:body_start + 16]
            if usertype == JUMBF_UUID:
                found.append((pos, box_type))
                if rewrite:
                    data[pos + 4:pos + 8] = b"free"
                    data[body_start:body_end] = b"\x00" * (body_end - body_start)
        elif box_type in CONTAINER_TYPES:
            child_start = body_start
            if box_type == b"meta":
                child_start = body_start + 4
            _walk(data, child_start, body_end, found, rewrite)
        pos += size


def find_c2pa_boxes(data):
    """Return a list of (offset, box_type) for every C2PA uuid box."""
    found = []
    _walk(bytearray(data), 0, len(data), found, rewrite=False)
    return found


def strip_c2pa_boxes(data):
    """Return (cleaned_bytes, count) with every C2PA uuid box rewritten
    as a zeroed `free` box. The input bytes are not modified."""
    mutable = bytearray(data)
    found = []
    _walk(mutable, 0, len(mutable), found, rewrite=True)
    return bytes(mutable), len(found)


def clean_video(src, dst):
    """Remove C2PA uuid boxes from an MP4/MOV/M4V file.

    The file is not re-encoded: only the C2PA boxes are neutralised, so
    duration, resolution and quality are unchanged. Returns a details
    dict for the result card.
    """
    try:
        with open(src, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise ProcessingError("UNREADABLE_FILE", f"This file could not be read: {exc}") from exc
    if len(data) < 8 or data[4:8] != b"ftyp":
        # Some MOV files have no ftyp at the top; only reject files that
        # are clearly not BMFF containers.
        if not data[4:8] in (b"moov", b"wide", b"free", b"mdat"):
            raise ProcessingError(
                "UNREADABLE_FILE",
                "This file does not look like an MP4, MOV or M4V video.",
            )
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
            "C2PA boxes were removed without re-encoding, so duration and "
            "quality are unchanged. Video frames are not re-encoded in this "
            "version, so any frame-level SynthID signal is not addressed here."
            if count else
            "No C2PA boxes were found in this file. Nothing was changed; "
            "the original file was returned as-is."
        ),
    }
