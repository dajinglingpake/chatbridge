from __future__ import annotations

import base64
import hashlib
import json
import random
import time
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_backends import get_backend_command_guide, supported_backend_keys
from agent_hub import HubConfig
from bridge_config import APP_DIR, CONFIG_PATH, WEIXIN_ACCOUNTS_DIR, BridgeConfig, normalize_backend
from core.context_relations import build_context_relation_lines
from core.http_json import request_json
from core.json_store import load_json, save_json
from core.state_models import HubTask, IpcResponseEnvelope, WeixinBridgeRuntimeState, WeixinConversationBinding, WeixinSessionMeta
from core.weixin_notifier import build_task_followup_hint
from local_ipc import create_request, wait_for_response
from localization import Localizer


RUNTIME_DIR = APP_DIR / ".runtime"
STATE_DIR = RUNTIME_DIR / "state"
EXPORT_DIR = RUNTIME_DIR / "exports"
STATE_PATH = STATE_DIR / "weixin_hub_bridge_state.json"
CONVERSATION_PATH = STATE_DIR / "weixin_conversations.json"
DEFAULT_WEIXIN_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (1 << 8) | 1
SUPPORTED_BACKENDS = set(supported_backend_keys())
SESSION_PAGE_SIZE = 5


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class WeixinBridge:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.localizer = Localizer(config.language)
        self.account_path = Path(config.account_file)
        self.sync_path = Path(config.sync_file)
        self._ensure_local_account_storage()
        self.conversations = self._load_conversations()
        self._recent_message_keys: list[str] = []
        self.state = WeixinBridgeRuntimeState.create(
            now=now_iso(),
            managed_conversations=len(self.conversations),
            account_file=str(self.account_path),
            sync_file=str(self.sync_path),
        )

    def run(self) -> None:
        print(f"Weixin Hub Bridge started at {now_iso()}")
        print(f"Config: {CONFIG_PATH}")
        print(f"State: {STATE_PATH}")
        while True:
            try:
                self.poll_once()
                self.state.clear_error()
            except Exception as exc:  # noqa: BLE001
                self.state.set_error(str(exc))
                self._save_state()
                print(f"[bridge] poll error: {exc}")
                time.sleep(3)

    def poll_once(self) -> None:
        account = self._load_account()
        token = (account.get("token") or "").strip()
        if not token:
            raise RuntimeError("weixin account token is missing; please log in first")
        base_url = (account.get("baseUrl") or DEFAULT_WEIXIN_BASE_URL).strip()
        buf = self._load_sync_buf()

        payload = {"get_updates_buf": buf, "base_info": {"channel_version": "2.1.1"}}
        response = self._post_json(f"{base_url}/ilink/bot/getupdates", payload, token=token, timeout_ms=self.config.poll_timeout_ms)
        self.state.mark_poll(now=now_iso())
        if response.get("ret") not in (None, 0):
            raise RuntimeError(f"weixin getupdates failed: ret={response.get('ret')} errcode={response.get('errcode')} errmsg={response.get('errmsg')}")

        next_buf = response.get("get_updates_buf")
        if isinstance(next_buf, str) and next_buf:
            self._save_sync_buf(next_buf)

        for msg in response.get("msgs") or []:
            self._handle_message(base_url, token, msg)

        self._save_state()

    def _handle_message(self, base_url: str, token: str, msg: dict[str, Any]) -> None:
        if msg.get("message_type") != 1:
            return

        sender_id = str(msg.get("from_user_id") or "").strip()
        if not sender_id:
            return

        text = self._extract_text(msg)
        if not text:
            return
        if any(text.startswith(prefix) for prefix in self.config.ignore_prefixes):
            return
        message_key = self._message_key(msg, text)
        if self._is_duplicate_message(message_key):
            return

        binding = self._ensure_conversation(sender_id)
        session_name, session_meta = binding.get_current_session(
            default_backend=self.config.default_backend,
            now=now_iso(),
            normalize_backend=normalize_backend,
        )
        passthrough_prompt = self._extract_passthrough_prompt(text)
        if passthrough_prompt is None:
            reply, handled = self._handle_control_command(sender_id, text)
            if handled:
                if reply:
                    self._send_text(base_url, token, sender_id, msg.get("context_token"), reply)
                    self.state.record_handled()
                self._save_state()
                return
            prompt = text.strip()
        else:
            prompt = passthrough_prompt
        if not prompt:
            return

        self.state.mark_message(now=now_iso(), sender_id=sender_id)

        response = self._ipc_request(
            "submit_task",
            {
                "agent_id": self.config.backend_id,
                "prompt": prompt,
                "source": "wechat",
                "sender_id": sender_id,
                "session_name": session_name,
                "backend": session_meta.backend,
                "workdir": self._resolve_session_workdir(session_meta),
                "model": self._resolve_session_model(session_meta),
            },
            timeout_seconds=15,
        )
        if not response.ok:
            raise RuntimeError(str(response.error or "submit_task failed"))
        task = response.payload.get("task") or {}
        task_id = str(task.get("id") or "")
        self._send_text(
            base_url,
            token,
            sender_id,
            msg.get("context_token"),
            self._t(
                "bridge.task.accepted",
                task_id=task_id or "-",
                session=session_name or "default",
                backend=session_meta.backend,
                model=self._resolve_session_model(session_meta),
            ),
        )

        result = self._wait_for_task(
            str(task["id"]),
            on_status_change=lambda status_task: self._notify_task_progress(
                base_url,
                token,
                sender_id,
                msg.get("context_token"),
                status_task,
            ),
        )
        if result.status == "succeeded":
            reply = result.output.strip()
            if self.config.auto_reply_prefix:
                reply = f"{self.config.auto_reply_prefix}{reply}"
            self._send_text(base_url, token, sender_id, msg.get("context_token"), reply)
            self.state.record_handled()
        elif result.status == "canceled":
            backend_name = str(result.backend or session_meta.backend or self.config.default_backend).strip()
            self._send_text(
                base_url,
                token,
                sender_id,
                msg.get("context_token"),
                self._t(
                    "bridge.task.canceled",
                    backend=backend_name,
                    error=str(result.error or "task canceled").strip(),
                    hint=build_task_followup_hint(
                        task_id=result.id,
                        session_name=result.session_name or "default",
                        allow_retry=True,
                    ),
                ),
            )
        else:
            backend_name = str(result.backend or session_meta.backend or self.config.default_backend).strip()
            error_text = str(result.error or "task failed").strip()
            self._send_text(
                base_url,
                token,
                sender_id,
                msg.get("context_token"),
                self._t(
                    "bridge.task.failed",
                    backend=backend_name,
                    error=error_text,
                    hint=build_task_followup_hint(
                        task_id=result.id,
                        session_name=result.session_name or "default",
                        allow_retry=True,
                    ),
                ),
            )
            self.state.record_failed()

    def _wait_for_task(self, task_id: str, on_status_change=None) -> HubTask:
        deadline = time.time() + max(self.config.hub_task_timeout_seconds, 10)
        last_status = ""
        while time.time() < deadline:
            data = self._ipc_request("get_task", {"task_id": task_id}, timeout_seconds=5)
            if not data.ok:
                raise RuntimeError(str(data.error or "get_task failed"))
            task = HubTask.from_dict(data.payload.get("task"), default_backend=self.config.default_backend)
            if task is None:
                raise RuntimeError("get_task returned invalid task payload")
            if task.status != last_status:
                last_status = task.status
                if on_status_change is not None and task.status in {"running"}:
                    on_status_change(task)
            if task.status in {"succeeded", "failed", "canceled"}:
                return task
            time.sleep(2)
        raise TimeoutError(f"task timed out: {task_id}")

    def _notify_task_progress(
        self,
        base_url: str,
        token: str,
        sender_id: str,
        context_token: Any,
        task: HubTask,
    ) -> None:
        if task.status != "running":
            return
        self._send_text(
            base_url,
            token,
            sender_id,
            context_token,
            self._t(
                "bridge.task.running",
                task_id=task.id,
                session=task.session_name or "default",
                backend=task.backend or self.config.default_backend,
            ),
        )

    def _send_text(self, base_url: str, token: str, to_user_id: str, context_token: Any, text: str) -> None:
        text = (text or "").strip()
        if not text:
            text = "(empty reply)"
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
        self._post_json(f"{base_url}/ilink/bot/sendmessage", body, token=token, timeout_ms=15000)

    def _extract_text(self, msg: dict[str, Any]) -> str:
        parts = []
        for item in msg.get("item_list") or []:
            if item.get("type") == 1:
                text_item = item.get("text_item") or {}
                text = str(text_item.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()

    def _message_key(self, msg: dict[str, Any], text: str) -> str:
        payload = {
            "id": msg.get("msg_id") or msg.get("message_id") or msg.get("client_id") or "",
            "context_token": msg.get("context_token") or "",
            "sender_id": msg.get("from_user_id") or "",
            "create_time": msg.get("create_time") or msg.get("create_timestamp") or "",
            "text": text,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha1(encoded).hexdigest()

    def _is_duplicate_message(self, message_key: str) -> bool:
        if message_key in self._recent_message_keys:
            return True
        self._recent_message_keys.append(message_key)
        if len(self._recent_message_keys) > 200:
            self._recent_message_keys = self._recent_message_keys[-200:]
        return False

    def _handle_control_command(self, sender_id: str, text: str) -> tuple[str, bool]:
        raw = self._normalize_command_text(text)
        if not raw.startswith("/"):
            return "", False

        binding = self._ensure_conversation(sender_id)
        current_session, current_meta = binding.get_current_session(
            default_backend=self.config.default_backend,
            now=now_iso(),
            normalize_backend=normalize_backend,
        )
        sessions = binding.sessions

        parts = raw.split(maxsplit=2)
        command = parts[0].lower()

        if command in {"/help", "/h", "/?"}:
            help_lines = [
                self._t("bridge.help.title"),
                self._t("bridge.help.help"),
                self._t("bridge.help.status"),
                self._t("bridge.help.context"),
                self._t("bridge.help.new"),
                self._t("bridge.help.list"),
                self._t("bridge.help.sessions.page"),
                self._t("bridge.help.sessions.search"),
                self._t("bridge.help.sessions.delete"),
                self._t("bridge.help.sessions.clear_empty"),
                self._t("bridge.help.preview"),
                self._t("bridge.help.history"),
                self._t("bridge.help.export"),
                self._t("bridge.help.use"),
                self._t("bridge.help.rename"),
                self._t("bridge.help.delete"),
                self._t("bridge.help.cancel"),
                self._t("bridge.help.retry"),
                self._t("bridge.help.task"),
                self._t("bridge.help.last"),
                self._t("bridge.help.agent.current"),
                self._t("bridge.help.agent.list"),
                self._t("bridge.help.agent.commands"),
                self._t("bridge.help.agent.switch"),
                self._t("bridge.help.notify.current"),
                self._t("bridge.help.notify.switch"),
                self._t("bridge.help.backend.current"),
                self._t("bridge.help.backend.switch"),
                self._t("bridge.help.model"),
                self._t("bridge.help.model.switch"),
                self._t("bridge.help.model.reset"),
                self._t("bridge.help.project"),
                self._t("bridge.help.project.list"),
                self._t("bridge.help.project.switch"),
                self._t("bridge.help.project.reset"),
                self._t("bridge.help.close"),
                self._t("bridge.help.reset"),
                "",
                self._t("bridge.help.normal"),
                self._t("bridge.help.normal.detail"),
                self._t("bridge.help.escape"),
            ]
            return "\n".join(help_lines), True

        if command == "/new":
            requested = parts[1].strip() if len(parts) >= 2 else ""
            session_name = self._allocate_session_name(binding, requested or "session")
            sessions[session_name] = self._new_session_meta(current_meta.backend)
            binding.current_session = session_name
            self._save_conversations()
            return self._t("bridge.session.created", session=session_name, backend=sessions[session_name].backend), True

        if command == "/context":
            return self._render_context(current_session, current_meta), True

        if command == "/list":
            return self._render_session_list(sender_id, binding), True

        if command == "/sessions":
            if len(parts) < 2:
                return self._render_session_list(sender_id, binding), True
            subcommand = parts[1].strip()
            lowered_subcommand = subcommand.lower()
            if lowered_subcommand in {"search", "find"}:
                keyword = parts[2].strip() if len(parts) >= 3 else ""
                return self._render_session_list(sender_id, binding, query=keyword), True
            if lowered_subcommand in {"delete", "remove"}:
                raw_names = parts[2].strip() if len(parts) >= 3 else ""
                return self._bulk_delete_sessions(binding, raw_names)
            if lowered_subcommand == "clear-empty":
                return self._clear_empty_sessions(sender_id, binding)
            try:
                page = int(subcommand)
            except ValueError:
                return self._t("bridge.sessions.usage"), True
            return self._render_session_list(sender_id, binding, page=page), True

        if command == "/preview":
            session_name = parts[1].strip() if len(parts) >= 2 else binding.current_session
            if not session_name:
                return self._t("bridge.session.preview.usage"), True
            if session_name not in sessions:
                return self._t("bridge.session.preview.not_found", session=session_name), True
            return self._render_session_preview(sender_id, session_name, binding), True

        if command == "/history":
            session_name = parts[1].strip() if len(parts) >= 2 else binding.current_session
            if not session_name:
                return self._t("bridge.session.history.usage"), True
            if session_name not in sessions:
                return self._t("bridge.session.preview.not_found", session=session_name), True
            return self._render_session_history(sender_id, session_name, binding), True

        if command == "/export":
            session_name = parts[1].strip() if len(parts) >= 2 else binding.current_session
            if not session_name:
                return self._t("bridge.session.export.usage"), True
            if session_name not in sessions:
                return self._t("bridge.session.preview.not_found", session=session_name), True
            return self._export_session_history(sender_id, session_name, binding)

        if command == "/task":
            if len(parts) < 2:
                return self._t("bridge.task.lookup.usage"), True
            task_id = parts[1].strip()
            if not task_id:
                return self._t("bridge.task.lookup.usage"), True
            lookup = self._ipc_request("get_task", {"task_id": task_id}, timeout_seconds=5)
            if not lookup.ok:
                return self._t("bridge.task.lookup.not_found", task_id=task_id), True
            task = HubTask.from_dict(lookup.payload.get("task"), default_backend=self.config.default_backend)
            if task is None:
                return self._t("bridge.task.lookup.not_found", task_id=task_id), True
            return self._render_task_summary(task), True

        if command == "/last":
            latest_task = self._find_latest_sender_task(sender_id)
            if latest_task is None:
                return self._t("bridge.task.lookup.none"), True
            return self._render_task_summary(latest_task), True

        if command == "/cancel":
            target_task = self._resolve_sender_task_for_command(
                sender_id,
                parts[1].strip() if len(parts) >= 2 else "",
                allowed_statuses={"queued", "running"},
            )
            if target_task is None:
                return self._t("bridge.task.cancel.none"), True
            response = self._ipc_request("cancel_task", {"task_id": target_task.id}, timeout_seconds=5)
            if not response.ok:
                return self._t("bridge.task.cancel.failed", task_id=target_task.id, error=str(response.error or "unknown error")), True
            canceled_task = HubTask.from_dict(response.payload.get("task"), default_backend=self.config.default_backend)
            if canceled_task is None:
                return self._t("bridge.task.cancel.failed", task_id=target_task.id, error="invalid task payload"), True
            return self._t("bridge.task.cancel.ok", task_id=canceled_task.id, session=canceled_task.session_name or "default"), True

        if command == "/retry":
            target_task = self._resolve_sender_task_for_command(sender_id, parts[1].strip() if len(parts) >= 2 else "")
            if target_task is None:
                return self._t("bridge.task.retry.none"), True
            response = self._ipc_request(
                "retry_task",
                {"task_id": target_task.id, "source": "wechat", "sender_id": sender_id},
                timeout_seconds=5,
            )
            if not response.ok:
                return self._t("bridge.task.retry.failed", task_id=target_task.id, error=str(response.error or "unknown error")), True
            retried_task = HubTask.from_dict(response.payload.get("task"), default_backend=self.config.default_backend)
            if retried_task is None:
                return self._t("bridge.task.retry.failed", task_id=target_task.id, error="invalid task payload"), True
            return self._t(
                "bridge.task.retry.ok",
                original=target_task.id,
                task_id=retried_task.id,
                session=retried_task.session_name or "default",
                backend=retried_task.backend or self.config.default_backend,
            ), True

        if command == "/use":
            if len(parts) < 2:
                return self._t("bridge.use.usage"), True
            session_name = parts[1].strip()
            if session_name not in sessions:
                return self._t("bridge.session.not_found", session=session_name), True
            binding.current_session = session_name
            self._save_conversations()
            backend = sessions[session_name].backend
            return self._t("bridge.session.switched", session=session_name, backend=backend), True

        if command == "/rename":
            source_session = current_session
            if len(parts) < 2:
                return self._t("bridge.rename.usage"), True
            requested_name = parts[1].strip()
            if len(parts) >= 3:
                source_session = requested_name
                requested_name = parts[2].strip()
            if not source_session or not requested_name:
                return self._t("bridge.rename.usage"), True
            if source_session not in sessions:
                return self._t("bridge.session.not_found", session=source_session), True
            if source_session == "default":
                return self._t("bridge.session.default_rename_blocked"), True
            target_session = self._sanitize_session_name(requested_name, fallback=source_session)
            if target_session != source_session and target_session in sessions:
                return self._t("bridge.session.rename.exists", session=target_session), True
            if target_session == source_session:
                return self._t("bridge.session.renamed", old=source_session, new=target_session, backend=sessions[target_session].backend), True
            session_meta = sessions.pop(source_session)
            session_meta.touch(now_iso())
            sessions[target_session] = session_meta
            if binding.current_session == source_session:
                binding.current_session = target_session
            self._save_conversations()
            return self._t("bridge.session.renamed", old=source_session, new=target_session, backend=session_meta.backend), True

        if command in {"/delete", "/remove"}:
            if len(parts) < 2:
                return self._t("bridge.delete.usage"), True
            target_session = parts[1].strip()
            if not target_session:
                return self._t("bridge.delete.usage"), True
            if target_session not in sessions:
                return self._t("bridge.session.not_found", session=target_session), True
            if target_session == "default":
                return self._t("bridge.session.default_delete_blocked"), True
            sessions.pop(target_session, None)
            if binding.current_session == target_session:
                binding.current_session = "default"
                sessions.setdefault("default", self._new_session_meta())
            self._save_conversations()
            return self._t("bridge.session.deleted", session=target_session, current=binding.current_session or "default"), True

        if command == "/backend":
            if len(parts) < 2:
                backend = current_meta.backend
                return self._t("bridge.backend.current", session=current_session, backend=backend), True
            requested_backend = parts[1].strip().lower()
            if requested_backend not in SUPPORTED_BACKENDS:
                return self._t("bridge.backend.usage"), True
            current_meta.touch(now_iso(), backend=requested_backend)
            sessions[current_session] = current_meta
            self._save_conversations()
            return self._t("bridge.backend.switched", backend=requested_backend, session=current_session), True

        if command == "/model":
            if len(parts) < 2:
                return self._render_model_status(current_session, current_meta), True
            model_arg = parts[1].strip()
            if not model_arg:
                return self._render_model_status(current_session, current_meta), True
            if model_arg.lower() == "reset":
                current_meta.touch(now_iso(), model="")
                sessions[current_session] = current_meta
                self._save_conversations()
                return self._t(
                    "bridge.model.reset",
                    session=current_session,
                    model=self._resolve_session_model(current_meta),
                ), True
            current_meta.touch(now_iso(), model=model_arg)
            sessions[current_session] = current_meta
            self._save_conversations()
            return self._t(
                "bridge.model.switched",
                session=current_session,
                model=self._resolve_session_model(current_meta),
            ), True

        if command == "/project":
            if len(parts) < 2:
                return self._render_project_status(current_session, current_meta), True
            project_arg = parts[1].strip()
            lowered_project_arg = project_arg.lower()
            if lowered_project_arg == "list":
                return self._render_project_list(current_meta), True
            if lowered_project_arg == "reset":
                current_meta.touch(now_iso(), workdir="")
                sessions[current_session] = current_meta
                self._save_conversations()
                return self._t(
                    "bridge.project.reset",
                    session=current_session,
                    workdir=self._resolve_session_workdir(current_meta),
                ), True
            resolved_workdir = self._resolve_project_workdir(project_arg)
            if resolved_workdir is None:
                return self._t("bridge.project.not_found", project=project_arg), True
            current_meta.touch(now_iso(), workdir=resolved_workdir)
            sessions[current_session] = current_meta
            self._save_conversations()
            return self._t(
                "bridge.project.switched",
                session=current_session,
                workdir=resolved_workdir,
            ), True

        if command == "/agent":
            if len(parts) < 2:
                return self._render_agent_details(self.config.backend_id), True
            subcommand = parts[1].strip().lower()
            if subcommand == "list":
                return self._render_agent_list(), True
            if subcommand in {"help", "commands"}:
                return self._render_agent_command_help(), True
            requested_agent = parts[1].strip()
            known_agents = {agent.id for agent in HubConfig.load().agents}
            if known_agents and requested_agent not in known_agents:
                return self._t("bridge.agent.not_found", agent=requested_agent), True
            self.config.set_backend_agent(requested_agent)
            self.config.save()
            return self._t("bridge.agent.switched", agent=requested_agent), True

        if command == "/notify":
            if len(parts) < 2:
                return self._t(
                    "bridge.notify.current",
                    service=self._t("bridge.notify.on") if self.config.service_notice_enabled else self._t("bridge.notify.off"),
                    config=self._t("bridge.notify.on") if self.config.config_notice_enabled else self._t("bridge.notify.off"),
                    task=self._t("bridge.notify.on") if self.config.task_notice_enabled else self._t("bridge.notify.off"),
                ), True
            desired = parts[1].strip().lower()
            if desired not in {"on", "off", "service-on", "service-off", "config-on", "config-off", "task-on", "task-off"}:
                return self._t("bridge.notify.usage"), True
            if desired == "on":
                self.config.service_notice_enabled = True
                self.config.config_notice_enabled = True
                self.config.task_notice_enabled = True
            elif desired == "off":
                self.config.service_notice_enabled = False
                self.config.config_notice_enabled = False
                self.config.task_notice_enabled = False
            elif desired == "service-on":
                self.config.service_notice_enabled = True
            elif desired == "service-off":
                self.config.service_notice_enabled = False
            elif desired == "config-on":
                self.config.config_notice_enabled = True
            elif desired == "config-off":
                self.config.config_notice_enabled = False
            elif desired == "task-on":
                self.config.task_notice_enabled = True
            elif desired == "task-off":
                self.config.task_notice_enabled = False
            self.config.save()
            return self._t(
                "bridge.notify.switched",
                service=self._t("bridge.notify.on") if self.config.service_notice_enabled else self._t("bridge.notify.off"),
                config=self._t("bridge.notify.on") if self.config.config_notice_enabled else self._t("bridge.notify.off"),
                task=self._t("bridge.notify.on") if self.config.task_notice_enabled else self._t("bridge.notify.off"),
            ), True

        if command in {"/close", "/end"}:
            if current_session == "default":
                return self._t("bridge.session.default_close_blocked"), True
            sessions.pop(current_session, None)
            binding.current_session = "default"
            sessions.setdefault("default", self._new_session_meta())
            self._save_conversations()
            return self._t("bridge.session.closed", session=current_session), True

        if command == "/status":
            return self._render_status(binding, current_session, current_meta.backend), True

        if command == "/reset":
            self.conversations.pop(sender_id, None)
            self._save_conversations()
            reset = self._ensure_conversation(sender_id)
            reset_session, reset_meta = reset.get_current_session(
                default_backend=self.config.default_backend,
                now=now_iso(),
                normalize_backend=normalize_backend,
            )
            return self._t("bridge.session.reset", session=reset_session, backend=reset_meta.backend), True

        return self._t("bridge.command.unknown"), True

    def _find_latest_sender_task(self, sender_id: str, *, allowed_statuses: set[str] | None = None) -> HubTask | None:
        sender_tasks = self._load_sender_tasks(sender_id)
        if allowed_statuses is not None:
            sender_tasks = [task for task in sender_tasks if task.status in allowed_statuses]
        return sender_tasks[0] if sender_tasks else None

    def _find_sender_task_by_id(self, sender_id: str, task_id: str) -> HubTask | None:
        cleaned_id = task_id.strip()
        if not cleaned_id:
            return None
        for task in self._load_sender_tasks(sender_id):
            if task.id == cleaned_id:
                return task
        return None

    def _resolve_sender_task_for_command(
        self,
        sender_id: str,
        task_id: str,
        *,
        allowed_statuses: set[str] | None = None,
    ) -> HubTask | None:
        if task_id.strip():
            explicit_task = self._find_sender_task_by_id(sender_id, task_id)
            if explicit_task is None:
                return None
            return explicit_task
        explicit_task = self._find_sender_task_by_id(sender_id, task_id)
        return self._find_latest_sender_task(sender_id, allowed_statuses=allowed_statuses)

    def _load_sender_tasks(self, sender_id: str) -> list[HubTask]:
        state = self._ipc_request("state", {}, timeout_seconds=5)
        if not state.ok:
            return []
        sender_tasks: list[HubTask] = []
        for raw_task in state.payload.get("tasks") or []:
            task = HubTask.from_dict(raw_task, default_backend=self.config.default_backend)
            if task is None or task.sender_id != sender_id:
                continue
            sender_tasks.append(task)
        return sorted(
            sender_tasks,
            key=lambda item: item.finished_at or item.started_at or item.created_at,
            reverse=True,
        )

    def _render_session_list(self, sender_id: str, binding: WeixinConversationBinding, *, page: int = 1, query: str = "") -> str:
        sender_tasks = self._load_sender_tasks(sender_id)
        tasks_by_session: dict[str, list[HubTask]] = {}
        for task in sender_tasks:
            tasks_by_session.setdefault(task.session_name or "default", []).append(task)

        all_session_names = self._filtered_session_names(binding, tasks_by_session, query=query)
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
    ) -> list[str]:
        ordered = self._ordered_session_names(binding, tasks_by_session)
        cleaned_query = query.strip().lower()
        if not cleaned_query:
            return ordered
        matched: list[str] = []
        for session_name in ordered:
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
            binding.current_session = "default"
            binding.sessions.setdefault("default", self._new_session_meta())
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
        spaces: dict[str, str] = {}
        agent = self._find_agent_config(self.config.backend_id)
        if agent is not None and agent.workdir:
            agent_path = Path(agent.workdir).resolve()
            spaces[agent_path.name or "agent-default"] = str(agent_path)
        workspace_root = APP_DIR / "workspace"
        if workspace_root.exists():
            for project_dir in sorted(item for item in workspace_root.iterdir() if item.is_dir()):
                spaces[project_dir.name] = str(project_dir.resolve())
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

    def _resolve_session_workdir(self, session_meta: WeixinSessionMeta) -> str:
        if session_meta.workdir.strip():
            return session_meta.workdir.strip()
        agent = self._find_agent_config(self.config.backend_id)
        if agent is not None and agent.workdir:
            return agent.workdir
        return str((APP_DIR / "workspace").resolve())

    def _resolve_session_model(self, session_meta: WeixinSessionMeta) -> str:
        if session_meta.model.strip():
            return session_meta.model.strip()
        agent = self._find_agent_config(self.config.backend_id)
        if agent is not None and agent.model.strip():
            return agent.model.strip()
        return "-"

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

    def _extract_passthrough_prompt(self, text: str) -> str | None:
        raw = self._normalize_command_text(text)
        if not raw.startswith("//"):
            return None
        prompt = raw[1:].strip()
        return prompt or "/"

    def _find_agent_config(self, agent_id: str):
        return next((agent for agent in HubConfig.load().agents if agent.id == agent_id), None)

    def _render_status(self, binding: WeixinConversationBinding, current_session: str, backend: str) -> str:
        agent = self._find_agent_config(self.config.backend_id)
        workdir = agent.workdir if agent is not None else "-"
        model = agent.model.strip() if agent is not None and agent.model.strip() else "-"
        agent_backend = agent.backend if agent is not None else "-"
        current_meta = binding.sessions.get(current_session) or self._new_session_meta()
        return self._t(
            "bridge.status",
            agent=self.config.backend_id,
            agent_backend=agent_backend,
            model=model,
            workdir=workdir,
            session=current_session,
            backend=backend,
            current_model=self._resolve_session_model(current_meta),
            project_workdir=self._resolve_session_workdir(current_meta),
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
        status = task.status
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

    @staticmethod
    def _normalize_command_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(text or ""))
        normalized = normalized.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        if not lines:
            return ""
        return lines[0]

    def _ensure_conversation(self, sender_id: str) -> WeixinConversationBinding:
        existing = self.conversations.get(sender_id)
        if existing:
            return existing

        created = WeixinConversationBinding.create(
            default_backend=normalize_backend(self.config.default_backend),
            now=now_iso(),
        )
        self.conversations[sender_id] = created
        self._save_conversations()
        return created

    def _new_session_meta(self, backend: Any = "") -> WeixinSessionMeta:
        return WeixinSessionMeta(
            backend=normalize_backend(str(backend or self.config.default_backend)),
            created_at=now_iso(),
            updated_at=now_iso(),
        )

    def _allocate_session_name(self, binding: WeixinConversationBinding, requested: str) -> str:
        sessions = binding.sessions
        base = self._sanitize_session_name(requested, fallback="session")
        if base not in sessions:
            return base
        index = 2
        while f"{base}-{index}" in sessions:
            index += 1
        return f"{base}-{index}"

    def _sanitize_session_name(self, requested: str, *, fallback: str) -> str:
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in requested).strip("-_") or fallback

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

    def _ipc_request(self, action: str, payload: dict[str, Any], timeout_seconds: float) -> IpcResponseEnvelope:
        request_id = create_request(action, payload)
        return wait_for_response(request_id, timeout_seconds)

    def _save_state(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.state.sync_files(
            managed_conversations=len(self.conversations),
            account_file=str(self.account_path),
            sync_file=str(self.sync_path),
        )
        save_json(STATE_PATH, self.state.to_dict())

    def _load_conversations(self) -> dict[str, Any]:
        data = load_json(CONVERSATION_PATH, {}, expect_type=dict)
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
            CONVERSATION_PATH,
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
