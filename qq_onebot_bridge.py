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

from bridge_config import BridgeConfig
from core.bridge_command_catalog import QQ_HELP_MESSAGE_KEYS, parse_bridge_command, render_bridge_help
from core.json_store import load_json, save_json
from core.runtime_paths import RUNTIME_DIR, STATE_DIR
from core.state_models import HubTask
from core.weixin_message_format import format_duration_since, prefix_weixin_output
from localization import Localizer
from local_ipc import cleanup_processed_requests, create_request, wait_for_response


ONEBOT_STATE_PATH = STATE_DIR / "qq_onebot_pending_media_context.json"
ONEBOT_UPLOAD_DIR = RUNTIME_DIR / "uploads" / "qq"
MEDIA_CONTEXT_TTL_SECONDS = 10 * 60
MEDIA_RECEIVE_MAX_BYTES = 50 * 1024 * 1024
TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "canceled", "unknown_after_restart"})
LOCAL_URL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
DEFAULT_QQ_AGENT_ID = "qq"


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
        self.pending_media_context = self._load_pending_media_context()

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
        _log(f"message sender={sender_key} type={message_type} text_preview={text[:80]!r} media={len(media_attachments)} media_errors={len(media_errors)}")
        if not text:
            if media_attachments:
                self._remember_pending_media_context(sender_key, media_attachments)
            elif media_errors:
                self._send_reply(event, "附件接收失败：\n" + "\n".join(media_errors))
            return
        command_reply, command_handled = self._handle_control_command(sender_key, text)
        if command_handled:
            self._send_reply(event, command_reply)
            return
        prompt = self._build_prompt_with_media(text, [*self._consume_pending_media_context(sender_key), *media_attachments], media_errors)
        task = self._submit_task(sender_key, prompt)
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            self._send_reply(event, "任务提交失败：Hub 没有返回 task id")
            return
        _log(f"submitted task_id={task_id} sender={sender_key}")
        self._start_thread("wait_and_reply", self._wait_and_reply, event, task_id)

    def _start_thread(self, name: str, target: Any, *args: Any) -> None:
        def run_target() -> None:
            try:
                target(*args)
            except Exception as exc:  # noqa: BLE001
                _log_error(f"{name} failed: {type(exc).__name__}: {exc}")

        threading.Thread(target=run_target, daemon=True, name=f"qq-bridge-{name}").start()

    def _submit_task(self, sender_key: str, prompt: str) -> dict[str, Any]:
        request_id = create_request(
            "submit_task",
            {
                "agent_id": self.agent_id,
                "prompt": prompt,
                "source": "qq",
                "sender_id": sender_key,
                "session_name": self._session_name(sender_key),
                "backend": self.config.default_backend,
            },
        )
        response = wait_for_response(request_id, timeout_seconds=60)
        if not response.ok:
            raise RuntimeError(str(response.error or "submit_task failed"))
        task = response.payload.get("task")
        return task if isinstance(task, dict) else {}

    def _handle_control_command(self, sender_key: str, text: str) -> tuple[str, bool]:
        parsed = parse_bridge_command(text)
        if parsed is None:
            return "", False
        if parsed.is_passthrough:
            if parsed.passthrough_prompt.strip().lower() == "/status":
                return self._render_codex_status(sender_key), True
            return "", False

        command = parsed.command
        parts = list(parsed.parts)
        if command in {"/help", "/h", "/?"}:
            return render_bridge_help(lambda key: self._t(key), QQ_HELP_MESSAGE_KEYS), True
        if command == "/status":
            return self._render_status(sender_key), True
        if command == "/task":
            task_id = parts[1].strip() if len(parts) >= 2 else ""
            if not task_id:
                return self._t("bridge.task.lookup.usage"), True
            task = self._get_task(task_id)
            if task is None or task.sender_id != sender_key:
                return self._t("bridge.task.lookup.not_found", task_id=task_id), True
            return self._render_task_summary(task), True
        if command == "/last":
            task = self._find_latest_sender_task(sender_key)
            if task is None:
                return self._t("bridge.task.lookup.none"), True
            return self._render_task_summary(task), True
        if command == "/cancel":
            task = self._resolve_sender_task_for_command(
                sender_key,
                parts[1].strip() if len(parts) >= 2 else "",
                allowed_statuses={"queued", "running"},
            )
            if task is None:
                return self._t("bridge.task.cancel.none"), True
            response = self._ipc_request("cancel_task", {"task_id": task.id}, timeout_seconds=5)
            if not response.ok:
                return self._t("bridge.task.cancel.failed", task_id=task.id, error=str(response.error or "unknown error")), True
            canceled = HubTask.from_dict(response.payload.get("task"), default_backend=self.config.default_backend)
            if canceled is None:
                return self._t("bridge.task.cancel.failed", task_id=task.id, error="invalid task payload"), True
            return self._t("bridge.task.cancel.ok", task_id=canceled.id, session=canceled.session_name or self._session_name(sender_key)), True
        if command == "/retry":
            task = self._resolve_sender_task_for_command(sender_key, parts[1].strip() if len(parts) >= 2 else "")
            if task is None:
                return self._t("bridge.task.retry.none"), True
            response = self._ipc_request("retry_task", {"task_id": task.id, "source": "qq", "sender_id": sender_key}, timeout_seconds=5)
            if not response.ok:
                return self._t("bridge.task.retry.failed", task_id=task.id, error=str(response.error or "unknown error")), True
            retried = HubTask.from_dict(response.payload.get("task"), default_backend=self.config.default_backend)
            if retried is None:
                return self._t("bridge.task.retry.failed", task_id=task.id, error="invalid task payload"), True
            return self._t(
                "bridge.task.retry.ok",
                original=task.id,
                task_id=retried.id,
                session=retried.session_name or self._session_name(sender_key),
                backend=retried.backend or self.config.default_backend,
            ), True
        return "QQ 桥暂不支持这个桥接命令。发送 /help 查看当前支持的命令。", True

    def _render_status(self, sender_key: str) -> str:
        latest_task = self._find_latest_sender_task(sender_key)
        latest_line = "-"
        if latest_task is not None:
            latest_line = f"{latest_task.id} [{self._display_task_status(latest_task.status)}]"
        return "\n".join(
            [
                "当前设置",
                f"当前助手: {self.agent_id}",
                f"当前后端: {self.config.default_backend}",
                f"当前会话: {self._session_name(sender_key)}",
                f"最近任务: {latest_line}",
            ]
        )

    def _render_codex_status(self, sender_key: str) -> str:
        if self.config.default_backend != "codex":
            return "当前会话后端不是 Codex，//status 只支持 Codex 会话。"
        response = self._ipc_request(
            "codex_status",
            {
                "agent_id": self.agent_id,
                "session_name": self._session_name(sender_key),
                "workdir": "",
            },
            timeout_seconds=15,
        )
        if not response.ok:
            return f"Codex 状态查询失败：{response.error or 'unknown error'}"
        status_panel = str(response.payload.get("status") or "").strip()
        if not status_panel:
            return "当前会话还没有可查询的 Codex 交互状态。请先在这个会话里发送一条普通消息。"
        return status_panel

    def _render_task_summary(self, task: HubTask) -> str:
        prompt = task.prompt.strip()[:400] or "(empty)"
        result = (task.output or task.error).strip()[:800] or "(empty)"
        return self._t(
            "bridge.task.lookup.summary",
            task_id=task.id,
            session=task.session_name or "default",
            status=self._display_task_status(task.status),
            agent=task.agent_name or task.agent_id,
            backend=task.backend or self.config.default_backend,
            model=task.model.strip() or "-",
            prompt=prompt,
            result=result,
        )

    def _display_task_status(self, status: str) -> str:
        cleaned = str(status or "").strip().lower()
        return self._t(f"bridge.task.status.{cleaned}") if cleaned else self._t("bridge.task.status.unknown")

    def _resolve_sender_task_for_command(
        self,
        sender_key: str,
        task_id: str,
        *,
        allowed_statuses: set[str] | None = None,
    ) -> HubTask | None:
        cleaned_id = task_id.strip()
        if cleaned_id:
            task = self._get_task(cleaned_id)
            if task is None or task.sender_id != sender_key:
                return None
            if allowed_statuses is not None and task.status not in allowed_statuses:
                return None
            return task
        return self._find_latest_sender_task(sender_key, allowed_statuses=allowed_statuses)

    def _find_latest_sender_task(self, sender_key: str, *, allowed_statuses: set[str] | None = None) -> HubTask | None:
        response = self._ipc_request("state", {}, timeout_seconds=5)
        if not response.ok:
            return None
        tasks: list[HubTask] = []
        for raw_task in response.payload.get("tasks") or []:
            task = HubTask.from_dict(raw_task, default_backend=self.config.default_backend)
            if task is None or task.sender_id != sender_key:
                continue
            if allowed_statuses is not None and task.status not in allowed_statuses:
                continue
            tasks.append(task)
        if not tasks:
            return None
        return sorted(tasks, key=lambda item: item.finished_at or item.started_at or item.created_at, reverse=True)[0]

    def _get_task(self, task_id: str) -> HubTask | None:
        response = self._ipc_request("get_task", {"task_id": task_id}, timeout_seconds=5)
        if not response.ok:
            return None
        return HubTask.from_dict(response.payload.get("task"), default_backend=self.config.default_backend)

    def _ipc_request(self, action: str, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        return wait_for_response(create_request(action, payload), timeout_seconds=timeout_seconds)

    def _t(self, key: str, **kwargs: Any) -> str:
        return self.localizer.translate(key, **kwargs)

    def _wait_and_reply(self, event: dict[str, Any], task_id: str) -> None:
        deadline = time.time() + max(60, int(self.config.hub_task_timeout_seconds))
        latest_task: HubTask | None = None
        while time.time() < deadline:
            response = wait_for_response(
                create_request("get_task", {"task_id": task_id}),
                timeout_seconds=10,
            )
            if response.ok:
                latest_task = HubTask.from_dict(response.payload.get("task"), default_backend=self.config.default_backend)
                if latest_task is not None and latest_task.status in TERMINAL_TASK_STATUSES:
                    break
            time.sleep(1.0)
        if latest_task is None:
            self._send_reply(event, f"任务状态查询失败：{task_id}")
            return
        _log(f"task terminal task_id={task_id} status={latest_task.status} output_preview={latest_task.output[:80]!r} error_preview={latest_task.error[:80]!r}")
        self._send_reply(event, self._format_task_reply(latest_task))

    def _format_task_reply(self, task: HubTask) -> str:
        if task.status == "succeeded":
            return prefix_weixin_output(
                "done",
                format_duration_since(task.started_at or task.created_at, ended_at=task.finished_at),
                task.output.strip() or "(empty)",
                at=task.finished_at,
                context_left_percent=task.context_left_percent,
            )
        return f"任务 {task.status or 'failed'}：{(task.error or 'unknown error').strip()}"

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
        return _safe_path_part(sender_key)

    def _load_pending_media_context(self) -> dict[str, list[dict[str, str]]]:
        payload = load_json(ONEBOT_STATE_PATH, {}, expect_type=dict)
        if not isinstance(payload, dict):
            return {}
        current = now_seconds()
        contexts: dict[str, list[dict[str, str]]] = {}
        for sender_key, raw_items in payload.items():
            if not isinstance(raw_items, list):
                continue
            items: list[dict[str, str]] = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                created_at = int(item.get("created_at") or 0)
                if created_at and current - created_at > MEDIA_CONTEXT_TTL_SECONDS:
                    continue
                path = str(item.get("path") or "").strip()
                if path:
                    items.append(
                        {
                            "kind": str(item.get("kind") or "file"),
                            "name": str(item.get("name") or Path(path).name),
                            "path": path,
                            "created_at": str(created_at or current),
                        }
                    )
            if items:
                contexts[str(sender_key)] = items
        return contexts

    def _save_pending_media_context(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        save_json(ONEBOT_STATE_PATH, self.pending_media_context)

    def _remember_pending_media_context(self, sender_key: str, attachments: list[dict[str, str]]) -> None:
        current = str(now_seconds())
        pending = self.pending_media_context.get(sender_key, [])
        pending.extend({**attachment, "created_at": current} for attachment in attachments)
        self.pending_media_context[sender_key] = pending[-10:]
        self._save_pending_media_context()

    def _consume_pending_media_context(self, sender_key: str) -> list[dict[str, str]]:
        raw_items = self.pending_media_context.pop(sender_key, [])
        current = now_seconds()
        items: list[dict[str, str]] = []
        for item in raw_items:
            try:
                created_at = int(item.get("created_at") or 0)
            except (TypeError, ValueError):
                created_at = 0
            if current - created_at <= MEDIA_CONTEXT_TTL_SECONDS:
                items.append(item)
        if raw_items:
            self._save_pending_media_context()
        return items

    @staticmethod
    def _build_prompt_with_media(prompt: str, attachments: list[dict[str, str]], errors: list[str]) -> str:
        parts = [prompt.strip()] if prompt.strip() else []
        if attachments:
            lines = ["用户发送了以下 QQ 附件，已保存到本地："]
            for attachment in attachments:
                label = "图片" if attachment.get("kind") == "image" else "文件"
                lines.append(f"- {label}: {attachment.get('name') or '-'}")
                lines.append(f"  本地路径: {attachment.get('path') or '-'}")
            parts.append("\n".join(lines))
        if errors:
            parts.append("以下 QQ 附件接收失败：\n" + "\n".join(errors))
        return "\n\n".join(part for part in parts if part).strip()

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
    host = os.environ.get("QQ_ONEBOT_LISTEN_HOST") or "127.0.0.1"
    port = int(os.environ.get("QQ_ONEBOT_LISTEN_PORT") or "5701")
    server = ThreadingHTTPServer((host, port), bridge.make_handler())
    print(f"QQ OneBot Bridge listening on http://{host}:{port}; api={bridge.api_base}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
