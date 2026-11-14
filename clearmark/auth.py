"""Accounts for ClearMark: email + password, session cookies.

Passwords are hashed with Werkzeug's salted PBKDF2 (never stored in
plain text). Sessions are Flask's signed server-side cookie holding
only the user id.
"""

from functools import wraps

from flask import session
from werkzeug.security import check_password_hash, generate_password_hash

from . import db as store
from .validate import ValidationError, valid_email

MIN_PASSWORD_LENGTH = 8


def register(db_path, email, password):
    """Create a new account. Returns the user dict.

    Raises ValidationError with a user-facing message.
    """
    email = (email or "").strip().lower()
    password = password or ""
    if not valid_email(email):
        raise ValidationError("INVALID_EMAIL", "Enter a valid email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            "PASSWORD_TOO_SHORT",
            f"Passwords must be at least {MIN_PASSWORD_LENGTH} characters.",
        )
    if store.get_user_by_email(db_path, email) is not None:
        raise ValidationError(
            "EMAIL_TAKEN",
            "An account with this email already exists. Try logging in.",
        )
    return store.create_user(
        db_path, email,
        generate_password_hash(password, method="pbkdf2:sha256:260000"),
    )


def authenticate(db_path, email, password):
    """Return the user dict when the email and password match, else None."""
    email = (email or "").strip().lower()
    user = store.get_user_by_email(db_path, email)
    if user is None:
        return None
    if not check_password_hash(user["password_hash"], password or ""):
        return None
    return user


def login_session(user):
    session.clear()
    session["user_id"] = user["id"]


def logout_session():
    session.clear()


def current_user(db_path):
    user_id = session.get("user_id")
    if not user_id:
        return None
    return store.get_user_by_id(db_path, user_id)


def public_user(user):
    return {
        "id": user["id"],
        "email": user["email"],
        "credits": user["credits"],
        "freeUsed": bool(user["free_used"]),
        "createdAt": user["created_at"],
    }


def login_required(view):
    """Decorator: return 401 JSON when no user is logged in."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        from flask import current_app, jsonify

        user = current_user(current_app.config["DB_PATH"])
        if user is None:
            return jsonify({"error": {"code": "NOT_LOGGED_IN",
                                      "message": "Log in to continue."}}), 401
        return view(*args, **kwargs)

    return wrapped
