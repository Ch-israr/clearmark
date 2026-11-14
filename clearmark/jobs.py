"""Job queue and background worker for ClearMark.

The Flask process runs one worker thread. It claims queued operations
one at a time, runs the matching processor in a child thread (so a
per-media timeout can be enforced), and marks the operation succeeded
or failed. A failed job refunds the reserved credits, so a failed job
is never charged.

A second thread deletes expired upload/result files every 15 minutes.
On startup, operations left `running` by a previous crashed process
are failed and refunded.
"""

import math
import os
import threading
import time
from pathlib import Path

from . import db as store
from .credits import price_for
from .processors.audio import clean_audio
from .processors.image import ProcessingError as ImageError
from .processors.image import clean_image
from .processors.video import ProcessingError as VideoError
from .processors.video import clean_video

TIMEOUT_IMAGE_AUDIO = 22 * 60
TIMEOUT_VIDEO = 70 * 60
FILE_TTL_SECONDS = 60 * 60
CLEANUP_INTERVAL_SECONDS = 15 * 60


class JobError(Exception):
    """A job failed for a user-facing reason (code + message)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _timeout_for(media):
    return TIMEOUT_VIDEO if media == "video" else TIMEOUT_IMAGE_AUDIO


def _run_processor(media, src, dst, ext):
    if media == "image":
        return clean_image(src, dst)
    if media == "video":
        return clean_video(src, dst)
    if media == "audio":
        return clean_audio(src, dst, ext)
    raise JobError("UNSUPPORTED_FORMAT", "That media type is not supported.")


def _process_one(db_path, op):
    """Run one operation synchronously and return its details dict."""
    src = Path(op["upload_path"])
    result_dir = src.parent.parent / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    dst = result_dir / f"{op['id']}.{src.suffix.lstrip('.') or 'bin'}"
    media = op["media"]
    ext = src.suffix.lstrip(".").lower()
    timeout = _timeout_for(media)

    outcome = {}

    def target():
        try:
            outcome["details"] = _run_processor(media, str(src), str(dst), ext)
        except (ImageError, VideoError) as exc:
            outcome["error"] = JobError(exc.code, exc.message)
        except Exception as exc:  # corrupt input, disk errors, anything else
            outcome["error"] = JobError(
                "PROCESSING_FAILED",
                f"Processing failed: {exc}",
            )

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise JobError(
            "JOB_TIMEOUT",
            "Processing took too long and was stopped. Your credits were refunded.",
        )
    if "error" in outcome:
        raise outcome["error"]
    if not dst.exists():
        raise JobError(
            "PROCESSING_FAILED",
            "Processing finished without producing a file. Your credits were refunded.",
        )
    return outcome.get("details") or {}, str(dst)


def _fail_operation(db_path, op, error):
    store.update_operation(
        db_path, op["id"],
        status="failed",
        funding_state="refunded",
        error_code=error.code,
        error_message=error.message,
    )
    if op["cost"]:
        store.adjust_credits(db_path, op["user_id"], op["cost"])
    _delete_quietly(op.get("upload_path"))


def _succeed_operation(db_path, op, details, result_path):
    store.update_operation(
        db_path, op["id"],
        status="succeeded",
        funding_state="charged",
        result_path=result_path,
        result_name=_result_name(op),
        details=store.details_to_json(details),
    )
    _delete_quietly(op.get("upload_path"))


def _result_name(op):
    stem = Path(op["filename"]).stem or "cleaned"
    ext = Path(op["upload_path"]).suffix if op.get("upload_path") else ""
    return f"clearmark-{stem}{ext}"


def _delete_quietly(path):
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def worker_loop(db_path, stop_event):
    """Claim and process queued operations until stop_event is set."""
    while not stop_event.is_set():
        op = store.claim_next_queued(db_path)
        if op is None:
            stop_event.wait(0.5)
            continue
        try:
            details, result_path = _process_one(db_path, op)
        except JobError as exc:
            _fail_operation(db_path, op, exc)
        except Exception as exc:  # never let the worker thread die
            _fail_operation(
                db_path, op,
                JobError("PROCESSING_FAILED", f"Processing failed: {exc}"),
            )
        else:
            _succeed_operation(db_path, op, details, result_path)


def cleanup_loop(db_path, stop_event):
    """Delete expired upload and result files every 15 minutes."""
    while not stop_event.is_set():
        try:
            for op in store.expired_operations(db_path):
                _delete_quietly(op.get("upload_path"))
                _delete_quietly(op.get("result_path"))
                store.update_operation(
                    db_path, op["id"], upload_path=None, result_path=None
                )
        except Exception:
            pass
        stop_event.wait(CLEANUP_INTERVAL_SECONDS)


def recover_on_startup(db_path):
    """Fail stale running operations from a crashed process and refund
    their reserved credits. Returns how many were recovered."""
    stale = store.fail_stale_running(db_path)
    for op in stale:
        if op["cost"]:
            store.adjust_credits(db_path, op["user_id"], op["cost"])
    return len(stale)


def start_background_threads(db_path):
    """Start the worker and cleanup threads (idempotent per process)."""
    stop_event = threading.Event()
    worker = threading.Thread(
        target=worker_loop, args=(db_path, stop_event), daemon=True,
        name="clearmark-worker",
    )
    cleaner = threading.Thread(
        target=cleanup_loop, args=(db_path, stop_event), daemon=True,
        name="clearmark-cleaner",
    )
    worker.start()
    cleaner.start()
    return stop_event


def public_operation(op, base_url=""):
    """The JSON shape the browser polls, mirroring the original site's
    operation object."""
    view = {
        "id": op["id"],
        "status": op["status"],
        "fundingState": op["funding_state"],
        "usedTrial": bool(op["used_trial"]),
        "media": op["media"],
        "filename": op["filename"],
        "cost": op["cost"],
        "createdAt": op["created_at"],
        "resultExpiresAt": op["expires_at"],
    }
    details = store.details_from_json(op.get("details"))
    if details:
        view["details"] = details
    if op["status"] == "succeeded" and op.get("result_path"):
        view["resultUrl"] = f"{base_url}/api/operations/{op['id']}/result"
        view["resultName"] = op.get("result_name") or op["filename"]
    if op["status"] == "failed" and op.get("error_code"):
        view["error"] = {
            "code": op["error_code"],
            "message": op["error_message"],
        }
    return view


def charge_for(user, media, duration_seconds=None):
    """Price one removal for this user (free trial included)."""
    return price_for(user, media, duration_seconds)
