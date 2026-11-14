"""ClearMark: Flask app serving the site and the cleaning API.

One service: HTML pages from templates/, a JSON API under /api/, and a
background worker thread (see clearmark/jobs.py) that cleans uploaded
files. Files live under data/uploads/ and data/results/ and are
deleted after one hour.
"""

import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from flask import (Flask, jsonify, redirect, render_template, request,
                   send_file, url_for)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from clearmark import auth, db as store
from clearmark.credits import can_afford, price_for
from clearmark.jobs import (FILE_TTL_SECONDS, public_operation,
                            recover_on_startup, start_background_threads)
from clearmark.validate import (MIN_FREE_DISK_BYTES, ValidationError,
                                check_batch, check_duration, check_size,
                                classify, media_duration)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CLEARMARK_DATA", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("CLEARMARK_DB", DATA_DIR / "clearmark.db"))
UPLOAD_DIR = DATA_DIR / "uploads"
RESULT_DIR = DATA_DIR / "results"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024 + 1024 * 1024

log = logging.getLogger("clearmark")


def create_app():
    app = Flask(__name__)
    secret = os.environ.get("CLEARMARK_SECRET_KEY", "")
    if not secret:
        secret = uuid.uuid4().hex
        log.warning("CLEARMARK_SECRET_KEY is not set; using a random key"
                    " that will change on restart. Set it in production.")
    app.config.update(
        SECRET_KEY=secret,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        DB_PATH=str(DB_PATH),
        DATA_DIR=str(DATA_DIR),
    )
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    store.init_db(DB_PATH)
    recovered = recover_on_startup(str(DB_PATH))
    if recovered:
        log.info("Recovered %d interrupted operation(s) on startup.", recovered)
    if not app.config.get("WORKER_STARTED"):
        start_background_threads(str(DB_PATH))
        app.config["WORKER_STARTED"] = True

    @app.context_processor
    def inject_user():
        return {"current_user": auth.current_user(str(DB_PATH))}

    # ---------------------------------------------------------------- pages

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/pricing")
    def pricing():
        return render_template("pricing.html")

    @app.get("/login")
    def login_page():
        if auth.current_user(str(DB_PATH)):
            return redirect(url_for("index"))
        return render_template("login.html")

    @app.get("/signup")
    def signup_page():
        if auth.current_user(str(DB_PATH)):
            return redirect(url_for("index"))
        return render_template("signup.html")

    @app.get("/logout")
    def logout_page():
        auth.logout_session()
        return redirect(url_for("index"))

    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html")

    @app.get("/terms")
    def terms():
        return render_template("terms.html")

    @app.get("/about")
    def about():
        return render_template("about.html")

    @app.get("/contact")
    def contact():
        return render_template("contact.html")

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    # ---------------------------------------------------------------- auth API

    @app.post("/api/auth/register")
    def api_register():
        data = request.get_json(silent=True) or {}
        user = auth.register(str(DB_PATH), data.get("email"), data.get("password"))
        auth.login_session(user)
        return jsonify({"user": auth.public_user(user)}), 201

    @app.post("/api/auth/login")
    def api_login():
        data = request.get_json(silent=True) or {}
        user = auth.authenticate(str(DB_PATH), data.get("email"), data.get("password"))
        if user is None:
            return jsonify({"error": {"code": "INVALID_CREDENTIALS",
                                      "message": "Email or password is wrong."}}), 401
        auth.login_session(user)
        return jsonify({"user": auth.public_user(user)})

    @app.post("/api/auth/logout")
    def api_logout():
        auth.logout_session()
        return jsonify({"ok": True})

    @app.get("/api/auth/me")
    @auth.login_required
    def api_me():
        user = auth.current_user(str(DB_PATH))
        return jsonify({"user": auth.public_user(user)})

    @app.get("/api/credits")
    @auth.login_required
    def api_credits():
        user = auth.current_user(str(DB_PATH))
        return jsonify({"balance": user["credits"],
                        "freeUsed": bool(user["free_used"])})

    # ---------------------------------------------------------------- uploads

    @app.post("/api/uploads")
    @auth.login_required
    def api_upload():
        user = auth.current_user(str(DB_PATH))
        operation_id = (request.form.get("operationId") or uuid.uuid4().hex).strip()
        existing = store.get_operation_by_upload(str(DB_PATH), user["id"], operation_id)
        if existing is not None:
            return jsonify({
                "uploadId": existing["id"],
                "operationId": operation_id,
                "operation": public_operation(existing),
            }), 200
        if "file" not in request.files:
            return _error("NO_FILE", "No file was attached.", 400)
        uploaded = request.files["file"]
        if not uploaded.filename:
            return _error("NO_FILE", "No file was attached.", 400)
        filename = secure_filename(uploaded.filename)
        try:
            media = classify(filename)
            batch_count = int(request.form.get("batchCount", "1") or "1")
            batch_total = int(request.form.get("batchTotalBytes", "0") or "0")
            check_batch(batch_count, batch_total)
        except ValidationError as exc:
            return _validation_error(exc)
        except ValueError:
            return _error("INVALID_REQUEST", "Batch values were not numbers.", 400)
        free_disk = shutil.disk_usage(UPLOAD_DIR).free
        if free_disk < MIN_FREE_DISK_BYTES:
            return _error("DISK_FULL",
                          "The server is out of temporary space. Try again shortly.",
                          413)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
        stored_name = f"{uuid.uuid4().hex}.{ext}"
        stored_path = UPLOAD_DIR / stored_name
        uploaded.save(stored_path)
        reserved = 0
        used_trial = False
        try:
            size = stored_path.stat().st_size
            check_size(media, size)
            duration = None
            if media in ("video", "audio"):
                duration = media_duration(stored_path, media)
            check_duration(media, duration)
            cost, used_trial = price_for(user, media, duration)
            if not used_trial and not can_afford(user, cost):
                raise ValidationError(
                    "INSUFFICIENT_CREDITS",
                    f"This file costs {cost} credits and your balance is"
                    f" {user['credits']}. See the pricing page to get more credits.",
                )
            if cost:
                store.adjust_credits(str(DB_PATH), user["id"], -cost)
                reserved = cost
            op = store.create_operation(
                str(DB_PATH), user["id"], media, filename, cost, used_trial,
                stored_path, operation_id, time.time() + FILE_TTL_SECONDS,
            )
            if used_trial:
                store.mark_free_used(str(DB_PATH), user["id"])
        except ValidationError as exc:
            if reserved:
                store.adjust_credits(str(DB_PATH), user["id"], reserved)
            stored_path.unlink(missing_ok=True)
            return _validation_error(exc)
        except Exception as exc:  # disk or database failure after reserving
            if reserved:
                store.adjust_credits(str(DB_PATH), user["id"], reserved)
            stored_path.unlink(missing_ok=True)
            log.exception("Upload failed for %s", filename)
            return _error("UPLOAD_FAILED", f"The upload failed: {exc}", 500)
        return jsonify({
            "uploadId": op["id"],
            "operationId": operation_id,
            "media": media,
            "filename": filename,
            "cost": cost,
            "usedTrial": used_trial,
            "operation": public_operation(op),
        }), 201

    # ---------------------------------------------------------------- operations

    @app.post("/api/operations")
    @auth.login_required
    def api_create_operation():
        user = auth.current_user(str(DB_PATH))
        data = request.get_json(silent=True) or {}
        op = None
        key = request.headers.get("Idempotency-Key")
        if key:
            op = store.get_operation_by_upload(str(DB_PATH), user["id"], key)
        if op is None and data.get("uploadId"):
            op = store.get_operation(str(DB_PATH), data["uploadId"])
        if op is None or op["user_id"] != user["id"]:
            return _error("OPERATION_NOT_FOUND",
                          "That upload could not be found.", 404)
        if op["status"] == "uploaded":
            store.update_operation(str(DB_PATH), op["id"], status="queued")
            op = store.get_operation(str(DB_PATH), op["id"])
        return jsonify({"operation": public_operation(op)})

    @app.get("/api/operations/<op_id>")
    @auth.login_required
    def api_get_operation(op_id):
        user = auth.current_user(str(DB_PATH))
        op = store.get_operation(str(DB_PATH), op_id)
        if op is None or op["user_id"] != user["id"]:
            return _error("OPERATION_NOT_FOUND",
                          "That operation could not be found.", 404)
        return jsonify({"operation": public_operation(op)})

    @app.get("/api/operations")
    @auth.login_required
    def api_list_operations():
        user = auth.current_user(str(DB_PATH))
        try:
            limit = min(int(request.args.get("limit", "20")), 50)
        except ValueError:
            limit = 20
        before = request.args.get("before")
        before_value = None
        if before:
            try:
                before_value = float(before)
            except ValueError:
                before_value = None
        ops = store.list_operations(str(DB_PATH), user["id"],
                                    limit=limit, before=before_value)
        return jsonify({
            "operations": [public_operation(op) for op in ops],
            "nextBefore": ops[-1]["created_at"] if len(ops) == limit else None,
        })

    @app.get("/api/operations/<op_id>/result")
    @auth.login_required
    def api_operation_result(op_id):
        user = auth.current_user(str(DB_PATH))
        op = store.get_operation(str(DB_PATH), op_id)
        if op is None or op["user_id"] != user["id"]:
            return _error("OPERATION_NOT_FOUND",
                          "That operation could not be found.", 404)
        if op["status"] != "succeeded" or not op.get("result_path"):
            return _error("RESULT_NOT_READY",
                          "This file is not ready to download yet.", 409)
        if time.time() > (op["expires_at"] or 0):
            return _error("RESULT_EXPIRED",
                          "This download link has expired. Upload the file again.",
                          410)
        result_path = Path(op["result_path"])
        if not result_path.exists():
            return _error("RESULT_EXPIRED",
                          "This download link has expired. Upload the file again.",
                          410)
        return send_file(
            result_path,
            as_attachment=True,
            download_name=op.get("result_name") or op["filename"],
        )

    # ---------------------------------------------------------------- errors

    @app.errorhandler(ValidationError)
    def handle_validation_error(exc):
        return _validation_error(exc)

    @app.errorhandler(RequestEntityTooLarge)
    def handle_too_large(exc):
        return _error("FILE_TOO_LARGE",
                      "That upload is too large. Batches must be under 200 MB.",
                      413)

    @app.errorhandler(404)
    def handle_not_found(exc):
        if request.path.startswith("/api/"):
            return _error("NOT_FOUND", "That address does not exist.", 404)
        return render_template("404.html"), 404

    return app


def _error(code, message, status):
    return jsonify({"error": {"code": code, "message": message}}), status


def _validation_error(exc):
    status = 402 if exc.code == "INSUFFICIENT_CREDITS" else 400
    if exc.code in ("FILE_TOO_LARGE", "BATCH_TOO_LARGE", "DISK_FULL"):
        status = 413
    return _error(exc.code, exc.message, status)


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
