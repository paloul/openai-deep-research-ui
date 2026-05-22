import argparse
import os
import sys
import threading
import uuid
import json
import tempfile
import time
import hmac
import hashlib
import secrets
from urllib.parse import urlsplit
from pathlib import Path
from queue import Queue, Empty

from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    redirect,
    session,
    stream_with_context,
    Response,
    url_for,
)
from flask_cors import CORS
from openai import OpenAI
from werkzeug.utils import secure_filename

from db import (
    init_db,
    create_session as db_create_session,
    append_event,
    complete_session,
    list_sessions,
    get_session,
    delete_session as db_delete_session,
    get_events_after,
    get_session_status,
)
from config import load_config, save_config, _deep_merge

load_dotenv(override=True)

app = Flask(__name__, template_folder="templates")


def _required_secret_key():
    auth_enabled = os.getenv("ODR_AUTH_ENABLED", "true").lower() not in (
        "false",
        "0",
        "no",
    )
    secret_key = os.getenv("ODR_SESSION_SECRET") or os.getenv("SECRET_KEY")
    if not secret_key:
        if not auth_enabled:
            return secrets.token_urlsafe(32)
        raise RuntimeError(
            "ODR_SESSION_SECRET is required when authentication is enabled. "
            "Set it to a long random value."
        )
    if not auth_enabled:
        return secret_key
    if secret_key in (
        "open-deep-research-dev-secret-change-me",
        "change_me_to_a_long_random_secret",
        "BeyondArtificialInteligence",
    ):
        raise RuntimeError("ODR_SESSION_SECRET must not use a default or example value.")
    if len(secret_key) < 32:
        raise RuntimeError("ODR_SESSION_SECRET must be at least 32 characters long.")
    return secret_key


app.secret_key = _required_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("ODR_COOKIE_SECURE", "false").lower()
    in ("true", "1", "yes"),
)

cors_origins = [
    origin.strip()
    for origin in os.getenv("ODR_CORS_ORIGINS", "").split(",")
    if origin.strip()
]
if cors_origins:
    CORS(app, origins=cors_origins, supports_credentials=True)

DEEP_RESEARCH_MODEL_OPTIONS = [
    {
        "id": "o3-deep-research",
        "name": "OpenAI o3 Deep Research",
        "description": "OpenAI Deep Research model for thorough research tasks",
        "reasoning_efforts": ["medium"],
        "default_reasoning_effort": "medium",
    },
    {
        "id": "o4-mini-deep-research",
        "name": "OpenAI o4-mini Deep Research",
        "description": "Faster, lower-cost OpenAI Deep Research model",
        "reasoning_efforts": ["medium"],
        "default_reasoning_effort": "medium",
    },
]
DEEP_RESEARCH_MODEL_BY_ID = {model["id"]: model for model in DEEP_RESEARCH_MODEL_OPTIONS}
DEEP_RESEARCH_MODELS = set(DEEP_RESEARCH_MODEL_BY_ID)
POLL_INTERVAL_SECONDS = 2
MAX_ATTACHMENT_FILES = 5
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".pdf", ".doc", ".docx"}
ALLOWED_ATTACHMENT_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/csv",
    "text/markdown",
    "text/plain",
    "application/json",
}
AUTH_EXEMPT_ENDPOINTS = {"login", "logout", "static"}
LOGIN_FAILURE_WINDOW_SECONDS = int(os.getenv("ODR_LOGIN_FAILURE_WINDOW_SECONDS", "300"))
LOGIN_MAX_FAILURES = int(os.getenv("ODR_LOGIN_MAX_FAILURES", "5"))
LOGIN_MAX_BODY_BYTES = int(os.getenv("ODR_LOGIN_MAX_BODY_BYTES", "4096"))
LOGIN_MAX_USERNAME_LENGTH = int(os.getenv("ODR_LOGIN_MAX_USERNAME_LENGTH", "256"))
LOGIN_MAX_PASSWORD_LENGTH = int(os.getenv("ODR_LOGIN_MAX_PASSWORD_LENGTH", "512"))
LOGIN_FAILURES: dict[str, list[float]] = {}
login_failures_lock = threading.Lock()

# Initialize session database (graceful degradation if it fails)
try:
    init_db()
except Exception as e:
    print(f"Warning: Failed to initialize database: {e}")

# Session tracking directory (shared across all workers)
SESSION_DIR = Path(tempfile.gettempdir()) / "open_deep_research_sessions"
SESSION_DIR.mkdir(exist_ok=True)
ATTACHMENT_DIR = Path(tempfile.gettempdir()) / "open_deep_research_attachments"
ATTACHMENT_DIR.mkdir(exist_ok=True)

# Note: cleanup_stale_sessions() is called after function definitions below

# In-memory session tracking for this worker
# session_id -> {'queue': Queue, 'stop_event': threading.Event, 'response_id': str | None}
active_sessions: dict[str, dict] = {}
sessions_lock = threading.Lock()


def write_session_file(session_id, worker_pid, run_mode="background", response_id=None):
    """Write session info to shared file"""
    session_file = SESSION_DIR / f"{session_id}.json"
    with open(session_file, "w") as f:
        json.dump(
            {
                "worker_pid": worker_pid,
                "run_mode": run_mode,
                "response_id": response_id,
                "created_at": time.time(),
            },
            f,
        )


def read_session_file(session_id):
    """Read session info from shared file"""
    session_file = SESSION_DIR / f"{session_id}.json"
    if session_file.exists():
        with open(session_file, "r") as f:
            return json.load(f)
    return None


def delete_session_file(session_id):
    """Delete session file"""
    session_file = SESSION_DIR / f"{session_id}.json"
    if session_file.exists():
        session_file.unlink()


def _auth_enabled():
    return os.getenv("ODR_AUTH_ENABLED", "true").lower() not in ("false", "0", "no")


def _auth_credentials():
    username = os.getenv("ODR_AUTH_USERNAME")
    password = os.getenv("ODR_AUTH_PASSWORD") or os.getenv("CONFIG_ADMIN_PASSWORD")
    weak_passwords = {"", "password", "change_me", "change_me_to_a_secure_password"}
    if not username or not password:
        raise RuntimeError("ODR_AUTH_USERNAME and ODR_AUTH_PASSWORD must be set.")
    if password in weak_passwords or len(password) < 12:
        raise RuntimeError("ODR_AUTH_PASSWORD must be a strong non-default password.")
    return (username, password)


if _auth_enabled():
    _auth_credentials()


def _safe_next_url(next_url):
    if not next_url or any(ch in next_url for ch in ("\r", "\n", "\x00")):
        return url_for("index")
    parsed = urlsplit(next_url)
    if (
        parsed.scheme == ""
        and parsed.netloc == ""
        and parsed.path.startswith("/")
        and not parsed.path.startswith("//")
    ):
        return next_url
    return url_for("index")


def _wants_json_response():
    return request.path.startswith("/api/") or (
        request.accept_mimetypes["application/json"]
        > request.accept_mimetypes["text/html"]
    )


def _login_failure_key(username):
    username_digest = hashlib.sha256(username.encode("utf-8")).hexdigest()
    return f"{request.remote_addr or 'unknown'}:{username_digest}"


def _bounded_form_value(name, max_length):
    value = request.form.get(name, "")
    if len(value) > max_length:
        raise ValueError(f"{name} is too long.")
    return value


def _login_is_rate_limited(username):
    key = _login_failure_key(username)
    cutoff = time.time() - LOGIN_FAILURE_WINDOW_SECONDS
    with login_failures_lock:
        failures = [ts for ts in LOGIN_FAILURES.get(key, []) if ts >= cutoff]
        LOGIN_FAILURES[key] = failures
        return len(failures) >= LOGIN_MAX_FAILURES


def _record_login_failure(username):
    key = _login_failure_key(username)
    cutoff = time.time() - LOGIN_FAILURE_WINDOW_SECONDS
    with login_failures_lock:
        failures = [ts for ts in LOGIN_FAILURES.get(key, []) if ts >= cutoff]
        failures.append(time.time())
        LOGIN_FAILURES[key] = failures


def _clear_login_failures(username):
    with login_failures_lock:
        LOGIN_FAILURES.pop(_login_failure_key(username), None)


def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _valid_csrf_token(token):
    expected = session.get("csrf_token")
    return bool(expected and token and hmac.compare_digest(token, expected))


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": _csrf_token}


@app.before_request
def require_authentication():
    """Protect the UI and API with a simple session login."""
    if not _auth_enabled():
        return None
    if request.endpoint in AUTH_EXEMPT_ENDPOINTS:
        return None
    if session.get("authenticated"):
        return None
    if _wants_json_response():
        return jsonify({"error": "Authentication required"}), 401
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not _auth_enabled():
        return redirect(url_for("index"))

    error = ""
    next_url = _safe_next_url(request.values.get("next"))

    if request.method == "POST":
        if request.content_length and request.content_length > LOGIN_MAX_BODY_BYTES:
            return "Request too large", 413
        try:
            username = _bounded_form_value("username", LOGIN_MAX_USERNAME_LENGTH)
            password = _bounded_form_value("password", LOGIN_MAX_PASSWORD_LENGTH)
        except ValueError:
            error = "Invalid username or password."
            return render_template(
                "login.html",
                error=error,
                next_url=next_url,
                csrf_token=_csrf_token(),
            )
        if not _valid_csrf_token(request.form.get("csrf_token", "")):
            error = "Invalid sign-in request. Please try again."
        elif _login_is_rate_limited(username):
            error = "Too many failed attempts. Please try again later."
        else:
            expected_username, expected_password = _auth_credentials()
            valid_login = hmac.compare_digest(
                username, expected_username
            ) and hmac.compare_digest(password, expected_password)
            if valid_login:
                _clear_login_failures(username)
                next_csrf_token = secrets.token_urlsafe(32)
                session.clear()
                session["csrf_token"] = next_csrf_token
                session["authenticated"] = True
                session["username"] = username
                return redirect(next_url)
            _record_login_failure(username)
            error = "Invalid username or password."

    return render_template(
        "login.html", error=error, next_url=next_url, csrf_token=_csrf_token()
    )


@app.route("/logout", methods=["POST"])
def logout():
    if not _auth_enabled():
        return redirect(url_for("login"))
    if not _valid_csrf_token(request.form.get("csrf_token", "")):
        return redirect(url_for("index"))
    session.clear()
    return redirect(url_for("login"))


def _openai_provider(cfg):
    providers = cfg.get("model", {}).get("providers", [])
    for provider in providers:
        if (provider.get("provider") or "").lower() == "openai":
            return provider
    return {}


def _openai_client(cfg):
    provider = _openai_provider(cfg)
    api_key = provider.get("api_key") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OpenAI API key is required. Set OPENAI_API_KEY or configure model.providers.openai.api_key."
        )

    kwargs = {"api_key": api_key}
    base_url = (provider.get("base_url") or "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def _is_allowed_attachment(filename, content_type):
    ext = Path(filename or "").suffix.lower()
    if ext in ALLOWED_ATTACHMENT_EXTENSIONS:
        return True
    return (content_type or "").split(";")[0].lower() in ALLOWED_ATTACHMENT_MIME_TYPES


def _save_request_attachments(files):
    attachments = []
    if len(files) > MAX_ATTACHMENT_FILES:
        raise ValueError(f"Attach up to {MAX_ATTACHMENT_FILES} files per run.")

    for uploaded in files:
        if not uploaded or not uploaded.filename:
            continue

        filename = secure_filename(uploaded.filename)
        if not filename:
            raise ValueError("Attachment filename is invalid.")
        if not _is_allowed_attachment(filename, uploaded.content_type):
            raise ValueError(
                f"Unsupported attachment type for {uploaded.filename}. Use text, PDF, .doc, or .docx files."
            )

        uploaded.stream.seek(0, os.SEEK_END)
        size = uploaded.stream.tell()
        uploaded.stream.seek(0)
        if size > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"{uploaded.filename} is too large. Maximum file size is {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB."
            )

        attachment_path = ATTACHMENT_DIR / f"{uuid.uuid4()}-{filename}"
        uploaded.save(attachment_path)
        attachments.append(
            {
                "path": str(attachment_path),
                "filename": filename,
                "content_type": uploaded.content_type or "",
                "bytes": size,
            }
        )

    return attachments


def _cleanup_attachment_files(attachments):
    for attachment in attachments or []:
        try:
            Path(attachment["path"]).unlink(missing_ok=True)
        except Exception as e:
            print(f"Failed to delete temporary attachment {attachment.get('path')}: {e}")


def _create_attachment_vector_store(client, attachments):
    if not attachments:
        return None

    vector_store = client.vector_stores.create(
        name=f"Open Deep Research attachments {uuid.uuid4()}",
        expires_after={"anchor": "last_active_at", "days": 1},
    )

    streams = []
    try:
        for attachment in attachments:
            streams.append(open(attachment["path"], "rb"))
        client.vector_stores.file_batches.upload_and_poll(
            vector_store_id=vector_store.id,
            files=streams,
        )
    finally:
        for stream in streams:
            try:
                stream.close()
            except Exception:
                pass

    return vector_store.id


def _reasoning_effort_for_model(model_id, requested_effort=None):
    model_info = DEEP_RESEARCH_MODEL_BY_ID.get(model_id, {})
    allowed = model_info.get("reasoning_efforts", ["medium"])
    if requested_effort in allowed:
        return requested_effort
    return model_info.get("default_reasoning_effort") or allowed[0]


def _response_text(response):
    text = getattr(response, "output_text", None)
    if text:
        return text

    data = response.model_dump(mode="json") if hasattr(response, "model_dump") else {}
    parts = []
    for item in data.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if content.get("type") in ("output_text", "text"):
                parts.append(content.get("text", ""))
    return "\n".join(part for part in parts if part).strip()


def _object_to_dict(obj):
    """Convert OpenAI SDK model/event objects to plain dictionaries."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return {}


def _event_response_id(event_data):
    response = event_data.get("response") or {}
    return response.get("id") or event_data.get("response_id")


def _response_text_from_data(response_data):
    text = response_data.get("output_text")
    if text:
        return text

    parts = []
    for item in response_data.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if content.get("type") in ("output_text", "text"):
                parts.append(content.get("text", ""))
    return "\n".join(part for part in parts if part).strip()


def _summarize_output_item(item):
    item_type = item.get("type", "response_item")
    status = item.get("status")
    item_id = item.get("id") or item.get("call_id") or json.dumps(item, sort_keys=True)

    if item_type == "web_search_call":
        return item_id, {
            "type": "action_step",
            "step_number": None,
            "model_output": f"OpenAI Deep Research web search {status or 'started'}.",
            "tool_calls": [{"name": "web_search_preview", "arguments": item}],
            "observations": status or "",
        }

    if item_type == "file_search_call":
        return item_id, {
            "type": "action_step",
            "step_number": None,
            "model_output": f"OpenAI Deep Research attachment search {status or 'started'}.",
            "tool_calls": [{"name": "file_search", "arguments": item}],
            "observations": status or "",
        }

    if item_type in ("reasoning", "reasoning_summary"):
        summary = item.get("summary") or item.get("content") or item.get("text") or ""
        if isinstance(summary, list):
            summary = "\n".join(
                part.get("text", str(part)) if isinstance(part, dict) else str(part)
                for part in summary
            )
        return item_id, {
            "type": "planning_step",
            "plan": summary or "OpenAI Deep Research is reasoning.",
        }

    return item_id, {
        "type": "message",
        "content": f"{item_type}: {status or 'updated'}",
    }


def _summarize_stream_event(event_data):
    """Map Responses API streaming events into UI events.

    Deep Research emits progress as response/item/tool streaming events before the
    final Response object is complete. These UI events are intentionally compact:
    they show the live status and tool activity without exposing raw SDK objects
    unless that is the only useful data available.
    """
    event_type = event_data.get("type", "")

    if event_type in ("response.created", "response.queued", "response.in_progress"):
        return []

    if event_type == "response.output_text.delta":
        delta = event_data.get("delta") or ""
        return [{"type": "answer_delta", "delta": delta}] if delta else []

    if event_type == "response.output_text.done":
        text = event_data.get("text") or ""
        return [{"type": "answer_snapshot", "content": text}] if text else []

    if event_type in ("response.output_item.added", "response.output_item.done"):
        item = event_data.get("item") or {}
        if not item:
            return []
        _, ui_event = _summarize_output_item(item)
        if event_type.endswith(".added"):
            item_type = item.get("type", "response_item")
            status = item.get("status") or "started"
            if item_type == "message":
                return []
            if item_type == "web_search_call":
                ui_event["model_output"] = f"OpenAI Deep Research web search {status}."
            elif item_type == "file_search_call":
                ui_event["model_output"] = f"OpenAI Deep Research attachment search {status}."
            elif item_type in ("reasoning", "reasoning_summary"):
                ui_event["plan"] = ui_event.get("plan") or "OpenAI Deep Research is reasoning."
            else:
                ui_event = {
                    "type": "message",
                    "content": f"{item_type}: {status}",
                }
        return [ui_event]

    if event_type.startswith("response.web_search_call."):
        status = event_type.rsplit(".", 1)[-1].replace("_", " ")
        return [
            {
                "type": "action_step",
                "step_number": None,
                "model_output": f"OpenAI Deep Research web search {status}.",
                "tool_calls": [
                    {
                        "name": "web_search_preview",
                        "arguments": {
                            key: value
                            for key, value in event_data.items()
                            if key not in ("type", "sequence_number")
                        },
                    }
                ],
                "observations": status,
            }
        ]

    if event_type.startswith("response.file_search_call."):
        status = event_type.rsplit(".", 1)[-1].replace("_", " ")
        return [
            {
                "type": "action_step",
                "step_number": None,
                "model_output": f"OpenAI Deep Research attachment search {status}.",
                "tool_calls": [
                    {
                        "name": "file_search",
                        "arguments": {
                            key: value
                            for key, value in event_data.items()
                            if key not in ("type", "sequence_number")
                        },
                    }
                ],
                "observations": status,
            }
        ]

    if event_type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
        delta = event_data.get("delta") or ""
        return [{"type": "planning_step", "plan": delta}] if delta else []

    if event_type in ("response.failed", "response.incomplete"):
        response = event_data.get("response") or {}
        error = response.get("error") or response.get("incomplete_details") or {}
        return [{"type": "error", "content": f"OpenAI response {event_type}: {error}"}]

    return []


def _cancel_response(cfg, response_id):
    if not response_id:
        return
    try:
        _openai_client(cfg).responses.cancel(response_id)
    except Exception as e:
        print(f"Failed to cancel OpenAI response {response_id}: {e}")


def deep_research_worker(session_id, question, cfg, output_queue, stop_event, attachments=None):
    """Run a single OpenAI Deep Research Responses job and persist UI events."""
    event_counter = 0
    step_counter = 0
    final_answer = None
    emitted_item_ids = set()
    last_status = None
    response_id = None
    final_status = None
    final_response_data = None
    attachments = attachments or []
    vector_store_id = None

    def emit(event):
        nonlocal event_counter, step_counter, final_answer
        if event.get("type") == "action_step" and event.get("step_number") is None:
            step_counter += 1
            event["step_number"] = step_counter
        if event.get("type") == "final_answer":
            final_answer = (event.get("output") or event.get("content") or "")[:5000]
        try:
            append_event(session_id, event_counter, event)
            event_counter += 1
        except Exception as db_err:
            print(f"DB: Failed to append event: {db_err}")
        output_queue.put(event)

    try:
        model_id = cfg.get("model", {}).get("default_model_id", "o3-deep-research")
        if model_id not in DEEP_RESEARCH_MODELS:
            raise ValueError(
                f"Unsupported model '{model_id}'. Choose o3-deep-research or o4-mini-deep-research."
            )

        client = _openai_client(cfg)
        tools = [{"type": "web_search_preview"}]
        if attachments:
            attachment_names = ", ".join(a["filename"] for a in attachments)
            emit(
                {
                    "type": "info",
                    "content": f"Indexing {len(attachments)} attachment(s): {attachment_names}",
                }
            )
            vector_store_id = _create_attachment_vector_store(client, attachments)
            tools.append({"type": "file_search", "vector_store_ids": [vector_store_id]})
            emit({"type": "info", "content": "Attachments are ready for Deep Research."})

        create_kwargs = {
            "model": model_id,
            "input": question,
            "background": True,
            "stream": True,
            "stream_options": {"include_obfuscation": False},
            "tools": tools,
        }

        max_output_tokens = cfg.get("model", {}).get("max_output_tokens")
        if max_output_tokens:
            create_kwargs["max_output_tokens"] = int(max_output_tokens)

        requested_effort = cfg.get("model", {}).get("reasoning_effort")
        create_kwargs["reasoning"] = {
            "effort": _reasoning_effort_for_model(model_id, requested_effort)
        }

        emit({"type": "info", "content": f"Submitted to OpenAI Deep Research ({model_id})."})

        stream = client.responses.create(**create_kwargs)
        for stream_event in stream:
            if stop_event.is_set():
                _cancel_response(cfg, response_id)
                emit({"type": "error", "content": "Deep Research run cancelled by user."})
                complete_session(session_id, final_answer=final_answer, status="stopped")
                return

            data = _object_to_dict(stream_event)
            event_response_id = _event_response_id(data)
            if event_response_id and event_response_id != response_id:
                response_id = event_response_id
                with sessions_lock:
                    if session_id in active_sessions:
                        active_sessions[session_id]["response_id"] = response_id
                write_session_file(
                    session_id,
                    worker_pid=os.getpid(),
                    run_mode=cfg.get("_run_mode", "background"),
                    response_id=response_id,
                )
                emit({"type": "info", "content": f"OpenAI response id: {response_id}"})

            response_data = data.get("response") or {}
            status = response_data.get("status")
            if status:
                final_status = status
                if status != last_status:
                    emit({"type": "info", "content": f"OpenAI response status: {status}"})
                    last_status = status

            for event in _summarize_stream_event(data):
                if event.get("type") == "answer_delta":
                    final_answer = (final_answer or "") + event.get("delta", "")
                elif event.get("type") == "answer_snapshot":
                    final_answer = event.get("content") or final_answer
                if event.get("type") not in ("answer_delta", "answer_snapshot"):
                    item_key = json.dumps(event, sort_keys=True)
                    if item_key in emitted_item_ids:
                        continue
                    emitted_item_ids.add(item_key)
                emit(event)

            if data.get("type") in (
                "response.completed",
                "response.failed",
                "response.incomplete",
                "response.cancelled",
            ):
                final_response_data = response_data
                final_status = status or data.get("type").removeprefix("response.")

        if response_id and not final_response_data:
            response = client.responses.retrieve(response_id)
            final_response_data = _object_to_dict(response)
            final_status = final_response_data.get("status") or final_status

        if final_status == "completed":
            final_text = _response_text_from_data(final_response_data or {}) or final_answer or ""
            final_answer = final_text
            emit(
                {
                    "type": "final_answer",
                    "content": final_text,
                    "output": final_text,
                }
            )
            complete_session(session_id, final_answer=final_answer, status="completed")
        elif final_status in ("cancelled", "canceled"):
            emit({"type": "error", "content": "Deep Research run was cancelled."})
            complete_session(session_id, final_answer=final_answer, status="stopped")
        else:
            error = (final_response_data or {}).get("error") or (final_response_data or {}).get(
                "incomplete_details"
            ) or {}
            emit(
                {
                    "type": "error",
                    "content": f"Deep Research run ended with status {final_status}: {error}",
                }
            )
            complete_session(session_id, final_answer=final_answer, status="error")

    except Exception as e:
        emit({"type": "error", "content": str(e)})
        try:
            complete_session(session_id, final_answer=final_answer, status="error")
        except Exception:
            pass
    finally:
        if vector_store_id:
            try:
                _openai_client(cfg).vector_stores.delete(vector_store_id)
            except Exception as e:
                print(f"Failed to delete attachment vector store {vector_store_id}: {e}")
        _cleanup_attachment_files(attachments)
        output_queue.put(None)
        with sessions_lock:
            active_sessions.pop(session_id, None)
        delete_session_file(session_id)


def cleanup_stale_sessions():
    """On startup, mark sessions from a previous web process as interrupted."""
    try:
        for session_file in SESSION_DIR.glob("*.json"):
            try:
                session_id = session_file.stem
                try:
                    complete_session(session_id, status="interrupted")
                except Exception:
                    pass
                session_file.unlink()
            except Exception:
                session_file.unlink(missing_ok=True)
    except Exception as e:
        print(f"Startup cleanup error: {e}")


# Clean up any stale sessions from previous runs
try:
    cleanup_stale_sessions()
except Exception as e:
    print(f"Warning: Failed to clean up stale sessions: {e}")


@app.route("/")
def index():
    """Serve the main HTML page"""
    return render_template("index.html")


@app.route("/api/run/stream", methods=["POST"])
def run_deep_research_stream():
    """Streaming API endpoint using Server-Sent Events.
    Supports run_mode: 'background' (default), 'auto-kill', 'live'."""
    attachments = []
    try:
        if request.content_type and request.content_type.startswith("multipart/form-data"):
            data = request.form.to_dict()
            client_config_raw = data.get("client_config") or "{}"
            try:
                data["client_config"] = json.loads(client_config_raw)
            except json.JSONDecodeError:
                return jsonify({"error": "client_config must be valid JSON"}), 400
            attachments = _save_request_attachments(request.files.getlist("attachments"))
        else:
            data = request.json or {}

        question = data.get("question", "").strip()
        model_id = data.get("model_id", "o3-deep-research")
        run_mode = data.get("run_mode", "background")
        client_config = data.get("client_config", {})

        if run_mode not in ("background", "auto-kill", "live"):
            run_mode = "background"

        if not question:
            return jsonify({"error": "Question is required"}), 400
        if model_id not in DEEP_RESEARCH_MODELS:
            return (
                jsonify(
                    {
                        "error": "Only o3-deep-research and o4-mini-deep-research are supported."
                    }
                ),
                400,
            )

        # Build merged config: server config + client overrides
        server_cfg = load_config()
        override = {"model": {"default_model_id": model_id}}

        # Merge client config overrides (excluding models and legacy local-runner settings)
        if client_config:
            client_config.pop("models", None)
            client_config.pop("agent", None)
            client_config.pop("search", None)
            client_config.pop("browser", None)
            client_config.pop("limits", None)
            client_config.pop("compaction", None)
            client_config.pop("other_keys", None)
            override = _deep_merge(override, client_config)

        merged_cfg = _deep_merge(server_cfg, override)
        merged_cfg["_run_mode"] = run_mode
        server_openai = _openai_provider(server_cfg)
        merged_openai = _openai_provider(merged_cfg)
        if server_openai and not merged_openai.get("api_key"):
            model_cfg = merged_cfg.setdefault("model", {})
            model_cfg["providers"] = [
                {
                    "provider": "openai",
                    "api_key": server_openai.get("api_key", ""),
                    "base_url": merged_openai.get("base_url")
                    or server_openai.get("base_url", ""),
                }
            ]

        # Create session with unique ID
        session_id = str(uuid.uuid4())
        output_queue = Queue()
        stop_event = threading.Event()

        # Persist session to database
        try:
            db_create_session(session_id, question, model_id, run_mode)
        except Exception as db_err:
            print(f"DB: Failed to create session: {db_err}")

        write_session_file(session_id, worker_pid=os.getpid(), run_mode=run_mode)

        with sessions_lock:
            active_sessions[session_id] = {
                "queue": output_queue,
                "stop_event": stop_event,
                "response_id": None,
            }

        worker_thread = threading.Thread(
            target=deep_research_worker,
            args=(session_id, question, merged_cfg, output_queue, stop_event, attachments),
            daemon=True,
        )
        worker_thread.start()

        if run_mode == "background":
            # Background persistent: decouple OpenAI polling from HTTP connection.
            # Client connects to /api/sessions/<id>/live for streaming.
            def generate_background():
                yield f"data: {json.dumps({'session_id': session_id})}\n\n"

            return Response(
                stream_with_context(generate_background()),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            # Auto-kill / Live mode: coupled behavior.
            # generate() streams worker output. Client disconnect cancels the OpenAI response.
            def generate():
                interrupted = False

                try:
                    # Send session_id as first message
                    yield f"data: {json.dumps({'session_id': session_id})}\n\n"

                    while True:
                        # Use timeout so we periodically yield, allowing
                        # GeneratorExit to be raised on client disconnect
                        try:
                            item = output_queue.get(timeout=2)
                        except Empty:
                            # No data yet — yield SSE comment as heartbeat
                            # This triggers GeneratorExit if client disconnected
                            yield ": heartbeat\n\n"
                            continue

                        if item is None:  # End of stream
                            yield f"data: {json.dumps({'done': True})}\n\n"
                            break

                        # Item is a structured JSON event from OpenAI Responses polling.
                        yield f"data: {json.dumps(item)}\n\n"

                except GeneratorExit:
                    interrupted = True
                    print(f"Client disconnected for session {session_id}, cancelling response...")
                    with sessions_lock:
                        if session_id in active_sessions:
                            session = active_sessions[session_id]
                            session.get("stop_event").set()
                            response_id = session.get("response_id")
                        else:
                            response_id = None
                    _cancel_response(merged_cfg, response_id)

                    try:
                        complete_session(session_id, status="interrupted")
                    except Exception:
                        pass
                    raise  # Re-raise to properly close the generator

                finally:
                    if interrupted:
                        delete_session_file(session_id)

            return Response(
                stream_with_context(generate()),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

    except ValueError as e:
        _cleanup_attachment_files(attachments)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        _cleanup_attachment_files(attachments)
        return jsonify({"error": str(e)}), 500


@app.route("/api/stop/<session_id>", methods=["POST"])
def stop_session(session_id):
    """Stop a running Deep Research session."""
    try:
        # Read session from shared file (works across workers)
        session_data = read_session_file(session_id)

        if not session_data:
            return jsonify({"success": False, "message": "Session not found"}), 404

        # Mark session as stopped in DB before cancelling the remote response.
        try:
            complete_session(session_id, status="stopped")
        except Exception:
            pass

        response_id = session_data.get("response_id")
        cfg = load_config()
        _cancel_response(cfg, response_id)

        # Unblock any local generator/poller.
        with sessions_lock:
            if session_id in active_sessions:
                try:
                    active_sessions[session_id]["stop_event"].set()
                    active_sessions[session_id]["queue"].put(None)
                except Exception:
                    pass

        # Cleanup session file
        delete_session_file(session_id)

        return jsonify({"success": True, "message": "Deep Research cancelled"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/models", methods=["GET"])
def get_models():
    """Return the Deep Research model picker options."""
    return jsonify(DEEP_RESEARCH_MODEL_OPTIONS)


@app.route("/api/models/discover", methods=["POST"])
def discover_models():
    """Return the only models this UI supports."""
    return jsonify(
        {
            "discovered": [
                {
                    "id": model["id"],
                    "provider": "openai",
                    "reasoning_efforts": model["reasoning_efforts"],
                    "default_reasoning_effort": model["default_reasoning_effort"],
                }
                for model in DEEP_RESEARCH_MODEL_OPTIONS
            ],
            "errors": [],
        }
    )


# ===== Config API =====


def _mask_api_key(key):
    """Mask an API key for display: 'sk-abc123xyz' -> 'sk-***xyz'"""
    if not key or len(key) < 6:
        return key
    return key[:3] + "***" + key[-3:]


@app.route("/api/config/meta", methods=["GET"])
def config_meta():
    """Return config UI metadata (no auth required)"""
    enable_ui = os.getenv("ENABLE_CONFIG_UI", "false").lower() in ("true", "1", "yes")
    return jsonify({"enable_config_ui": enable_ui})


def _check_admin_password(password):
    """Validate admin password against env var."""
    admin_password = os.getenv("CONFIG_ADMIN_PASSWORD", "")
    return bool(
        admin_password
        and password
        and hmac.compare_digest(password, admin_password)
    )


@app.route("/api/config/verify", methods=["POST"])
def verify_admin_password():
    """Verify admin password without changing anything"""
    enable_ui = os.getenv("ENABLE_CONFIG_UI", "false").lower() in ("true", "1", "yes")
    if not enable_ui:
        return jsonify({"error": "Config UI is disabled"}), 403

    data = request.json
    password = data.get("password", "")
    if _check_admin_password(password):
        return jsonify({"valid": True})
    return jsonify({"valid": False}), 401


@app.route("/api/config", methods=["GET"])
def get_config_endpoint():
    """Return server config with API keys masked. Requires admin password."""
    enable_ui = os.getenv("ENABLE_CONFIG_UI", "false").lower() in ("true", "1", "yes")
    if not enable_ui:
        return jsonify({"error": "Config UI is disabled"}), 403

    password = request.headers.get("X-Admin-Password", "")
    if not _check_admin_password(password):
        return jsonify({"error": "Invalid admin password"}), 401

    cfg = load_config()
    model_cfg = dict(cfg.get("model", {}))
    model_cfg["providers"] = [
        p
        for p in model_cfg.get("providers", [])
        if (p.get("provider") or "").lower() == "openai"
    ] or [{"provider": "openai", "api_key": "", "base_url": ""}]
    if model_cfg.get("default_model_id") not in DEEP_RESEARCH_MODELS:
        model_cfg["default_model_id"] = "o3-deep-research"
    model_cfg["reasoning_effort"] = _reasoning_effort_for_model(
        model_cfg["default_model_id"],
        model_cfg.get("reasoning_effort"),
    )
    cfg = {
        "model": model_cfg,
        "models": DEEP_RESEARCH_MODEL_OPTIONS,
    }
    # Mask sensitive keys for display
    for p in cfg.get("model", {}).get("providers", []):
        if p.get("api_key"):
            p["api_key"] = _mask_api_key(p["api_key"])
    return jsonify(cfg)


@app.route("/api/config", methods=["POST"])
def update_config_endpoint():
    """Update server config (requires admin password when ENABLE_CONFIG_UI is true)"""
    enable_ui = os.getenv("ENABLE_CONFIG_UI", "false").lower() in ("true", "1", "yes")
    if not enable_ui:
        return jsonify({"error": "Config UI is disabled"}), 403

    data = request.json
    password = data.get("_password", "")

    if not _check_admin_password(password):
        return jsonify({"error": "Invalid admin password"}), 401

    # Remove password and unsupported legacy sections before saving
    config_data = {
        k: v
        for k, v in data.items()
        if k in ("model", "models") and k not in ("_password", "api_keys")
    }
    if "model" in config_data:
        model_cfg = config_data["model"]
        model_cfg["providers"] = [
            p
            for p in model_cfg.get("providers", [])
            if (p.get("provider") or "").lower() == "openai"
        ]
        if model_cfg.get("default_model_id") not in DEEP_RESEARCH_MODELS:
            model_cfg["default_model_id"] = "o3-deep-research"
        model_cfg["reasoning_effort"] = _reasoning_effort_for_model(
            model_cfg["default_model_id"],
            model_cfg.get("reasoning_effort"),
        )
    config_data["models"] = DEEP_RESEARCH_MODEL_OPTIONS

    current = load_config()
    merged = _deep_merge(
        {
            "model": {
                "providers": [{"provider": "openai", "api_key": "", "base_url": ""}],
                "default_model_id": "o3-deep-research",
                "max_completion_tokens": 32768,
                "reasoning_effort": "medium",
            },
            "models": [],
        },
        config_data,
    )

    # Preserve original keys when masked values (containing '***') are sent back
    for i, p in enumerate(merged.get("model", {}).get("providers", [])):
        if "***" in (p.get("api_key") or ""):
            orig = current.get("model", {}).get("providers", [])
            if i < len(orig):
                p["api_key"] = orig[i].get("api_key", "")

    save_config(merged)

    return jsonify({"success": True})


# ===== Session History API =====


@app.route("/api/sessions", methods=["GET"])
def api_list_sessions():
    """Return paginated session list for sidebar"""
    try:
        limit = request.args.get("limit", 50, type=int)
        offset = request.args.get("offset", 0, type=int)
        sessions = list_sessions(limit=min(limit, 100), offset=offset)
        return jsonify(sessions)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions/<session_id>", methods=["GET"])
def api_get_session(session_id):
    """Return a single session with all events for replay"""
    try:
        session = get_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404
        return jsonify(session)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions/<session_id>/live")
def session_live_stream(session_id):
    """SSE stream: replay existing events from DB, then poll for new ones.
    Used for reconnecting to background sessions."""
    after_order = request.args.get("after_order", -1, type=int)

    def generate_live():
        nonlocal after_order

        # Send session_id first
        yield f"data: {json.dumps({'session_id': session_id})}\n\n"

        # Check session exists
        status_info = get_session_status(session_id)
        if not status_info:
            yield f"data: {json.dumps({'type': 'error', 'content': 'Session not found'})}\n\n"
            return

        # Replay existing events from DB (after the given order)
        existing_events = get_events_after(session_id, after_order)
        for evt_row in existing_events:
            yield f"data: {json.dumps(evt_row['event_data'])}\n\n"
            after_order = evt_row["event_order"]

        # If session already finished, send done and return
        if status_info["status"] != "running":
            yield f"data: {json.dumps({'done': True})}\n\n"
            return

        # Poll for new events until session ends
        while True:
            time.sleep(0.5)

            new_events = get_events_after(session_id, after_order)
            for evt_row in new_events:
                yield f"data: {json.dumps(evt_row['event_data'])}\n\n"
                after_order = evt_row["event_order"]

            status_info = get_session_status(session_id)
            if not status_info or status_info["status"] != "running":
                yield f"data: {json.dumps({'done': True})}\n\n"
                break

    return Response(
        stream_with_context(generate_live()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/sessions/<session_id>", methods=["DELETE"])
def api_delete_session(session_id):
    """Delete a session and its events"""
    try:
        deleted = db_delete_session(session_id)
        if not deleted:
            return jsonify({"error": "Session not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5080)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--debug", type=bool, default=True)
    args = parser.parse_args()

    print(f"Starting web UI at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)
