from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agent_hub import HubConfig
from bridge_config import APP_DIR, BridgeConfig, normalize_backend
from core.bridge_command_catalog import QQ_HELP_MESSAGE_KEYS
from core.bridge_command_router import BridgeCommandRouter
from core.bridge_conversation_runtime import BridgeConversationRuntime
from core.bridge_control_runtime import BridgeControlRuntime
from core.bridge_interrupt_runtime import BridgeInterruptRuntime
from core.bridge_message_control import (
    normalize_context_left_percent,
)
from core.bridge_pending_tasks import BridgePendingReplyTask, JsonBackedTaskStore
from core.bridge_runtime import (
    BridgeMessageRuntime,
    BridgeSubmittedTask,
    IncomingBridgeMessage,
    PendingMediaContextStore,
)
from core.app_service import schedule_named_action
from core.bridge_media_control_runtime import BridgeMediaControlRuntime
from core.bridge_native_menu_runtime import BridgeNativeMenuRuntime
from core.bridge_notify_control_runtime import BridgeNotifyControlRuntime
from core.bridge_prompt_runtime import BridgePromptRuntime
from core.bridge_service_control_runtime import BridgeServiceControlRuntime, QQ_RESTART_SCOPES
from core.bridge_session_control_runtime import BridgeSessionControlRuntime
from core.bridge_session_utils import (
    allocate_session_name,
    create_session_meta,
    resolve_fallback_session_target,
    sanitize_project_name,
    sanitize_session_name,
    split_named_path_args,
)
from core.bridge_task_delivery import PollingTaskDeliveryController, TaskUpdateDeliveryController, format_bridge_progress_reply, format_bridge_task_reply
from core.bridge_task_query_runtime import BridgeTaskQueryRuntime
from core.bridge_task_submit_runtime import BridgeTaskSubmitContext, BridgeTaskSubmitRuntime
from core.codex_model_catalog import load_codex_model_catalog
from core.json_store import load_json, save_json
from core.runtime_paths import RUNTIME_DIR, SERVICE_ACTION_STATE_PATH, SESSION_DIR, STATE_DIR, WORKSPACE_DIR
from core.state_models import HubTask, BridgeConversationBinding, BridgeSessionMeta
from localization import Localizer
from local_ipc import bridge_request_dir, cleanup_processed_requests, create_request, mark_bridge_processed, read_request, wait_for_response


ONEBOT_STATE_PATH = STATE_DIR / "qq_onebot_pending_media_context.json"
QQ_PENDING_TASKS_PATH = STATE_DIR / "qq_pending_tasks.json"
QQ_CONVERSATIONS_PATH = STATE_DIR / "qq_conversations.json"
ONEBOT_UPLOAD_DIR = RUNTIME_DIR / "uploads" / "qq"
MEDIA_CONTEXT_TTL_SECONDS = 10 * 60
PENDING_TASK_TTL_SECONDS = 24 * 60 * 60
MEDIA_RECEIVE_MAX_BYTES = 50 * 1024 * 1024
LOCAL_URL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
DEFAULT_QQ_AGENT_ID = "qq"
QQ_TYPING_KEEPALIVE_SECONDS = 5.0
QQ_BRIDGE_CHANNEL = "qq"


def _configure_process_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


_configure_process_stdio()


def _log(message: str) -> None:
    print(f"[qq-bridge] {message}", flush=True)


def _log_error(message: str) -> None:
    print(f"[qq-bridge] {message}", file=sys.stderr, flush=True)


def now_seconds() -> int:
    return int(time.time())


def _safe_path_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in str(value or "").strip())
    return cleaned.strip(".-") or "unknown"


def _format_text_segments(message: object) -> str:
    if isinstance(message, str):
        return message.strip()
    if not isinstance(message, list):
        return ""
    parts: list[str] = []
    for segment in message:
        if not isinstance(segment, dict) or segment.get("type") != "text":
            continue
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        text = str(data.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _media_segments(message: object) -> list[dict[str, Any]]:
    if not isinstance(message, list):
        return []
    return [
        segment
        for segment in message
        if isinstance(segment, dict) and str(segment.get("type") or "").strip().lower() in {"image", "file", "record", "video"}
    ]


def _message_mentions_user(message: object, user_id: str) -> bool:
    if not user_id or not isinstance(message, list):
        return False
    for segment in message:
        if not isinstance(segment, dict) or str(segment.get("type") or "").strip().lower() != "at":
            continue
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if str(data.get("qq") or "").strip() == user_id:
            return True
    return False


class QQOneBotBridge:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        api_base: str = "",
        access_token: str = "",
    ) -> None:
        self.config = config
        self.api_base = (api_base or os.environ.get("QQ_ONEBOT_API_BASE") or "http://127.0.0.1:3000").rstrip("/")
        self.access_token = access_token or os.environ.get("QQ_ONEBOT_ACCESS_TOKEN", "")
        self.agent_id = str(os.environ.get("QQ_BRIDGE_AGENT_ID") or DEFAULT_QQ_AGENT_ID).strip() or DEFAULT_QQ_AGENT_ID
        self.localizer = Localizer(str(getattr(config, "language", "") or ""))
        self._login_user_id = ""
        self._typing_status_available = True
        self.conversations = self._load_conversations()
        self.pending_media_store = PendingMediaContextStore(
            ONEBOT_STATE_PATH,
            ttl_seconds=MEDIA_CONTEXT_TTL_SECONDS,
            now_seconds=now_seconds,
        )
        self.pending_media_context = self.pending_media_store.contexts
        self.task_query_runtime = BridgeTaskQueryRuntime(
            ipc_request=lambda action, payload, timeout_seconds: self._ipc_request(action, payload, timeout_seconds=timeout_seconds),
            default_backend=lambda: self.config.default_backend,
        )
        self.control_runtime = BridgeControlRuntime(
            help_message_keys=QQ_HELP_MESSAGE_KEYS,
            translate=lambda key, **kwargs: self._t(key, **kwargs),
            default_backend=lambda: self.config.default_backend,
            agent_id=lambda: self.agent_id,
            session_name=self._session_name,
            get_task=self.task_query_runtime.get_task,
            find_latest_sender_task=lambda sender_key, allowed_statuses: self.task_query_runtime.find_latest_sender_task(
                sender_key,
                allowed_statuses=allowed_statuses,
            ),
            ipc_request=lambda action, payload, timeout_seconds: self._ipc_request(action, payload, timeout_seconds=timeout_seconds),
            retry_source="qq",
            unsupported_message=None,
        )
        self.session_control_runtime = BridgeSessionControlRuntime(
            adapter=self,
            app_dir=APP_DIR,
            supported_backends=set(getattr(config, "supported_backends", []) or {"codex", "claude", "opencode"}),
        )
        self.service_control_runtime = BridgeServiceControlRuntime(
            schedule_action=lambda action: schedule_named_action(action, delay_seconds=1.0).message,
            render_usage=lambda: self._t("bridge.restart.qq.usage"),
            state_path=SERVICE_ACTION_STATE_PATH,
            default_restart_scope="all",
            restart_scopes=QQ_RESTART_SCOPES,
            translate=lambda key, **kwargs: self._t(key, **kwargs),
        )
        self.notify_control_runtime = BridgeNotifyControlRuntime(
            config=self.config,
            translate=lambda key, **kwargs: self._t(key, **kwargs),
            send_test_notice=lambda: self._t("bridge.notify.qq.test_unsupported"),
        )
        self.command_router = BridgeCommandRouter(
            (
                self.control_runtime,
                self.session_control_runtime,
                self.service_control_runtime,
                self.notify_control_runtime,
            )
        )
        self.conversation_runtime = BridgeConversationRuntime(
            ensure_conversation=self._ensure_conversation,
            default_backend=lambda: self.config.default_backend,
            now=self._now_iso,
            normalize_backend=self._normalize_backend,
        )
        self.media_control_runtime = BridgeMediaControlRuntime(
            translate=lambda key, **kwargs: self._t(key, **kwargs),
            resolve_file=lambda raw_path: self._resolve_shareable_project_file(raw_path),
            send_file=lambda reply_target, file_path: self._send_media_to_reply_target(reply_target, file_path),
        )
        self.native_menu_runtime = BridgeNativeMenuRuntime(
            translate=lambda key, **kwargs: self._t(key, **kwargs),
            now=self._now_iso,
            load_model_catalog=load_codex_model_catalog,
            resolve_session_model=self._resolve_session_model,
            resolve_session_permission_mode=self._resolve_session_permission_mode,
        )
        self.prompt_runtime = BridgePromptRuntime(
            native_menu=self.native_menu_runtime,
            media_control=self.media_control_runtime,
            handle_control=self.command_router.handle,
            send_reply=self._send_reply,
            save_conversations=self._save_conversations,
            unsupported_agent_slash_reply=lambda _command: "QQ 桥暂不支持这个桥接命令。发送 /help 查看当前支持的命令。",
            unsupported_bridge_command_reply=lambda _command: "QQ 桥暂不支持这个桥接命令。发送 /help 查看当前支持的命令。",
            reject_unknown_bridge_slash=True,
            reject_unknown_passthrough_slash=False,
            submit_raw_passthrough=True,
        )
        self.task_submit_runtime = BridgeTaskSubmitRuntime(
            ipc_request=lambda action, payload, timeout_seconds: self._ipc_request(action, payload, timeout_seconds=timeout_seconds),
            resolve_context=self._resolve_task_submit_context,
        )
        self.interrupt_runtime = BridgeInterruptRuntime(
            find_active_task=self._find_active_pending_task,
            cancel_task=self._cancel_task_best_effort,
            submit_delayed=lambda message, session, prompt, passthrough: self.message_runtime.submit_prepared(message, session, prompt, passthrough),
            send_reply=self._send_reply,
            save_pending_tasks=self._save_pending_tasks,
        )
        self.message_runtime = BridgeMessageRuntime(
            pending_media=self.pending_media_store,
            resolve_session=self.conversation_runtime.resolve_session,
            prepare_prompt=self.prompt_runtime.prepare_for_session,
            submit_task=self._submit_runtime_task,
            remember_pending_task=self._remember_submitted_runtime_task,
            send_reply=self._send_reply,
            interrupt_runtime=self.interrupt_runtime,
            on_after_submit=self._start_submitted_task_delivery,
            log=_log,
        )
        self.pending_task_store = JsonBackedTaskStore(
            QQ_PENDING_TASKS_PATH,
            from_dict=lambda raw: BridgePendingReplyTask.from_dict(raw, ttl_seconds=PENDING_TASK_TTL_SECONDS),
            to_dict=lambda task: task.to_dict(),
        )
        self.pending_tasks = self.pending_task_store.load()
        self._interrupted_task_ids: set[str] = set()
        self._typing_sent_at_by_task: dict[str, float] = {}

    def handle_event(self, event: dict[str, Any]) -> None:
        if str(event.get("post_type") or "") != "message":
            return
        message_type = str(event.get("message_type") or "").strip().lower()
        if message_type not in {"private", "group"}:
            return
        if message_type == "group" and not self._is_group_message_for_self(event):
            return
        sender_key = self._sender_key(event)
        text = _format_text_segments(event.get("message"))
        media_attachments, media_errors = self._extract_media_attachments(sender_key, event.get("message"))
        self.message_runtime.handle_message(
            IncomingBridgeMessage(
                sender_id=sender_key,
                text=text,
                reply_target=self._reply_event_snapshot(event),
                source="qq",
                session_name=self._session_name(sender_key),
                attachments=media_attachments,
                attachment_errors=media_errors,
                message_type=message_type,
            )
        )

    def _start_thread(self, name: str, target: Any, *args: Any) -> None:
        def run_target() -> None:
            try:
                target(*args)
            except Exception as exc:  # noqa: BLE001
                _log_error(f"{name} failed: {type(exc).__name__}: {exc}")

        threading.Thread(target=run_target, daemon=True, name=f"qq-bridge-{name}").start()

    def _submit_task(self, sender_key: str, prompt: str) -> dict[str, Any]:
        message = IncomingBridgeMessage(
            sender_id=sender_key,
            text="",
            reply_target={},
            source="qq",
            session_name=self._session_name(sender_key),
            attachments=[],
            attachment_errors=[],
        )
        submitted = self.task_submit_runtime.submit(message, self.conversation_runtime.resolve_session(message), prompt)
        return submitted.payload

    def _submit_runtime_task(
        self,
        message: IncomingBridgeMessage,
        _session_name: str,
        prompt: str,
        _passthrough: bool,
    ) -> BridgeSubmittedTask:
        task = self._submit_task(message.sender_id, prompt)
        return BridgeSubmittedTask(task_id=str(task.get("id") or "").strip(), payload=task)

    def _resolve_task_submit_context(self, message: IncomingBridgeMessage, session: Any) -> BridgeTaskSubmitContext:
        if isinstance(session, dict):
            session_name = str(session.get("session_name") or message.session_name or self._session_name(message.sender_id))
            session_meta = session.get("session_meta")
        else:
            session_name = str(session or message.session_name or self._session_name(message.sender_id))
            session_meta = None
        backend = str(getattr(session_meta, "backend", "") or self.config.default_backend)
        return BridgeTaskSubmitContext(
            agent_id=self.agent_id,
            session_name=session_name,
            backend=backend,
            workdir=str(getattr(session_meta, "workdir", "") or ""),
            model=str(getattr(session_meta, "model", "") or ""),
            reasoning_effort=str(getattr(session_meta, "reasoning_effort", "") or ""),
            permission_mode=str(getattr(session_meta, "permission_mode", "") or ""),
        )

    def _ipc_request(self, action: str, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        return wait_for_response(create_request(action, payload), timeout_seconds=timeout_seconds)

    def _t(self, key: str, **kwargs: Any) -> str:
        return self.localizer.translate(key, **kwargs)

    def _wait_and_reply(self, event: dict[str, Any], task_id: str) -> None:
        PollingTaskDeliveryController(
            default_backend=self.config.default_backend,
            task_timeout_seconds=int(self.config.hub_task_timeout_seconds),
            get_task=self._get_task_for_delivery,
            send_reply=self._send_reply,
            send_typing_keepalive=self._send_typing_keepalive,
            stop_typing=self._stop_typing_best_effort,
            resolve_context_left_percent=self._resolve_task_context_left_percent,
            get_pending_task=lambda pending_task_id: self.pending_tasks.get(pending_task_id),
            update_pending_progress=lambda pending_task_id, last_progress_seq, last_progress_text: self._update_pending_task_progress(
                pending_task_id,
                last_progress_seq=last_progress_seq,
                last_progress_text=last_progress_text,
            ),
            forget_pending_task=self._forget_pending_task,
            log=_log,
        ).wait_and_reply(event, task_id)

    def _bridge_ipc_worker(self) -> None:
        while True:
            self._process_bridge_ipc_once()
            time.sleep(0.2)

    def _process_bridge_ipc_once(self) -> None:
        for request_path in sorted(bridge_request_dir(QQ_BRIDGE_CHANNEL).glob("*.json")):
            action = "unknown"
            try:
                request = read_request(request_path)
                action = request.action or "unknown"
                if action == "task_update":
                    self._handle_pushed_task_update(request.payload)
            except Exception as exc:  # noqa: BLE001
                _log_error(f"bridge ipc request failed {request_path.name}: {type(exc).__name__}: {exc}")
            finally:
                try:
                    mark_bridge_processed(request_path, channel=QQ_BRIDGE_CHANNEL)
                except FileNotFoundError:
                    pass
                _log(f"bridge ipc action={action} path={request_path.name}")

    def _handle_pushed_task_update(self, payload: dict[str, object]) -> None:
        task = HubTask.from_dict(payload.get("task"), default_backend=self.config.default_backend)
        if task is None or not task.source.strip().lower().startswith("qq"):
            return
        pending = self.pending_tasks.get(task.id)
        if pending is None:
            if task.id in self._interrupted_task_ids:
                if task.status in {"canceled", "failed", "succeeded", "unknown_after_restart"}:
                    self._interrupted_task_ids.discard(task.id)
                return
            reply_target = self._reply_target_from_sender_key(task.sender_id)
            if not reply_target:
                return
            pending = BridgePendingReplyTask(
                task_id=task.id,
                sender_key=task.sender_id,
                reply_target=reply_target,
                created_at=now_seconds(),
            )
            self.pending_tasks[task.id] = pending
            self._save_pending_tasks()
        typing_last_sent_at = self._typing_sent_at_by_task.get(task.id, 0.0)
        next_typing_sent_at, _state_updated = TaskUpdateDeliveryController(
            send_progress=self._send_task_progress_update,
            send_terminal=self._send_task_terminal_update,
            save_pending_task=lambda _task_id: self._save_pending_tasks(),
            forget_pending_task=self._forget_pending_task,
            send_typing_keepalive=self._send_typing_keepalive,
        ).handle_task_update(
            reply_target=pending.reply_target,
            task=task,
            pending_task=pending,
            typing_last_sent_at=typing_last_sent_at,
        )
        if task.id in self.pending_tasks:
            self._typing_sent_at_by_task[task.id] = next_typing_sent_at
        else:
            self._typing_sent_at_by_task.pop(task.id, None)

    def _reconcile_pending_tasks(self) -> None:
        for task_id, pending in list(self.pending_tasks.items()):
            task = self._get_task_for_delivery(task_id)
            if task is None:
                continue
            _log(f"reconcile pending task_id={task_id} sender={pending.sender_key}")
            self._handle_pushed_task_update({"task": task.to_dict()})

    def _send_task_progress_update(self, event: dict[str, Any], pending: BridgePendingReplyTask, task: HubTask, progress_delta: str) -> None:
        self._send_reply(
            event,
            format_bridge_progress_reply(
                task,
                progress_text=progress_delta,
                context_left_percent=self._resolve_task_context_left_percent(task),
            ),
        )

    def _send_task_terminal_update(self, event: dict[str, Any], pending: BridgePendingReplyTask, task: HubTask) -> None:
        self._stop_typing_best_effort(event)
        _log(
            f"task terminal task_id={task.id} status={task.status} "
            f"output_preview={task.output[:80]!r} error_preview={task.error[:80]!r}"
        )
        final_reply = format_bridge_task_reply(
            task,
            last_progress_text=pending.last_progress_text,
            context_left_percent=self._resolve_task_context_left_percent(task),
        )
        if final_reply:
            self._send_reply(event, final_reply)

    def _send_typing_keepalive(self, event: dict[str, Any], last_sent_at: float) -> float:
        now_value = time.time()
        if last_sent_at and now_value - last_sent_at < QQ_TYPING_KEEPALIVE_SECONDS:
            return last_sent_at
        if self._send_typing_best_effort(event, enabled=True):
            return now_value
        return last_sent_at

    def _send_typing_best_effort(self, event: dict[str, Any], *, enabled: bool) -> bool:
        if not self._typing_status_available:
            return False
        message_type = str(event.get("message_type") or "").strip().lower()
        if message_type != "private":
            return False
        user_id = event.get("user_id")
        if not user_id:
            return False
        try:
            response = self._onebot_api("set_input_status", {"user_id": user_id, "event_type": 1 if enabled else 0})
            ok = str(response.get("status") or "").lower() == "ok" or int(response.get("retcode") or 0) == 0
            _log(f"typing user_id={user_id} enabled={enabled} status={response.get('status')} retcode={response.get('retcode')}")
            if not ok:
                self._typing_status_available = False
            return ok
        except Exception as exc:  # noqa: BLE001
            self._typing_status_available = False
            _log_error(f"typing failed user_id={user_id} enabled={enabled}: {type(exc).__name__}: {exc}")
            return False

    def _stop_typing_best_effort(self, event: dict[str, Any]) -> None:
        self._send_typing_best_effort(event, enabled=False)

    def _resolve_task_context_left_percent(self, task: HubTask) -> int | None:
        if str(task.backend or self.config.default_backend).strip().lower() != "codex":
            return None
        live_percent = self._query_task_context_left_percent(task.id)
        if live_percent is not None:
            task.context_left_percent = live_percent
            return live_percent
        return normalize_context_left_percent(task.context_left_percent)

    def _query_task_context_left_percent(self, task_id: str) -> int | None:
        try:
            response = self._ipc_request(
                "task_context_left",
                {"task_id": task_id},
                timeout_seconds=2,
            )
        except Exception as exc:  # noqa: BLE001
            _log_error(f"context_left query failed task_id={task_id}: {type(exc).__name__}: {exc}")
            return None
        return normalize_context_left_percent(response.payload.get("context_left_percent") if response.ok else None)

    def _get_task_for_delivery(self, task_id: str) -> HubTask | None:
        response = wait_for_response(
            create_request("get_task", {"task_id": task_id}),
            timeout_seconds=10,
        )
        if not response.ok:
            return None
        return HubTask.from_dict(response.payload.get("task"), default_backend=self.config.default_backend)

    def _send_reply(self, event: dict[str, Any], text: str) -> None:
        message_type = str(event.get("message_type") or "").strip().lower()
        if message_type == "group":
            payload = {"group_id": event.get("group_id"), "message": text}
            response = self._onebot_api("send_group_msg", payload)
            _log(f"sent group_id={event.get('group_id')} status={response.get('status')} retcode={response.get('retcode')} preview={text[:120]!r}")
            return
        payload = {"user_id": event.get("user_id"), "message": text}
        response = self._onebot_api("send_private_msg", payload)
        _log(f"sent user_id={event.get('user_id')} status={response.get('status')} retcode={response.get('retcode')} message_id={(response.get('data') or {}).get('message_id') if isinstance(response.get('data'), dict) else '-'} preview={text[:120]!r}")

    def _onebot_api(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        request = urllib.request.Request(f"{self.api_base}/{action}", data=data, headers=headers, method="POST")
        with LOCAL_URL_OPENER.open(request, timeout=30) as response:  # noqa: S310 - user-configured local OneBot endpoint
            body = response.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _login_user_id_for_matching(self, event: dict[str, Any]) -> str:
        event_self_id = str(event.get("self_id") or "").strip()
        if event_self_id:
            return event_self_id
        if self._login_user_id:
            return self._login_user_id
        payload = self._onebot_api("get_login_info", {})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        self._login_user_id = str(data.get("user_id") or "").strip()
        return self._login_user_id

    def _is_group_message_for_self(self, event: dict[str, Any]) -> bool:
        user_id = self._login_user_id_for_matching(event)
        return _message_mentions_user(event.get("message"), user_id)

    def _extract_media_attachments(self, sender_key: str, message: object) -> tuple[list[dict[str, str]], list[str]]:
        attachments: list[dict[str, str]] = []
        errors: list[str] = []
        for index, segment in enumerate(_media_segments(message), start=1):
            try:
                attachments.append(self._save_media_segment(sender_key, segment, index=index))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"- 第 {index} 个附件：{exc}")
        return attachments, errors

    def _save_media_segment(self, sender_key: str, segment: dict[str, Any], *, index: int) -> dict[str, str]:
        kind = str(segment.get("type") or "file").strip().lower() or "file"
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        url = str(data.get("url") or "").strip()
        if not url:
            raise ValueError("missing OneBot media url")
        raw_name = str(data.get("file") or data.get("filename") or data.get("name") or f"{kind}-{index}")
        filename = _safe_path_part(Path(raw_name.replace("\\", "/")).name)
        saved_path = self._download_media_url(sender_key, url, filename=filename)
        return {"kind": "image" if kind == "image" else "file", "name": filename, "path": str(saved_path)}

    def _download_media_url(self, sender_key: str, url: str, *, filename: str) -> Path:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - URL is supplied by the local OneBot service
            data = response.read(MEDIA_RECEIVE_MAX_BYTES + 1)
        if len(data) > MEDIA_RECEIVE_MAX_BYTES:
            raise ValueError(f"file is too large: {len(data)} bytes")
        target_dir = ONEBOT_UPLOAD_DIR / _safe_path_part(sender_key)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{now_seconds()}-{secrets.token_hex(4)}-{filename}"
        target.write_bytes(data)
        return target

    def _sender_key(self, event: dict[str, Any]) -> str:
        message_type = str(event.get("message_type") or "").strip().lower()
        if message_type == "group":
            return f"qq:group:{event.get('group_id')}:{event.get('user_id')}"
        return f"qq:private:{event.get('user_id')}"

    def _session_name(self, sender_key: str) -> str:
        binding = self._ensure_conversation(sender_key)
        session_name, _ = binding.get_current_session(
            default_backend=self.config.default_backend,
            now=self._now_iso(),
            normalize_backend=self._normalize_backend,
        )
        return session_name or _safe_path_part(sender_key)

    @staticmethod
    def _now_iso() -> str:
        from core.weixin_message_format import now_iso

        return now_iso()

    @staticmethod
    def _normalize_backend(value: str) -> str:
        return normalize_backend(value)

    def _load_conversations(self) -> dict[str, BridgeConversationBinding]:
        payload = load_json(QQ_CONVERSATIONS_PATH, {}, expect_type=dict)
        conversations: dict[str, BridgeConversationBinding] = {}
        now = self._now_iso()
        for sender_key, raw in payload.items():
            cleaned = str(sender_key or "").strip()
            if cleaned:
                conversations[cleaned] = BridgeConversationBinding.from_dict(
                    raw,
                    default_backend=self.config.default_backend,
                    now=now,
                    normalize_backend=normalize_backend,
                )
        return conversations

    def _save_conversations(self) -> None:
        save_json(QQ_CONVERSATIONS_PATH, {key: value.to_dict() for key, value in self.conversations.items()})

    def _ensure_conversation(self, sender_key: str) -> BridgeConversationBinding:
        cleaned = str(sender_key or "").strip()
        binding = self.conversations.get(cleaned)
        if binding is None:
            binding = BridgeConversationBinding.create(default_backend=normalize_backend(self.config.default_backend), now=self._now_iso())
            binding.current_session = _safe_path_part(cleaned)
            binding.last_regular_session = binding.current_session
            binding.sessions = {
                binding.current_session: self._new_session_meta(),
            }
            self.conversations[cleaned] = binding
            self._save_conversations()
        return binding

    def _remove_conversation(self, sender_key: str) -> None:
        self.conversations.pop(str(sender_key or "").strip(), None)
        self._save_conversations()

    def _new_session_meta(
        self,
        backend: Any = "",
        *,
        workdir: str = "",
        model: str = "",
        reasoning_effort: str = "",
        permission_mode: str = "",
    ) -> BridgeSessionMeta:
        return create_session_meta(
            BridgeSessionMeta,
            backend=backend,
            default_backend=self.config.default_backend,
            now=self._now_iso(),
            normalize_backend=normalize_backend,
            workdir=workdir.strip(),
            model=model.strip(),
            reasoning_effort=reasoning_effort.strip(),
            permission_mode=permission_mode.strip(),
        )

    def _allocate_session_name(self, binding: BridgeConversationBinding, requested: str) -> str:
        return allocate_session_name(binding, requested)

    @staticmethod
    def _sanitize_session_name(requested: str, *, fallback: str) -> str:
        return sanitize_session_name(requested, fallback=fallback)

    @staticmethod
    def _sanitize_project_name(requested: str) -> str:
        return sanitize_project_name(requested)

    @staticmethod
    def _split_named_path_args(raw: str) -> tuple[str, str]:
        return split_named_path_args(raw)

    def _load_sender_tasks(self, sender_key: str) -> list[HubTask]:
        return self.task_query_runtime.load_sender_tasks(sender_key)

    def _resolve_fallback_session_target(self, binding: BridgeConversationBinding) -> str:
        return resolve_fallback_session_target(binding)

    def _render_context(self, session_name: str, session_meta: BridgeSessionMeta) -> str:
        return "\n".join(
            [
                f"当前会话: {session_name}",
                f"当前后端: {session_meta.backend}",
                f"当前模型: {self._resolve_session_model(session_meta)}",
                f"当前项目: {self._resolve_session_workdir(session_meta)}",
            ]
        )

    def _render_session_list(
        self,
        sender_key: str,
        binding: BridgeConversationBinding,
        *,
        page: int = 1,
        query: str = "",
        project_path: str | None = "",
        scope_label: str = "",
    ) -> str:
        del project_path, scope_label
        tasks_by_session: dict[str, list[HubTask]] = {}
        for task in self._load_sender_tasks(sender_key):
            tasks_by_session.setdefault(task.session_name or "default", []).append(task)
        names = [name for name in binding.sessions if not query.strip() or query.strip().lower() in name.lower()]
        total_pages = max(1, (len(names) + 10 - 1) // 10)
        current_page = min(max(page, 1), total_pages)
        page_names = names[(current_page - 1) * 10 : current_page * 10]
        lines = [f"Sessions: page {current_page}/{total_pages}"]
        for name in page_names:
            marker = "*" if name == binding.current_session else "-"
            latest = tasks_by_session.get(name, [None])[0]
            summary = self._task_summary_excerpt(latest) if latest is not None else "-"
            lines.append(f"{marker} {name} [{binding.sessions[name].backend}] {summary}")
        if len(lines) == 1:
            lines.append("(empty)")
        return "\n".join(lines)

    def _bulk_delete_sessions(self, binding: BridgeConversationBinding, raw_names: str) -> tuple[str, bool]:
        deleted: list[str] = []
        skipped: list[str] = []
        for name in [item.strip() for item in raw_names.split(",") if item.strip()]:
            if name == "default" or name not in binding.sessions:
                skipped.append(name)
                continue
            binding.sessions.pop(name, None)
            deleted.append(name)
        if binding.current_session not in binding.sessions:
            binding.current_session = self._resolve_fallback_session_target(binding) or next(iter(binding.sessions), "default")
        self._save_conversations()
        return f"Deleted: {', '.join(deleted) or '-'}\nSkipped: {', '.join(skipped) or '-'}\nCurrent session: {binding.current_session}", True

    def _clear_empty_sessions(self, sender_key: str, binding: BridgeConversationBinding) -> tuple[str, bool]:
        sessions_with_tasks = {task.session_name or "default" for task in self._load_sender_tasks(sender_key)}
        deleted: list[str] = []
        for name in list(binding.sessions):
            if name in {binding.current_session, "default"} or name in sessions_with_tasks:
                continue
            binding.sessions.pop(name, None)
            deleted.append(name)
        self._save_conversations()
        return f"Deleted: {', '.join(deleted) or '-'}\nCurrent session: {binding.current_session}", True

    def _render_session_preview(self, sender_key: str, session_name: str, binding: BridgeConversationBinding) -> str:
        tasks = [task for task in self._load_sender_tasks(sender_key) if (task.session_name or "default") == session_name]
        lines = [f"Session preview: {session_name}", f"Backend: {binding.sessions.get(session_name, self._new_session_meta()).backend}", f"Tasks: {len(tasks)}"]
        for task in tasks[:3]:
            lines.append(f"- {task.id} [{task.status}] {self._task_summary_excerpt(task)}")
        return "\n".join(lines)

    def _render_session_history(self, sender_key: str, session_name: str, binding: BridgeConversationBinding) -> str:
        return self._render_session_preview(sender_key, session_name, binding)

    def _export_session_history(self, sender_key: str, session_name: str, binding: BridgeConversationBinding) -> tuple[str, bool]:
        del binding
        export_dir = RUNTIME_DIR / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        path = export_dir / f"{_safe_path_part(sender_key)}__{_safe_path_part(session_name)}.md"
        tasks = [task for task in self._load_sender_tasks(sender_key) if (task.session_name or "default") == session_name]
        lines = [f"# Session Export: {session_name}", "", f"- Sender: {sender_key}", f"- Task Count: {len(tasks)}", ""]
        for task in reversed(tasks):
            lines.extend([f"## {task.id}", "", task.prompt or "(empty)", "", task.output or task.error or "(empty)", ""])
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return f"Session history exported: {path}", True

    def _render_project_file_preview(self, raw_path: str) -> str:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = APP_DIR / candidate
        if not candidate.exists() or not candidate.is_file():
            return f"File not found: {raw_path}"
        return candidate.read_text(encoding="utf-8", errors="replace")[:3000] or "(empty)"

    def _resolve_shareable_project_file(self, raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = APP_DIR / candidate
        resolved = candidate.resolve()
        if not resolved.exists() or not resolved.is_file():
            raise FileNotFoundError(raw_path)
        allowed_roots = [APP_DIR.resolve(), RUNTIME_DIR.resolve()]
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            raise ValueError(f"file is outside allowed roots: {raw_path}")
        return resolved

    def _send_media_to_reply_target(self, reply_target: dict[str, Any], file_path: Path) -> None:
        message_type = str(reply_target.get("message_type") or "").strip().lower()
        if message_type == "group":
            payload = {
                "group_id": reply_target.get("group_id"),
                "file": str(file_path),
                "name": file_path.name,
            }
            response = self._onebot_api("upload_group_file", payload)
            _log(f"sent file group_id={reply_target.get('group_id')} status={response.get('status')} retcode={response.get('retcode')} file={file_path.name}")
            return
        payload = {
            "user_id": reply_target.get("user_id"),
            "file": str(file_path),
            "name": file_path.name,
        }
        response = self._onebot_api("upload_private_file", payload)
        _log(f"sent file user_id={reply_target.get('user_id')} status={response.get('status')} retcode={response.get('retcode')} file={file_path.name}")

    def _render_recent_events(self, sender_key: str, *, limit: int) -> str:
        del sender_key, limit
        return "QQ events are not recorded separately yet."

    def _task_summary_excerpt(self, task: HubTask | None) -> str:
        if task is None:
            return "-"
        return " ".join((task.output or task.error or task.prompt or "").split())[:80] or "-"

    def _resolve_session_workdir(self, session_meta: BridgeSessionMeta) -> str:
        return session_meta.workdir.strip() or str(WORKSPACE_DIR.resolve())

    def _resolve_session_model(self, session_meta: BridgeSessionMeta) -> str:
        return session_meta.model.strip() or "-"

    @staticmethod
    def _resolve_session_permission_mode(session_meta: BridgeSessionMeta) -> str:
        return session_meta.permission_mode.strip().lower() or "full-access"

    def _render_model_status(self, session_name: str, session_meta: BridgeSessionMeta) -> str:
        return f"Current model\nSession: {session_name}\nModel: {self._resolve_session_model(session_meta)}"

    def _render_project_status(self, session_name: str, session_meta: BridgeSessionMeta) -> str:
        return f"Current project\nSession: {session_name}\nDirectory: {self._resolve_session_workdir(session_meta)}"

    def _project_spaces(self) -> dict[str, str]:
        spaces = self._load_registered_project_spaces()
        if WORKSPACE_DIR.exists():
            for child in sorted(item for item in WORKSPACE_DIR.iterdir() if item.is_dir()):
                spaces.setdefault(child.name, str(child.resolve()))
        return spaces

    def _load_registered_project_spaces(self) -> dict[str, str]:
        payload = load_json(STATE_DIR / "project_spaces.json", {}, expect_type=dict)
        return {str(key): str(value) for key, value in payload.items()}

    def _save_registered_project_spaces(self, spaces: dict[str, str]) -> None:
        save_json(STATE_DIR / "project_spaces.json", spaces)

    def _resolve_project_workdir(self, project_arg: str) -> str | None:
        spaces = self._project_spaces()
        if project_arg in spaces:
            return spaces[project_arg]
        candidate = Path(project_arg).expanduser()
        if not candidate.is_absolute():
            candidate = APP_DIR / candidate
        return str(candidate.resolve()) if candidate.exists() and candidate.is_dir() else None

    def _render_project_list(self, session_meta: BridgeSessionMeta) -> str:
        current = self._resolve_session_workdir(session_meta)
        lines = ["Available project directories:"]
        for name, path in self._project_spaces().items():
            marker = "*" if path == current else "-"
            lines.append(f"{marker} {name}: {path}")
        return "\n".join(lines)

    def _render_project_session_list(self, sender_key: str, binding: BridgeConversationBinding, project_arg: str) -> tuple[str, bool]:
        del project_arg
        return self._render_session_list(sender_key, binding), True

    def _render_agent_details(self, agent_id: str) -> str:
        agent = next((item for item in self._load_agents() if item.id == agent_id), None)
        if agent is None:
            return f"Agent not found: {agent_id}"
        return f"Current assistant: {agent.id}\nBackend: {agent.backend or '-'}\nModel: {agent.model or '-'}\nDirectory: {agent.workdir or '-'}"

    def _render_agent_list(self) -> str:
        lines = ["Available assistants:"]
        for agent in self._load_agents():
            marker = "*" if agent.id == self.agent_id else "-"
            lines.append(f"{marker} {agent.id} | {agent.backend or '-'} | {agent.model or '-'}")
        return "\n".join(lines)

    def _render_agent_command_help(self) -> str:
        return "Agent command help is shared with the selected backend CLI."

    @staticmethod
    def _load_agents() -> list[Any]:
        return list(HubConfig.load().agents)

    def _set_backend_agent(self, agent_id: str) -> None:
        self.agent_id = agent_id

    def _clear_current_agent_session(self, sender_key: str, current_session: str) -> str:
        del sender_key
        session_file = SESSION_DIR / f"{_safe_path_part(current_session)}.jsonl"
        if session_file.exists():
            session_file.write_text("", encoding="utf-8")
        return f"Cleared current agent session: {current_session}"

    def _save_pending_tasks(self) -> None:
        self.pending_task_store.save(self.pending_tasks)

    def _remember_pending_task(self, task_id: str, event: dict[str, Any], sender_key: str) -> None:
        self.pending_tasks[task_id] = BridgePendingReplyTask(
            task_id=task_id,
            sender_key=sender_key,
            reply_target=self._reply_event_snapshot(event),
            created_at=now_seconds(),
        )
        self._save_pending_tasks()

    def _remember_submitted_runtime_task(self, message: IncomingBridgeMessage, _session_name: str, submitted: BridgeSubmittedTask) -> None:
        interrupt_messages = message.metadata.get("interrupt_messages")
        self.pending_tasks[submitted.task_id] = BridgePendingReplyTask(
            task_id=submitted.task_id,
            sender_key=message.sender_id,
            reply_target=dict(message.reply_target),
            created_at=now_seconds(),
            interrupt_base_prompt=str(message.metadata.get("interrupt_base_prompt") or submitted.payload.get("prompt") or ""),
            interrupt_messages=[str(item) for item in interrupt_messages if str(item or "").strip()] if isinstance(interrupt_messages, list) else [],
        )
        self._save_pending_tasks()

    def _start_submitted_task_delivery(self, message: IncomingBridgeMessage, _session_name: str, submitted: BridgeSubmittedTask) -> None:
        self._typing_sent_at_by_task[submitted.task_id] = self._send_typing_keepalive(message.reply_target, 0.0)

    def _update_pending_task_progress(self, task_id: str, *, last_progress_seq: int, last_progress_text: str) -> None:
        pending = self.pending_tasks.get(task_id)
        if pending is None:
            return
        pending.last_progress_seq = int(last_progress_seq or 0)
        pending.last_progress_text = str(last_progress_text or "")
        self._save_pending_tasks()

    def _forget_pending_task(self, task_id: str) -> None:
        if self.pending_tasks.pop(task_id, None) is not None:
            self._save_pending_tasks()

    def _find_active_pending_task(self, sender_key: str) -> BridgePendingReplyTask | None:
        for pending in self.pending_tasks.values():
            if pending.sender_key == sender_key:
                return pending
        return None

    def _cancel_task_best_effort(self, task_id: str) -> None:
        self._interrupted_task_ids.add(task_id)
        try:
            self._ipc_request("cancel_task", {"task_id": task_id}, timeout_seconds=5)
        except Exception as exc:  # noqa: BLE001
            _log_error(f"interrupt cancel failed task_id={task_id}: {exc}")
        self._forget_pending_task(task_id)

    def recover_pending_tasks(self) -> None:
        self._start_thread("reconcile_pending_tasks", self._reconcile_pending_tasks)

    @staticmethod
    def _reply_event_snapshot(event: dict[str, Any]) -> dict[str, Any]:
        message_type = str(event.get("message_type") or "").strip().lower()
        if message_type == "group":
            return {"message_type": "group", "group_id": event.get("group_id"), "user_id": event.get("user_id")}
        user_id = event.get("user_id")
        if user_id:
            return {"message_type": "private", "user_id": user_id}
        return {}

    @staticmethod
    def _reply_target_from_sender_key(sender_key: str) -> dict[str, Any]:
        parts = str(sender_key or "").split(":")
        if len(parts) == 3 and parts[0] == "qq" and parts[1] == "private" and parts[2]:
            return {"message_type": "private", "user_id": parts[2]}
        if len(parts) == 4 and parts[0] == "qq" and parts[1] == "group" and parts[2] and parts[3]:
            return {"message_type": "group", "group_id": parts[2], "user_id": parts[3]}
        return {}

    def make_handler(self) -> type[BaseHTTPRequestHandler]:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def _send_onebot_ok(self) -> None:
                payload = b'{"status":"ok","retcode":0,"data":null}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _read_body(self) -> bytes:
                transfer_encoding = str(self.headers.get("Transfer-Encoding") or "").lower()
                if "chunked" in transfer_encoding:
                    chunks: list[bytes] = []
                    while True:
                        size_line = self.rfile.readline().strip()
                        if not size_line:
                            break
                        size_text = size_line.split(b";", 1)[0]
                        try:
                            chunk_size = int(size_text, 16)
                        except ValueError:
                            raise ValueError(f"invalid chunk size: {size_line!r}") from None
                        if chunk_size <= 0:
                            while True:
                                trailer = self.rfile.readline()
                                if trailer in {b"\r\n", b"\n", b""}:
                                    break
                            break
                        chunks.append(self.rfile.read(chunk_size))
                        self.rfile.read(2)
                    return b"".join(chunks)
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length > 0 else b""

            def do_POST(self) -> None:  # noqa: N802
                body = self._read_body().decode("utf-8", errors="replace")
                if not body.strip():
                    self._send_onebot_ok()
                    return
                try:
                    payload = json.loads(body)
                except Exception as exc:  # noqa: BLE001
                    print(f"[qq-bridge] invalid OneBot payload: {exc}; body={body[:500]!r}", file=sys.stderr, flush=True)
                    self._send_onebot_ok()
                    return

                events = payload if isinstance(payload, list) else [payload]
                accepted = 0
                for event in events:
                    if not isinstance(event, dict):
                        _log_error(f"ignored non-object OneBot event: {event!r}")
                        continue
                    accepted += 1
                    bridge._start_thread("handle_event", bridge.handle_event, event)
                if accepted:
                    _log(f"accepted {accepted} OneBot event(s)")
                self._send_onebot_ok()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        return Handler


def run() -> int:
    cleanup_processed_requests()
    config = BridgeConfig.load()
    bridge = QQOneBotBridge(config)
    bridge._start_thread("bridge_ipc", bridge._bridge_ipc_worker)
    bridge.recover_pending_tasks()
    host = os.environ.get("QQ_ONEBOT_LISTEN_HOST") or "127.0.0.1"
    port = int(os.environ.get("QQ_ONEBOT_LISTEN_PORT") or "5701")
    server = ThreadingHTTPServer((host, port), bridge.make_handler())
    print(f"QQ OneBot Bridge listening on http://{host}:{port}; api={bridge.api_base}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
