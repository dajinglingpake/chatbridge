from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import heapq
import io
import json
import mimetypes
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import qrcode
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse

from bridge_config import APP_DIR
from core.app_service import (
    list_codex_threads,
    read_codex_thread,
    run_named_action,
    schedule_named_action,
    submit_hub_task,
    switch_sender_current_session,
)
from core.dashboard import load_dashboard_state
from core.runtime_paths import STATE_DIR
from core.sessions import build_session_rows_page
from core.state_models import HubTask
from runtime_stack import HUB_STATE_PATH, get_runtime_snapshot, read_json

MOBILE_ACCESS_PATH = STATE_DIR / "mobile_access.json"
MOBILE_UPLOAD_ROOT = APP_DIR / ".runtime" / "uploads"
MOBILE_TOKEN_BYTES = 24
MOBILE_POLL_SECONDS = 1.0
MOBILE_HEARTBEAT_SECONDS = 15.0
MOBILE_TASK_LIMIT = 200
MOBILE_SESSION_TASK_LIMIT = 200
CODEX_THREAD_SESSION_PREFIX = "codex:"
CODEX_THREAD_CACHE_SECONDS = 5.0
CODEX_THREAD_PAGE_LIMIT = 50
CODEX_THREAD_MAX_PAGES = 100
_CODEX_THREADS_CACHE: dict[str, object] = {"loaded_at": 0.0, "payload": {"threads": [], "error": ""}}
_CODEX_THREAD_DETAIL_CACHE: dict[str, dict[str, object]] = {}
_CODEX_THREAD_DETAIL_INFLIGHT: set[str] = set()
_CODEX_THREAD_DETAIL_LOCK = threading.Lock()
_MOBILE_RUNTIME_CACHE: dict[str, object] = {"loaded_at": 0.0, "payload": {}}
_RAW_HUB_STATE_CACHE: dict[str, object] = {"signature": None, "payload": {}}
_RAW_STREAM_WINDOW_CACHE: dict[str, object] = {"key": None, "window": None}
_RAW_STREAM_INDEX_CACHE: dict[str, object] = {"key": None, "index": None}
_RAW_STREAM_SIDEBAR_CACHE: dict[str, object] = {"key": None, "state": None}
MOBILE_ASYNC_ACTIONS = {
    "restart",
    "restart-bridge",
    "restart-hub",
    "restart-onebot-runtime",
    "restart-qq-bridge",
    "restart-qq-stack",
}
MOBILE_ALLOWED_ACTIONS = {
    "start",
    "start-weixin",
    "stop",
    "restart",
    "restart-bridge",
    "restart-hub",
    "start-onebot-runtime",
    "stop-onebot-runtime",
    "restart-onebot-runtime",
    "start-qq-bridge",
    "stop-qq-bridge",
    "restart-qq-bridge",
    "restart-qq-stack",
    "emergency-stop",
}

@dataclass(frozen=True)
class StreamTaskWindow:
    tasks: list[HubTask]
    task_order: dict[str, int]
    counts: dict[str, int]
    session_task_counts: dict[str, int]

@dataclass(frozen=True)
class StreamRawTaskWindow:
    tasks: list[dict[str, object]]
    task_order: dict[str, int]
    counts: dict[str, int]
    session_task_counts: dict[str, int]

@dataclass(frozen=True)
class StreamRawTaskIndex:
    entries: list[tuple[tuple[tuple[int, float, str], int, str], int, dict[str, object]]]
    session_entries: dict[str, list[tuple[tuple[tuple[int, float, str], int, str], int, dict[str, object]]]]
    counts: dict[str, int]
    session_task_counts: dict[str, int]


def _state_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _stream_time_sort_key(value: object) -> tuple[int, float, str]:
    text = str(value or "").strip()
    if not text:
        return (1, 0.0, "")
    normalized = text.replace("Z", "+00:00")
    try:
        return (0, datetime.fromisoformat(normalized).timestamp(), text)
    except ValueError:
        pass
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return (0, datetime.strptime(text, fmt).timestamp(), text)
        except ValueError:
            continue
    return (1, 0.0, text)


def _parse_mobile_time(value: object, *, assume_utc_naive: bool = False) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if assume_utc_naive and parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed

def _mobile_display_time(value: object, *, assume_utc_naive: bool = False) -> str:
    text = str(value or "").strip()
    parsed = _parse_mobile_time(text, assume_utc_naive=assume_utc_naive)
    if parsed is None:
        return text
    if parsed.tzinfo is None:
        return parsed.isoformat(timespec="seconds")
    return parsed.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")

def _source_uses_utc_naive_time(source: object) -> bool:
    return str(source or "").strip() != "codex-app-server"

def _display_activity_items(items: list[dict[str, object]], *, assume_utc_naive: bool) -> list[dict[str, object]]:
    displayed: list[dict[str, object]] = []
    for item in items:
        copied = dict(item)
        if copied.get("at"):
            copied["at"] = _mobile_display_time(copied.get("at"), assume_utc_naive=assume_utc_naive)
        displayed.append(copied)
    return displayed

def build_mobile_access_url(*, host: str, port: int) -> str:
    token = _load_or_create_access_token()
    access_host = host if host not in {"", "0.0.0.0", "127.0.0.1", "localhost", "::1"} else _detect_lan_ip()
    return f"http://{access_host}:{port}/mobile-ui?token={token}"


def build_mobile_qr_data_url(value: str) -> str:
    return _qr_data_url(value)

def is_mobile_access_authorized(token: str) -> bool:
    return _is_authorized_token(token)


def stream_hub_state_file_signature() -> tuple[int, int]:
    try:
        stat = HUB_STATE_PATH.stat()
    except OSError:
        return (-1, -1)
    return int(stat.st_mtime_ns), int(stat.st_size)


def _load_raw_hub_state() -> dict[str, object]:
    signature = stream_hub_state_file_signature()
    if _RAW_HUB_STATE_CACHE.get("signature") == signature:
        payload = _RAW_HUB_STATE_CACHE.get("payload")
        return payload if isinstance(payload, dict) else {}
    payload = read_json(HUB_STATE_PATH)
    cached_payload = dict(payload) if isinstance(payload, dict) else {}
    _RAW_HUB_STATE_CACHE["signature"] = signature
    _RAW_HUB_STATE_CACHE["payload"] = cached_payload
    _RAW_STREAM_WINDOW_CACHE["key"] = None
    _RAW_STREAM_WINDOW_CACHE["window"] = None
    _RAW_STREAM_SIDEBAR_CACHE["key"] = None
    _RAW_STREAM_SIDEBAR_CACHE["state"] = None
    return cached_payload

def _mobile_runtime_snapshot() -> dict[str, object]:
    now = time.monotonic()
    loaded_at = float(_MOBILE_RUNTIME_CACHE.get("loaded_at") or 0.0)
    payload = _MOBILE_RUNTIME_CACHE.get("payload")
    if isinstance(payload, dict) and now - loaded_at <= 2.0:
        return dict(payload)
    snapshot = get_runtime_snapshot(
        include_agent_processes=False,
        include_qq_login_status=False,
        discover_missing_processes=False,
    ).to_dict()
    _MOBILE_RUNTIME_CACHE["loaded_at"] = now
    _MOBILE_RUNTIME_CACHE["payload"] = snapshot
    return dict(snapshot)


def build_mobile_state_snapshot(
    *,
    selected_session_name: str = "",
    task_limit: int = MOBILE_TASK_LIMIT,
    session_task_limit: int = MOBILE_SESSION_TASK_LIMIT,
    include_codex_threads: bool = False,
    codex_threads_cursor: str = "",
    codex_threads_archived: bool = False,
    include_sessions: bool = False,
    include_senders: bool = False,
    session_page: int = 1,
    session_limit: int = 50,
) -> dict[str, object]:
    return _build_mobile_state(
        selected_session_name=selected_session_name,
        task_limit=task_limit,
        session_task_limit=session_task_limit,
        include_codex_threads=include_codex_threads,
        codex_threads_cursor=codex_threads_cursor,
        codex_threads_archived=codex_threads_archived,
        include_sessions=include_sessions,
        include_senders=include_senders,
        session_page=session_page,
        session_limit=session_limit,
    )


def build_stream_state_snapshot(
    *,
    selected_session_name: str = "",
    task_limit: int = 1,
    session_task_limit: int = MOBILE_SESSION_TASK_LIMIT,
) -> dict[str, object]:
    return _build_stream_state(
        selected_session_name=selected_session_name,
        task_limit=task_limit,
        session_task_limit=session_task_limit,
    )

def build_stream_signature_snapshot(
    *,
    selected_session_name: str = "",
    task_limit: int = 1,
    session_task_limit: int = MOBILE_SESSION_TASK_LIMIT,
) -> tuple:
    raw_state = _load_raw_hub_state()
    window = _stream_raw_window_from_state(
        raw_state,
        selected_session_name=selected_session_name,
        task_limit=task_limit,
        session_task_limit=session_task_limit,
    )
    task_parts = [_raw_hub_task_signature_part(task) for task in window.tasks]
    selected_codex_thread_id = codex_thread_id_from_session_name(selected_session_name)
    if selected_codex_thread_id:
        selected_limit = _safe_limit(session_task_limit, MOBILE_SESSION_TASK_LIMIT)
        codex_task_parts, codex_turn_count = _codex_thread_signature_parts_cached(selected_codex_thread_id, limit=selected_limit)
        window.session_task_counts[codex_thread_session_name(selected_codex_thread_id)] = codex_turn_count
        task_parts = list(codex_task_parts) + task_parts

    selected_session = selected_session_name.strip()
    if selected_session:
        counts_part: tuple[tuple[str, str], ...] = ()
        session_counts_part = ((selected_session, str(window.session_task_counts.get(selected_session, 0))),)
    else:
        counts_part = tuple(sorted((str(key), str(value)) for key, value in window.counts.items()))
        session_counts_part = tuple(sorted((str(key), str(value)) for key, value in window.session_task_counts.items()))

    return (
        counts_part,
        session_counts_part,
        tuple(task_parts),
    )

def _copy_stream_sidebar_state(state: dict[str, object]) -> dict[str, object]:
    copied = dict(state)
    for key in ("codex_threads", "agents", "sessions", "senders", "tasks"):
        value = copied.get(key)
        if isinstance(value, list):
            copied[key] = [dict(item) if isinstance(item, dict) else item for item in value]
    for key in ("counts", "session_task_counts", "selected_codex_thread"):
        value = copied.get(key)
        if isinstance(value, dict):
            copied[key] = dict(value)
    return copied

def build_stream_sidebar_state_snapshot(
    *,
    task_limit: int = 80,
    include_codex_threads: bool = False,
) -> dict[str, object]:
    raw_state = _load_raw_hub_state()
    raw_tasks = raw_state.get("tasks")
    safe_task_limit = _safe_limit(task_limit, MOBILE_TASK_LIMIT)
    raw_task_count = len(raw_tasks) if isinstance(raw_tasks, list) else 0
    cache_key = (id(raw_state), raw_task_count, safe_task_limit)
    if not include_codex_threads:
        cached_state = _RAW_STREAM_SIDEBAR_CACHE.get("state")
        if _RAW_STREAM_SIDEBAR_CACHE.get("key") == cache_key and isinstance(cached_state, dict):
            return _copy_stream_sidebar_state(cached_state)
    counts = {"total": 0, "queued": 0, "running": 0, "failed": 0, "succeeded": 0}
    session_task_counts: dict[str, int] = {}
    latest_by_session: dict[str, tuple[tuple[tuple[int, float, str], int, str], int, dict[str, object]]] = {}
    time_key_cache: dict[str, tuple[int, float, str]] = {}
    task_items = raw_tasks if isinstance(raw_tasks, list) else []
    for index, raw in enumerate(task_items):
        if not isinstance(raw, dict):
            continue
        task_id = _raw_clean_text(raw, "id")
        if not task_id:
            continue
        counts["total"] += 1
        status = _raw_clean_text(raw, "status", "queued") or "queued"
        if status in counts:
            counts[status] += 1
        session_name = _raw_task_session_name(raw)
        session_task_counts[session_name] = session_task_counts.get(session_name, 0) + 1
        created_at = _raw_clean_text(raw, "created_at")
        time_key = time_key_cache.get(created_at)
        if time_key is None:
            time_key = _stream_time_sort_key(created_at)
            time_key_cache[created_at] = time_key
        entry = ((time_key, index, task_id), index, raw)
        if session_name not in latest_by_session or entry[0] > latest_by_session[session_name][0]:
            latest_by_session[session_name] = entry
    selected_entries = heapq.nlargest(safe_task_limit, latest_by_session.values(), key=lambda item: item[0])
    oldest_first = sorted(selected_entries, key=lambda item: item[0])
    visible_session_task_counts = {
        _raw_task_session_name(task): session_task_counts.get(_raw_task_session_name(task), 0)
        for _key, _index, task in oldest_first
    }
    task_order = {
        _raw_clean_text(task, "id"): order
        for order, (_key, _index, task) in enumerate(oldest_first, start=1)
        if _raw_clean_text(task, "id")
    }
    state = {
        "updated_at": _state_now(),
        "counts": counts,
        "task_limit": safe_task_limit,
        "session_task_limit": 1,
        "session_task_counts": visible_session_task_counts,
        "session_total_count": len(session_task_counts),
        "codex_threads": [],
        "codex_threads_error": "",
        "codex_threads_next_cursor": "",
        "codex_threads_backwards_cursor": "",
        "codex_threads_archived": False,
        "selected_codex_thread": {},
        "agents": _raw_agent_payloads(raw_state),
        "sessions": [],
        "senders": [],
        "tasks": [
            _raw_task_payload(task, stream_order=task_order.get(_raw_clean_text(task, "id"), 0))
            for _key, _index, task in oldest_first
        ],
    }
    if include_codex_threads:
        codex_threads_payload = load_codex_threads_page()
        codex_threads = codex_threads_payload.get("threads") if isinstance(codex_threads_payload.get("threads"), list) else []
        state["codex_threads"] = codex_threads
        state["codex_threads_error"] = str(codex_threads_payload.get("error") or "")
        state["codex_threads_next_cursor"] = str(codex_threads_payload.get("next_cursor") or "")
        state["codex_threads_backwards_cursor"] = str(codex_threads_payload.get("backwards_cursor") or "")
        state["codex_threads_archived"] = bool(codex_threads_payload.get("archived"))
    if not include_codex_threads:
        _RAW_STREAM_SIDEBAR_CACHE["key"] = cache_key
        _RAW_STREAM_SIDEBAR_CACHE["state"] = _copy_stream_sidebar_state(state)
    return state

def codex_thread_session_name(thread_id: str) -> str:
    cleaned_thread_id = str(thread_id or "").strip()
    return f"{CODEX_THREAD_SESSION_PREFIX}{cleaned_thread_id}" if cleaned_thread_id else ""


def codex_thread_id_from_session_name(session_name: str) -> str:
    cleaned_session_name = str(session_name or "").strip()
    if not cleaned_session_name.startswith(CODEX_THREAD_SESSION_PREFIX):
        return ""
    return cleaned_session_name[len(CODEX_THREAD_SESSION_PREFIX):].strip()

def install_mobile_routes(app: Any, *, host: str, port: int) -> None:
    if getattr(app, "_chatbridge_mobile_routes_installed", False):
        return
    setattr(app, "_chatbridge_mobile_routes_installed", True)

    @app.get("/mobile-link")
    async def mobile_link(request: Request) -> HTMLResponse:
        token = _load_or_create_access_token()
        mobile_url = _mobile_url(request, host=host, port=port, token=token)
        qr_data_url = build_mobile_qr_data_url(mobile_url)
        return HTMLResponse(_mobile_link_html(mobile_url, qr_data_url))

    @app.get("/mobile")
    async def mobile_page(request: Request):
        token = str(request.query_params.get("token") or "").strip()
        if not _is_authorized_token(token):
            return HTMLResponse(_mobile_denied_html(), status_code=401)
        return RedirectResponse(url=f"/mobile-ui?token={token}", status_code=307)

    @app.get("/api/mobile/state")
    async def mobile_state(request: Request) -> JSONResponse:
        if not _is_authorized_request(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return JSONResponse(
            {
                "ok": True,
                "data": _build_mobile_state(
                    selected_session_name=str(request.query_params.get("session") or "").strip(),
                    task_limit=_query_int(request, "task_limit", MOBILE_TASK_LIMIT),
                    session_task_limit=_query_int(request, "session_task_limit", MOBILE_SESSION_TASK_LIMIT),
                    include_codex_threads=_query_bool(request, "include_codex_threads", False),
                    codex_threads_cursor=str(request.query_params.get("codex_threads_cursor") or "").strip(),
                    codex_threads_archived=_query_bool(request, "codex_threads_archived", False),
                    include_sessions=_query_bool(request, "include_sessions", False),
                    include_senders=_query_bool(request, "include_senders", False),
                    session_page=_query_int(request, "session_page", 1),
                    session_limit=_query_int(request, "session_limit", 50),
                ),
            }
        )

    @app.get("/api/mobile/diagnostics")
    async def mobile_diagnostics(request: Request) -> JSONResponse:
        if not _is_authorized_request(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return JSONResponse(
            {
                "ok": True,
                "data": _build_mobile_diagnostics(
                    include_logs=_query_bool(request, "include_logs", False),
                    include_external=_query_bool(request, "include_external", False),
                ),
            }
        )

    @app.get("/mobile-upload/{path:path}")
    async def mobile_upload(path: str):
        root = MOBILE_UPLOAD_ROOT.resolve()
        target = (root / path).resolve()
        if not _is_relative_to(target, root) or not target.is_file() or not _is_image_path(target):
            return PlainTextResponse("not found", status_code=404)
        media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return FileResponse(target, media_type=media_type)

    @app.get("/api/mobile/events")
    async def mobile_events(request: Request) -> StreamingResponse:
        if not _is_authorized_request(request):
            return StreamingResponse(_single_sse("error", {"message": "unauthorized"}), media_type="text/event-stream", status_code=401)
        task_id = str(request.query_params.get("task_id") or "").strip()
        selected_session = str(request.query_params.get("session") or "").strip()
        return StreamingResponse(_event_stream(task_id, selected_session_name=selected_session), media_type="text/event-stream")

    @app.post("/api/mobile/tasks")
    async def mobile_submit_task(request: Request) -> JSONResponse:
        if not _is_authorized_request(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "invalid payload"}, status_code=400)
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return JSONResponse({"ok": False, "error": "请输入要发送的内容"}, status_code=400)
        result = submit_hub_task(
            agent_id=str(payload.get("agent_id") or "main"),
            prompt=prompt,
            session_name=str(payload.get("session_name") or ""),
            backend=str(payload.get("backend") or ""),
            source="mobile-web",
            sender_id=str(payload.get("sender_id") or ""),
            workdir=str(payload.get("workdir") or ""),
            session_id=str(payload.get("session_id") or ""),
            images=[str(item).strip() for item in (payload.get("images") or []) if str(item).strip()] if isinstance(payload.get("images"), list) else [],
        )
        return JSONResponse({"ok": result.ok, "message": result.message})

    @app.post("/api/mobile/actions")
    async def mobile_run_action(request: Request) -> JSONResponse:
        if not _is_authorized_request(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "invalid payload"}, status_code=400)
        action = str(payload.get("action") or "").strip()
        if action not in MOBILE_ALLOWED_ACTIONS:
            return JSONResponse({"ok": False, "error": f"不支持的操作：{action}"}, status_code=400)
        result = schedule_named_action(action, delay_seconds=1.0) if action in MOBILE_ASYNC_ACTIONS else run_named_action(action)
        return JSONResponse({"ok": result.ok, "message": result.message, "action": action})

    @app.post("/api/mobile/senders/current-session")
    async def mobile_set_current_session(request: Request) -> JSONResponse:
        if not _is_authorized_request(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "invalid payload"}, status_code=400)
        result = _set_sender_current_session(
            sender_id=str(payload.get("sender_id") or ""),
            session_name=str(payload.get("session_name") or ""),
        )
        status_code = 200 if result["ok"] else 400
        return JSONResponse(result, status_code=status_code)

    @app.get("/api/mobile/access")
    async def mobile_access(request: Request) -> JSONResponse:
        if not _is_local_request(request):
            return JSONResponse({"ok": False, "error": "local only"}, status_code=403)
        token = _load_or_create_access_token()
        return JSONResponse({"ok": True, "url": _mobile_url(request, host=host, port=port, token=token)})


def _load_or_create_access_token() -> str:
    if MOBILE_ACCESS_PATH.exists():
        try:
            payload = json.loads(MOBILE_ACCESS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        token = str(payload.get("token") or "").strip() if isinstance(payload, dict) else ""
        if token:
            return token
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(MOBILE_TOKEN_BYTES)
    MOBILE_ACCESS_PATH.write_text(json.dumps({"token": token}, ensure_ascii=False, indent=2), encoding="utf-8")
    return token


def _is_authorized_token(token: str) -> bool:
    expected = _load_or_create_access_token()
    return bool(token) and secrets.compare_digest(token, expected)


def _bearer_token(request: Request) -> str:
    auth = str(request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _query_bool(request: Request, name: str, default: bool = False) -> bool:
    raw = str(request.query_params.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}

def _query_int(request: Request, name: str, default: int) -> int:
    try:
        return int(str(request.query_params.get(name) or "").strip())
    except (TypeError, ValueError):
        return default

def _is_authorized_request(request: Request) -> bool:
    token = str(request.query_params.get("token") or "").strip() or _bearer_token(request)
    return _is_authorized_token(token)


def _is_local_request(request: Request) -> bool:
    client_host = request.client.host if request.client else ""
    return client_host in {"127.0.0.1", "::1", "localhost"}


def _detect_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _mobile_url(request: Request, *, host: str, port: int, token: str) -> str:
    request_host = request.url.hostname or ""
    request_port = request.url.port or port
    if request_host in {"", "0.0.0.0", "127.0.0.1", "localhost", "::1"}:
        request_host = _detect_lan_ip()
    return f"http://{request_host}:{request_port}/mobile-ui?token={token}"


def _qr_data_url(value: str) -> str:
    image = qrcode.make(value)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _compact_text(value: str, *, limit: int = 160) -> str:
    compact = " ".join(str(value or "").split())
    if not compact:
        return ""
    return compact if len(compact) <= limit else f"{compact[: limit - 1]}..."


def _latest_task_summary(task: HubTask | None) -> str:
    if task is None:
        return "暂无任务"
    if task.error.strip():
        return _compact_text(task.error, limit=180) or "任务报错"
    if task.progress_text.strip():
        return _compact_text(task.progress_text, limit=180)
    if task.output.strip():
        return _compact_text(task.output, limit=180)
    return _compact_text(task.prompt, limit=120) or "暂无输出"


def _raw_text(raw: dict[str, object], key: str, default: str = "") -> str:
    return str(raw.get(key) or default)


def _raw_clean_text(raw: dict[str, object], key: str, default: str = "") -> str:
    return str(raw.get(key) or default).strip()


def _raw_progress_seq(raw: dict[str, object]) -> int:
    try:
        return int(raw.get("progress_seq") or 0)
    except (TypeError, ValueError):
        return 0


def _raw_optional_percent(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        percent = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, percent))


def _raw_string_list(value: object) -> list[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _raw_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _latest_raw_task_summary(raw: dict[str, object]) -> str:
    error = _raw_text(raw, "error")
    if error.strip():
        return _compact_text(error, limit=180) or "任务报错"
    progress_text = _raw_text(raw, "progress_text")
    if progress_text.strip():
        return _compact_text(progress_text, limit=180)
    output = _raw_text(raw, "output")
    if output.strip():
        return _compact_text(output, limit=180)
    return _compact_text(_raw_text(raw, "prompt"), limit=120) or "暂无输出"


def _raw_task_activity_metadata(raw: dict[str, object]) -> dict[str, str]:
    metadata = {
        "task_id": _raw_clean_text(raw, "id"),
        "agent": _raw_clean_text(raw, "agent_id") or _raw_clean_text(raw, "agent_name") or "main",
        "backend": _raw_clean_text(raw, "backend"),
        "source": _raw_clean_text(raw, "source", "desktop") or "desktop",
        "session": _raw_clean_text(raw, "session_name") or "default",
        "status": _raw_clean_text(raw, "status", "queued") or "queued",
        "progress_seq": str(_raw_progress_seq(raw)),
    }
    context_left_percent = _raw_optional_percent(raw.get("context_left_percent"))
    if context_left_percent is not None:
        metadata["context_left_percent"] = str(context_left_percent)
    session_id = _raw_clean_text(raw, "session_id")
    if session_id:
        metadata["session_id"] = session_id
    workdir = _raw_clean_text(raw, "workdir")
    if workdir:
        metadata["workdir"] = workdir
    return {key: value for key, value in metadata.items() if str(value or "").strip()}


def _raw_task_activity_items(raw: dict[str, object]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    metadata = _raw_task_activity_metadata(raw)

    def append(event: str, activity_type: str, at: str, detail: str = "") -> None:
        cleaned_at = str(at or "").strip()
        if not cleaned_at:
            return
        items.append(
            {
                "event": event,
                "type": activity_type,
                "at": cleaned_at,
                "detail": _compact_text(detail, limit=220),
                "metadata": metadata,
            }
        )

    append("accepted", "system", _raw_clean_text(raw, "created_at"))
    append("running", "info", _raw_clean_text(raw, "started_at"))
    progress_text = _raw_text(raw, "progress_text")
    if _raw_progress_seq(raw) > 0 and progress_text.strip():
        append("progress", "info", _raw_clean_text(raw, "progress_at") or _raw_clean_text(raw, "started_at") or _raw_clean_text(raw, "created_at"), progress_text)
    status = _raw_clean_text(raw, "status")
    if status in {"succeeded", "failed", "canceled", "unknown_after_restart"}:
        activity_type = "success" if status == "succeeded" else "error" if status == "failed" else "info"
        detail = "" if status == "succeeded" else _raw_text(raw, "error")
        append(status, activity_type, _raw_clean_text(raw, "finished_at") or _raw_clean_text(raw, "progress_at") or _raw_clean_text(raw, "created_at"), detail)
    return items


def _raw_task_session_name(raw: dict[str, object]) -> str:
    return _raw_clean_text(raw, "session_name") or "default"


def _raw_task_window_key(index: int, raw: dict[str, object]) -> tuple[tuple[int, float, str], int, str]:
    return (_stream_time_sort_key(_raw_clean_text(raw, "created_at")), index, _raw_clean_text(raw, "id"))



def _build_stream_raw_task_window(
    tasks: list[dict[str, object]],
    *,
    selected_session_name: str = "",
    task_limit: int = 1,
    session_task_limit: int = MOBILE_SESSION_TASK_LIMIT,
) -> StreamRawTaskWindow:
    return _stream_raw_window_from_index(
        _build_stream_raw_task_index(tasks),
        selected_session_name=selected_session_name,
        task_limit=task_limit,
        session_task_limit=session_task_limit,
    )

def _build_stream_raw_task_index(tasks: list[dict[str, object]]) -> StreamRawTaskIndex:
    entries: list[tuple[tuple[tuple[int, float, str], int, str], int, dict[str, object]]] = []
    session_entries: dict[str, list[tuple[tuple[tuple[int, float, str], int, str], int, dict[str, object]]]] = {}
    counts = {"total": 0, "queued": 0, "running": 0, "failed": 0, "succeeded": 0}
    session_task_counts: dict[str, int] = {}

    for index, raw in enumerate(tasks):
        task_id = _raw_clean_text(raw, "id")
        if not task_id:
            continue
        counts["total"] += 1
        status = _raw_clean_text(raw, "status", "queued") or "queued"
        if status in counts:
            counts[status] += 1
        session_name = _raw_task_session_name(raw)
        session_task_counts[session_name] = session_task_counts.get(session_name, 0) + 1
        entry = (_raw_task_window_key(index, raw), index, raw)
        entries.append(entry)
        session_entries.setdefault(session_name, []).append(entry)

    entries.sort(key=lambda item: item[0])
    for session_items in session_entries.values():
        session_items.sort(key=lambda item: item[0])
    return StreamRawTaskIndex(
        entries=entries,
        session_entries=session_entries,
        counts=counts,
        session_task_counts=session_task_counts,
    )

def _raw_task_index_structure_key(tasks: list[dict[str, object]]) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (
            _raw_clean_text(raw, "id"),
            _raw_task_session_name(raw),
            _raw_clean_text(raw, "created_at"),
            _raw_clean_text(raw, "status", "queued") or "queued",
        )
        for raw in tasks
        if _raw_clean_text(raw, "id")
    )

def _refresh_stream_raw_task_index(index: StreamRawTaskIndex, tasks: list[dict[str, object]]) -> StreamRawTaskIndex:
    tasks_by_id = {_raw_clean_text(raw, "id"): raw for raw in tasks if _raw_clean_text(raw, "id")}

    def refresh_entry(entry):
        key, original_index, raw = entry
        return (key, original_index, tasks_by_id.get(_raw_clean_text(raw, "id"), raw))

    return StreamRawTaskIndex(
        entries=[refresh_entry(entry) for entry in index.entries],
        session_entries={
            session_name: [refresh_entry(entry) for entry in entries]
            for session_name, entries in index.session_entries.items()
        },
        counts=dict(index.counts),
        session_task_counts=dict(index.session_task_counts),
    )

def _stream_raw_window_from_index(
    task_index: StreamRawTaskIndex,
    *,
    selected_session_name: str = "",
    task_limit: int = 1,
    session_task_limit: int = MOBILE_SESSION_TASK_LIMIT,
) -> StreamRawTaskWindow:
    global_limit = _safe_optional_limit(task_limit, MOBILE_TASK_LIMIT)
    selected_limit = _safe_limit(session_task_limit, MOBILE_SESSION_TASK_LIMIT)
    selected_session = selected_session_name.strip()
    global_entries = task_index.entries[-global_limit:] if global_limit else []
    selected_entries = task_index.session_entries.get(selected_session, [])[-selected_limit:] if selected_session else []

    visible_entries_by_id: dict[str, tuple[tuple[str, int, str], int, dict[str, object]]] = {}
    for entry in [*global_entries, *selected_entries]:
        task_id = _raw_clean_text(entry[2], "id")
        if task_id:
            visible_entries_by_id[task_id] = entry

    oldest_first = sorted(visible_entries_by_id.values(), key=lambda item: item[0])
    task_order = {
        _raw_clean_text(task, "id"): order
        for order, (_key, _index, task) in enumerate(oldest_first, start=1)
        if _raw_clean_text(task, "id")
    }
    ordered_tasks = [task for _key, _index, task in oldest_first]
    return StreamRawTaskWindow(
        tasks=ordered_tasks,
        task_order=task_order,
        counts=dict(task_index.counts),
        session_task_counts=dict(task_index.session_task_counts),
    )


def _copy_stream_raw_task_window(window: StreamRawTaskWindow) -> StreamRawTaskWindow:
    return StreamRawTaskWindow(
        tasks=list(window.tasks),
        task_order=dict(window.task_order),
        counts=dict(window.counts),
        session_task_counts=dict(window.session_task_counts),
    )


def _stream_raw_index_from_state(raw_state: dict[str, object]) -> StreamRawTaskIndex:
    tasks = _raw_hub_tasks(raw_state)
    key = _raw_task_index_structure_key(tasks)
    cached_index = _RAW_STREAM_INDEX_CACHE.get("index")
    if _RAW_STREAM_INDEX_CACHE.get("key") == key and isinstance(cached_index, StreamRawTaskIndex):
        return _refresh_stream_raw_task_index(cached_index, tasks)
    task_index = _build_stream_raw_task_index(tasks)
    _RAW_STREAM_INDEX_CACHE["key"] = key
    _RAW_STREAM_INDEX_CACHE["index"] = task_index
    _RAW_STREAM_WINDOW_CACHE["key"] = None
    _RAW_STREAM_WINDOW_CACHE["window"] = None
    return task_index

def _stream_raw_window_from_state(
    raw_state: dict[str, object],
    *,
    selected_session_name: str = "",
    task_limit: int = 1,
    session_task_limit: int = MOBILE_SESSION_TASK_LIMIT,
) -> StreamRawTaskWindow:
    raw_tasks = raw_state.get("tasks")
    raw_task_count = len(raw_tasks) if isinstance(raw_tasks, list) else 0
    key = (
        id(raw_state),
        raw_task_count,
        selected_session_name.strip(),
        _safe_optional_limit(task_limit, MOBILE_TASK_LIMIT),
        _safe_limit(session_task_limit, MOBILE_SESSION_TASK_LIMIT),
    )
    cached_window = _RAW_STREAM_WINDOW_CACHE.get("window")
    if _RAW_STREAM_WINDOW_CACHE.get("key") == key and isinstance(cached_window, StreamRawTaskWindow):
        return _copy_stream_raw_task_window(cached_window)
    window = _stream_raw_window_from_index(
        _stream_raw_index_from_state(raw_state),
        selected_session_name=selected_session_name,
        task_limit=task_limit,
        session_task_limit=session_task_limit,
    )
    _RAW_STREAM_WINDOW_CACHE["key"] = key
    _RAW_STREAM_WINDOW_CACHE["window"] = _copy_stream_raw_task_window(window)
    return window


def _raw_task_payload(raw: dict[str, object], *, stream_order: int = 0) -> dict[str, object]:
    images = _raw_string_list(raw.get("images"))
    agent_id = _raw_clean_text(raw, "agent_id") or _raw_clean_text(raw, "agent_name") or "main"
    source = _raw_clean_text(raw, "source", "desktop") or "desktop"
    assume_utc_naive = _source_uses_utc_naive_time(source)
    return {
        "id": _raw_clean_text(raw, "id"),
        "agent_id": agent_id,
        "agent_name": _raw_clean_text(raw, "agent_name") or agent_id,
        "backend": _raw_clean_text(raw, "backend", "codex") or "codex",
        "source": source,
        "sender_id": _raw_clean_text(raw, "sender_id"),
        "session_id": _raw_clean_text(raw, "session_id"),
        "session_name": _raw_task_session_name(raw),
        "workdir": _raw_clean_text(raw, "workdir"),
        "model": _raw_clean_text(raw, "model"),
        "status": _raw_clean_text(raw, "status", "queued") or "queued",
        "stream_order": stream_order,
        "created_at": _mobile_display_time(_raw_clean_text(raw, "created_at"), assume_utc_naive=assume_utc_naive),
        "started_at": _mobile_display_time(_raw_clean_text(raw, "started_at"), assume_utc_naive=assume_utc_naive),
        "finished_at": _mobile_display_time(_raw_clean_text(raw, "finished_at"), assume_utc_naive=assume_utc_naive),
        "prompt": _raw_text(raw, "prompt"),
        "images": images,
        "image_previews": [_image_preview_payload(item) for item in images],
        "output": _raw_text(raw, "output"),
        "error": _raw_text(raw, "error"),
        "progress_text": _raw_text(raw, "progress_text"),
        "progress_at": _mobile_display_time(_raw_clean_text(raw, "progress_at"), assume_utc_naive=assume_utc_naive),
        "progress_seq": _raw_progress_seq(raw),
        "context_left_percent": _raw_optional_percent(raw.get("context_left_percent")),
        "activity_items": _display_activity_items(_raw_task_activity_items(raw), assume_utc_naive=assume_utc_naive),
        "summary": _latest_raw_task_summary(raw),
    }


def _raw_hub_task_signature_part(raw: dict[str, object]) -> tuple[str, str, str, str, str, int, int, int, str]:
    return (
        _raw_clean_text(raw, "id"),
        _raw_clean_text(raw, "status"),
        str(_raw_progress_seq(raw)),
        _raw_clean_text(raw, "progress_at"),
        _raw_clean_text(raw, "finished_at"),
        len(_raw_text(raw, "progress_text")),
        len(_raw_text(raw, "output")),
        len(_raw_text(raw, "error")),
        str(_raw_optional_percent(raw.get("context_left_percent")) or ""),
    )


def _raw_hub_tasks(raw_state: dict[str, object]) -> list[dict[str, object]]:
    raw_tasks = raw_state.get("tasks")
    if not isinstance(raw_tasks, list):
        return []
    tasks: list[dict[str, object]] = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("id") or "").strip()
        if task_id:
            tasks.append(dict(item))
    return tasks


def _raw_agent_payloads(raw_state: dict[str, object]) -> list[dict[str, object]]:
    raw_agents = raw_state.get("agents")
    if not isinstance(raw_agents, list):
        return []
    agents: list[dict[str, object]] = []
    for item in raw_agents:
        if not isinstance(item, dict):
            continue
        agent_id = str(item.get("id") or "").strip()
        if not agent_id:
            continue
        runtime = item.get("runtime") if isinstance(item.get("runtime"), dict) else {}
        agents.append(
            {
                "id": agent_id,
                "name": str(item.get("name") or agent_id).strip() or agent_id,
                "backend": str(item.get("backend") or "").strip(),
                "model": str(item.get("model") or "").strip(),
                "status": str(runtime.get("status") or "idle").strip() if isinstance(runtime, dict) else "idle",
                "queue_size": _raw_int(runtime.get("queue_size")) if isinstance(runtime, dict) else 0,
            }
        )
    return agents


def _task_activity_metadata(task: HubTask) -> dict[str, str]:
    metadata = {
        "task_id": task.id,
        "agent": task.agent_id,
        "backend": task.backend,
        "source": task.source,
        "session": task.session_name or "default",
        "status": task.status or "queued",
        "progress_seq": str(task.progress_seq or 0),
    }
    if task.context_left_percent is not None:
        metadata["context_left_percent"] = str(task.context_left_percent)
    if task.session_id.strip():
        metadata["session_id"] = task.session_id.strip()
    if task.workdir.strip():
        metadata["workdir"] = task.workdir.strip()
    return {key: value for key, value in metadata.items() if str(value or "").strip()}


def _task_activity_items(task: HubTask) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    metadata = _task_activity_metadata(task)

    def append(event: str, activity_type: str, at: str, detail: str = "") -> None:
        cleaned_at = str(at or "").strip()
        if not cleaned_at:
            return
        items.append(
            {
                "event": event,
                "type": activity_type,
                "at": cleaned_at,
                "detail": _compact_text(detail, limit=220),
                "metadata": metadata,
            }
        )

    append("accepted", "system", task.created_at)
    append("running", "info", task.started_at)
    if task.progress_seq > 0 and task.progress_text.strip():
        append("progress", "info", task.progress_at or task.started_at or task.created_at, task.progress_text)
    status = (task.status or "").strip()
    if status in {"succeeded", "failed", "canceled", "unknown_after_restart"}:
        activity_type = "success" if status == "succeeded" else "error" if status == "failed" else "info"
        detail = "" if status == "succeeded" else task.error
        append(status, activity_type, task.finished_at or task.progress_at or task.created_at, detail)
    return items


def _hub_task_order_lookup(tasks: list[HubTask]) -> dict[str, int]:
    ordered_tasks = sorted(
        enumerate(tasks),
        key=lambda item: (_stream_time_sort_key(item[1].created_at), item[0]),
    )
    return {task.id: index for index, (_original_index, task) in enumerate(ordered_tasks, start=1) if task.id}

def _hub_task_sort_key(task: HubTask, order_lookup: dict[str, int]) -> tuple[tuple[int, float, str], int, str]:
    return (_stream_time_sort_key(task.created_at), order_lookup.get(task.id, 0), task.id)

def _hub_task_window_key(index: int, task: HubTask) -> tuple[tuple[int, float, str], int, str]:
    return (_stream_time_sort_key(task.created_at), index, str(task.id or ""))

def _push_recent_task(
    heap: list[tuple[tuple[tuple[int, float, str], int, str], int, HubTask]],
    *,
    limit: int,
    key: tuple[tuple[int, float, str], int, str],
    index: int,
    task: HubTask,
) -> None:
    if limit <= 0:
        return
    entry = (key, index, task)
    if len(heap) < limit:
        heapq.heappush(heap, entry)
        return
    if key > heap[0][0]:
        heapq.heapreplace(heap, entry)

def _build_stream_task_window(
    tasks: list[HubTask],
    *,
    selected_session_name: str = "",
    task_limit: int = 1,
    session_task_limit: int = MOBILE_SESSION_TASK_LIMIT,
) -> StreamTaskWindow:
    global_limit = _safe_optional_limit(task_limit, MOBILE_TASK_LIMIT)
    selected_limit = _safe_limit(session_task_limit, MOBILE_SESSION_TASK_LIMIT)
    selected_session = selected_session_name.strip()
    global_heap: list[tuple[tuple[tuple[int, float, str], int, str], int, HubTask]] = []
    selected_heap: list[tuple[tuple[tuple[int, float, str], int, str], int, HubTask]] = []
    counts = {"total": 0, "queued": 0, "running": 0, "failed": 0, "succeeded": 0}
    session_task_counts: dict[str, int] = {}

    for index, task in enumerate(tasks):
        counts["total"] += 1
        if task.status in counts:
            counts[task.status] += 1
        session_name = _session_name_for_task(task)
        session_task_counts[session_name] = session_task_counts.get(session_name, 0) + 1
        key = _hub_task_window_key(index, task)
        _push_recent_task(global_heap, limit=global_limit, key=key, index=index, task=task)
        if selected_session and session_name == selected_session:
            _push_recent_task(selected_heap, limit=selected_limit, key=key, index=index, task=task)

    visible_entries_by_id: dict[str, tuple[tuple[tuple[int, float, str], int, str], int, HubTask]] = {}
    fallback_index = 0
    for entry in [*global_heap, *selected_heap]:
        task_id = str(entry[2].id or "")
        if not task_id:
            task_id = f"__task_index_{entry[1]}_{fallback_index}"
            fallback_index += 1
        visible_entries_by_id[task_id] = entry

    oldest_first = sorted(visible_entries_by_id.values(), key=lambda item: item[0])
    task_order = {
        str(task.id): order
        for order, (_key, _index, task) in enumerate(oldest_first, start=1)
        if str(task.id or "")
    }
    ordered_tasks = [task for _key, _index, task in oldest_first]
    return StreamTaskWindow(
        tasks=ordered_tasks,
        task_order=task_order,
        counts=counts,
        session_task_counts=session_task_counts,
    )

def _task_payload(task: HubTask, *, stream_order: int = 0) -> dict[str, object]:
    assume_utc_naive = _source_uses_utc_naive_time(task.source)
    return {
        "id": task.id,
        "agent_id": task.agent_id,
        "agent_name": task.agent_name or task.agent_id,
        "backend": task.backend,
        "source": task.source,
        "sender_id": task.sender_id,
        "session_id": task.session_id,
        "session_name": task.session_name or "default",
        "workdir": task.workdir,
        "model": task.model,
        "status": task.status or "queued",
        "stream_order": stream_order,
        "created_at": _mobile_display_time(task.created_at, assume_utc_naive=assume_utc_naive),
        "started_at": _mobile_display_time(task.started_at, assume_utc_naive=assume_utc_naive),
        "finished_at": _mobile_display_time(task.finished_at, assume_utc_naive=assume_utc_naive),
        "prompt": task.prompt,
        "images": list(task.images),
        "image_previews": [_image_preview_payload(item) for item in task.images],
        "output": task.output,
        "error": task.error,
        "progress_text": task.progress_text,
        "progress_at": _mobile_display_time(task.progress_at, assume_utc_naive=assume_utc_naive),
        "progress_seq": task.progress_seq,
        "context_left_percent": task.context_left_percent,
        "activity_items": _display_activity_items(_task_activity_items(task), assume_utc_naive=assume_utc_naive),
        "summary": _latest_task_summary(task),
    }


def _hub_task_signature_part(task: HubTask) -> tuple[str, str, str, str, str, int, int, int, str]:
    return (
        str(task.id or ""),
        str(task.status or ""),
        str(task.progress_seq or ""),
        str(task.progress_at or ""),
        str(task.finished_at or ""),
        len(str(task.progress_text or "")),
        len(str(task.output or "")),
        len(str(task.error or "")),
        str(task.context_left_percent or ""),
    )

def _payload_activity_signature(value: object) -> tuple[int, str, str, int]:
    items = value if isinstance(value, list) else []
    activity_items = [item for item in items if isinstance(item, dict)]
    latest = activity_items[-1] if activity_items else {}
    return (
        len(activity_items),
        str(latest.get("event") or ""),
        str(latest.get("at") or ""),
        len(str(latest.get("detail") or "")),
    )


def _payload_task_signature_part(task: dict[str, object]) -> tuple[str, str, str, str, str, int, int, int, str, tuple[int, str, str, int]]:
    return (
        str(task.get("id") or ""),
        str(task.get("status") or ""),
        str(task.get("progress_seq") or ""),
        str(task.get("progress_at") or ""),
        str(task.get("finished_at") or ""),
        len(str(task.get("progress_text") or "")),
        len(str(task.get("output") or "")),
        len(str(task.get("error") or "")),
        str(task.get("context_left_percent") or ""),
        _payload_activity_signature(task.get("activity_items")),
    )

def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

def _is_image_path(path: Path) -> bool:
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

def _image_preview_payload(value: str) -> dict[str, str]:
    cleaned = str(value or "").strip()
    label = cleaned.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or cleaned
    lowered = cleaned.lower()
    if lowered.startswith(("data:image/", "http://", "https://")):
        return {"source": cleaned, "label": label}
    try:
        path = Path(cleaned).expanduser().resolve()
    except OSError:
        return {"source": "", "label": label}
    root = MOBILE_UPLOAD_ROOT.resolve()
    if _is_relative_to(path, root) and _is_image_path(path):
        rel = path.relative_to(root).as_posix()
        return {"source": f"/mobile-upload/{quote(rel)}", "label": label}
    return {"source": "", "label": label}

def _session_name_for_task(task: HubTask) -> str:
    return task.session_name or "default"

def _safe_limit(value: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(1, parsed)

def _safe_optional_limit(value: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, parsed)

def _select_mobile_tasks(
    tasks: list[HubTask],
    *,
    selected_session_name: str = "",
    task_limit: int = MOBILE_TASK_LIMIT,
    session_task_limit: int = MOBILE_SESSION_TASK_LIMIT,
) -> list[HubTask]:
    global_limit = _safe_optional_limit(task_limit, MOBILE_TASK_LIMIT)
    selected_limit = _safe_limit(session_task_limit, MOBILE_SESSION_TASK_LIMIT)
    selected_session = selected_session_name.strip()
    selected_tasks: list[HubTask] = []
    if selected_session:
        for task in tasks:
            if _session_name_for_task(task) != selected_session:
                continue
            selected_tasks.append(task)
            if len(selected_tasks) >= selected_limit:
                break

    selected_ids: set[str] = set()
    merged_tasks: list[HubTask] = []
    for task in [*tasks[:global_limit], *selected_tasks]:
        if task.id in selected_ids:
            continue
        selected_ids.add(task.id)
        merged_tasks.append(task)
    return merged_tasks

def _build_mobile_state(
    *,
    selected_session_name: str = "",
    task_limit: int = MOBILE_TASK_LIMIT,
    session_task_limit: int = MOBILE_SESSION_TASK_LIMIT,
    include_codex_threads: bool = False,
    codex_threads_cursor: str = "",
    codex_threads_archived: bool = False,
    include_sessions: bool = False,
    include_senders: bool = False,
    session_page: int = 1,
    session_limit: int = 50,
) -> dict[str, object]:
    if not include_sessions and not include_senders:
        raw_state = _load_raw_hub_state()
        window = _stream_raw_window_from_state(
            raw_state,
            selected_session_name=selected_session_name,
            task_limit=task_limit,
            session_task_limit=session_task_limit,
        )
        selected_codex_thread_id = codex_thread_id_from_session_name(selected_session_name)
        codex_threads_payload = load_codex_threads_page(
            cursor=codex_threads_cursor,
            archived=codex_threads_archived,
        ) if include_codex_threads else {"threads": [], "error": "", "next_cursor": "", "backwards_cursor": "", "archived": False}
        codex_threads = codex_threads_payload.get("threads") if isinstance(codex_threads_payload.get("threads"), list) else []
        task_payloads = [
            _raw_task_payload(task, stream_order=window.task_order.get(_raw_clean_text(task, "id"), 0))
            for task in window.tasks
        ]
        selected_codex_thread: dict[str, object] = {}
        if selected_codex_thread_id:
            selected_codex_thread = _load_codex_thread_cached(selected_codex_thread_id)
            selected_limit = _safe_limit(session_task_limit, MOBILE_SESSION_TASK_LIMIT)
            codex_thread_tasks = _codex_thread_task_payloads_cached(selected_codex_thread_id, selected_codex_thread, limit=selected_limit)
            window.session_task_counts[codex_thread_session_name(selected_codex_thread_id)] = _codex_thread_turn_count_cached(selected_codex_thread_id, selected_codex_thread)
            task_payloads = codex_thread_tasks + task_payloads
        return {
            "updated_at": _state_now(),
            "runtime": _mobile_runtime_snapshot(),
            "counts": window.counts,
            "task_limit": _safe_optional_limit(task_limit, MOBILE_TASK_LIMIT),
            "session_task_limit": _safe_limit(session_task_limit, MOBILE_SESSION_TASK_LIMIT),
            "session_task_counts": window.session_task_counts,
            "codex_threads": codex_threads,
            "codex_threads_error": str(codex_threads_payload.get("error") or ""),
            "codex_threads_next_cursor": str(codex_threads_payload.get("next_cursor") or ""),
            "codex_threads_backwards_cursor": str(codex_threads_payload.get("backwards_cursor") or ""),
            "codex_threads_archived": bool(codex_threads_payload.get("archived")),
            "selected_codex_thread": selected_codex_thread,
            "agents": _raw_agent_payloads(raw_state),
            "sessions": [],
            "sessions_loaded": False,
            "session_page": 1,
            "session_total_pages": 1,
            "session_total_count": 0,
            "senders_loaded": False,
            "senders": [],
            "tasks": task_payloads,
        }

    dashboard = load_dashboard_state(
        APP_DIR,
        page_key="stream",
        load_bridge_conversations=include_senders,
        include_hub_task_text=include_senders,
    )
    window = _build_stream_task_window(
        dashboard.hub_state.tasks,
        selected_session_name=selected_session_name,
        task_limit=task_limit,
        session_task_limit=session_task_limit,
    )
    selected_codex_thread_id = codex_thread_id_from_session_name(selected_session_name)
    session_rows_page = build_session_rows_page(
        dashboard.hub_state,
        APP_DIR / "sessions",
        session_page,
        session_limit,
        include_session_files=False,
    ) if include_sessions else None
    codex_threads_payload = load_codex_threads_page(
        cursor=codex_threads_cursor,
        archived=codex_threads_archived,
    ) if include_codex_threads else {"threads": [], "error": "", "next_cursor": "", "backwards_cursor": "", "archived": False}
    codex_threads = codex_threads_payload.get("threads") if isinstance(codex_threads_payload.get("threads"), list) else []
    codex_thread_tasks: list[dict[str, object]] = []
    selected_codex_thread: dict[str, object] = {}
    if selected_codex_thread_id:
        selected_codex_thread = _load_codex_thread_cached(selected_codex_thread_id)
        selected_limit = _safe_limit(session_task_limit, MOBILE_SESSION_TASK_LIMIT)
        codex_thread_tasks = _codex_thread_task_payloads_cached(selected_codex_thread_id, selected_codex_thread, limit=selected_limit)
        window.session_task_counts[codex_thread_session_name(selected_codex_thread_id)] = _codex_thread_turn_count_cached(selected_codex_thread_id, selected_codex_thread)
    agents = [
        {
            "id": agent.id,
            "name": agent.name or agent.id,
            "backend": agent.backend,
            "model": agent.model,
            "status": agent.runtime.status,
            "queue_size": agent.runtime.queue_size,
        }
        for agent in dashboard.hub_state.agents
    ]
    latest_by_sender_session: dict[tuple[str, str], HubTask] = {}
    latest_sender_keys: dict[tuple[str, str], tuple[str, int, str]] = {}
    if include_senders:
        for index, task in enumerate(dashboard.hub_state.tasks):
            sender_id = task.sender_id.strip()
            session_name = task.session_name or "default"
            key = (sender_id, session_name)
            if not sender_id:
                continue
            task_key = _hub_task_window_key(index, task)
            latest_key = latest_sender_keys.get(key)
            if latest_key is None or task_key > latest_key:
                latest_by_sender_session[key] = task
                latest_sender_keys[key] = task_key
    senders = []
    if include_senders:
        for sender_id, binding in sorted(dashboard.bridge_conversations.items()):
            sessions = []
            for name, meta in sorted(binding.sessions.items()):
                latest_task = latest_by_sender_session.get((sender_id, name))
                sessions.append(
                    {
                        "name": name,
                        "backend": meta.backend,
                        "updated_at": meta.updated_at,
                        "is_current": name == binding.current_session,
                        "latest_task_id": latest_task.id if latest_task else "",
                        "latest_status": latest_task.status if latest_task else "idle",
                        "latest_summary": _latest_task_summary(latest_task),
                    }
                )
            senders.append(
                {
                    "sender_id": sender_id,
                    "current_session": binding.current_session,
                    "session_count": len(binding.sessions),
                    "sessions": sessions,
                }
            )
    task_payloads = [_task_payload(task, stream_order=window.task_order.get(task.id, 0)) for task in window.tasks]
    if selected_codex_thread_id:
        task_payloads = codex_thread_tasks + task_payloads
    return {
        "updated_at": _state_now(),
        "runtime": dashboard.snapshot.to_dict(),
        "counts": window.counts,
        "task_limit": _safe_optional_limit(task_limit, MOBILE_TASK_LIMIT),
        "session_task_limit": _safe_limit(session_task_limit, MOBILE_SESSION_TASK_LIMIT),
        "session_task_counts": window.session_task_counts,
        "codex_threads": codex_threads,
        "codex_threads_error": str(codex_threads_payload.get("error") or ""),
        "codex_threads_next_cursor": str(codex_threads_payload.get("next_cursor") or ""),
        "codex_threads_backwards_cursor": str(codex_threads_payload.get("backwards_cursor") or ""),
        "codex_threads_archived": bool(codex_threads_payload.get("archived")),
        "selected_codex_thread": selected_codex_thread,
        "agents": agents,
        "sessions": [
            {
                "name": row.name,
                "status": row.status,
                "queue_size": row.queue_size,
                "success_count": row.success_count,
                "failure_count": row.failure_count,
            }
            for row in (session_rows_page.rows if session_rows_page is not None else [])
        ],
        "sessions_loaded": include_sessions,
        "session_page": session_rows_page.page if session_rows_page is not None else 1,
        "session_total_pages": session_rows_page.total_pages if session_rows_page is not None else 1,
        "session_total_count": session_rows_page.total_count if session_rows_page is not None else 0,
        "senders_loaded": include_senders,
        "senders": senders,
        "tasks": task_payloads,
    }

def _build_stream_state(
    *,
    selected_session_name: str = "",
    task_limit: int = 1,
    session_task_limit: int = MOBILE_SESSION_TASK_LIMIT,
) -> dict[str, object]:
    raw_state = _load_raw_hub_state()
    window = _stream_raw_window_from_state(
        raw_state,
        selected_session_name=selected_session_name,
        task_limit=task_limit,
        session_task_limit=session_task_limit,
    )
    selected_codex_thread_id = codex_thread_id_from_session_name(selected_session_name)

    agents = _raw_agent_payloads(raw_state)
    task_payloads = [
        _raw_task_payload(task, stream_order=window.task_order.get(_raw_clean_text(task, "id"), 0))
        for task in window.tasks
    ]
    selected_codex_thread: dict[str, object] = {}
    if selected_codex_thread_id:
        selected_codex_thread = _load_codex_thread_cached(selected_codex_thread_id)
        selected_limit = _safe_limit(session_task_limit, MOBILE_SESSION_TASK_LIMIT)
        codex_thread_tasks = _codex_thread_task_payloads_cached(selected_codex_thread_id, selected_codex_thread, limit=selected_limit)
        window.session_task_counts[codex_thread_session_name(selected_codex_thread_id)] = _codex_thread_turn_count_cached(selected_codex_thread_id, selected_codex_thread)
        task_payloads = codex_thread_tasks + task_payloads

    return {
        "updated_at": _state_now(),
        "counts": window.counts,
        "task_limit": _safe_optional_limit(task_limit, MOBILE_TASK_LIMIT),
        "session_task_limit": _safe_limit(session_task_limit, MOBILE_SESSION_TASK_LIMIT),
        "session_task_counts": window.session_task_counts,
        "codex_threads": [],
        "codex_threads_error": "",
        "selected_codex_thread": selected_codex_thread,
        "agents": agents,
        "sessions": [],
        "senders": [],
        "tasks": task_payloads,
    }


def _load_codex_threads_cached() -> dict[str, object]:
    now = time.monotonic()
    loaded_at = float(_CODEX_THREADS_CACHE.get("loaded_at") or 0.0)
    if now - loaded_at <= CODEX_THREAD_CACHE_SECONDS:
        payload = _CODEX_THREADS_CACHE.get("payload")
        return dict(payload) if isinstance(payload, dict) else {"threads": [], "error": ""}
    try:
        payload = _load_all_codex_threads()
        threads = payload.get("threads") if isinstance(payload.get("threads"), list) else []
        normalized = [_mobile_codex_thread_payload(item) for item in threads if isinstance(item, dict)]
        result: dict[str, object] = {
            "threads": normalized,
            "next_cursor": str(payload.get("next_cursor") or ""),
            "backwards_cursor": str(payload.get("backwards_cursor") or ""),
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        result = {"threads": [], "error": str(exc)}
    _CODEX_THREADS_CACHE["loaded_at"] = now
    _CODEX_THREADS_CACHE["payload"] = result
    return dict(result)


def load_codex_threads_page(
    *,
    cursor: str = "",
    archived: bool = False,
    limit: int = CODEX_THREAD_PAGE_LIMIT,
) -> dict[str, object]:
    try:
        payload = list_codex_threads(
            limit=limit,
            cursor=str(cursor or "").strip(),
            archived=archived,
            timeout_seconds=8,
        )
        threads = payload.get("threads") if isinstance(payload.get("threads"), list) else []
        normalized_threads = []
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            thread = dict(thread)
            thread["archived"] = bool(thread.get("archived", archived))
            normalized_threads.append(_mobile_codex_thread_payload(thread))
        return {
            "threads": normalized_threads,
            "next_cursor": str(payload.get("next_cursor") or "").strip(),
            "backwards_cursor": str(payload.get("backwards_cursor") or "").strip(),
            "archived": bool(archived),
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "threads": [],
            "next_cursor": "",
            "backwards_cursor": "",
            "archived": bool(archived),
            "error": str(exc),
        }

def _load_all_codex_threads() -> dict[str, object]:
    threads: list[dict[str, object]] = []
    seen_thread_ids: set[str] = set()
    backwards_cursor = ""
    for archived in (False, True):
        page_payload = _load_codex_thread_pages(archived=archived)
        for thread in page_payload["threads"]:
            thread_id = str(thread.get("id") or thread.get("session_id") or "").strip()
            if not thread_id or thread_id in seen_thread_ids:
                continue
            seen_thread_ids.add(thread_id)
            threads.append(thread)
        if page_payload.get("backwards_cursor"):
            backwards_cursor = str(page_payload.get("backwards_cursor") or "").strip()
    return {"threads": threads, "next_cursor": "", "backwards_cursor": backwards_cursor}


def _load_codex_thread_pages(*, archived: bool) -> dict[str, object]:
    threads: list[dict[str, object]] = []
    seen_cursors: set[str] = set()
    cursor = ""
    backwards_cursor = ""
    for _page in range(CODEX_THREAD_MAX_PAGES):
        payload = list_codex_threads(
            limit=CODEX_THREAD_PAGE_LIMIT,
            cursor=cursor,
            archived=archived,
            timeout_seconds=8,
        )
        page_threads = payload.get("threads") if isinstance(payload.get("threads"), list) else []
        for thread in page_threads:
            if not isinstance(thread, dict):
                continue
            thread["archived"] = bool(thread.get("archived", archived))
            threads.append(thread)
        backwards_cursor = str(payload.get("backwards_cursor") or backwards_cursor).strip()
        next_cursor = str(payload.get("next_cursor") or "").strip()
        if not next_cursor:
            return {"threads": threads, "next_cursor": "", "backwards_cursor": backwards_cursor}
        if next_cursor in seen_cursors:
            raise RuntimeError(f"Codex thread pagination returned a repeated cursor: {next_cursor}")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise RuntimeError(f"Codex thread pagination exceeded {CODEX_THREAD_MAX_PAGES} pages")


def _load_codex_thread_cached(thread_id: str, *, blocking: bool = False) -> dict[str, object]:
    cleaned_thread_id = thread_id.strip()
    if not cleaned_thread_id:
        return {}
    now = time.monotonic()
    with _CODEX_THREAD_DETAIL_LOCK:
        cached = _CODEX_THREAD_DETAIL_CACHE.get(cleaned_thread_id)
        if cached:
            thread = cached.get("thread")
            if now - float(cached.get("loaded_at") or 0.0) <= CODEX_THREAD_CACHE_SECONDS:
                return dict(thread) if isinstance(thread, dict) else {}
            stale_thread = dict(thread) if isinstance(thread, dict) else {}
        else:
            stale_thread = {}
    if blocking:
        return _load_codex_thread_now(cleaned_thread_id)
    _start_codex_thread_detail_load(cleaned_thread_id)
    return stale_thread or {"id": cleaned_thread_id, "messages": [], "loading": True}


def _load_codex_thread_now(cleaned_thread_id: str) -> dict[str, object]:
    try:
        thread = read_codex_thread(cleaned_thread_id, timeout_seconds=3)
    except Exception as exc:  # noqa: BLE001
        thread = {"id": cleaned_thread_id, "messages": [], "error": str(exc)}
    with _CODEX_THREAD_DETAIL_LOCK:
        _CODEX_THREAD_DETAIL_CACHE[cleaned_thread_id] = {
            "loaded_at": time.monotonic(),
            "thread": thread,
            "signature_parts_by_limit": {},
            "task_payloads_by_limit": {},
            "turn_count": None,
        }
        _CODEX_THREAD_DETAIL_INFLIGHT.discard(cleaned_thread_id)
    return dict(thread)


def _start_codex_thread_detail_load(cleaned_thread_id: str) -> None:
    with _CODEX_THREAD_DETAIL_LOCK:
        if cleaned_thread_id in _CODEX_THREAD_DETAIL_INFLIGHT:
            return
        _CODEX_THREAD_DETAIL_INFLIGHT.add(cleaned_thread_id)
    thread = threading.Thread(
        target=_load_codex_thread_now,
        args=(cleaned_thread_id,),
        daemon=True,
        name=f"codex-thread-detail-{cleaned_thread_id[:12]}",
    )
    thread.start()

def _codex_thread_signature_parts_cached(
    thread_id: str,
    *,
    limit: int,
) -> tuple[tuple[tuple[str, str, str, str, str, int, int, int, str], ...], int]:
    cleaned_thread_id = thread_id.strip()
    if not cleaned_thread_id:
        return (), 0
    safe_limit = _safe_limit(limit, MOBILE_SESSION_TASK_LIMIT)
    thread = _load_codex_thread_cached(cleaned_thread_id)
    cached = _CODEX_THREAD_DETAIL_CACHE.get(cleaned_thread_id)
    if cached and time.monotonic() - float(cached.get("loaded_at") or 0.0) <= CODEX_THREAD_CACHE_SECONDS:
        signatures = cached.get("signature_parts_by_limit")
        if isinstance(signatures, dict):
            cached_signature = signatures.get(safe_limit)
            if isinstance(cached_signature, tuple) and len(cached_signature) == 2:
                parts, turn_count = cached_signature
                if isinstance(parts, tuple):
                    return parts, int(turn_count or 0)
    codex_thread_tasks = _codex_thread_task_payloads_cached(cleaned_thread_id, thread, limit=safe_limit)
    signature = (
        tuple(_payload_task_signature_part(task) for task in codex_thread_tasks),
        _codex_thread_turn_count_cached(cleaned_thread_id, thread),
    )
    cached = _CODEX_THREAD_DETAIL_CACHE.get(cleaned_thread_id)
    if cached is not None:
        signatures = cached.get("signature_parts_by_limit")
        if not isinstance(signatures, dict):
            signatures = {}
            cached["signature_parts_by_limit"] = signatures
        signatures[safe_limit] = signature
    return signature


def _mobile_codex_thread_payload(thread: dict[str, object]) -> dict[str, object]:
    thread_id = str(thread.get("id") or "").strip()
    title = str(thread.get("title") or thread.get("preview") or thread_id).strip()
    cwd = str(thread.get("cwd") or "").strip()
    return {
        "id": thread_id,
        "session_name": codex_thread_session_name(thread_id),
        "title": title or thread_id,
        "preview": str(thread.get("preview") or title).strip(),
        "cwd": cwd,
        "project": Path(cwd).name if cwd else "",
        "source": str(thread.get("source") or "").strip(),
        "updated_at": str(thread.get("updated_at") or thread.get("recency_at") or "").strip(),
        "branch": str(thread.get("branch") or "").strip(),
        "archived": bool(thread.get("archived")),
        "status": str(thread.get("status") or "").strip(),
    }


def _codex_message_activity_item(message: dict[str, object], *, fallback_at: str = "") -> dict[str, object] | None:
    activity = message.get("activity") if isinstance(message.get("activity"), dict) else {}
    event = str(activity.get("event") or "codex_item").strip()
    at = str(activity.get("at") or fallback_at).strip()
    if not event or not at:
        return None
    raw_metadata = activity.get("metadata") if isinstance(activity.get("metadata"), dict) else {}
    metadata = {
        str(key): str(value)
        for key, value in raw_metadata.items()
        if str(key).strip() and str(value).strip()
    }
    return {
        "event": event,
        "type": str(activity.get("type") or "info").strip() or "info",
        "at": at,
        "detail": str(activity.get("detail") or message.get("text") or "").strip(),
        "metadata": metadata,
    }

def _codex_thread_turn_sort_items(thread: dict[str, object]) -> list[tuple[str, tuple[int, str, int, int]]]:
    messages = thread.get("messages") if isinstance(thread.get("messages"), list) else []
    first_seen: dict[str, int] = {}
    first_at: dict[str, str] = {}
    first_turn_order: dict[str, int] = {}
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        turn_id = str(message.get("turn_id") or message.get("id") or "").strip()
        if not turn_id:
            continue
        first_seen.setdefault(turn_id, index)
        turn_order = _codex_message_int(message.get("turn_order"))
        if turn_order and (turn_id not in first_turn_order or turn_order < first_turn_order[turn_id]):
            first_turn_order[turn_id] = turn_order
        at = _codex_message_at(message)
        if at and (turn_id not in first_at or at < first_at[turn_id]):
            first_at[turn_id] = at
    return [
        (
            turn_id,
            (
                0 if first_at.get(turn_id) else 1,
                first_at.get(turn_id) or "",
                first_turn_order.get(turn_id, 0),
                first_seen[turn_id],
            ),
        )
        for turn_id in first_seen
    ]


def _codex_thread_turn_order(thread: dict[str, object]) -> list[str]:
    return [
        turn_id
        for turn_id, _sort_key in sorted(
            _codex_thread_turn_sort_items(thread),
            key=lambda item: item[1],
        )
    ]


def _codex_thread_selected_turns(thread: dict[str, object], limit: int | None) -> tuple[list[str], dict[str, int]]:
    turn_items = _codex_thread_turn_sort_items(thread)
    if limit is None:
        ordered_items = sorted(turn_items, key=lambda item: item[1])
        return [turn_id for turn_id, _sort_key in ordered_items], {
            turn_id: index
            for index, (turn_id, _sort_key) in enumerate(ordered_items, start=1)
        }
    safe_limit = max(0, int(limit))
    if safe_limit <= 0:
        return [], {}
    selected_items = heapq.nlargest(safe_limit, turn_items, key=lambda item: item[1])
    selected_items.sort(key=lambda item: item[1])
    selected_order = [turn_id for turn_id, _sort_key in selected_items]
    selected_ranks = {
        turn_id: len(turn_items) - len(selected_items) + index
        for index, (turn_id, _sort_key) in enumerate(selected_items, start=1)
    }
    return selected_order, selected_ranks


def _codex_message_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _codex_message_at(message: dict[str, object]) -> str:
    for key in ("at", "created_at", "createdAt", "timestamp", "updatedAt", "completedAt", "completedAtMs"):
        value = message.get(key)
        if value not in (None, ""):
            return str(value)
    activity = message.get("activity")
    if isinstance(activity, dict):
        value = activity.get("at")
        if value not in (None, ""):
            return str(value)
    return ""


def _codex_message_sort_key(index: int, message: dict[str, object], turn_order_lookup: dict[str, int]) -> tuple[int, int, str, int, int]:
    turn_id = str(message.get("turn_id") or message.get("id") or "").strip()
    at = _codex_message_at(message)
    return (
        turn_order_lookup.get(turn_id, 0),
        0 if at else 1,
        at,
        _codex_message_int(message.get("item_order")),
        index,
    )


def _codex_thread_turn_count(thread: dict[str, object]) -> int:
    messages = thread.get("messages") if isinstance(thread.get("messages"), list) else []
    turn_ids = {
        str(message.get("turn_id") or message.get("id") or "").strip()
        for message in messages
        if isinstance(message, dict) and str(message.get("turn_id") or message.get("id") or "").strip()
    }
    return len(turn_ids)


def _codex_thread_turn_count_cached(thread_id: str, thread: dict[str, object]) -> int:
    cleaned_thread_id = thread_id.strip()
    if not cleaned_thread_id or thread.get("loading"):
        return _codex_thread_turn_count(thread)
    with _CODEX_THREAD_DETAIL_LOCK:
        cached = _CODEX_THREAD_DETAIL_CACHE.get(cleaned_thread_id)
        cached_count = cached.get("turn_count") if isinstance(cached, dict) else None
        if isinstance(cached_count, int):
            return cached_count
    count = _codex_thread_turn_count(thread)
    with _CODEX_THREAD_DETAIL_LOCK:
        cached = _CODEX_THREAD_DETAIL_CACHE.get(cleaned_thread_id)
        if isinstance(cached, dict):
            cached["turn_count"] = count
    return count


def _copy_codex_thread_task_payloads(tasks: list[dict[str, object]]) -> list[dict[str, object]]:
    copied: list[dict[str, object]] = []
    for task in tasks:
        item = dict(task)
        activity_items = item.get("activity_items")
        if isinstance(activity_items, list):
            item["activity_items"] = [dict(activity) if isinstance(activity, dict) else activity for activity in activity_items]
        copied.append(item)
    return copied


def _codex_thread_task_payloads_cached(thread_id: str, thread: dict[str, object], *, limit: int | None = None) -> list[dict[str, object]]:
    cleaned_thread_id = thread_id.strip()
    if not cleaned_thread_id or thread.get("loading"):
        return _codex_thread_task_payloads(thread, limit=limit)
    safe_limit = _safe_limit(limit, MOBILE_SESSION_TASK_LIMIT) if limit is not None else -1
    with _CODEX_THREAD_DETAIL_LOCK:
        cached = _CODEX_THREAD_DETAIL_CACHE.get(cleaned_thread_id)
        payloads = cached.get("task_payloads_by_limit") if isinstance(cached, dict) else None
        cached_payload = payloads.get(safe_limit) if isinstance(payloads, dict) else None
        if isinstance(cached_payload, list):
            return _copy_codex_thread_task_payloads(cached_payload)
    tasks = _codex_thread_task_payloads(thread, limit=limit)
    with _CODEX_THREAD_DETAIL_LOCK:
        cached = _CODEX_THREAD_DETAIL_CACHE.get(cleaned_thread_id)
        if isinstance(cached, dict):
            payloads = cached.get("task_payloads_by_limit")
            if not isinstance(payloads, dict):
                payloads = {}
                cached["task_payloads_by_limit"] = payloads
            payloads[safe_limit] = _copy_codex_thread_task_payloads(tasks)
    return tasks


def _codex_thread_task_payloads(thread: dict[str, object], *, limit: int | None = None) -> list[dict[str, object]]:
    thread_id = str(thread.get("id") or "").strip()
    if not thread_id:
        return []
    session_name = codex_thread_session_name(thread_id)
    messages = thread.get("messages") if isinstance(thread.get("messages"), list) else []
    selected_turns, turn_order_lookup = _codex_thread_selected_turns(thread, limit)
    selected_turn_set = set(selected_turns)
    by_turn: dict[str, dict[str, list[object]]] = {
        turn_id: {"user": [], "assistant": [], "reasoning": [], "activity": []}
        for turn_id in selected_turns
    }
    created_base = str(thread.get("created_at") or thread.get("updated_at") or "").strip()
    turn_created_at: dict[str, str] = {}
    selected_messages = [
        (index, message)
        for index, message in enumerate(messages)
        if isinstance(message, dict)
        and str(message.get("turn_id") or message.get("id") or "").strip() in selected_turn_set
    ]
    for _index, message in selected_messages:
        turn_id = str(message.get("turn_id") or message.get("id") or "").strip()
        at = _codex_message_at(message)
        if at and (turn_id not in turn_created_at or at < turn_created_at[turn_id]):
            turn_created_at[turn_id] = at
    sorted_messages = sorted(selected_messages, key=lambda item: _codex_message_sort_key(item[0], item[1], turn_order_lookup))
    for _index, message in sorted_messages:
        turn_id = str(message.get("turn_id") or message.get("id") or "").strip()
        role = str(message.get("role") or "").strip()
        text = str(message.get("text") or "").strip()
        if role in {"user", "assistant", "reasoning"} and text:
            by_turn[turn_id][role].append(text)
        elif role == "activity":
            activity = _codex_message_activity_item(message, fallback_at=_codex_message_at(message) or turn_created_at.get(turn_id, "") or created_base)
            if activity is not None:
                by_turn[turn_id]["activity"].append(activity)
    tasks: list[dict[str, object]] = []
    cwd = str(thread.get("cwd") or "").strip()
    thread_error = str(thread.get("error") or "").strip()
    for turn_id in selected_turns:
        parts = by_turn[turn_id]
        created_at = turn_created_at.get(turn_id, "") or created_base
        prompt = "\n\n".join(str(part) for part in parts["user"]).strip()
        output = "\n\n".join(str(part) for part in parts["assistant"]).strip()
        reasoning = "\n\n".join(str(part) for part in parts["reasoning"]).strip()
        activity_items = [item for item in parts["activity"] if isinstance(item, dict)]
        if not prompt and not output and not reasoning and not activity_items:
            continue
        tasks.append(
            {
                "id": f"codex-{thread_id}-{turn_id}",
                "agent_id": "codex",
                "agent_name": "Codex",
                "backend": "codex",
                "source": "codex-app-server",
                "sender_id": "",
                "session_id": thread_id,
                "session_name": session_name,
                "workdir": cwd,
                "model": "",
                "status": "succeeded",
                "stream_order": turn_order_lookup.get(turn_id, 0),
                "created_at": created_at,
                "started_at": "",
                "finished_at": "",
                "prompt": prompt,
                "images": [],
                "image_previews": [],
                "output": output,
                "error": "",
                "progress_text": reasoning,
                "progress_at": "",
                "progress_seq": 1 if reasoning else 0,
                "context_left_percent": None,
                "activity_items": activity_items,
                "summary": output or reasoning or prompt or str(activity_items[-1].get("detail") or activity_items[-1].get("event") or ""),
            }
        )
    if not tasks and thread_error:
        tasks.append(
            {
                "id": f"codex-{thread_id}-error",
                "agent_id": "codex",
                "agent_name": "Codex",
                "backend": "codex",
                "source": "codex-app-server",
                "sender_id": "",
                "session_id": thread_id,
                "session_name": session_name,
                "workdir": cwd,
                "model": "",
                "status": "failed",
                "stream_order": 1,
                "created_at": created_base,
                "started_at": "",
                "finished_at": created_base,
                "prompt": "",
                "images": [],
                "image_previews": [],
                "output": "",
                "error": thread_error,
                "progress_text": "",
                "progress_at": "",
                "progress_seq": 0,
                "context_left_percent": None,
                "activity_items": [{"event": "codex_error", "type": "error", "at": created_base, "detail": thread_error, "metadata": {}}],
                "summary": thread_error,
            }
        )
    return tasks


def _build_mobile_diagnostics(*, include_logs: bool = False, include_external: bool = False) -> dict[str, object]:
    dashboard = load_dashboard_state(APP_DIR, page_key="diagnostics")
    checks = [
        {
            "key": check.key,
            "label": check.label,
            "ok": check.ok,
            "detail": check.detail,
        }
        for check in dashboard.checks.values()
    ]
    logs = []
    if include_logs:
        logs = [
            {"title": title, "content": content}
            for title, content in (
                ("Hub stdout", dashboard.logs.get("hub_out", "(empty)")),
                ("Hub stderr", dashboard.logs.get("hub_err", "(empty)")),
                ("Bridge stdout", dashboard.logs.get("bridge_out", "(empty)")),
                ("Bridge stderr", dashboard.logs.get("bridge_err", "(empty)")),
                ("QQ OneBot Runtime stdout", dashboard.logs.get("onebot_runtime_out", "(empty)")),
                ("QQ OneBot Runtime stderr", dashboard.logs.get("onebot_runtime_err", "(empty)")),
                ("QQ Bridge stdout", dashboard.logs.get("qq_bridge_out", "(empty)")),
                ("QQ Bridge stderr", dashboard.logs.get("qq_bridge_err", "(empty)")),
            )
        ]
    external_processes = []
    if include_external:
        external_processes = [
            {
                "pid": process.pid,
                "name": process.name,
                "backend": process.backend,
                "session_hint": process.session_hint,
                "command_line": process.command_line,
            }
            for process in dashboard.external_agent_processes or dashboard.hub_state.external_agent_processes
        ]
    return {
        "updated_at": _state_now(),
        "checks": checks,
        "logs": logs,
        "logs_loaded": include_logs,
        "external_processes": external_processes,
        "external_loaded": include_external,
        "checks_in_progress": dashboard.checks_in_progress,
        "checks_progress_text": dashboard.checks_progress_text,
    }

def _set_sender_current_session(*, sender_id: str, session_name: str) -> dict[str, object]:
    cleaned_sender_id = sender_id.strip()
    cleaned_session_name = session_name.strip() or "default"
    result = switch_sender_current_session(cleaned_sender_id, cleaned_session_name)
    return {
        "ok": result.ok,
        "message": result.message,
        "sender_id": cleaned_sender_id,
        "session_name": cleaned_session_name,
    }


def _sse(event: str, payload: object) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n"


async def _single_sse(event: str, payload: object):
    yield _sse(event, payload)


def _state_signature(state: dict[str, object]) -> str:
    useful = {
        "counts": state.get("counts"),
        "tasks": [
            {
                "id": task.get("id"),
                "status": task.get("status"),
                "progress_seq": task.get("progress_seq"),
                "progress_text": task.get("progress_text"),
                "output_len": len(str(task.get("output") or "")),
                "error_len": len(str(task.get("error") or "")),
            }
            for task in state.get("tasks", [])
            if isinstance(task, dict)
        ],
        "senders": state.get("senders"),
    }
    return json.dumps(useful, ensure_ascii=False, sort_keys=True)


async def _event_stream(task_id: str, *, selected_session_name: str = ""):
    last_signature = ""
    last_hub_file_signature: tuple[int, int] | None = None
    heartbeat_deadline = asyncio.get_running_loop().time() + MOBILE_HEARTBEAT_SECONDS
    while True:
        now = asyncio.get_running_loop().time()
        if not codex_thread_id_from_session_name(selected_session_name):
            next_hub_file_signature = stream_hub_state_file_signature()
            if next_hub_file_signature == last_hub_file_signature:
                if now >= heartbeat_deadline:
                    yield ": heartbeat\n\n"
                    heartbeat_deadline = now + MOBILE_HEARTBEAT_SECONDS
                await asyncio.sleep(MOBILE_POLL_SECONDS)
                continue
            last_hub_file_signature = next_hub_file_signature
        state = _build_mobile_state(
            selected_session_name=selected_session_name,
            task_limit=1,
            session_task_limit=MOBILE_SESSION_TASK_LIMIT,
            include_codex_threads=False,
            include_sessions=False,
            include_senders=False,
        )
        signature = _state_signature(state)
        if signature != last_signature:
            last_signature = signature
            yield _sse("state", state)
            if task_id:
                task = next((item for item in state["tasks"] if isinstance(item, dict) and item.get("id") == task_id), None)
                if task is not None:
                    yield _sse("task", task)
            heartbeat_deadline = asyncio.get_running_loop().time() + MOBILE_HEARTBEAT_SECONDS
        elif asyncio.get_running_loop().time() >= heartbeat_deadline:
            yield ": heartbeat\n\n"
            heartbeat_deadline = asyncio.get_running_loop().time() + MOBILE_HEARTBEAT_SECONDS
        await asyncio.sleep(MOBILE_POLL_SECONDS)


def _mobile_link_html(mobile_url: str, qr_data_url: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ChatBridge 手机入口</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #111111; color: #f5f5f0; }}
    main {{ max-width: 520px; margin: 0 auto; padding: 28px 18px; }}
    h1 {{ font-size: 24px; margin: 0 0 8px; }}
    p {{ color: #a3a39b; line-height: 1.6; }}
    .panel {{ background: #191919; border: 1px solid #30302d; border-radius: 8px; padding: 18px; box-shadow: 0 1px 2px rgba(0, 0, 0, .30); }}
    img {{ display: block; width: 260px; height: 260px; margin: 14px auto; }}
    code {{ display: block; word-break: break-all; background: #242424; padding: 12px; border-radius: 6px; color: #3f8f72; }}
  </style>
</head>
<body>
  <main>
    <h1>ChatBridge 手机入口</h1>
    <p>用手机扫码打开移动端任务看板。电脑和手机需要在同一个 WiFi 下。</p>
    <section class="panel">
      <img src="{qr_data_url}" alt="移动端入口二维码">
      <code>{mobile_url}</code>
    </section>
  </main>
</body>
</html>"""


def _mobile_denied_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>未授权</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:24px;background:#111111;color:#f5f5f0">
<h1>未授权</h1><p>请从电脑端打开 <code>/mobile-link</code> 后扫码进入。</p>
</body>
</html>"""


def _mobile_app_html(token: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>ChatBridge Mobile</title>
  <style>
    :root {{
      --bg: #111111;
      --surface: #191919;
      --surface-muted: #242424;
      --muted: #a3a39b;
      --ink: #f5f5f0;
      --border: #30302d;
      --accent: #2f6f5e;
      --accent-soft: rgba(47, 111, 94, .16);
      --danger: #c85d68;
      --danger-soft: rgba(200, 93, 104, .13);
      --ok: #3f8f72;
      --ok-soft: rgba(63, 143, 114, .12);
      --warn: #b7791f;
      --warn-soft: rgba(183, 121, 31, .13);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--ink); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    header {{ position: sticky; top: 0; z-index: 3; background: rgba(17,17,17,.94); border-bottom: 1px solid var(--border); backdrop-filter: blur(12px); padding: max(12px, env(safe-area-inset-top)) 14px 10px; }}
    h1 {{ font-size: 18px; margin: 0; }}
    button, input, textarea, select {{ font: inherit; }}
    button {{ border: 0; border-radius: 7px; min-height: 38px; padding: 0 12px; background: var(--accent); color: #ffffff; font-weight: 700; }}
    button.secondary {{ background: var(--surface-muted); color: var(--ink); }}
    button.ghost {{ background: transparent; color: var(--accent); padding: 0 6px; }}
    main {{ padding: 12px 12px 148px; max-width: 760px; margin: 0 auto; }}
    .tabs {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(5rem, 1fr)); gap: 8px; margin: 10px 0 12px; }}
    .tabs button {{ background: var(--surface); color: var(--muted); border: 1px solid var(--border); }}
    .tabs button.active {{ background: var(--accent); color: #ffffff; border-color: var(--accent); }}
    .cards {{ display: grid; gap: 10px; }}
    .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; box-shadow: 0 1px 2px rgba(0,0,0,.32); }}
    .row {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; min-width: 0; }}
    .row > .title {{ min-width: 0; flex: 1 1 auto; }}
    .title {{ font-size: 15px; font-weight: 800; overflow-wrap: anywhere; }}
    .meta {{ color: var(--muted); font-size: 12px; line-height: 1.5; }}
    .summary {{ color: var(--ink); font-size: 13px; line-height: 1.55; margin-top: 8px; overflow-wrap: anywhere; }}
    .stats {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }}
    .stat {{ background: var(--surface-muted); border: 1px solid var(--border); border-radius: 7px; padding: 10px; }}
    .stat strong {{ display: block; font-size: 20px; line-height: 1; }}
    .stat span {{ display: block; color: var(--muted); font-size: 12px; margin-top: 5px; }}
    .badge {{ display: inline-flex; align-items: center; flex: 0 0 auto; min-height: 24px; padding: 0 8px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 12px; font-weight: 800; white-space: nowrap; }}
    .badge.running, .badge.queued {{ background: var(--warn-soft); color: var(--warn); }}
    .badge.succeeded {{ background: var(--ok-soft); color: var(--ok); }}
    .badge.failed {{ background: var(--danger-soft); color: var(--danger); }}
    .badge.canceled {{ background: var(--warn-soft); color: var(--warn); }}
    .detail {{ display: none; }}
    .detail.active {{ display: block; }}
    pre {{ margin: 8px 0 0; padding: 12px; background: #111827; color: #e5e7eb; border-radius: 8px; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; line-height: 1.5; max-height: 46vh; overflow: auto; }}
    .composer {{ position: fixed; left: 0; right: 0; bottom: 0; background: rgba(17,17,17,.96); border-top: 1px solid var(--border); padding: 10px 12px max(10px, env(safe-area-inset-bottom)); }}
    .composer-inner {{ max-width: 760px; margin: 0 auto; display: grid; grid-template-columns: 1fr auto; gap: 8px; }}
    textarea {{ width: 100%; min-height: 42px; max-height: 110px; resize: none; border: 1px solid var(--border); border-radius: 7px; padding: 9px 10px; background: var(--surface); color: var(--ink); }}
    .empty {{ padding: 24px 12px; text-align: center; color: var(--muted); }}
    .statusline {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
  </style>
</head>
<body>
  <header>
    <div class="row"><h1>ChatBridge Mobile</h1><button class="secondary" id="refreshBtn">刷新</button></div>
    <div class="statusline" id="statusLine">正在连接...</div>
  </header>
  <main>
    <nav class="tabs">
      <button class="active" data-tab="overview">总览</button>
      <button data-tab="tasks">任务</button>
      <button data-tab="sessions">会话</button>
      <button data-tab="senders">入口</button>
      <button data-tab="diagnostics">诊断</button>
      <button data-tab="logs">日志</button>
    </nav>
    <section id="overview" class="detail active"><div class="cards" id="overviewList"></div></section>
    <section id="tasks" class="detail"><div class="cards" id="taskList"></div></section>
    <section id="sessions" class="detail"><div class="cards" id="sessionList"></div></section>
    <section id="senders" class="detail"><div class="cards" id="senderList"></div></section>
    <section id="diagnostics" class="detail"><div class="cards" id="diagnosticList"></div></section>
    <section id="logs" class="detail"><div class="cards" id="logList"></div></section>
  </main>
  <div class="composer">
    <div class="composer-inner">
      <textarea id="promptInput" placeholder="发送到选中的会话"></textarea>
      <button id="sendBtn">发送</button>
    </div>
  </div>
  <script>
    const TOKEN = {json.dumps(token)};
    let state = null;
    let selected = {{ taskId: "", sessionName: "default", senderId: "", agentId: "main", backend: "" }};
    const statusLine = document.getElementById("statusLine");
    const overviewList = document.getElementById("overviewList");
    const taskList = document.getElementById("taskList");
    const sessionList = document.getElementById("sessionList");
    const senderList = document.getElementById("senderList");
    const diagnosticList = document.getElementById("diagnosticList");
    const logList = document.getElementById("logList");
    const promptInput = document.getElementById("promptInput");
    let diagnosticsState = null;
    let currentTab = "overview";

    function escapeHtml(value) {{
      return String(value ?? "").replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[c]));
    }}
    function badge(status) {{
      return `<span class="badge ${{escapeHtml(status)}}">${{escapeHtml(status || "idle")}}</span>`;
    }}
    function setSelected(next) {{
      selected = {{ ...selected, ...next }};
      statusLine.textContent = `选中会话：${{selected.sessionName || "default"}}${{selected.senderId ? " | " + selected.senderId : ""}}`;
      renderCurrentTab();
    }}
    function renderOverview() {{
      if (!state) {{ overviewList.innerHTML = '<div class="empty">正在加载总览</div>'; return; }}
      const runtime = state.runtime || {{}};
      const counts = state.counts || {{}};
      overviewList.innerHTML = `
        <article class="card">
          <div class="title">服务状态</div>
          <div class="stats" style="margin-top:10px">
            <div class="stat"><strong>${{runtime.hub_running ? "运行" : "停止"}}</strong><span>Hub ${{runtime.hub_pid || "-"}}</span></div>
            <div class="stat"><strong>${{runtime.qq_bridge_running ? "运行" : "停止"}}</strong><span>QQ Bridge ${{runtime.qq_bridge_pid || "-"}}</span></div>
            <div class="stat"><strong>${{runtime.onebot_runtime_running ? "运行" : "停止"}}</strong><span>QQ OneBot ${{runtime.onebot_runtime_pid || "-"}}</span></div>
            <div class="stat"><strong>${{runtime.bridge_running ? "运行" : "停止"}}</strong><span>微信 Bridge ${{runtime.bridge_pid || "-"}}</span></div>
          </div>
          <div class="row" style="margin-top:12px; flex-wrap:wrap; justify-content:flex-start">
            <button data-action="run-service" data-service-action="start-qq-bridge">启动 QQ</button>
            <button class="secondary" data-action="run-service" data-service-action="restart-qq-stack">重启 QQ</button>
            <button class="secondary" data-action="run-service" data-service-action="start-weixin">启动微信</button>
            <button class="secondary" data-action="run-service" data-service-action="restart">重启全部</button>
            <button class="ghost" data-action="run-service" data-service-action="stop" data-confirm="确认停止 ChatBridge 服务？">停止</button>
          </div>
        </article>
        <article class="card">
          <div class="title">任务概览</div>
          <div class="stats" style="margin-top:10px">
            <div class="stat"><strong>${{counts.running || 0}}</strong><span>运行中</span></div>
            <div class="stat"><strong>${{counts.queued || 0}}</strong><span>排队</span></div>
            <div class="stat"><strong>${{counts.succeeded || 0}}</strong><span>成功</span></div>
            <div class="stat"><strong>${{counts.failed || 0}}</strong><span>失败</span></div>
          </div>
        </article>
        <article class="card">
          <div class="title">Agent</div>
          <div class="cards" style="margin-top:10px">
            ${{(state.agents || []).map(agent => `
              <div class="card" style="box-shadow:none">
                <div class="row"><div class="title">${{escapeHtml(agent.name)}} · ${{escapeHtml(agent.id)}}</div>${{badge(agent.status)}}</div>
                <div class="meta">${{escapeHtml(agent.backend || "-")}} · ${{escapeHtml(agent.model || "-")}} · 队列 ${{agent.queue_size || 0}}</div>
              </div>
            `).join("")}}
          </div>
        </article>
      `;
    }}
    function renderTasks() {{
      const tasks = state?.tasks || [];
      if (!tasks.length) {{ taskList.innerHTML = '<div class="empty">还没有任务</div>'; return; }}
      taskList.innerHTML = tasks.map(task => `
        <article class="card" data-task-id="${{escapeHtml(task.id)}}">
          <div class="row"><div class="title">${{escapeHtml(task.session_name)}} · ${{escapeHtml(task.agent_name)}}</div>${{badge(task.status)}}</div>
          <div class="meta">${{escapeHtml(task.created_at || "-")}} · ${{escapeHtml(task.backend || "-")}} · ${{escapeHtml(task.id)}}</div>
          <div class="summary">${{escapeHtml(task.summary || "")}}</div>
          ${{selected.taskId === task.id ? `<pre>${{escapeHtml(task.output || task.error || task.progress_text || "(暂无输出)")}}</pre>` : ""}}
          <div class="row" style="margin-top:10px">
            <button class="secondary" data-action="select-task" data-id="${{escapeHtml(task.id)}}">选中</button>
            <button class="ghost" data-action="copy-id" data-id="${{escapeHtml(task.id)}}">复制 ID</button>
          </div>
        </article>
      `).join("");
    }}
    function renderSessions() {{
      const sessions = state?.sessions || [];
      if (!sessions.length) {{ sessionList.innerHTML = '<div class="empty">还没有会话</div>'; return; }}
      sessionList.innerHTML = sessions.map(item => `
        <article class="card">
          <div class="row"><div class="title">${{escapeHtml(item.name)}}</div>${{badge(item.status)}}</div>
          <div class="meta">队列 ${{item.queue_size}} · 成功 ${{item.success_count}} · 失败 ${{item.failure_count}}</div>
          <div class="row" style="margin-top:10px"><button class="secondary" data-action="select-session" data-name="${{escapeHtml(item.name)}}">选中会话</button></div>
        </article>
      `).join("");
    }}
    function renderSenders() {{
      const senders = state?.senders || [];
      if (!senders.length) {{ senderList.innerHTML = '<div class="empty">还没有 QQ/微信入口记录</div>'; return; }}
      senderList.innerHTML = senders.map(sender => `
        <article class="card">
          <div class="title">${{escapeHtml(sender.sender_id)}}</div>
          <div class="meta">当前会话：${{escapeHtml(sender.current_session)}} · 共 ${{sender.session_count}} 个</div>
          <div class="cards" style="margin-top:10px">
            ${{(sender.sessions || []).map(sess => `
              <div class="card" style="box-shadow:none">
                <div class="row"><div class="title">${{sess.is_current ? "当前 · " : ""}}${{escapeHtml(sess.name)}}</div>${{badge(sess.latest_status)}}</div>
                <div class="summary">${{escapeHtml(sess.latest_summary || "")}}</div>
                <div class="row" style="margin-top:8px">
                  <button class="secondary" data-action="select-sender-session" data-sender="${{escapeHtml(sender.sender_id)}}" data-name="${{escapeHtml(sess.name)}}">选中</button>
                  <button class="ghost" data-action="set-current" data-sender="${{escapeHtml(sender.sender_id)}}" data-name="${{escapeHtml(sess.name)}}">设为入口当前</button>
                </div>
              </div>
            `).join("")}}
          </div>
        </article>
      `).join("");
    }}
    function renderDiagnostics() {{
      if (!diagnosticsState) {{ diagnosticList.innerHTML = '<div class="empty">点刷新加载诊断信息</div>'; logList.innerHTML = '<div class="empty">点刷新加载日志</div>'; return; }}
      const checks = diagnosticsState.checks || [];
      diagnosticList.innerHTML = `
        <article class="card">
          <div class="row"><div class="title">环境检查</div><button class="secondary" data-action="load-diagnostics">刷新诊断</button></div>
          ${{diagnosticsState.checks_in_progress ? `<div class="summary">${{escapeHtml(diagnosticsState.checks_progress_text || "检查进行中")}}</div>` : ""}}
        </article>
        ${{checks.map(check => `
          <article class="card">
            <div class="row"><div class="title">${{escapeHtml(check.label)}}</div>${{badge(check.ok ? "ok" : "failed")}}</div>
            <div class="summary">${{escapeHtml(check.detail || "")}}</div>
          </article>
        `).join("") || '<div class="empty">暂无诊断信息</div>'}}
        <article class="card">
          <div class="title">外部 Agent 进程</div>
          ${{diagnosticsState.external_loaded ? (diagnosticsState.external_processes || []).map(proc => `
            <div class="summary">PID ${{proc.pid}} · ${{escapeHtml(proc.backend)}} · ${{escapeHtml(proc.name)}}<br>${{escapeHtml(proc.session_hint || "")}}</div>
          `).join("") || '<div class="summary">没有发现外部 Agent 进程</div>' : '<div class="summary">点刷新诊断后加载外部进程</div>'}}
        </article>
      `;
      logList.innerHTML = `
        <article class="card"><div class="row"><div class="title">运行日志</div><button class="secondary" data-action="load-diagnostics">刷新日志</button></div></article>
        ${{diagnosticsState.logs_loaded ? (diagnosticsState.logs || []).map(log => `
          <article class="card">
            <div class="title">${{escapeHtml(log.title)}}</div>
            <pre>${{escapeHtml(log.content || "(empty)")}}</pre>
          </article>
        `).join("") : '<div class="empty">点刷新日志后加载运行日志</div>'}}
      `;
    }}
    function mergeStatePayload(payload) {{
      if (!state) return payload;
      const merged = {{ ...state, ...payload }};
      if (!payload.sessions_loaded) {{
        merged.sessions_loaded = state.sessions_loaded;
        merged.sessions = state.sessions;
        merged.session_page = state.session_page;
        merged.session_total_pages = state.session_total_pages;
        merged.session_total_count = state.session_total_count;
      }}
      if (!payload.senders_loaded) {{
        merged.senders_loaded = state.senders_loaded;
        merged.senders = state.senders;
      }}
      return merged;
    }}
    function renderStateSections(options = {{}}) {{
      const forceLoadedLists = options.forceLoadedLists === true;
      renderOverview();
      renderTasks();
      if (forceLoadedLists || state?.sessions_loaded) renderSessions();
      if (forceLoadedLists || state?.senders_loaded) renderSenders();
    }}
    function renderCurrentTab() {{
      if (currentTab === "overview") renderOverview();
      else if (currentTab === "tasks") renderTasks();
      else if (currentTab === "sessions" && state?.sessions_loaded) renderSessions();
      else if (currentTab === "senders" && state?.senders_loaded) renderSenders();
      else if (currentTab === "diagnostics" || currentTab === "logs") renderDiagnostics();
    }}
    async function loadState() {{
      const params = new URLSearchParams({{ token: TOKEN }});
      params.set("session", selected.sessionName || "");
      if (currentTab === "sessions") params.set("include_sessions", "1");
      if (currentTab === "senders") params.set("include_senders", "1");
      const res = await fetch(`/api/mobile/state?${{params.toString()}}`);
      const payload = await res.json();
      if (!payload.ok) throw new Error(payload.error || "加载失败");
      state = mergeStatePayload(payload.data);
      statusLine.textContent = `已更新：${{state.updated_at}} · 运行中 ${{state.counts.running}} · 排队 ${{state.counts.queued}}`;
      renderStateSections();
    }}
    async function loadDiagnostics(options = {{}}) {{
      const includeLogs = options.includeLogs === true;
      const includeExternal = options.includeExternal !== false;
      diagnosticList.innerHTML = '<div class="empty">正在加载诊断信息...</div>';
      if (includeLogs) logList.innerHTML = '<div class="empty">正在加载日志...</div>';
      const params = new URLSearchParams({{ token: TOKEN }});
      if (includeLogs) params.set("include_logs", "1");
      if (includeExternal) params.set("include_external", "1");
      const res = await fetch(`/api/mobile/diagnostics?${{params.toString()}}`);
      const payload = await res.json();
      if (!payload.ok) throw new Error(payload.error || "加载诊断失败");
      diagnosticsState = payload.data;
      statusLine.textContent = `诊断已更新：${{diagnosticsState.updated_at}}`;
      renderDiagnostics();
    }}
    async function runServiceAction(action, confirmText = "") {{
      if (confirmText && !window.confirm(confirmText)) return;
      statusLine.textContent = `正在执行：${{action}}`;
      const res = await fetch(`/api/mobile/actions?token=${{encodeURIComponent(TOKEN)}}`, {{
        method: "POST",
        headers: {{ "content-type": "application/json" }},
        body: JSON.stringify({{ action }})
      }});
      const payload = await res.json();
      statusLine.textContent = payload.message || payload.error || "操作已提交";
      await loadState();
    }}
    async function submitPrompt() {{
      const prompt = promptInput.value.trim();
      if (!prompt) return;
      const res = await fetch(`/api/mobile/tasks?token=${{encodeURIComponent(TOKEN)}}`, {{
        method: "POST",
        headers: {{ "content-type": "application/json" }},
        body: JSON.stringify({{
          prompt,
          session_name: selected.sessionName || "default",
          sender_id: selected.senderId || "",
          agent_id: selected.agentId || "main",
          backend: selected.backend || ""
        }})
      }});
      const payload = await res.json();
      statusLine.textContent = payload.message || payload.error || "已发送";
      if (payload.ok) promptInput.value = "";
      await loadState();
    }}
    async function setCurrent(senderId, sessionName) {{
      const res = await fetch(`/api/mobile/senders/current-session?token=${{encodeURIComponent(TOKEN)}}`, {{
        method: "POST",
        headers: {{ "content-type": "application/json" }},
        body: JSON.stringify({{ sender_id: senderId, session_name: sessionName }})
      }});
      const payload = await res.json();
      statusLine.textContent = payload.message || payload.error || "已处理";
      await loadState();
    }}
    document.querySelector(".tabs").addEventListener("click", event => {{
      const button = event.target.closest("button[data-tab]");
      if (!button) return;
      document.querySelectorAll(".tabs button").forEach(item => item.classList.remove("active"));
      document.querySelectorAll(".detail").forEach(item => item.classList.remove("active"));
      button.classList.add("active");
      document.getElementById(button.dataset.tab).classList.add("active");
      currentTab = button.dataset.tab || "overview";
      if ((button.dataset.tab === "diagnostics" || button.dataset.tab === "logs") && !diagnosticsState) {{
        loadDiagnostics({{ includeLogs: button.dataset.tab === "logs" }}).catch(error => {{ statusLine.textContent = error.message; }});
      }} else if (button.dataset.tab === "sessions" || button.dataset.tab === "senders") {{
        loadState().catch(error => {{ statusLine.textContent = error.message; }});
      }}
    }});
    document.body.addEventListener("click", event => {{
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const action = button.dataset.action;
      if (action === "select-task") {{
        const task = (state?.tasks || []).find(item => item.id === button.dataset.id);
        if (task) setSelected({{ taskId: task.id, sessionName: task.session_name, senderId: task.sender_id || "", agentId: task.agent_id, backend: task.backend }});
      }}
      if (action === "copy-id") navigator.clipboard?.writeText(button.dataset.id || "");
      if (action === "select-session") setSelected({{ sessionName: button.dataset.name || "default", taskId: "" }});
      if (action === "select-sender-session") setSelected({{ senderId: button.dataset.sender || "", sessionName: button.dataset.name || "default", taskId: "" }});
      if (action === "set-current") setCurrent(button.dataset.sender || "", button.dataset.name || "default");
      if (action === "load-diagnostics") loadDiagnostics({{ includeLogs: currentTab === "logs" }}).catch(error => {{ statusLine.textContent = error.message; }});
      if (action === "run-service") runServiceAction(button.dataset.serviceAction || "", button.dataset.confirm || "").catch(error => {{ statusLine.textContent = error.message; }});
    }});
    document.getElementById("refreshBtn").addEventListener("click", loadState);
    document.getElementById("sendBtn").addEventListener("click", submitPrompt);
    const events = new EventSource(`/api/mobile/events?token=${{encodeURIComponent(TOKEN)}}`);
    events.addEventListener("state", event => {{
      state = mergeStatePayload(JSON.parse(event.data));
      statusLine.textContent = `已更新：${{state.updated_at}} · 运行中 ${{state.counts.running}} · 排队 ${{state.counts.queued}}`;
      renderCurrentTab();
    }});
    events.onerror = () => {{ statusLine.textContent = "实时连接断开，正在重连..."; }};
    loadState().catch(error => {{ statusLine.textContent = error.message; }});
  </script>
</body>
</html>"""
