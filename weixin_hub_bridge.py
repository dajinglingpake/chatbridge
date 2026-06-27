from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import random
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

from agent_backends import get_backend_command_guide, supported_backend_keys
from agent_backends.shared import resolve_session_file
from agent_hub import HubConfig
from bridge_config import APP_DIR, CONFIG_PATH, WEIXIN_ACCOUNTS_DIR, BridgeConfig, normalize_backend
from core.accounts import account_conversation_path, load_account_context_tokens, save_account_context_tokens
from core.app_service import schedule_named_action
from core.bridge_command_catalog import HELP_MESSAGE_KEYS, normalize_command_text, parse_bridge_command
from core.bridge_command_router import BridgeCommandRouter
from core.bridge_conversation_runtime import BridgeConversationRuntime
from core.bridge_control_runtime import BridgeControlRuntime
from core.bridge_interrupt_runtime import BridgeInterruptRuntime
from core.bridge_message_control import normalize_context_left_percent, normalize_message_for_dedupe
from core.bridge_pending_tasks import JsonBackedTaskStore
from core.bridge_runtime import (
    BridgeMessageRuntime,
    BridgeSubmittedTask,
    IncomingBridgeMessage,
    PendingMediaContextStore,
)
from core.bridge_media_control_runtime import BridgeMediaControlRuntime
from core.bridge_native_menu_runtime import BridgeNativeMenuRuntime, PERMISSION_MODE_PRESETS
from core.bridge_notify_control_runtime import BridgeNotifyControlRuntime
from core.bridge_prompt_runtime import BridgePromptRuntime
from core.bridge_service_control_runtime import BridgeServiceControlRuntime, WEIXIN_RESTART_SCOPES
from core.bridge_session_control_runtime import BridgeSessionControlRuntime
from core.bridge_session_utils import (
    allocate_session_name,
    create_session_meta,
    resolve_fallback_session_target,
    sanitize_project_name,
    sanitize_session_name,
    split_named_path_args,
)
from core.bridge_task_delivery import TaskUpdateDeliveryController
from core.bridge_task_query_runtime import BridgeTaskQueryRuntime
from core.bridge_task_submit_runtime import BridgeTaskSubmitContext, BridgeTaskSubmitRuntime
from core.codex_model_catalog import display_model, display_reasoning_effort, load_codex_model_catalog
from core.context_relations import build_context_relation_lines
from core.http_json import request_json
from core.json_store import load_json, save_json
from core.weixin_delivery_failures import pop_failed_delivery, record_failed_delivery
from core.runtime_paths import (
    BRIDGE_EVENT_LOG_PATH,
    BRIDGE_MESSAGE_AUDIT_LOG_PATH,
    BRIDGE_PENDING_TASKS_PATH,
    BRIDGE_RESTART_NOTICE_PATH,
    SERVICE_ACTION_STATE_PATH,
    BRIDGE_STATE_PATH,
    BRIDGE_CONVERSATIONS_PATH,
    LOG_DIR,
    PROJECT_SPACES_PATH as BRIDGE_PROJECT_SPACES_PATH,
    RUNTIME_DIR,
    SESSION_DIR,
    STATE_DIR,
)
from core.state_models import (
    HubTask,
    IpcResponseEnvelope,
    BridgeRuntimeState,
    WeixinConversationBinding,
    WeixinPendingTaskState,
    WeixinSessionMeta,
)
from core.weixin_send_gate import sender_send_lock
from core.weixin_text_outbox import MAX_RETRY_ATTEMPTS, enqueue_text_message, pop_text_messages, requeue_text_message
from core.weixin_notifier import broadcast_weixin_notice_by_kind, build_task_followup_hint
from core.weixin_message_format import format_duration_since, format_weixin_reply, now_iso, prefix_weixin_output
from local_ipc import bridge_request_dir, cleanup_processed_requests, mark_bridge_processed, create_request, read_request, wait_for_response
from localization import Localizer


def _configure_process_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


_configure_process_stdio()


EXPORT_DIR = RUNTIME_DIR / "exports"
UPLOAD_DIR = RUNTIME_DIR / "uploads"
STATE_PATH = BRIDGE_STATE_PATH
CONVERSATION_PATH = BRIDGE_CONVERSATIONS_PATH
PENDING_TASKS_PATH = BRIDGE_PENDING_TASKS_PATH
EVENT_LOG_PATH = BRIDGE_EVENT_LOG_PATH
MESSAGE_AUDIT_LOG_PATH = BRIDGE_MESSAGE_AUDIT_LOG_PATH
RESTART_NOTICE_PATH = BRIDGE_RESTART_NOTICE_PATH
SERVICE_ACTION_STATE_FILE = SERVICE_ACTION_STATE_PATH
PROJECT_SPACES_PATH = BRIDGE_PROJECT_SPACES_PATH
PENDING_MEDIA_CONTEXT_PATH = STATE_DIR / "weixin_pending_media_context.json"
DEFAULT_WEIXIN_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (1 << 8) | 1
SUPPORTED_BACKENDS = set(supported_backend_keys())
SESSION_PAGE_SIZE = 5
ACTIVE_TASK_POLL_TIMEOUT_MS = 500
TYPING_KEEPALIVE_SECONDS = 5
TYPING_TICKET_TTL_SECONDS = 23 * 60 * 60
PENDING_TASK_RECONCILE_INTERVAL_SECONDS = 45.0
PENDING_TASK_RECONCILE_BATCH_SIZE = 5
PENDING_TASK_RECONCILE_TIMEOUT_SECONDS = 2.0
PERF_LOG_MIN_SECONDS = 0.25
ACTIVE_GETUPDATES_SLOW_LOG_SECONDS = 1.0
CONTEXT_LEFT_QUERY_TIMEOUT_SECONDS = 2.0
PROGRESS_REPLY_MAX_QUEUE_AGE_SECONDS = 30
NOTICE_MAX_QUEUE_AGE_SECONDS = 30
MEDIA_SEND_MAX_BYTES = 25 * 1024 * 1024
MEDIA_RECEIVE_MAX_BYTES = 50 * 1024 * 1024
MEDIA_CONTEXT_TTL_SECONDS = 10 * 60
MEDIA_UPLOAD_TYPE_IMAGE = 1
MEDIA_UPLOAD_TYPE_FILE = 3
MESSAGE_ITEM_TYPE_IMAGE = 2
MESSAGE_ITEM_TYPE_FILE = 4
INCOMING_MEDIA_ITEM_TYPES = frozenset({MESSAGE_ITEM_TYPE_IMAGE, MESSAGE_ITEM_TYPE_FILE})
SHOWFILE_PREVIEW_LIMIT = 3200
SENDMEDIA_IMAGE_EXTENSIONS = frozenset({".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"})
SHOWFILE_ALLOWED_EXTENSIONS = frozenset(
    {
        ".bat",
        ".cmd",
        ".css",
        ".html",
        ".js",
        ".json",
        ".md",
        ".ps1",
        ".py",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_PERMANENT_DELIVERY_ERROR_MARKERS = (
    "errcode=-14",
    "session timeout",
    "missing context token",
)


def _is_permanent_delivery_error(error: Exception | str) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in _PERMANENT_DELIVERY_ERROR_MARKERS)

def _is_ephemeral_delivery_text(text: object) -> bool:
    first_line = str(text or "").strip().splitlines()[0:1]
    if not first_line:
        return False
    return first_line[0].split(" · ", 1)[0].split(maxsplit=1)[0] == "running"

def _is_stale_ephemeral_delivery(text: object, queue_delay_ms: int | None) -> bool:
    return (
        queue_delay_ms is not None
        and queue_delay_ms > PROGRESS_REPLY_MAX_QUEUE_AGE_SECONDS * 1000
        and _is_ephemeral_delivery_text(text)
    )


def _is_notice_delivery_message(message: dict[str, object]) -> bool:
    return str(message.get("source") or "").strip().lower() == "notice"

def _is_stale_notice_delivery(message: dict[str, object], queue_delay_ms: int | None) -> bool:
    return (
        queue_delay_ms is not None
        and queue_delay_ms > NOTICE_MAX_QUEUE_AGE_SECONDS * 1000
        and _is_notice_delivery_message(message)
    )

SHOWFILE_BLOCKED_PATH_PARTS = frozenset({".git", ".runtime", ".venv", "__pycache__", "accounts", "sessions"})


def _encrypt_aes_128_ecb(data: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _decrypt_aes_128_ecb(data: bytes, key: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def resolve_bridge_language(config_language: str) -> str:
    cleaned = str(config_language or "").strip()
    if cleaned and cleaned.lower() != "auto":
        return cleaned
    env_language = str(os.environ.get("CHATBRIDGE_LANG") or "").strip()
    if env_language:
        return env_language
    return "zh-CN"


def _append_delivery_header_suffix(text: str, suffix: str) -> str:
    lines = str(text or "").splitlines()
    if not lines or not suffix:
        return str(text or "")
    header_parts = lines[0].split(" · ")
    if header_parts:
        header_parts[0] = f"{header_parts[0]} {suffix}"
        lines[0] = " · ".join(header_parts)
    else:
        lines[0] = f"{lines[0]} {suffix}"
    return "\n".join(lines)


class WeixinBridge:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.localizer = Localizer(resolve_bridge_language(config.language))
        self.account_path = Path(config.account_file)
        self.sync_path = Path(config.sync_file)
        self._ensure_local_account_storage()
        self.context_tokens = load_account_context_tokens(self.account_path)
        self.conversation_path = account_conversation_path(CONVERSATION_PATH, config.active_account_id, self.account_path)
        self.conversations = self._load_conversations()
        self.pending_task_store = JsonBackedTaskStore(
            PENDING_TASKS_PATH,
            from_dict=WeixinPendingTaskState.from_dict,
            to_dict=lambda tracked: tracked.to_dict(),
        )
        self.pending_tasks = self._load_pending_tasks()
        self._interrupted_task_ids: set[str] = set()
        self.pending_media_store = PendingMediaContextStore(
            PENDING_MEDIA_CONTEXT_PATH,
            ttl_seconds=MEDIA_CONTEXT_TTL_SECONDS,
            now_seconds=lambda: int(time.time()),
        )
        self.pending_media_context = self.pending_media_store.contexts
        self.conversation_runtime = BridgeConversationRuntime(
            ensure_conversation=self._ensure_conversation,
            default_backend=lambda: self.config.default_backend,
            now=now_iso,
            normalize_backend=normalize_backend,
        )
        self.task_query_runtime = BridgeTaskQueryRuntime(
            ipc_request=lambda action, payload, timeout_seconds: self._ipc_request(action, payload, timeout_seconds=timeout_seconds),
            default_backend=lambda: self.config.default_backend,
        )
        self.message_runtime = BridgeMessageRuntime(
            pending_media=self.pending_media_store,
            resolve_session=self.conversation_runtime.resolve_session,
            prepare_prompt=lambda message, session: self.prompt_runtime.prepare_for_session(message, session),
            submit_task=self._submit_runtime_task,
            remember_pending_task=self._remember_runtime_pending_task,
            send_reply=self._send_runtime_reply,
            should_ignore=self._runtime_should_ignore,
            is_duplicate=self._runtime_is_duplicate,
            on_ignored=self._runtime_ignored,
            on_media_context=self._runtime_media_context,
            on_media_error=self._runtime_media_error,
            on_empty_prompt=self._runtime_empty_prompt,
            on_before_submit=self._runtime_before_submit,
            on_after_submit=self._runtime_after_submit,
            log=lambda message: print(f"[bridge] {message}", flush=True),
        )
        self.control_runtime = BridgeControlRuntime(
            help_message_keys=HELP_MESSAGE_KEYS,
            translate=lambda key, **kwargs: self._t(key, **kwargs),
            default_backend=lambda: self.config.default_backend,
            agent_id=lambda: self.config.backend_id,
            session_name=lambda sender_id: self._current_session_name(sender_id),
            get_task=self.task_query_runtime.get_task,
            find_latest_sender_task=lambda sender_id, allowed_statuses: self.task_query_runtime.find_latest_sender_task(
                sender_id,
                allowed_statuses=allowed_statuses,
            ),
            ipc_request=lambda action, payload, timeout_seconds: self._ipc_request(action, payload, timeout_seconds=timeout_seconds),
            retry_source="wechat",
            unsupported_message=None,
            render_status_reply=self._render_control_status,
            render_task_summary_reply=self._render_task_summary,
            restrict_task_lookup_to_sender=False,
        )
        self.session_control_runtime = BridgeSessionControlRuntime(
            adapter=self,
            app_dir=APP_DIR,
            supported_backends=SUPPORTED_BACKENDS,
        )
        self.service_control_runtime = BridgeServiceControlRuntime(
            schedule_action=lambda action: schedule_named_action(action, delay_seconds=1.0).message,
            render_usage=lambda: self._t("bridge.restart.usage"),
            state_path=SERVICE_ACTION_STATE_FILE,
            default_restart_scope="all",
            restart_scopes=WEIXIN_RESTART_SCOPES,
            before_restart=lambda sender_id, scope: self._store_pending_restart_notice(sender_id, scope=scope),
            render_status=self._render_restart_status,
        )
        self.notify_control_runtime = BridgeNotifyControlRuntime(
            config=self.config,
            translate=lambda key, **kwargs: self._t(key, **kwargs),
            send_test_notice=self._send_notify_test_notice,
        )
        self.command_router = BridgeCommandRouter(
            (
                self.control_runtime,
                self.session_control_runtime,
                self.service_control_runtime,
                self.notify_control_runtime,
            ),
            unknown_bridge_command_reply=lambda: self._t("bridge.command.unknown"),
        )
        self.media_control_runtime = BridgeMediaControlRuntime(
            translate=lambda key, **kwargs: self._t(key, **kwargs),
            resolve_file=lambda raw_path: self._resolve_shareable_project_file(raw_path),
            send_file=lambda reply_target, file_path: self._send_media_to_reply_target(reply_target, file_path),
        )
        self.native_menu_runtime = BridgeNativeMenuRuntime(
            translate=lambda key, **kwargs: self._t(key, **kwargs),
            now=now_iso,
            load_model_catalog=lambda: self._load_codex_model_catalog(),
            resolve_session_model=self._resolve_session_model,
            resolve_session_permission_mode=self._resolve_session_permission_mode,
            display_permission_mode=self._display_permission_mode,
        )
        self.prompt_runtime = BridgePromptRuntime(
            native_menu=self.native_menu_runtime,
            media_control=self.media_control_runtime,
            handle_control=self.command_router.handle,
            send_reply=self._send_runtime_reply,
            save_conversations=self._save_conversations,
            unsupported_agent_slash_reply=lambda command: self._t("bridge.passthrough.unsupported", command=command),
            unsupported_bridge_command_reply=lambda command: self._t("bridge.passthrough.unsupported", command=command),
            render_local_passthrough=self._render_local_codex_status,
            on_handled=self._runtime_prompt_handled,
            save_state=self._save_state,
        )
        self.task_submit_runtime = BridgeTaskSubmitRuntime(
            ipc_request=lambda action, payload, timeout_seconds: self._ipc_request(action, payload, timeout_seconds=timeout_seconds),
            resolve_context=self._resolve_task_submit_context,
            on_complete=self._task_submit_completed,
        )
        self.interrupt_runtime = BridgeInterruptRuntime(
            find_active_task=self._find_active_pending_task,
            cancel_task=self._cancel_task_best_effort,
            submit_delayed=lambda message, session, prompt, passthrough: self.message_runtime.submit_prepared(message, session, prompt, passthrough),
            send_reply=self._send_runtime_reply,
            save_pending_tasks=self._save_pending_tasks,
        )
        self.message_runtime.interrupt_runtime = self.interrupt_runtime
        self._recent_message_keys: list[str] = []
        self._recent_message_fingerprints: dict[str, float] = {}
        self._send_worker_started = False
        self._typing_worker_started = False
        self._pending_tasks_save_lock = threading.Lock()
        self._runtime_config_lock = threading.Lock()
        self._pending_reconcile_last_at = 0.0
        self._pending_reconcile_cursor = 0
        self.state = BridgeRuntimeState.create(
            now=now_iso(),
            managed_conversations=len(self.conversations),
            account_file=str(self.account_path),
            sync_file=str(self.sync_path),
        )

    def _load_registered_project_spaces(self) -> dict[str, str]:
        raw = load_json(PROJECT_SPACES_PATH, {}, expect_type=dict)
        payload = raw.get("projects") if isinstance(raw, dict) else {}
        if not isinstance(payload, dict):
            return {}
        spaces: dict[str, str] = {}
        for raw_name, raw_path in payload.items():
            name = self._sanitize_project_name(str(raw_name))
            if not name:
                continue
            candidate = Path(str(raw_path or "").strip()).expanduser()
            if not candidate.exists() or not candidate.is_dir():
                continue
            spaces[name] = str(candidate.resolve())
        return spaces

    def _save_registered_project_spaces(self, spaces: dict[str, str]) -> None:
        PROJECT_SPACES_PATH.parent.mkdir(parents=True, exist_ok=True)
        ordered = {name: spaces[name] for name in sorted(spaces)}
        save_json(PROJECT_SPACES_PATH, {"projects": ordered})

    def run(self) -> None:
        print(f"Weixin Hub Bridge started at {now_iso()}", flush=True)
        print(f"Config: {CONFIG_PATH}", flush=True)
        print(f"State: {STATE_PATH}", flush=True)
        cleanup_processed_requests()
        self._start_send_worker()
        self._ensure_typing_worker_started()
        self._notify_service_started()
        self._deliver_pending_restart_notice()
        while True:
            try:
                self.poll_once()
                self.state.clear_error()
                self._save_state()
            except Exception as exc:  # noqa: BLE001
                self.state.set_error(str(exc))
                self._save_state()
                print(f"[bridge] poll error: {exc}", flush=True)
                time.sleep(3)

    def _start_send_worker(self) -> None:
        if self._send_worker_started:
            return
        worker = threading.Thread(target=self._send_worker_loop, daemon=True, name="weixin-send-worker")
        worker.start()
        self._send_worker_started = True

    def _reload_runtime_config_if_changed(self) -> None:
        with self._runtime_config_lock:
            latest = BridgeConfig.load()
            latest_account_path = Path(latest.account_file)
            latest_sync_path = Path(latest.sync_file)
            same_account = latest_account_path == self.account_path and latest_sync_path == self.sync_path
            same_language = latest.language == self.config.language
            same_backend = latest.backend_id == self.config.backend_id
            if same_account and same_language and same_backend:
                return

            save_account_context_tokens(self.account_path, self.context_tokens)
            self._save_conversations()
            previous_account_id = self.config.active_account_id
            self.config = latest
            self.localizer = Localizer(resolve_bridge_language(latest.language))
            self.account_path = latest_account_path
            self.sync_path = latest_sync_path
            self._ensure_local_account_storage()
            self.context_tokens = load_account_context_tokens(self.account_path)
            self.conversation_path = account_conversation_path(CONVERSATION_PATH, latest.active_account_id, self.account_path)
            self.conversations = self._load_conversations()
            self.state.sync_files(
                managed_conversations=len(self.conversations),
                account_file=str(self.account_path),
                sync_file=str(self.sync_path),
            )
            print(
                f"[bridge] runtime config reloaded: account {previous_account_id or '-'} -> {self.config.active_account_id or '-'}",
                flush=True,
            )

    def _send_worker_loop(self) -> None:
        while True:
            messages = pop_text_messages(limit=20)
            if not messages:
                time.sleep(0.2)
                continue
            try:
                self._reload_runtime_config_if_changed()
                account = self._load_account()
                token = (account.get("token") or "").strip()
                base_url = (account.get("baseUrl") or DEFAULT_WEIXIN_BASE_URL).strip()
                if not token:
                    raise RuntimeError("weixin account token is missing")
            except Exception as exc:  # noqa: BLE001
                print(f"[bridge] send worker account load failed: {exc}", flush=True)
                for message in messages:
                    self._handle_async_send_failure(message, exc)
                time.sleep(1)
                continue
            for message in messages:
                if not self._message_matches_active_account(message):
                    self._drop_stale_account_message(message)
                    continue
                try:
                    queue_delay_ms = self._message_queue_delay_ms(message)
                    if queue_delay_ms is not None and queue_delay_ms >= int(PERF_LOG_MIN_SECONDS * 1000):
                        print(
                            f"[bridge-perf] outbox_queue_delay duration_ms={queue_delay_ms} "
                            f"to={message.get('to_user_id', '')} attempt={int(message.get('attempt') or 0)}",
                            flush=True,
                        )
                    text = str(message.get("text") or "")
                    attempt = int(message.get("attempt") or 0)
                    if _is_stale_ephemeral_delivery(text, queue_delay_ms):
                        preview = " ".join(text.split())[:160]
                        print(
                            f"[bridge] dropped stale progress reply to={message.get('to_user_id', '')} "
                            f"age_ms={queue_delay_ms} attempt={attempt} preview={preview}",
                            flush=True,
                        )
                        continue
                    if _is_stale_notice_delivery(message, queue_delay_ms):
                        preview = " ".join(text.split())[:160]
                        print(
                            f"[bridge] dropped stale notice to={message.get('to_user_id', '')} "
                            f"age_ms={queue_delay_ms} attempt={attempt} preview={preview}",
                            flush=True,
                        )
                        continue
                    if attempt > 0:
                        text = self._format_retried_delivery_text(text, attempt)
                    to_user_id = str(message.get("to_user_id") or "")
                    queued_context_token = str(message.get("context_token") or "")
                    context_token = self._resolve_context_token_for_delivery(to_user_id, queued_context_token)
                    if context_token != queued_context_token:
                        print(
                            f"[bridge] refreshed queued reply context token to={to_user_id} "
                            f"attempt={attempt}",
                            flush=True,
                        )
                    self._deliver_text_now(
                        base_url,
                        token,
                        to_user_id,
                        context_token,
                        text,
                        log_failure=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    attempt = int(message.get("attempt") or 0)
                    permanent = _is_permanent_delivery_error(exc)
                    if (
                        not permanent
                        and attempt == 0
                        and not _is_ephemeral_delivery_text(message.get("text"))
                        and not _is_notice_delivery_message(message)
                    ):
                        print(
                            "[bridge] async send "
                            f"retry scheduled to={message.get('to_user_id', '')} "
                            f"attempt={attempt + 1}/{MAX_RETRY_ATTEMPTS + 1} error={exc}",
                            flush=True,
                        )
                    self._handle_async_send_failure(message, exc)

    def _message_matches_active_account(self, message: dict[str, object]) -> bool:
        message_account_id = str(message.get("account_id") or "").strip()
        message_account_file = str(message.get("account_file") or "").strip()
        if message_account_id and message_account_id != self.config.active_account_id:
            return False
        if message_account_file and Path(message_account_file) != self.account_path:
            return False
        return True

    @staticmethod
    def _message_queue_delay_ms(message: dict[str, object]) -> int | None:
        created_at_ms = message.get("created_at_ms")
        try:
            if created_at_ms not in (None, ""):
                return max(0, int(time.time() * 1000) - int(created_at_ms))
            created_at = message.get("created_at")
            if created_at in (None, ""):
                return None
            return max(0, int(time.time()) - int(created_at)) * 1000
        except (TypeError, ValueError):
            return None

    def _drop_stale_account_message(self, message: dict[str, object]) -> None:
        preview = " ".join(str(message.get("text") or "").split())[:160]
        print(
            "[bridge] dropped stale queued reply "
            f"message_account={message.get('account_id') or '-'} "
            f"active_account={self.config.active_account_id or '-'} "
            f"to={message.get('to_user_id') or '-'} preview={preview}",
            flush=True,
        )

    def _notify_service_started(self) -> None:
        if self._has_pending_restart_notice():
            print("[bridge] startup notice skipped: pending restart notice will be delivered directly", flush=True)
            return
        detail = (
            f"Bridge 已启动\n"
            f"账号: {self.config.active_account_id or '-'}\n"
            f"默认 Agent: {self.config.backend_id or 'main'}"
        )
        result = broadcast_weixin_notice_by_kind("service", "Bridge 启动", detail, config=self.config)
        print(f"[bridge] startup notice: {result.summary}", flush=True)
        if result.error and result.error != "disabled":
            print(f"[bridge] startup notice error: {result.error}", flush=True)

    def _has_pending_restart_notice(self) -> bool:
        payload = load_json(RESTART_NOTICE_PATH, {}, expect_type=dict)
        if not isinstance(payload, dict):
            return False
        sender_id = str(payload.get("sender_id") or "").strip()
        context_token = str(payload.get("context_token") or "").strip()
        return bool(sender_id and context_token)

    def _deliver_pending_restart_notice(self) -> None:
        payload = load_json(RESTART_NOTICE_PATH, {}, expect_type=dict)
        if not isinstance(payload, dict):
            return
        sender_id = str(payload.get("sender_id") or "").strip()
        context_token = str(payload.get("context_token") or "").strip()
        scope = str(payload.get("scope") or "all").strip().lower() or "all"
        requested_at = str(payload.get("requested_at") or "").strip()
        if not sender_id or not context_token:
            RESTART_NOTICE_PATH.unlink(missing_ok=True)
            return
        try:
            account = self._load_account()
            token = (account.get("token") or "").strip()
            base_url = (account.get("baseUrl") or DEFAULT_WEIXIN_BASE_URL).strip()
            if not token:
                raise RuntimeError("weixin account token is missing")
            scope_label = "Bridge" if scope == "bridge" else "Hub + Bridge"
            detail_lines = [
                "服务已重启成功",
                f"范围: {scope_label}",
                f"时间: {now_iso()}",
            ]
            if requested_at:
                detail_lines.append(f"请求时间: {requested_at}")
            self._deliver_text_now(base_url, token, sender_id, context_token, "\n".join(detail_lines))
            print(f"[bridge] restart notice delivered to {sender_id}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge] restart notice delivery failed: {exc}", flush=True)
        finally:
            RESTART_NOTICE_PATH.unlink(missing_ok=True)

    def poll_once(self) -> None:
        poll_started = time.perf_counter()
        self._reload_runtime_config_if_changed()
        account = self._load_account()
        token = (account.get("token") or "").strip()
        if not token:
            raise RuntimeError("weixin account token is missing; please log in first")
        base_url = (account.get("baseUrl") or DEFAULT_WEIXIN_BASE_URL).strip()
        self._process_bridge_ipc_once(base_url, token)
        self._reconcile_pending_tasks(base_url, token)
        buf = self._load_sync_buf()

        payload = {"get_updates_buf": buf, "base_info": {"channel_version": "2.1.1"}}
        active_task_poll = bool(self.pending_tasks)
        timeout_ms = ACTIVE_TASK_POLL_TIMEOUT_MS if active_task_poll else self.config.poll_timeout_ms
        getupdates_started = time.perf_counter()
        try:
            response = self._post_json(f"{base_url}/ilink/bot/getupdates", payload, token=token, timeout_ms=timeout_ms)
        except RuntimeError as exc:
            expected_timeout = self._is_expected_getupdates_timeout(exc)
            if expected_timeout:
                if active_task_poll:
                    self._log_perf(
                        "getupdates",
                        getupdates_started,
                        min_seconds=ACTIVE_GETUPDATES_SLOW_LOG_SECONDS,
                        status="timeout",
                        active="true",
                        timeout_ms=timeout_ms,
                    )
                self.state.mark_poll(now=now_iso())
                self._save_state()
                return
            self._log_perf("getupdates", getupdates_started, force=True, status="failed", active=str(active_task_poll).lower(), timeout_ms=timeout_ms)
            raise
        message_count = len(response.get("msgs") or [])
        if message_count:
            self._log_perf("getupdates", getupdates_started, force=True, status="ok", messages=message_count, timeout_ms=timeout_ms)
        elif active_task_poll:
            self._log_perf(
                "getupdates",
                getupdates_started,
                min_seconds=ACTIVE_GETUPDATES_SLOW_LOG_SECONDS,
                status="ok",
                messages=0,
                active="true",
                timeout_ms=timeout_ms,
            )
        self.state.mark_poll(now=now_iso())
        if response.get("ret") not in (None, 0):
            raise RuntimeError(f"weixin getupdates failed: ret={response.get('ret')} errcode={response.get('errcode')} errmsg={response.get('errmsg')}")

        next_buf = response.get("get_updates_buf")
        if isinstance(next_buf, str) and next_buf:
            self._save_sync_buf(next_buf)

        for msg in response.get("msgs") or []:
            self._handle_message(base_url, token, msg)

        self._process_bridge_ipc_once(base_url, token)
        self._reconcile_pending_tasks(base_url, token)
        self._save_state()
        if message_count or self.pending_tasks:
            self._log_perf("poll_once", poll_started, force=bool(message_count), status="ok", pending=len(self.pending_tasks), messages=message_count)

    def _handle_message(self, base_url: str, token: str, msg: dict[str, Any]) -> None:
        message_started = time.perf_counter()
        sender_id = str(msg.get("from_user_id") or "").strip()
        if not sender_id:
            return
        self._remember_context_token(sender_id, msg.get("context_token"))
        text = self._extract_text(msg)
        message_key = self._message_key(msg, text)
        media_attachments, media_errors = self._extract_media_attachments(base_url, token, sender_id, msg)
        self.message_runtime.handle_message(
            IncomingBridgeMessage(
                sender_id=sender_id,
                text=text,
                reply_target={
                    "base_url": base_url,
                    "token": token,
                    "to_user_id": sender_id,
                    "context_token": str(msg.get("context_token") or "").strip(),
                },
                source="wechat",
                session_name="",
                attachments=media_attachments,
                attachment_errors=media_errors,
                message_type=str(msg.get("message_type") or ""),
                context_token=str(msg.get("context_token") or "").strip(),
                metadata={
                    "raw_message": msg,
                    "message_key": message_key,
                    "started_at": message_started,
                    "has_media": self._has_incoming_media(msg),
                },
            )
        )

    def _send_runtime_reply(self, reply_target: dict[str, Any], text: str) -> None:
        self._send_text(
            str(reply_target.get("base_url") or ""),
            str(reply_target.get("token") or ""),
            str(reply_target.get("to_user_id") or ""),
            str(reply_target.get("context_token") or ""),
            text,
        )

    def _runtime_should_ignore(self, message: IncomingBridgeMessage) -> bool:
        text = str(message.text or "")
        return any(text.startswith(prefix) for prefix in self.config.ignore_prefixes)

    def _runtime_is_duplicate(self, message: IncomingBridgeMessage) -> bool:
        return self._is_duplicate_message(
            str(message.metadata.get("message_key") or ""),
            sender_id=message.sender_id,
            text=message.text,
        )

    def _runtime_ignored(self, message: IncomingBridgeMessage, reason: str) -> None:
        self._append_message_audit(
            sender_id=message.sender_id,
            text=message.text,
            route="ignored",
            reason=reason,
        )

    def _runtime_media_context(self, message: IncomingBridgeMessage) -> None:
        self._append_message_audit(
            sender_id=message.sender_id,
            text="",
            route="media_context",
            attachment_count=len(message.attachments),
            error_count=len(message.attachment_errors),
        )
        self.state.record_handled()
        self._save_state()

    def _runtime_media_error(self, _message: IncomingBridgeMessage) -> None:
        self.state.record_failed()
        self._save_state()

    def _runtime_prompt_handled(self, message: IncomingBridgeMessage, route: str, command: str, session: Any) -> None:
        session_name = str(session.get("session_name") or "default") if isinstance(session, dict) else "default"
        self._append_message_audit(
            sender_id=message.sender_id,
            text=message.text,
            route=route,
            session_name=session_name,
            command=command,
        )
        self.state.record_handled()

    def _runtime_empty_prompt(self, message: IncomingBridgeMessage, _session: dict[str, Any]) -> None:
        self._append_message_audit(
            sender_id=message.sender_id,
            text=message.text,
            route="ignored",
            reason="empty_prompt",
        )

    def _runtime_before_submit(self, message: IncomingBridgeMessage, session: dict[str, Any], _prompt: str, passthrough: bool) -> None:
        session_name = str(session["session_name"] or "")
        session_meta = session["session_meta"]
        task_backend = session_meta.backend
        task_model = self._effective_session_model(session_meta)
        task_workdir = self._resolve_session_workdir(session_meta)

        self.state.mark_message(now=now_iso(), sender_id=message.sender_id)
        self._append_message_audit(
            sender_id=message.sender_id,
            text=message.text,
            route="task_submission",
            passthrough=passthrough,
            session_name=session_name or "default",
            source=message.source,
            backend=task_backend or session_meta.backend,
            model=self._display_model(task_model),
            workdir=task_workdir or "-",
        )

    def _submit_runtime_task(
        self,
        message: IncomingBridgeMessage,
        session: dict[str, Any],
        prompt: str,
        _passthrough: bool,
    ) -> BridgeSubmittedTask:
        return self.task_submit_runtime.submit(message, session, prompt)

    def _resolve_task_submit_context(self, message: IncomingBridgeMessage, session: dict[str, Any]) -> BridgeTaskSubmitContext:
        session_name = str(session["session_name"] or "")
        session_meta = session["session_meta"]
        return BridgeTaskSubmitContext(
            agent_id=self.config.backend_id,
            session_name=session_name,
            backend=session_meta.backend,
            workdir=self._resolve_session_workdir(session_meta),
            model=self._effective_session_model(session_meta),
            reasoning_effort=session_meta.reasoning_effort,
            permission_mode=session_meta.permission_mode,
            bridge_conversations_path=str(CONVERSATION_PATH),
            bridge_event_log_path=str(EVENT_LOG_PATH),
            context_token=message.context_token,
        )

    def _task_submit_completed(self, message: IncomingBridgeMessage, _context: BridgeTaskSubmitContext, started_at: float, response: Any) -> None:
        self._log_perf("submit_task_ipc", started_at, sender_id=message.sender_id, status="ok" if response.ok else "not_ok")

    def _remember_runtime_pending_task(self, message: IncomingBridgeMessage, session: dict[str, Any], submitted: BridgeSubmittedTask) -> None:
        session_name = str(session["session_name"] or "")
        session_meta = session["session_meta"]
        interrupt_messages = message.metadata.get("interrupt_messages")
        accepted_backend = session_meta.backend
        accepted_model = self._display_model(self._effective_session_model(session_meta))
        accepted_workdir = self._resolve_session_workdir(session_meta)
        self._append_event_log(
            event="accepted",
            task_id=submitted.task_id,
            sender_id=message.sender_id,
            session_name=session_name or "default",
            backend=accepted_backend,
            model=accepted_model,
            workdir=accepted_workdir,
            source=message.source,
        )
        tracked_task = WeixinPendingTaskState(
            task_id=submitted.task_id,
            sender_id=message.sender_id,
            session_name=session_name or "default",
            backend=accepted_backend,
            source=message.source,
            model=accepted_model,
            workdir=accepted_workdir,
            context_token=message.context_token,
            interrupt_base_prompt=str(message.metadata.get("interrupt_base_prompt") or submitted.payload.get("prompt") or ""),
            interrupt_messages=[str(item) for item in interrupt_messages if str(item or "").strip()] if isinstance(interrupt_messages, list) else [],
        )
        self.pending_tasks[tracked_task.task_id] = tracked_task
        self._save_pending_tasks()

    def _runtime_after_submit(self, message: IncomingBridgeMessage, _session: dict[str, Any], submitted: BridgeSubmittedTask) -> None:
        self._ensure_typing_worker_started()
        started_at = float(message.metadata.get("started_at") or time.perf_counter())
        self._log_perf("handle_message", started_at, sender_id=message.sender_id, route="task_submission", task_id=submitted.task_id)

    def _notify_task_progress(
        self,
        base_url: str,
        token: str,
        tracked: WeixinPendingTaskState,
        task: HubTask,
    ) -> None:
        if task.status != "running":
            return
        self._append_event_log(
            event="running",
            task_id=task.id,
            sender_id=tracked.sender_id,
            session_name=task.session_name or tracked.session_name or "default",
            session_id=task.session_id or "",
            backend=task.backend or self.config.default_backend,
            model=self._display_model(task.model.strip() or tracked.model),
            workdir=task.workdir.strip() or tracked.workdir or "-",
            source=tracked.source,
        )

    def _ensure_task_typing(
        self,
        base_url: str,
        token: str,
        tracked: WeixinPendingTaskState,
    ) -> bool:
        now_seconds = int(time.time())
        if tracked.typing_last_sent_at and now_seconds - tracked.typing_last_sent_at < TYPING_KEEPALIVE_SECONDS:
            return False
        if (
            not tracked.typing_ticket
            or not tracked.typing_ticket_refreshed_at
            or now_seconds - tracked.typing_ticket_refreshed_at >= TYPING_TICKET_TTL_SECONDS
        ):
            tracked.typing_ticket = self._get_typing_ticket(
                base_url,
                token,
                tracked.sender_id,
                self._resolve_context_token_for_sender(tracked),
            )
            tracked.typing_ticket_refreshed_at = now_seconds
        self._send_typing(base_url, token, tracked.sender_id, tracked.typing_ticket, status=1)
        tracked.typing_last_sent_at = now_seconds
        return True

    def _stop_task_typing(
        self,
        base_url: str,
        token: str,
        tracked: WeixinPendingTaskState,
    ) -> bool:
        if not tracked.typing_ticket or not tracked.typing_last_sent_at:
            return False
        self._send_typing(base_url, token, tracked.sender_id, tracked.typing_ticket, status=2)
        tracked.typing_last_sent_at = 0
        return True

    def _notify_task_progress_update(
        self,
        base_url: str,
        token: str,
        tracked: WeixinPendingTaskState,
        task: HubTask,
        progress_text: str | None = None,
    ) -> None:
        progress_reply_text = str(progress_text if progress_text is not None else task.progress_text).strip()
        if not progress_reply_text:
            return
        if progress_reply_text != tracked.last_progress_text:
            context_token = self._resolve_context_token_for_sender(tracked)
            self._send_text(
                base_url,
                token,
                tracked.sender_id,
                context_token,
                prefix_weixin_output(
                    "running",
                    format_duration_since(task.started_at or task.created_at),
                    progress_reply_text,
                    at=now_iso(),
                    context_left_percent=self._resolve_task_context_left_percent(task),
                ),
            )
            tracked.last_progress_text = progress_reply_text
        self._append_event_log(
            event="progress",
            task_id=task.id,
            sender_id=tracked.sender_id,
            session_name=task.session_name or tracked.session_name or "default",
            session_id=task.session_id or "",
            backend=task.backend or tracked.backend or self.config.default_backend,
            model=self._display_model(task.model.strip() or tracked.model),
            workdir=task.workdir.strip() or tracked.workdir or "-",
            result_preview=progress_reply_text[:240],
            source=tracked.source,
        )

    def _process_bridge_ipc_once(self, base_url: str, token: str) -> None:
        for request_path in sorted(bridge_request_dir("wechat").glob("*.json")):
            started_at = time.perf_counter()
            action = "unknown"
            status = "ok"
            try:
                request = read_request(request_path)
                action = request.action or "unknown"
                if action == "task_update":
                    self._handle_pushed_task_update(base_url, token, request.payload)
                else:
                    status = "ignored"
            except Exception as exc:  # noqa: BLE001
                status = "failed"
                print(f"[bridge] bridge ipc request failed {request_path.name}: {exc}", flush=True)
            finally:
                mark_bridge_processed(request_path)
                self._log_perf("bridge_ipc", started_at, force=True, action=action, status=status)

    def _handle_pushed_task_update(self, base_url: str, token: str, payload: dict[str, object]) -> None:
        task = HubTask.from_dict(payload.get("task"), default_backend=self.config.default_backend)
        if task is None:
            return
        tracked = self.pending_tasks.get(task.id)
        if tracked is None and task.id in self._interrupted_task_ids:
            if task.status in {"canceled", "failed", "succeeded", "unknown_after_restart"}:
                self._interrupted_task_ids.discard(task.id)
            return
        if tracked is None and task.source and not task.source.strip().lower().startswith("wechat"):
            return
        if tracked is None:
            tracked = WeixinPendingTaskState(
                task_id=task.id,
                sender_id=task.sender_id,
                session_name=task.session_name or "default",
                backend=task.backend or self.config.default_backend,
                source=task.source or "wechat",
                model=task.model,
                workdir=task.workdir,
                context_token=task.context_token,
            )
        _typing_last_sent_at, state_updated = TaskUpdateDeliveryController(
            send_progress=lambda _delivery_context, pending, pushed_task, progress_delta: self._notify_task_progress_update(
                base_url,
                token,
                pending,
                pushed_task,
                progress_text=progress_delta,
            ),
            send_terminal=lambda _delivery_context, pending, pushed_task: self._notify_task_terminal(base_url, token, pending, pushed_task),
            save_pending_task=lambda _task_id: None,
            forget_pending_task=lambda task_id: self.pending_tasks.pop(task_id, None),
            on_running=lambda _delivery_context, pending, pushed_task: self._notify_task_progress(base_url, token, pending, pushed_task),
            should_send_progress=lambda progress_delta: bool(str(progress_delta or "").strip()),
        ).handle_task_update(
            reply_target={"base_url": base_url},
            task=task,
            pending_task=tracked,
        )
        if state_updated:
            self._save_pending_tasks()

    def _reconcile_pending_tasks(self, base_url: str, token: str) -> None:
        if not self.pending_tasks:
            return
        now_value = time.monotonic()
        if now_value - self._pending_reconcile_last_at < PENDING_TASK_RECONCILE_INTERVAL_SECONDS:
            return
        self._pending_reconcile_last_at = now_value

        task_ids = list(self.pending_tasks.keys())
        if self._pending_reconcile_cursor >= len(task_ids):
            self._pending_reconcile_cursor = 0
        start_index = self._pending_reconcile_cursor
        selected = task_ids[start_index : start_index + PENDING_TASK_RECONCILE_BATCH_SIZE]
        if len(selected) < PENDING_TASK_RECONCILE_BATCH_SIZE and start_index > 0:
            selected.extend(task_ids[: PENDING_TASK_RECONCILE_BATCH_SIZE - len(selected)])
        if not selected:
            return
        self._pending_reconcile_cursor = (start_index + len(selected)) % max(1, len(task_ids))

        for task_id in selected:
            if task_id not in self.pending_tasks:
                continue
            reconcile_started = time.perf_counter()
            try:
                response = self._ipc_request(
                    "get_task",
                    {"task_id": task_id},
                    timeout_seconds=PENDING_TASK_RECONCILE_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001
                self._log_perf("pending_reconcile", reconcile_started, task_id=task_id, status="failed", error=str(exc)[:120])
                continue
            if not response.ok:
                self._log_perf("pending_reconcile", reconcile_started, task_id=task_id, status="not_found")
                continue
            task = HubTask.from_dict(response.payload.get("task"), default_backend=self.config.default_backend)
            if task is None:
                self._log_perf("pending_reconcile", reconcile_started, task_id=task_id, status="invalid")
                continue
            self._handle_pushed_task_update(base_url, token, {"event": "reconcile", "task": task.to_dict()})
            self._log_perf("pending_reconcile", reconcile_started, task_id=task_id, status=task.status)

    def _ensure_task_typing_best_effort(
        self,
        base_url: str,
        token: str,
        tracked: WeixinPendingTaskState,
        task_id: str,
    ) -> bool:
        try:
            typing_started = time.perf_counter()
            typing_sent = self._ensure_task_typing(base_url, token, tracked)
            self._log_perf("typing_keepalive", typing_started, task_id=task_id, sent=str(typing_sent).lower())
            return typing_sent
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge] typing keepalive failed for {task_id}: {exc}", flush=True)
            return False

    def _ensure_typing_worker_started(self) -> None:
        if self._typing_worker_started:
            return
        self._typing_worker_started = True
        threading.Thread(target=self._typing_worker, daemon=True).start()

    def _typing_worker(self) -> None:
        while True:
            try:
                account = self._load_account()
                token = (account.get("token") or "").strip()
                base_url = (account.get("baseUrl") or DEFAULT_WEIXIN_BASE_URL).strip()
                if token:
                    self._run_typing_scheduler_once(base_url, token)
            except Exception as exc:  # noqa: BLE001
                print(f"[bridge] typing scheduler failed: {exc}", flush=True)
            time.sleep(1)

    def _run_typing_scheduler_once(self, base_url: str, token: str) -> None:
        state_updated = False
        for task_id, tracked in list(self.pending_tasks.items()):
            if self._ensure_task_typing_best_effort(base_url, token, tracked, task_id):
                state_updated = True
        if state_updated:
            self._save_pending_tasks()

    def _stop_task_typing_async(self, base_url: str, token: str, tracked: WeixinPendingTaskState, task_id: str) -> None:
        threading.Thread(
            target=self._stop_task_typing_best_effort,
            args=(base_url, token, tracked, task_id),
            daemon=True,
        ).start()

    def _notify_task_terminal(
        self,
        base_url: str,
        token: str,
        tracked: WeixinPendingTaskState,
        task: HubTask,
    ) -> None:
        terminal_started = time.perf_counter()
        context_token = self._resolve_context_token_for_sender(tracked)
        if task.status == "succeeded":
            output = task.output.strip()
            if output and normalize_message_for_dedupe(output) == normalize_message_for_dedupe(tracked.last_progress_text):
                self._send_text(
                    base_url,
                    token,
                    tracked.sender_id,
                    context_token,
                    prefix_weixin_output(
                        "done",
                        format_duration_since(task.started_at or task.created_at, ended_at=task.finished_at),
                        "",
                        at=task.finished_at or now_iso(),
                        context_left_percent=self._resolve_task_context_left_percent(task),
                    ),
                )
                self._stop_task_typing_async(base_url, token, tracked, task.id)
                self._append_event_log(
                    event="succeeded",
                    task_id=task.id,
                    sender_id=tracked.sender_id,
                    session_name=task.session_name or tracked.session_name or "default",
                    session_id=task.session_id or "",
                    backend=task.backend or tracked.backend or self.config.default_backend,
                    model=self._display_model(task.model.strip() or tracked.model),
                    workdir=task.workdir.strip() or tracked.workdir or "-",
                    status=task.status,
                    result_preview=output[:240],
                    source=tracked.source,
                )
                self.state.record_handled()
                self._log_perf("notify_terminal", terminal_started, task_id=task.id, status=task.status, deduped="true")
                return
            if output:
                self._send_text(
                    base_url,
                    token,
                    tracked.sender_id,
                    context_token,
                    prefix_weixin_output(
                        "done",
                        format_duration_since(task.started_at or task.created_at, ended_at=task.finished_at),
                        output,
                        at=task.finished_at or now_iso(),
                        context_left_percent=self._resolve_task_context_left_percent(task),
                    ),
                )
            self._stop_task_typing_async(base_url, token, tracked, task.id)
            self._append_event_log(
                event="succeeded",
                task_id=task.id,
                sender_id=tracked.sender_id,
                session_name=task.session_name or tracked.session_name or "default",
                session_id=task.session_id or "",
                backend=task.backend or tracked.backend or self.config.default_backend,
                model=self._display_model(task.model.strip() or tracked.model),
                workdir=task.workdir.strip() or tracked.workdir or "-",
                status=task.status,
                result_preview=(task.output or "").strip()[:240],
                source=tracked.source,
            )
            self.state.record_handled()
            self._log_perf("notify_terminal", terminal_started, task_id=task.id, status=task.status)
            return
        if task.status == "canceled":
            self._send_text(
                base_url,
                token,
                tracked.sender_id,
                context_token,
                self._t(
                    "bridge.task.canceled",
                    task_id=task.id,
                    session=task.session_name or tracked.session_name or "default",
                    session_id=task.session_id or "-",
                    backend=task.backend or tracked.backend or self.config.default_backend,
                    error=str(task.error or "task canceled").strip(),
                    hint=build_task_followup_hint(
                        task_id=task.id,
                        session_name=task.session_name or tracked.session_name or "default",
                        allow_retry=True,
                    ),
                ),
            )
            self._stop_task_typing_async(base_url, token, tracked, task.id)
            self._append_event_log(
                event="canceled",
                task_id=task.id,
                sender_id=tracked.sender_id,
                session_name=task.session_name or tracked.session_name or "default",
                session_id=task.session_id or "",
                backend=task.backend or tracked.backend or self.config.default_backend,
                model=self._display_model(task.model.strip() or tracked.model),
                workdir=task.workdir.strip() or tracked.workdir or "-",
                status=task.status,
                error=(task.error or "").strip()[:240],
                source=tracked.source,
            )
            self._log_perf("notify_terminal", terminal_started, task_id=task.id, status=task.status)
            return
        self._send_text(
            base_url,
            token,
            tracked.sender_id,
            context_token,
            self._t(
                "bridge.task.failed",
                task_id=task.id,
                session=task.session_name or tracked.session_name or "default",
                session_id=task.session_id or "-",
                backend=task.backend or tracked.backend or self.config.default_backend,
                error=str(task.error or "task failed").strip(),
                hint=build_task_followup_hint(
                    task_id=task.id,
                    session_name=task.session_name or tracked.session_name or "default",
                    allow_retry=True,
                ),
            ),
        )
        self._stop_task_typing_async(base_url, token, tracked, task.id)
        self._append_event_log(
            event="failed",
            task_id=task.id,
            sender_id=tracked.sender_id,
            session_name=task.session_name or tracked.session_name or "default",
            session_id=task.session_id or "",
            backend=task.backend or tracked.backend or self.config.default_backend,
            model=self._display_model(task.model.strip() or tracked.model),
            workdir=task.workdir.strip() or tracked.workdir or "-",
            status=task.status,
            error=(task.error or "").strip()[:240],
            source=tracked.source,
        )
        self.state.record_failed()
        self._log_perf("notify_terminal", terminal_started, task_id=task.id, status=task.status)

    def _stop_task_typing_best_effort(
        self,
        base_url: str,
        token: str,
        tracked: WeixinPendingTaskState,
        task_id: str,
    ) -> None:
        try:
            self._stop_task_typing(base_url, token, tracked)
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge] typing stop failed for {task_id}: {exc}", flush=True)

    def _resolve_task_context_left_percent(self, task: HubTask) -> int | None:
        if str(task.backend or "").strip().lower() != "codex":
            return None
        live_percent = self._query_task_context_left_percent(task.id)
        if live_percent is not None:
            task.context_left_percent = live_percent
            return live_percent
        return normalize_context_left_percent(task.context_left_percent)

    def _query_task_context_left_percent(self, task_id: str) -> int | None:
        started_at = time.perf_counter()
        try:
            response = self._ipc_request(
                "task_context_left",
                {"task_id": task_id},
                timeout_seconds=CONTEXT_LEFT_QUERY_TIMEOUT_SECONDS,
            )
        except Exception:
            self._log_perf("context_left_query", started_at, force=True, task_id=task_id, status="timeout_or_failed")
            return None
        percent = normalize_context_left_percent(response.payload.get("context_left_percent") if response.ok else None)
        self._log_perf(
            "context_left_query",
            started_at,
            force=True,
            task_id=task_id,
            percent=percent,
            status="ok" if percent is not None else "empty" if response.ok else "not_ok",
        )
        return percent

    def _log_perf(
        self,
        label: str,
        started_at: float,
        *,
        force: bool = False,
        min_seconds: float | None = None,
        **fields: object,
    ) -> None:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        threshold_seconds = PERF_LOG_MIN_SECONDS if min_seconds is None else min_seconds
        if not force and elapsed_ms < int(threshold_seconds * 1000):
            return
        rendered_fields = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
        suffix = f" {rendered_fields}" if rendered_fields else ""
        print(f"[bridge-perf] {label} duration_ms={elapsed_ms}{suffix}", flush=True)

    def _send_text(self, base_url: str, token: str, to_user_id: str, context_token: Any, text: str) -> None:
        text = format_weixin_reply(text)
        self._start_send_worker()
        enqueue_text_message(
            to_user_id=str(to_user_id or ""),
            context_token=str(context_token or ""),
            text=text,
            source="bridge",
            account_id=self.config.active_account_id,
            account_file=str(self.account_path),
        )
        preview = " ".join(text.split())[:160]
        print(f"[bridge] queued reply to={to_user_id} preview={preview}", flush=True)

    def _format_retried_delivery_text(self, text: str, attempt: int) -> str:
        if attempt <= 0:
            return text
        return _append_delivery_header_suffix(
            text,
            self._t("bridge.delivery_retry.header", attempt=attempt),
        )

    def _handle_async_send_failure(self, message: dict[str, object], error: Exception) -> None:
        attempt = int(message.get("attempt") or 0)
        permanent = _is_permanent_delivery_error(error)
        preview = " ".join(str(message.get("text") or "").split())[:160]
        attempts = attempt + 1
        if _is_ephemeral_delivery_text(message.get("text")):
            print(
                f"[bridge] dropped transient progress reply to={message.get('to_user_id', '')} attempts={attempts} preview={preview}",
                flush=True,
            )
            return
        if _is_notice_delivery_message(message):
            print(
                f"[bridge] dropped transient notice to={message.get('to_user_id', '')} attempts={attempts} preview={preview}",
                flush=True,
            )
            return
        if not permanent and attempt < MAX_RETRY_ATTEMPTS:
            requeue_text_message(message)
            return
        record_failed_delivery(
            to_user_id=str(message.get("to_user_id") or ""),
            context_token=str(message.get("context_token") or ""),
            text_preview=preview,
            attempts=attempts,
            error=str(error),
        )
        print(
            f"[bridge] dropped undeliverable reply to={message.get('to_user_id', '')} attempts={attempts} preview={preview}",
            flush=True,
        )

    def _deliver_text_now(
        self,
        base_url: str,
        token: str,
        to_user_id: str,
        context_token: Any,
        text: str,
        *,
        flush_failure_notice: bool = True,
        log_failure: bool = True,
    ) -> None:
        delivery_started = time.perf_counter()
        text = format_weixin_reply(text)
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"bridge-{int(time.time() * 1000)}-{random.randint(1000, 9999)}",
                "message_type": 2,
                "message_state": 2,
                "item_list": [
                    {
                        "type": 1,
                        "text_item": {"text": text[:4000]},
                    }
                ],
                "context_token": context_token or None,
            },
            "base_info": {"channel_version": "2.1.1"},
        }
        preview = " ".join(text.split())[:160]
        try:
            with sender_send_lock(to_user_id):
                response = self._post_json(f"{base_url}/ilink/bot/sendmessage", body, token=token, timeout_ms=15000)
            if isinstance(response, dict) and response.get("ret") not in (None, 0):
                raise RuntimeError(f"sendmessage returned ret={response.get('ret')}: {response}")
            if isinstance(response, dict):
                print(
                    f"[bridge] sent reply to={to_user_id} ret={response.get('ret')} errcode={response.get('errcode')} errmsg={response.get('errmsg')} preview={preview}",
                    flush=True,
                )
            else:
                print(f"[bridge] sent reply to={to_user_id} preview={preview}", flush=True)
            self._log_perf("sendmessage", delivery_started, to=to_user_id, status="ok")
        except Exception as exc:  # noqa: BLE001
            self._log_perf("sendmessage", delivery_started, to=to_user_id, status="failed")
            if log_failure:
                print(f"[bridge] send reply failed to={to_user_id} error={exc} preview={preview}", flush=True)
            raise
        if flush_failure_notice:
            self._flush_failed_delivery_notice(base_url, token, str(to_user_id or ""), str(context_token or ""))

    def _flush_failed_delivery_notice(self, base_url: str, token: str, to_user_id: str, context_token: str) -> None:
        failed = pop_failed_delivery(to_user_id)
        if not failed:
            return
        notice = self._t(
            "bridge.delivery_failed.notice",
            count=int(failed.get("count") or 1),
            attempts=int(failed.get("attempts") or 0),
            error=str(failed.get("error") or "-"),
            preview=str(failed.get("text_preview") or "-"),
        )
        try:
            self._deliver_text_now(
                base_url,
                token,
                to_user_id,
                context_token or str(failed.get("context_token") or ""),
                notice,
                flush_failure_notice=False,
            )
        except Exception as exc:  # noqa: BLE001
            record_failed_delivery(
                to_user_id=to_user_id,
                context_token=context_token or str(failed.get("context_token") or ""),
                text_preview=str(failed.get("text_preview") or ""),
                attempts=int(failed.get("attempts") or 0),
                error=str(exc),
            )

    def _get_typing_ticket(self, base_url: str, token: str, to_user_id: str, context_token: str) -> str:
        response = self._post_json(
            f"{base_url}/ilink/bot/getconfig",
            {
                "ilink_user_id": to_user_id,
                "context_token": context_token or None,
                "base_info": {"channel_version": "2.1.1"},
            },
            token=token,
            timeout_ms=15000,
        )
        if isinstance(response, dict) and response.get("ret") not in (None, 0):
            raise RuntimeError(f"getconfig returned ret={response.get('ret')}: {response}")
        ticket = str((response or {}).get("typing_ticket") or "").strip()
        if not ticket:
            raise RuntimeError(f"getconfig returned no typing_ticket: {response}")
        return ticket

    def _send_typing(self, base_url: str, token: str, to_user_id: str, typing_ticket: str, *, status: int) -> None:
        with sender_send_lock(to_user_id):
            response = self._post_json(
                f"{base_url}/ilink/bot/sendtyping",
                {
                    "ilink_user_id": to_user_id,
                    "typing_ticket": typing_ticket,
                    "status": status,
                    "base_info": {"channel_version": "2.1.1"},
                },
                token=token,
                timeout_ms=15000,
            )
        if isinstance(response, dict) and response.get("ret") not in (None, 0):
            raise RuntimeError(f"sendtyping returned ret={response.get('ret')}: {response}")

    def _send_media_to_reply_target(self, reply_target: dict[str, Any], file_path: Path) -> None:
        self._send_media_file(
            str(reply_target.get("base_url") or ""),
            str(reply_target.get("token") or ""),
            str(reply_target.get("to_user_id") or ""),
            reply_target.get("context_token"),
            file_path,
        )

    def _send_media_file(self, base_url: str, token: str, to_user_id: str, context_token: Any, file_path: Path) -> dict[str, Any]:
        guessed_mime = mimetypes.guess_type(file_path.name)[0] or ""
        media_type = MEDIA_UPLOAD_TYPE_IMAGE if guessed_mime.startswith("image/") or file_path.suffix.lower() in SENDMEDIA_IMAGE_EXTENSIONS else MEDIA_UPLOAD_TYPE_FILE
        uploaded = self._upload_media_file(base_url, token, to_user_id, file_path, media_type=media_type)
        aes_key = base64.b64encode(str(uploaded["aes_hex"]).encode("utf-8")).decode("ascii")
        media = {
            "encrypt_query_param": str(uploaded["download_param"]),
            "aes_key": aes_key,
            "encrypt_type": 1,
        }
        if media_type == MEDIA_UPLOAD_TYPE_IMAGE:
            item = {
                "type": MESSAGE_ITEM_TYPE_IMAGE,
                "image_item": {
                    "media": media,
                    "mid_size": int(uploaded["cipher_size"]),
                },
            }
        else:
            item = {
                "type": MESSAGE_ITEM_TYPE_FILE,
                "file_item": {
                    "media": media,
                    "file_name": file_path.name,
                    "md5": str(uploaded["md5"]),
                    "len": str(uploaded["raw_size"]),
                },
            }
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"media-{int(time.time() * 1000)}-{random.randint(1000, 9999)}",
                "message_type": 2,
                "message_state": 2,
                "item_list": [item],
                "context_token": context_token or None,
            },
            "base_info": {"channel_version": "2.1.1"},
        }
        with sender_send_lock(to_user_id):
            response = self._post_json(f"{base_url}/ilink/bot/sendmessage", body, token=token, timeout_ms=15000)
        if isinstance(response, dict) and response.get("ret") not in (None, 0):
            raise RuntimeError(f"sendmessage returned ret={response.get('ret')}: {response}")
        print(
            f"[bridge] sent media to={to_user_id} file={file_path.name} ret={response.get('ret') if isinstance(response, dict) else '-'}",
            flush=True,
        )
        return response if isinstance(response, dict) else {}

    def _upload_media_file(self, base_url: str, token: str, to_user_id: str, file_path: Path, *, media_type: int) -> dict[str, object]:
        data = file_path.read_bytes()
        if len(data) > MEDIA_SEND_MAX_BYTES:
            raise ValueError(f"file is too large: {len(data)} bytes")
        aes_key = secrets.token_bytes(16)
        aes_hex = aes_key.hex()
        ciphertext = _encrypt_aes_128_ecb(data, aes_key)
        filekey = secrets.token_hex(16)
        raw_md5 = hashlib.md5(data).hexdigest()
        upload_response = self._post_json(
            f"{base_url}/ilink/bot/getuploadurl",
            {
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": to_user_id,
                "rawsize": len(data),
                "rawfilemd5": raw_md5,
                "filesize": len(ciphertext),
                "no_need_thumb": True,
                "aeskey": aes_hex,
                "base_info": {"channel_version": "2.1.1"},
            },
            token=token,
            timeout_ms=15000,
        )
        cdn_url = str(upload_response.get("upload_full_url") if isinstance(upload_response, dict) else "").strip()
        if not cdn_url:
            upload_param = str(upload_response.get("upload_param") if isinstance(upload_response, dict) else "").strip()
            if not upload_param:
                raise RuntimeError(f"getuploadurl returned no upload URL: {upload_response}")
            cdn_url = (
                f"{DEFAULT_WEIXIN_CDN_BASE_URL}/upload"
                f"?encrypted_query_param={urllib.parse.quote(upload_param, safe='')}"
                f"&filekey={urllib.parse.quote(filekey, safe='')}"
            )
        request = urllib.request.Request(
            cdn_url,
            data=ciphertext,
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(ciphertext))},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - endpoint is fixed WeChat CDN URL
            download_param = str(response.headers.get("x-encrypted-param") or "").strip()
        if not download_param:
            raise RuntimeError("CDN upload response missing x-encrypted-param")
        return {
            "download_param": download_param,
            "aes_hex": aes_hex,
            "raw_size": len(data),
            "cipher_size": len(ciphertext),
            "md5": raw_md5,
        }

    def _remember_context_token(self, sender_id: str, context_token: Any) -> None:
        cleaned_sender_id = str(sender_id or "").strip()
        cleaned_context_token = str(context_token or "").strip()
        if not cleaned_sender_id or not cleaned_context_token:
            return
        if self.context_tokens.get(cleaned_sender_id) == cleaned_context_token:
            return
        self.context_tokens[cleaned_sender_id] = cleaned_context_token
        save_account_context_tokens(self.account_path, self.context_tokens)

    def _resolve_context_token_for_sender(self, tracked: WeixinPendingTaskState) -> str:
        return self._resolve_context_token_for_delivery(tracked.sender_id, tracked.context_token)

    def _resolve_context_token_for_delivery(self, sender_id: str, fallback_context_token: Any) -> str:
        cleaned_sender_id = str(sender_id or "").strip()
        latest_context_token = str(self.context_tokens.get(cleaned_sender_id, "") or "").strip()
        if latest_context_token:
            return latest_context_token
        return str(fallback_context_token or "").strip()

    def _append_event_log(self, event: str, **payload: Any) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": now_iso(),
            "event": event,
            **payload,
        }
        with EVENT_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _append_message_audit(self, *, sender_id: str, text: str, route: str, **payload: Any) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        preview = " ".join(str(text or "").split())[:240]
        entry = {
            "at": now_iso(),
            "sender_id": sender_id,
            "text": str(text or ""),
            "text_preview": preview,
            "route": route,
            **payload,
        }
        with MESSAGE_AUDIT_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _load_recent_events(self, *, sender_id: str = "", limit: int = 5) -> list[dict[str, str]]:
        if not EVENT_LOG_PATH.exists():
            return []
        cleaned_sender_id = sender_id.strip()
        entries: list[dict[str, str]] = []
        for line in reversed(EVENT_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            raw_sender_id = str(raw.get("sender_id") or "").strip()
            if cleaned_sender_id and raw_sender_id != cleaned_sender_id:
                continue
            if self._is_hidden_legacy_event(raw):
                continue
            entries.append({str(key): str(value) for key, value in raw.items() if value is not None})
            if len(entries) >= max(limit, 1):
                break
        return entries

    @staticmethod
    def _is_hidden_legacy_event(entry: dict[str, Any]) -> bool:
        preview = str(entry.get("result_preview") or "")
        if not preview:
            return False
        legacy_markers = ("发送方 2", "发送方 3", "其他联系人", "全局共有")
        return any(marker in preview for marker in legacy_markers)

    def _extract_text(self, msg: dict[str, Any]) -> str:
        parts = []
        for item in msg.get("item_list") or []:
            if item.get("type") == 1:
                text_item = item.get("text_item") or {}
                text = str(text_item.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()

    @staticmethod
    def _has_incoming_media(msg: dict[str, Any]) -> bool:
        return any(isinstance(item, dict) and item.get("type") in INCOMING_MEDIA_ITEM_TYPES for item in msg.get("item_list") or [])

    def _extract_media_attachments(
        self,
        base_url: str,
        token: str,
        sender_id: str,
        msg: dict[str, Any],
    ) -> tuple[list[dict[str, str]], list[str]]:
        attachments: list[dict[str, str]] = []
        errors: list[str] = []
        for index, item in enumerate(msg.get("item_list") or [], start=1):
            if not isinstance(item, dict) or item.get("type") not in INCOMING_MEDIA_ITEM_TYPES:
                continue
            try:
                attachments.append(self._save_incoming_media_item(base_url, token, sender_id, item, index=index))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"- 第 {index} 个附件：{str(exc)}")
                print(f"[bridge] incoming media save failed sender={sender_id} index={index}: {exc}", flush=True)
        return attachments, errors

    def _save_incoming_media_item(
        self,
        base_url: str,
        token: str,
        sender_id: str,
        item: dict[str, Any],
        *,
        index: int,
    ) -> dict[str, str]:
        raw_type = item.get("type")
        if raw_type == MESSAGE_ITEM_TYPE_IMAGE:
            kind = "image"
            payload = item.get("image_item") or {}
            default_name = f"image-{index}.jpg"
        elif raw_type == MESSAGE_ITEM_TYPE_FILE:
            kind = "file"
            payload = item.get("file_item") or {}
            default_name = f"file-{index}"
        else:
            raise ValueError(f"unsupported media item type: {raw_type}")
        if not isinstance(payload, dict):
            raise ValueError("invalid media item payload")
        media = payload.get("media") or {}
        if not isinstance(media, dict):
            raise ValueError("missing media metadata")
        raw_name = str(payload.get("file_name") or payload.get("name") or media.get("file_name") or default_name)
        filename = self._safe_incoming_media_filename(raw_name, fallback=default_name)
        saved_path = self._download_incoming_media_file(
            base_url,
            token,
            sender_id,
            media,
            filename=filename,
        )
        return {"kind": kind, "name": filename, "path": str(saved_path)}

    def _download_incoming_media_file(
        self,
        base_url: str,
        token: str,
        sender_id: str,
        media: dict[str, Any],
        *,
        filename: str,
    ) -> Path:
        download_url = self._incoming_media_download_url(base_url, media)
        request = urllib.request.Request(download_url, headers={"Authorization": f"Bearer {token}"} if token else {}, method="GET")
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - URL comes from the authenticated WeChat payload
            ciphertext = response.read(MEDIA_RECEIVE_MAX_BYTES + 1)
        if len(ciphertext) > MEDIA_RECEIVE_MAX_BYTES:
            raise ValueError(f"file is too large: {len(ciphertext)} bytes")
        data = ciphertext
        if str(media.get("encrypt_type") or "").strip() == "1" or media.get("aes_key"):
            data = _decrypt_aes_128_ecb(ciphertext, self._decode_media_aes_key(media.get("aes_key")))
        sender_dir = UPLOAD_DIR / self._safe_path_part(sender_id or "unknown")
        sender_dir.mkdir(parents=True, exist_ok=True)
        target = sender_dir / f"{int(time.time() * 1000)}-{secrets.token_hex(4)}-{filename}"
        target.write_bytes(data)
        return target

    def _incoming_media_download_url(self, base_url: str, media: dict[str, Any]) -> str:
        for key in ("download_url", "url", "cdn_url", "full_url"):
            value = str(media.get(key) or "").strip()
            if value:
                return value
        download_param = str(media.get("encrypt_query_param") or media.get("download_param") or "").strip()
        if not download_param:
            raise ValueError("missing media download parameter")
        return f"{DEFAULT_WEIXIN_CDN_BASE_URL}/download?encrypted_query_param={urllib.parse.quote(download_param, safe='')}"

    @staticmethod
    def _decode_media_aes_key(value: Any) -> bytes:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("missing media aes key")
        if len(raw) == 32:
            try:
                return bytes.fromhex(raw)
            except ValueError:
                pass
        try:
            decoded = base64.b64decode(raw, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("invalid media aes key") from exc
        if len(decoded) == 16:
            return decoded
        try:
            decoded_text = decoded.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("invalid media aes key length") from exc
        if len(decoded_text) == 32:
            try:
                return bytes.fromhex(decoded_text)
            except ValueError as exc:
                raise ValueError("invalid media aes key hex") from exc
        raise ValueError("invalid media aes key length")

    @staticmethod
    def _safe_path_part(value: str) -> str:
        cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in str(value or "").strip())
        return cleaned.strip(".-") or "unknown"

    def _safe_incoming_media_filename(self, value: str, *, fallback: str) -> str:
        candidate = Path(str(value or "").replace("\\", "/")).name
        safe = self._safe_path_part(candidate)
        return safe if safe and safe != "unknown" else fallback

    def _message_key(self, msg: dict[str, Any], text: str) -> str:
        media_fingerprint: list[dict[str, str]] = []
        for item in msg.get("item_list") or []:
            if not isinstance(item, dict) or item.get("type") not in INCOMING_MEDIA_ITEM_TYPES:
                continue
            payload = item.get("image_item") if item.get("type") == MESSAGE_ITEM_TYPE_IMAGE else item.get("file_item")
            payload = payload if isinstance(payload, dict) else {}
            media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
            media_fingerprint.append(
                {
                    "type": str(item.get("type") or ""),
                    "name": str(payload.get("file_name") or payload.get("name") or ""),
                    "download": str(media.get("encrypt_query_param") or media.get("download_param") or media.get("download_url") or media.get("url") or ""),
                }
            )
        payload = {
            "id": msg.get("msg_id") or msg.get("message_id") or msg.get("client_id") or "",
            "context_token": msg.get("context_token") or "",
            "sender_id": msg.get("from_user_id") or "",
            "create_time": msg.get("create_time") or msg.get("create_timestamp") or "",
            "text": text,
            "media": media_fingerprint,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha1(encoded).hexdigest()

    def _is_duplicate_message(self, message_key: str, *, sender_id: str = "", text: str = "") -> bool:
        now_value = time.monotonic()
        cleaned_text = text.strip()
        fingerprint = f"{sender_id.strip()}::{cleaned_text}" if cleaned_text.startswith("/") else ""
        if message_key in self._recent_message_keys:
            return True
        recent_seen_at = self._recent_message_fingerprints.get(fingerprint)
        if fingerprint.strip(":") and recent_seen_at is not None and now_value - recent_seen_at <= 2.0:
            return True
        self._recent_message_keys.append(message_key)
        if len(self._recent_message_keys) > 200:
            self._recent_message_keys = self._recent_message_keys[-200:]
        self._recent_message_fingerprints[fingerprint] = now_value
        expired = [key for key, seen_at in self._recent_message_fingerprints.items() if now_value - seen_at > 10.0]
        for key in expired:
            self._recent_message_fingerprints.pop(key, None)
        return False

    def _handle_control_command(self, sender_id: str, text: str) -> tuple[str, bool]:
        return self.command_router.handle(sender_id, text)

    def _send_notify_test_notice(self) -> str:
        result = broadcast_weixin_notice_by_kind(
            "service",
            "通知测试",
            f"Bridge 通知链路测试\n账号: {self.config.active_account_id or '-'}\n默认 Agent: {self.config.backend_id or 'main'}",
            config=self.config,
        )
        print(f"[bridge] notify test: {result.summary}", flush=True)
        if result.error and result.error != "disabled":
            print(f"[bridge] notify test error: {result.error}", flush=True)
        return result.summary

    def _current_session_name(self, sender_id: str) -> str:
        binding = self._ensure_conversation(sender_id)
        session_name, _ = binding.get_current_session(
            default_backend=self.config.default_backend,
            now=now_iso(),
            normalize_backend=normalize_backend,
        )
        return session_name or "default"

    @staticmethod
    def _now_iso() -> str:
        return now_iso()

    @staticmethod
    def _normalize_backend(value: str) -> str:
        return normalize_backend(value)

    @staticmethod
    def _load_agents() -> list[Any]:
        return list(HubConfig.load().agents)

    def _set_backend_agent(self, agent_id: str) -> None:
        self.config.set_backend_agent(agent_id)
        self.config.save()

    def _remove_conversation(self, sender_id: str) -> None:
        self.conversations.pop(sender_id, None)
        self._save_conversations()

    def _render_control_status(self, sender_id: str) -> str:
        binding = self._ensure_conversation(sender_id)
        current_session, current_meta = binding.get_current_session(
            default_backend=self.config.default_backend,
            now=now_iso(),
            normalize_backend=normalize_backend,
        )
        return self._render_status(binding, current_session, current_meta.backend)

    def _load_sender_tasks(self, sender_id: str) -> list[HubTask]:
        return self.task_query_runtime.load_sender_tasks(sender_id)

    def _resolve_fallback_session_target(self, binding: WeixinConversationBinding) -> str:
        return resolve_fallback_session_target(binding)

    def _render_session_list(
        self,
        sender_id: str,
        binding: WeixinConversationBinding,
        *,
        page: int = 1,
        query: str = "",
        project_path: str | None = "",
        scope_label: str = "",
    ) -> str:
        sender_tasks = self._load_sender_tasks(sender_id)
        tasks_by_session: dict[str, list[HubTask]] = {}
        for task in sender_tasks:
            tasks_by_session.setdefault(task.session_name or "default", []).append(task)

        if project_path == "":
            _, current_meta = binding.get_current_session(
                default_backend=self.config.default_backend,
                now=now_iso(),
                normalize_backend=normalize_backend,
            )
            project_path = self._resolve_session_workdir(current_meta)
            scope_label = scope_label or self._t(
                "bridge.session.list.scope.project",
                project=self._project_name_for_workdir(project_path),
            )
        elif project_path is None:
            scope_label = scope_label or self._t("bridge.session.list.scope.all")
        else:
            scope_label = scope_label or self._t(
                "bridge.session.list.scope.project",
                project=self._project_name_for_workdir(project_path),
            )

        all_session_names = self._filtered_session_names(binding, tasks_by_session, query=query, project_path=project_path)
        total_count = len(all_session_names)
        total_pages = max(1, (total_count + SESSION_PAGE_SIZE - 1) // SESSION_PAGE_SIZE)
        current_page = min(max(page, 1), total_pages)
        start = (current_page - 1) * SESSION_PAGE_SIZE
        paged_session_names = all_session_names[start : start + SESSION_PAGE_SIZE]

        lines = [
            self._t(
                "bridge.session.list.title",
                page=current_page,
                total_pages=total_pages,
                count=total_count,
                query=query.strip() or "-",
                scope=scope_label,
            )
        ]
        if not paged_session_names:
            lines.append(self._t("bridge.session.list.empty"))
            return "\n".join(lines)

        for session_name in paged_session_names:
            marker = "*" if session_name == binding.current_session else "-"
            backend = binding.sessions[session_name].backend
            recent_tasks = tasks_by_session.get(session_name, [])
            latest_task = recent_tasks[0] if recent_tasks else None
            latest_at = self._session_latest_activity(session_name, binding, tasks_by_session) or "-"
            summary = self._t("bridge.session.preview.none_short")
            if latest_task is not None:
                summary = self._task_summary_excerpt(latest_task)
            lines.append(
                self._t(
                    "bridge.list.item.detail",
                    marker=marker,
                    name=session_name,
                    backend=backend,
                    latest=latest_at,
                    count=len(recent_tasks),
                    summary=summary,
                )
            )
        return "\n".join(lines)

    def _filtered_session_names(
        self,
        binding: WeixinConversationBinding,
        tasks_by_session: dict[str, list[HubTask]],
        *,
        query: str = "",
        project_path: str | None = "",
    ) -> list[str]:
        ordered = self._ordered_session_names(binding, tasks_by_session)
        cleaned_query = query.strip().lower()
        resolved_project_path = str(Path(project_path).expanduser().resolve()) if project_path else ""
        if not cleaned_query:
            if not resolved_project_path:
                return ordered
            return [
                session_name
                for session_name in ordered
                if self._resolve_session_workdir(binding.sessions[session_name]) == resolved_project_path
            ]
        matched: list[str] = []
        for session_name in ordered:
            if resolved_project_path and self._resolve_session_workdir(binding.sessions[session_name]) != resolved_project_path:
                continue
            if cleaned_query in session_name.lower():
                matched.append(session_name)
                continue
            recent_tasks = tasks_by_session.get(session_name, [])
            latest_task = recent_tasks[0] if recent_tasks else None
            summary = self._task_summary_excerpt(latest_task) if latest_task is not None else ""
            if cleaned_query in summary.lower():
                matched.append(session_name)
        return matched

    def _bulk_delete_sessions(self, binding: WeixinConversationBinding, raw_names: str) -> tuple[str, bool]:
        requested_names = [item.strip() for item in raw_names.split(",") if item.strip()]
        if not requested_names:
            return self._t("bridge.sessions.delete.usage"), True
        deleted: list[str] = []
        skipped: list[str] = []
        for session_name in requested_names:
            if session_name not in binding.sessions or session_name == "default":
                skipped.append(session_name)
                continue
            binding.sessions.pop(session_name, None)
            deleted.append(session_name)
        if binding.current_session not in binding.sessions:
            binding.current_session = self._resolve_fallback_session_target(binding) or "default"
            binding.sessions.setdefault("default", self._new_session_meta())
        if binding.last_regular_session not in binding.sessions:
            binding.last_regular_session = self._resolve_fallback_session_target(binding) or "default"
        self._save_conversations()
        return (
            self._t(
                "bridge.sessions.delete.result",
                deleted=", ".join(deleted) or "-",
                skipped=", ".join(skipped) or "-",
                current=binding.current_session or "default",
            ),
            True,
        )

    def _clear_empty_sessions(self, sender_id: str, binding: WeixinConversationBinding) -> tuple[str, bool]:
        sender_tasks = self._load_sender_tasks(sender_id)
        sessions_with_tasks = {task.session_name or "default" for task in sender_tasks}
        deleted: list[str] = []
        for session_name in list(binding.sessions.keys()):
            if session_name in {"default", binding.current_session}:
                continue
            if session_name in sessions_with_tasks:
                continue
            binding.sessions.pop(session_name, None)
            deleted.append(session_name)
        self._save_conversations()
        return (
            self._t(
                "bridge.sessions.clear_empty.result",
                deleted=", ".join(deleted) or "-",
                current=binding.current_session or "default",
            ),
            True,
        )

    def _render_session_preview(self, sender_id: str, session_name: str, binding: WeixinConversationBinding) -> str:
        sender_tasks = self._load_sender_tasks(sender_id)
        session_tasks = [task for task in sender_tasks if (task.session_name or "default") == session_name]
        session_meta = binding.sessions.get(session_name)
        backend = session_meta.backend if session_meta is not None else normalize_backend(self.config.default_backend)
        lines = [
            self._t("bridge.session.preview.header", session=session_name, backend=backend, count=len(session_tasks)),
        ]
        if not session_tasks:
            lines.append(self._t("bridge.session.preview.none"))
            return "\n".join(lines)

        latest_at = session_tasks[0].finished_at or session_tasks[0].started_at or session_tasks[0].created_at or "-"
        lines.append(self._t("bridge.session.preview.latest", latest=latest_at))
        for index, task in enumerate(reversed(session_tasks[:3]), start=1):
            lines.append("")
            lines.append(
                self._t(
                    "bridge.session.preview.round",
                    index=index,
                    created_at=task.created_at or "-",
                    status=task.status or "unknown",
                )
            )
            lines.append(self._t("bridge.session.preview.prompt", text=(task.prompt or "(empty)").strip()[:280]))
            if task.output:
                lines.append(self._t("bridge.session.preview.output", text=task.output.strip()[:280]))
            elif task.error:
                lines.append(self._t("bridge.session.preview.error", text=task.error.strip()[:280]))
            else:
                lines.append(self._t("bridge.session.preview.no_output"))
        return "\n".join(lines)

    def _render_session_history(self, sender_id: str, session_name: str, binding: WeixinConversationBinding) -> str:
        sender_tasks = self._load_sender_tasks(sender_id)
        session_tasks = [task for task in sender_tasks if (task.session_name or "default") == session_name]
        session_meta = binding.sessions.get(session_name)
        backend = session_meta.backend if session_meta is not None else normalize_backend(self.config.default_backend)
        lines = [
            self._t("bridge.session.history.header", session=session_name, backend=backend, count=len(session_tasks)),
        ]
        if not session_tasks:
            lines.append(self._t("bridge.session.preview.none"))
            return "\n".join(lines)

        latest_at = session_tasks[0].finished_at or session_tasks[0].started_at or session_tasks[0].created_at or "-"
        lines.append(self._t("bridge.session.preview.latest", latest=latest_at))
        lines.append(
            self._t(
                "bridge.session.history.summary",
                summary=self._build_session_history_summary(session_tasks),
            )
        )
        for index, task in enumerate(session_tasks[:5], start=1):
            lines.append("")
            lines.append(
                self._t(
                    "bridge.session.history.item",
                    index=index,
                    created_at=task.created_at or "-",
                    status=task.status or "unknown",
                    task_id=task.id,
                )
            )
            lines.append(self._t("bridge.session.preview.prompt", text=(task.prompt or "(empty)").strip()[:280]))
            if task.output:
                lines.append(self._t("bridge.session.preview.output", text=task.output.strip()[:280]))
            elif task.error:
                lines.append(self._t("bridge.session.preview.error", text=task.error.strip()[:280]))
            else:
                lines.append(self._t("bridge.session.preview.no_output"))
        return "\n".join(lines)

    def _build_session_history_summary(self, session_tasks: list[HubTask]) -> str:
        if not session_tasks:
            return self._t("bridge.session.preview.none_short")
        latest = session_tasks[0]
        recent_statuses = [task.status for task in session_tasks[:5] if task.status]
        unique_statuses = ", ".join(dict.fromkeys(recent_statuses)) or "unknown"
        latest_excerpt = self._task_summary_excerpt(latest)
        return self._t(
            "bridge.session.history.summary.template",
            latest_task=latest.id,
            statuses=unique_statuses,
            excerpt=latest_excerpt,
        )

    def _export_session_history(self, sender_id: str, session_name: str, binding: WeixinConversationBinding) -> tuple[str, bool]:
        sender_tasks = self._load_sender_tasks(sender_id)
        session_tasks = [task for task in sender_tasks if (task.session_name or "default") == session_name]
        session_meta = binding.sessions.get(session_name)
        backend = session_meta.backend if session_meta is not None else normalize_backend(self.config.default_backend)
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        safe_session = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in session_name).strip("-_") or "default"
        safe_sender = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in sender_id).strip("-_") or "sender"
        export_path = EXPORT_DIR / f"{safe_sender}__{safe_session}.md"
        lines = [
            f"# Session Export: {session_name}",
            "",
            f"- Sender: {sender_id}",
            f"- Backend: {backend}",
            f"- Exported At: {now_iso()}",
            f"- Task Count: {len(session_tasks)}",
            "",
            "## Summary",
            "",
            self._build_session_history_summary(session_tasks),
        ]
        if not session_tasks:
            lines.extend(["", "## Rounds", "", "(empty)"])
        else:
            lines.extend(["", "## Rounds"])
            for index, task in enumerate(reversed(session_tasks), start=1):
                lines.extend(
                    [
                        "",
                        f"### Round {index}",
                        f"- Task ID: {task.id}",
                        f"- Status: {task.status or 'unknown'}",
                        f"- Created At: {task.created_at or '-'}",
                        "",
                        "#### User",
                        "",
                        task.prompt or "(empty)",
                        "",
                        "#### Assistant",
                        "",
                        task.output or task.error or "(empty)",
                    ]
                )
        export_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return (
            self._t(
                "bridge.session.export.done",
                session=session_name,
                path=export_path,
                count=len(session_tasks),
            ),
            True,
        )

    def _resolve_shareable_project_file(self, raw_path: str) -> Path:
        cleaned_path = raw_path.strip()
        if not cleaned_path:
            raise ValueError("path is required")
        project_root = APP_DIR.resolve()
        candidate = Path(cleaned_path).expanduser()
        if not candidate.is_absolute():
            candidate = project_root / candidate
        resolved = candidate.resolve()
        try:
            relative_path = resolved.relative_to(project_root)
        except ValueError as exc:
            raise ValueError(f"path is outside project: {cleaned_path}") from exc
        if self._is_blocked_share_path(relative_path):
            raise ValueError(f"path is blocked: {relative_path}")
        if not resolved.exists() or not resolved.is_file():
            raise FileNotFoundError(str(relative_path))
        return resolved

    def _is_blocked_share_path(self, relative_path: Path) -> bool:
        parts = relative_path.parts
        if len(parts) >= 2 and parts[0] == ".runtime" and parts[1] == "exports":
            return False
        return any(part in SHOWFILE_BLOCKED_PATH_PARTS for part in parts)

    def _render_project_file_preview(self, raw_path: str) -> str:
        cleaned_path = raw_path.strip()
        if not cleaned_path:
            return self._t("bridge.showfile.usage")
        project_root = APP_DIR.resolve()
        candidate = Path(cleaned_path).expanduser()
        if not candidate.is_absolute():
            candidate = project_root / candidate
        try:
            resolved = candidate.resolve()
            relative_path = resolved.relative_to(project_root)
        except ValueError:
            return self._t("bridge.showfile.denied", path=cleaned_path)
        if self._is_blocked_share_path(relative_path):
            return self._t("bridge.showfile.denied", path=str(relative_path))
        if not resolved.exists() or not resolved.is_file():
            return self._t("bridge.showfile.not_found", path=str(relative_path))
        suffix = resolved.suffix.lower()
        if suffix not in SHOWFILE_ALLOWED_EXTENSIONS:
            return self._t("bridge.showfile.unsupported", path=str(relative_path), suffix=suffix or "-")
        content = resolved.read_text(encoding="utf-8", errors="replace")
        truncated = len(content) > SHOWFILE_PREVIEW_LIMIT
        preview = content[:SHOWFILE_PREVIEW_LIMIT].rstrip()
        if truncated:
            preview = f"{preview}\n\n...（内容过长，已截断）"
        return self._t(
            "bridge.showfile.content",
            path=str(relative_path),
            size=resolved.stat().st_size,
            content=preview or "(empty)",
        )

    def _task_summary_excerpt(self, task: HubTask) -> str:
        source = (task.output or task.error or task.prompt or "").strip()
        if not source:
            return self._t("bridge.session.preview.none_short")
        return " ".join(source.split())[:80]

    def _session_latest_activity(
        self,
        session_name: str,
        binding: WeixinConversationBinding,
        tasks_by_session: dict[str, list[HubTask]],
    ) -> str:
        recent_tasks = tasks_by_session.get(session_name, [])
        if recent_tasks:
            latest_task = recent_tasks[0]
            return latest_task.finished_at or latest_task.started_at or latest_task.created_at or ""
        session_meta = binding.sessions.get(session_name)
        if session_meta is None:
            return ""
        return session_meta.updated_at or session_meta.created_at or ""

    def _ordered_session_names(
        self,
        binding: WeixinConversationBinding,
        tasks_by_session: dict[str, list[HubTask]],
    ) -> list[str]:
        return sorted(
            binding.sessions,
            key=lambda name: (
                self._session_latest_activity(name, binding, tasks_by_session),
                name,
            ),
            reverse=True,
        )

    def _project_spaces(self) -> dict[str, str]:
        spaces = self._load_registered_project_spaces()
        agent = self._find_agent_config(self.config.backend_id)
        if agent is not None and agent.workdir:
            agent_path = Path(agent.workdir).resolve()
            spaces.setdefault(agent_path.name or "agent-default", str(agent_path))
        workspace_root = APP_DIR / "workspace"
        if workspace_root.exists():
            for project_dir in sorted(item for item in workspace_root.iterdir() if item.is_dir()):
                spaces.setdefault(project_dir.name, str(project_dir.resolve()))
        return spaces

    def _resolve_project_workdir(self, project_arg: str) -> str | None:
        cleaned = project_arg.strip()
        if not cleaned:
            return None
        project_spaces = self._project_spaces()
        named = project_spaces.get(cleaned)
        if named is not None:
            return named
        candidate = Path(cleaned)
        if not candidate.is_absolute():
            candidate = APP_DIR / candidate
        if candidate.exists() and candidate.is_dir():
            return str(candidate.resolve())
        return None

    def _project_name_for_workdir(self, workdir: str) -> str:
        resolved = str(Path(workdir).expanduser().resolve())
        for name, path in self._project_spaces().items():
            if path == resolved:
                return name
        return Path(resolved).name or resolved

    def _resolve_project_scope(self, project_arg: str, current_meta: WeixinSessionMeta) -> tuple[str | None, str]:
        if project_arg.strip():
            resolved = self._resolve_project_workdir(project_arg)
            if resolved is None:
                return None, ""
            return resolved, self._project_name_for_workdir(resolved)
        current_workdir = self._resolve_session_workdir(current_meta)
        return current_workdir, self._project_name_for_workdir(current_workdir)

    def _resolve_session_workdir(self, session_meta: WeixinSessionMeta) -> str:
        if session_meta.workdir.strip():
            return session_meta.workdir.strip()
        agent = self._find_agent_config(self.config.backend_id)
        if agent is not None and agent.workdir:
            return agent.workdir
        return str((APP_DIR / "workspace").resolve())

    def _resolve_session_model(self, session_meta: WeixinSessionMeta) -> str:
        model = self._effective_session_model(session_meta)
        return display_model(model)

    @staticmethod
    def _display_reasoning_effort(effort: str) -> str:
        return display_reasoning_effort(effort)

    def _resolve_session_permission_mode(self, session_meta: WeixinSessionMeta) -> str:
        cleaned = session_meta.permission_mode.strip().lower()
        return cleaned if cleaned else "full-access"

    def _display_permission_mode(self, mode: str) -> str:
        cleaned = str(mode or "").strip().lower()
        for value, label in PERMISSION_MODE_PRESETS:
            if value == cleaned:
                return label
        return cleaned or "Full Access"

    def _effective_session_model(self, session_meta: WeixinSessionMeta) -> str:
        if session_meta.model.strip():
            return session_meta.model.strip()
        agent = self._find_agent_config(self.config.backend_id)
        if agent is not None and agent.model.strip():
            return agent.model.strip()
        return ""

    @staticmethod
    def _display_model(model: str) -> str:
        return display_model(model)

    def _render_model_status(self, session_name: str, session_meta: WeixinSessionMeta) -> str:
        session_model = self._resolve_session_model(session_meta)
        agent = self._find_agent_config(self.config.backend_id)
        agent_model = agent.model.strip() if agent is not None and agent.model.strip() else "-"
        mode = self._t("bridge.model.mode.custom") if session_meta.model.strip() else self._t("bridge.model.mode.agent")
        return self._t(
            "bridge.model.current",
            session=session_name,
            mode=mode,
            model=session_model,
            agent_model=agent_model,
            reasoning=self._display_reasoning_effort(session_meta.reasoning_effort),
        )

    def _render_project_status(self, session_name: str, session_meta: WeixinSessionMeta) -> str:
        session_workdir = self._resolve_session_workdir(session_meta)
        agent = self._find_agent_config(self.config.backend_id)
        agent_workdir = agent.workdir if agent is not None else "-"
        mode = self._t("bridge.project.mode.custom") if session_meta.workdir.strip() else self._t("bridge.project.mode.agent")
        return self._t(
            "bridge.project.current",
            session=session_name,
            mode=mode,
            workdir=session_workdir,
            agent_workdir=agent_workdir,
        )

    def _render_project_list(self, session_meta: WeixinSessionMeta) -> str:
        current_workdir = self._resolve_session_workdir(session_meta)
        lines = [self._t("bridge.project.list.title")]
        for name, path in self._project_spaces().items():
            marker = "*" if path == current_workdir else "-"
            lines.append(self._t("bridge.project.list.item", marker=marker, name=name, path=path))
        if len(lines) == 1:
            lines.append(self._t("bridge.project.list.empty"))
        return "\n".join(lines)

    def _render_project_session_list(
        self,
        sender_id: str,
        binding: WeixinConversationBinding,
        project_arg: str,
    ) -> tuple[str, bool]:
        _, current_meta = binding.get_current_session(
            default_backend=self.config.default_backend,
            now=now_iso(),
            normalize_backend=normalize_backend,
        )
        project_path, project_name = self._resolve_project_scope(project_arg, current_meta)
        if project_path is None:
            return self._t("bridge.project.not_found", project=project_arg), True
        return (
            self._render_session_list(
                sender_id,
                binding,
                project_path=project_path,
                scope_label=self._t("bridge.session.list.scope.project", project=project_name),
            ),
            True,
        )

    def _render_local_codex_status(
        self,
        session_name: str,
        session_meta: WeixinSessionMeta,
        passthrough_prompt: str,
    ) -> str | None:
        if str(passthrough_prompt or "").strip().lower() != "/status":
            return None
        if session_meta.backend != "codex":
            return "当前会话后端不是 Codex，//status 只支持 Codex 会话。"
        response = self._ipc_request(
            "codex_status",
            {
                "agent_id": self.config.backend_id,
                "session_name": session_name,
                "workdir": self._resolve_session_workdir(session_meta),
            },
            timeout_seconds=15,
        )
        if not response.ok:
            return f"Codex 状态查询失败：{response.error or 'unknown error'}"
        status_panel = str(response.payload.get("status") or "").strip()
        if not status_panel:
            return "当前会话还没有可查询的 Codex 交互状态。请先在这个会话里发送一条普通消息。"
        return status_panel

    @staticmethod
    def _extract_passthrough_prompt(text: str) -> str | None:
        return BridgeNativeMenuRuntime.passthrough_prompt(text)

    def _render_restart_status(self) -> str:
        payload = load_json(SERVICE_ACTION_STATE_FILE, {}, expect_type=dict)
        if not isinstance(payload, dict) or not payload:
            return self._t("bridge.restart.status.empty")
        lines = [
            self._t(
                "bridge.restart.status.header",
                request_id=str(payload.get("request_id") or "-"),
                action=str(payload.get("action") or "-"),
                status=str(payload.get("status") or "-"),
                updated_at=str(payload.get("updated_at") or "-"),
            )
        ]
        if payload.get("hub_pid_before") is not None or payload.get("bridge_pid_before") is not None:
            lines.append(
                self._t(
                    "bridge.restart.status.before",
                    hub=str(payload.get("hub_pid_before") or "-"),
                    bridge=str(payload.get("bridge_pid_before") or "-"),
                )
            )
        if payload.get("hub_pid_after") is not None or payload.get("bridge_pid_after") is not None:
            lines.append(
                self._t(
                    "bridge.restart.status.after",
                    hub=str(payload.get("hub_pid_after") or "-"),
                    bridge=str(payload.get("bridge_pid_after") or "-"),
                )
            )
        result_message = str(payload.get("result_message") or "").strip()
        if result_message:
            lines.append(self._t("bridge.restart.status.result", result=result_message))
        error = str(payload.get("error") or "").strip()
        if error:
            lines.append(self._t("bridge.restart.status.error", error=error))
        return "\n".join(lines)

    def _find_agent_config(self, agent_id: str):
        return next((agent for agent in HubConfig.load().agents if agent.id == agent_id), None)

    @staticmethod
    def _load_codex_model_catalog() -> list[dict[str, Any]]:
        return load_codex_model_catalog()

    def _clear_current_agent_session(self, sender_id: str, current_session: str) -> str:
        agent = self._find_agent_config(self.config.backend_id)
        if agent is None:
            return self._t("bridge.agent.not_found", agent=self.config.backend_id)

        session_name = current_session or "default"
        canceled_count = self._cancel_active_session_tasks(sender_id, session_name)
        session_file = resolve_session_file(agent, session_name, SESSION_DIR)
        backend = normalize_backend(getattr(agent, "backend", "") or self.config.default_backend)
        if not session_file.exists() or not session_file.read_text(encoding="utf-8").strip():
            message = self._t("bridge.session.clear.empty", session=session_name, backend=backend)
            if canceled_count:
                message += "\n" + self._t("bridge.session.clear.canceled", count=canceled_count)
            return message

        session_file.write_text("", encoding="utf-8")
        message = self._t("bridge.session.clear", session=session_name, backend=backend)
        if canceled_count:
            message += "\n" + self._t("bridge.session.clear.canceled", count=canceled_count)
        return message

    def _cancel_active_session_tasks(self, sender_id: str, session_name: str) -> int:
        canceled_count = 0
        removed_pending = False
        task_ids = self._active_session_task_ids(sender_id, session_name)
        for task_id in task_ids:
            try:
                response = self._ipc_request("cancel_task", {"task_id": task_id}, timeout_seconds=5)
            except Exception as exc:  # noqa: BLE001
                print(f"[bridge] clear failed to cancel task {task_id}: {exc}", flush=True)
            else:
                if response.ok:
                    canceled_count += 1
                else:
                    print(f"[bridge] clear could not cancel task {task_id}: {response.error or 'unknown error'}", flush=True)
            if self.pending_tasks.pop(task_id, None) is not None:
                removed_pending = True
        if canceled_count or removed_pending:
            self._save_pending_tasks()
        return canceled_count

    def _active_session_task_ids(self, sender_id: str, session_name: str) -> list[str]:
        target_session = session_name or "default"
        task_ids: list[str] = []
        seen: set[str] = set()
        for task_id, tracked in self.pending_tasks.items():
            if tracked.sender_id != sender_id:
                continue
            if (tracked.session_name or "default") != target_session:
                continue
            seen.add(task_id)
            task_ids.append(task_id)
        try:
            sender_tasks = self._load_sender_tasks(sender_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge] clear failed to load sender tasks: {exc}", flush=True)
            return task_ids
        for task in sender_tasks:
            if task.status not in {"queued", "running"}:
                continue
            if (task.session_name or "default") != target_session:
                continue
            if task.id in seen:
                continue
            seen.add(task.id)
            task_ids.append(task.id)
        return task_ids

    def _render_status(self, binding: WeixinConversationBinding, current_session: str, backend: str) -> str:
        agent = self._find_agent_config(self.config.backend_id)
        workdir = agent.workdir if agent is not None else "-"
        model = agent.model.strip() if agent is not None and agent.model.strip() else "-"
        agent_backend = agent.backend if agent is not None else "-"
        current_meta = binding.sessions.get(current_session) or self._new_session_meta()
        project_workdir = self._resolve_session_workdir(current_meta)
        return self._t(
            "bridge.status",
            agent=self.config.backend_id,
            agent_backend=agent_backend,
            model=model,
            workdir=workdir,
            session=current_session,
            backend=backend,
            current_model=self._resolve_session_model(current_meta),
            current_project=self._project_name_for_workdir(project_workdir),
            project_workdir=project_workdir,
            count=len(binding.sessions),
        ) + "\n" + self._t("bridge.status.relation")

    def _render_context(self, session_name: str, session_meta: WeixinSessionMeta) -> str:
        agent = self._find_agent_config(self.config.backend_id)
        agent_backend = agent.backend if agent is not None and agent.backend else "-"
        agent_model = agent.model.strip() if agent is not None and agent.model.strip() else "-"
        agent_workdir = agent.workdir if agent is not None and agent.workdir else "-"
        return "\n".join(
            build_context_relation_lines(
                self._t,
                agent_id=self.config.backend_id,
                agent_backend=agent_backend,
                agent_model=agent_model,
                agent_workdir=agent_workdir,
                session_name=session_name,
                session_backend=session_meta.backend,
                session_model=self._resolve_session_model(session_meta),
                session_workdir=self._resolve_session_workdir(session_meta),
            )
        )

    def _render_agent_details(self, agent_id: str) -> str:
        agent = self._find_agent_config(agent_id)
        if agent is None:
            return self._t("bridge.agent.not_found", agent=agent_id)
        return self._t(
            "bridge.agent.current",
            agent=agent.id,
            name=agent.name or agent.id,
            backend=agent.backend or "-",
            model=agent.model.strip() or "-",
            workdir=agent.workdir or "-",
            enabled=self._t("bridge.notify.on") if agent.enabled else self._t("bridge.notify.off"),
        )

    def _render_agent_list(self) -> str:
        lines = [self._t("bridge.agent.list.title")]
        for agent in HubConfig.load().agents:
            marker = "*" if agent.id == self.config.backend_id else "-"
            lines.append(
                self._t(
                    "bridge.agent.list.item",
                    marker=marker,
                    agent=agent.id,
                    backend=agent.backend or "-",
                    model=agent.model.strip() or "-",
                    workdir=agent.workdir or "-",
                )
            )
        return "\n".join(lines)

    def _render_agent_command_help(self) -> str:
        agent = self._find_agent_config(self.config.backend_id)
        backend = agent.backend if agent is not None and agent.backend else "-"
        guide = get_backend_command_guide(backend)
        if guide is None:
            return self._t(
                "bridge.agent.command_help.generic",
                agent=self.config.backend_id,
                backend=backend,
            )
        lines = [
            self._t("bridge.agent.command_help.header", agent=self.config.backend_id, backend=backend),
            "",
            guide.title,
            guide.summary,
        ]
        lines.extend(f"- {item}" for item in guide.command_groups)
        if guide.footer:
            lines.extend(["", guide.footer])
        return "\n".join(lines)

    def _render_task_summary(self, task: HubTask) -> str:
        task_id = task.id
        session_name = task.session_name or "default"
        status = self._display_task_status(task.status)
        agent_name = task.agent_name or task.agent_id
        backend = task.backend or self.config.default_backend
        prompt = task.prompt.strip()[:400] or "(empty)"
        result = (task.output or task.error).strip()[:800] or "(empty)"
        return self._t(
            "bridge.task.lookup.summary",
            task_id=task_id,
            session=session_name,
            status=status,
            agent=agent_name,
            backend=backend,
            model=task.model.strip() or "-",
            prompt=prompt,
            result=result,
        )

    def _display_task_status(self, status: str) -> str:
        cleaned = str(status or "").strip().lower()
        return self._t(f"bridge.task.status.{cleaned}") if cleaned else self._t("bridge.task.status.unknown")

    def _render_recent_events(self, sender_id: str, *, limit: int) -> str:
        bounded_limit = min(max(limit, 1), 20)
        entries = self._load_recent_events(sender_id=sender_id, limit=bounded_limit)
        lines = [self._t("bridge.events.title", count=len(entries), limit=bounded_limit)]
        if not entries:
            lines.append(self._t("bridge.events.empty"))
            return "\n".join(lines)
        for entry in entries:
            lines.append(
                self._t(
                    "bridge.events.item",
                    at=str(entry.get("at") or "-"),
                    event=self._display_event_name(str(entry.get("event") or "unknown")),
                    task_id=str(entry.get("task_id") or "-"),
                    session=str(entry.get("session_name") or "default"),
                    session_id=str(entry.get("session_id") or "-") or "-",
                    detail=self._build_event_detail(entry),
                )
            )
        return "\n".join(lines)

    def _display_event_name(self, event: str) -> str:
        cleaned = str(event or "").strip().lower()
        return self._t(f"bridge.events.event.{cleaned}") if cleaned else self._t("bridge.events.event.unknown")

    def _build_event_detail(self, entry: dict[str, str]) -> str:
        event = str(entry.get("event") or "").strip().lower()
        backend = str(entry.get("backend") or "-").strip() or "-"
        result_preview = str(entry.get("result_preview") or "").strip()
        error = str(entry.get("error") or "").strip()
        if event == "accepted":
            return self._t("bridge.events.detail.accepted", backend=backend)
        if event == "running":
            return self._t("bridge.events.detail.running", backend=backend)
        if event == "progress" and result_preview:
            return result_preview
        if result_preview:
            return result_preview
        if error:
            return error
        if backend and backend != "-":
            return self._t("bridge.events.detail.backend", backend=backend)
        return "-"

    @staticmethod
    def _normalize_command_text(text: str) -> str:
        return normalize_command_text(text)

    def _ensure_conversation(self, sender_id: str) -> WeixinConversationBinding:
        existing = self.conversations.get(sender_id)
        if existing:
            if self._normalize_unified_conversation(existing):
                self._save_conversations()
            return existing

        created = WeixinConversationBinding.create(
            default_backend=normalize_backend(self.config.default_backend),
            now=now_iso(),
        )
        created.last_regular_session = "default"
        self.conversations[sender_id] = created
        self._save_conversations()
        return created

    def _normalize_unified_conversation(self, binding: WeixinConversationBinding) -> bool:
        changed = False
        if "default" not in binding.sessions:
            binding.sessions["default"] = self._new_session_meta()
            changed = True
        if binding.current_session not in binding.sessions:
            binding.current_session = binding.last_regular_session if binding.last_regular_session in binding.sessions else "default"
            changed = True
        if not binding.current_session or binding.current_session not in binding.sessions:
            binding.current_session = "default"
            changed = True
        if (
            not binding.last_regular_session
            or binding.last_regular_session not in binding.sessions
        ):
            binding.last_regular_session = binding.current_session if binding.current_session in binding.sessions else "default"
            changed = True
        return changed

    def _new_session_meta(
        self,
        backend: Any = "",
        *,
        workdir: str = "",
        model: str = "",
        reasoning_effort: str = "",
        permission_mode: str = "",
    ) -> WeixinSessionMeta:
        return create_session_meta(
            WeixinSessionMeta,
            backend=backend,
            default_backend=self.config.default_backend,
            now=now_iso(),
            normalize_backend=normalize_backend,
            workdir=workdir.strip(),
            model=model.strip(),
            reasoning_effort=reasoning_effort.strip(),
            permission_mode=permission_mode.strip(),
        )

    def _allocate_session_name(self, binding: WeixinConversationBinding, requested: str) -> str:
        return allocate_session_name(binding, requested)

    def _sanitize_session_name(self, requested: str, *, fallback: str) -> str:
        return sanitize_session_name(requested, fallback=fallback)

    def _sanitize_project_name(self, requested: str) -> str:
        return sanitize_project_name(requested)

    def _store_pending_restart_notice(self, sender_id: str, *, scope: str) -> None:
        cleaned_sender_id = str(sender_id or "").strip()
        context_token = self.context_tokens.get(cleaned_sender_id, "")
        RESTART_NOTICE_PATH.parent.mkdir(parents=True, exist_ok=True)
        save_json(
            RESTART_NOTICE_PATH,
            {
                "sender_id": cleaned_sender_id,
                "context_token": context_token,
                "scope": scope,
                "requested_at": now_iso(),
            },
        )

    @staticmethod
    def _split_named_path_args(raw: str) -> tuple[str, str]:
        return split_named_path_args(raw)

    def _load_account(self) -> dict[str, Any]:
        self._ensure_local_account_storage()
        if not self.account_path.exists():
            raise FileNotFoundError(f"account file not found: {self.account_path}")
        data = load_json(self.account_path, None, expect_type=dict)
        if data is None:
            raise RuntimeError(f"account file is invalid: {self.account_path}")
        return data

    def _load_sync_buf(self) -> str:
        self._ensure_local_account_storage()
        data = load_json(self.sync_path, {}, expect_type=dict)
        return str(data.get("get_updates_buf") or "")

    def _save_sync_buf(self, buf: str) -> None:
        self.sync_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(self.sync_path, {"get_updates_buf": buf})

    def _ensure_local_account_storage(self) -> None:
        self.account_path.parent.mkdir(parents=True, exist_ok=True)
        self.sync_path.parent.mkdir(parents=True, exist_ok=True)

    def _request(self, method: str, url: str, body: dict[str, Any] | None = None, token: str = "", timeout_ms: int = 15000) -> dict[str, Any]:
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {}
        if url.startswith("https://ilinkai.weixin.qq.com") or "/ilink/bot/" in url:
            headers["AuthorizationType"] = "ilink_bot_token"
            headers["X-WECHAT-UIN"] = base64.b64encode(str(random.randint(1, 2**32 - 1)).encode("utf-8")).decode("ascii")
            headers["iLink-App-Id"] = ILINK_APP_ID
            headers["iLink-App-ClientVersion"] = str(ILINK_APP_CLIENT_VERSION)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        req = urllib.request.Request(url=url, data=payload, headers=headers, method=method)
        return request_json(req, timeout=timeout_ms / 1000)

    def _post_json(self, url: str, body: dict[str, Any], token: str = "", timeout_ms: int = 15000) -> dict[str, Any]:
        try:
            return self._request("POST", url, body=body, token=token, timeout_ms=timeout_ms)
        except RuntimeError as exc:
            raise RuntimeError(f"POST {url} failed: {exc}") from exc

    @staticmethod
    def _is_expected_getupdates_timeout(exc: RuntimeError) -> bool:
        message = str(exc).lower()
        if "/ilink/bot/getupdates" not in message:
            return False
        return "timed out" in message or "timeout" in message

    def _ipc_request(self, action: str, payload: dict[str, Any], timeout_seconds: float) -> IpcResponseEnvelope:
        started_at = time.perf_counter()
        request_id = create_request(action, payload)
        try:
            response = wait_for_response(request_id, timeout_seconds)
        except Exception:
            self._log_perf("ipc_request", started_at, action=action, request_id=request_id, status="failed")
            raise
        self._log_perf("ipc_request", started_at, action=action, request_id=request_id, status="ok" if response.ok else "not_ok")
        return response

    def _save_state(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.state.sync_files(
            managed_conversations=len(self.conversations),
            account_file=str(self.account_path),
            sync_file=str(self.sync_path),
        )
        save_json(STATE_PATH, self.state.to_dict())

    def _load_pending_tasks(self) -> dict[str, WeixinPendingTaskState]:
        return self.pending_task_store.load()

    def _save_pending_tasks(self) -> None:
        with self._pending_tasks_save_lock:
            self.pending_task_store.save(self.pending_tasks)

    def _find_active_pending_task(self, sender_id: str) -> WeixinPendingTaskState | None:
        for pending in self.pending_tasks.values():
            if pending.sender_id == sender_id:
                return pending
        return None

    def _cancel_task_best_effort(self, task_id: str) -> None:
        self._interrupted_task_ids.add(task_id)
        try:
            self._ipc_request("cancel_task", {"task_id": task_id}, timeout_seconds=5)
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge] interrupt cancel failed task_id={task_id}: {exc}", flush=True)
        if self.pending_tasks.pop(task_id, None) is not None:
            self._save_pending_tasks()

    def _load_conversations(self) -> dict[str, Any]:
        source_path = self.conversation_path
        if source_path != CONVERSATION_PATH and not source_path.exists() and CONVERSATION_PATH != BRIDGE_CONVERSATIONS_PATH:
            source_path = CONVERSATION_PATH
            self.conversation_path = source_path
        data = load_json(source_path, {}, expect_type=dict)
        if not isinstance(data, dict):
            return {}
        conversations: dict[str, WeixinConversationBinding] = {}
        for sender_id, binding in data.items():
            cleaned_sender_id = str(sender_id or "").strip()
            if not cleaned_sender_id:
                continue
            conversations[cleaned_sender_id] = WeixinConversationBinding.from_dict(
                binding,
                default_backend=self.config.default_backend,
                now=now_iso(),
                normalize_backend=normalize_backend,
            )
        return conversations

    def _save_conversations(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        save_json(
            self.conversation_path,
            {sender_id: binding.to_dict() for sender_id, binding in self.conversations.items()},
        )

    def _t(self, key: str, **kwargs: Any) -> str:
        return self.localizer.translate(key, **kwargs)


def main() -> int:
    cfg = BridgeConfig.load()
    WeixinBridge(cfg).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
