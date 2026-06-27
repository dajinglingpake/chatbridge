from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.json_store import load_json, save_json

Attachment = dict[str, str]

@dataclass(frozen=True)
class IncomingBridgeMessage:
    sender_id: str
    text: str
    reply_target: dict[str, Any]
    source: str
    session_name: str
    attachments: list[Attachment]
    attachment_errors: list[str]
    message_type: str = ""
    context_token: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class BridgeCommandResult:
    handled: bool
    reply: str = ""

@dataclass(frozen=True)
class BridgePromptDecision:
    handled: bool = False
    prompt: str = ""
    passthrough: bool = False

@dataclass(frozen=True)
class BridgeSubmittedTask:
    task_id: str
    payload: dict[str, Any]

class PendingMediaContextStore:
    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: int,
        max_items_per_sender: int = 10,
        now_seconds: Callable[[], int] | None = None,
    ) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self.max_items_per_sender = max_items_per_sender
        self._now_seconds = now_seconds or (lambda: int(time.time()))
        self.contexts = self._load()

    def remember(self, sender_id: str, attachments: list[Attachment]) -> None:
        if not sender_id or not attachments:
            return
        current = str(self._now_seconds())
        pending = self.contexts.get(sender_id, [])
        pending.extend({**attachment, "created_at": current} for attachment in attachments)
        self.contexts[sender_id] = pending[-self.max_items_per_sender :]
        self.save()

    def consume(self, sender_id: str) -> list[Attachment]:
        raw_items = self.contexts.pop(sender_id, [])
        current = self._now_seconds()
        items: list[Attachment] = []
        for item in raw_items:
            try:
                created_at = int(item.get("created_at") or 0)
            except (TypeError, ValueError):
                created_at = 0
            if current - created_at <= self.ttl_seconds:
                items.append(item)
        if raw_items:
            self.save()
        return items

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        save_json(self.path, self.contexts)

    def _load(self) -> dict[str, list[Attachment]]:
        payload = load_json(self.path, {}, expect_type=dict)
        if not isinstance(payload, dict):
            return {}
        current = self._now_seconds()
        contexts: dict[str, list[Attachment]] = {}
        for sender_id, raw_items in payload.items():
            if not isinstance(raw_items, list):
                continue
            items: list[Attachment] = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                try:
                    created_at = int(item.get("created_at") or 0)
                except (TypeError, ValueError):
                    created_at = 0
                if created_at and current - created_at > self.ttl_seconds:
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
                contexts[str(sender_id)] = items
        return contexts

def build_prompt_with_media(
    prompt: str,
    attachments: list[Attachment],
    errors: list[str],
) -> str:
    parts: list[str] = []
    cleaned_prompt = str(prompt or "").strip()
    if cleaned_prompt:
        parts.append(cleaned_prompt)
    if attachments:
        lines = ["用户发送了以下附件，已保存到本地："]
        for attachment in attachments:
            label = "图片" if attachment.get("kind") == "image" else "文件"
            lines.append(f"- {label}: {attachment.get('name') or '-'}")
            lines.append(f"  本地路径: {attachment.get('path') or '-'}")
        if not cleaned_prompt:
            lines.append("请根据这些附件继续处理。")
        parts.append("\n".join(lines))
    if errors:
        parts.append("以下附件接收失败：\n" + "\n".join(errors))
    return "\n\n".join(part for part in parts if part).strip()

class BridgeMessageRuntime:
    def __init__(
        self,
        *,
        pending_media: PendingMediaContextStore,
        resolve_session: Callable[[IncomingBridgeMessage], Any],
        prepare_prompt: Callable[[IncomingBridgeMessage, Any], BridgePromptDecision],
        submit_task: Callable[[IncomingBridgeMessage, Any, str, bool], BridgeSubmittedTask],
        remember_pending_task: Callable[[IncomingBridgeMessage, Any, BridgeSubmittedTask], None],
        send_reply: Callable[[dict[str, Any], str], None],
        should_ignore: Callable[[IncomingBridgeMessage], bool] | None = None,
        is_duplicate: Callable[[IncomingBridgeMessage], bool] | None = None,
        on_ignored: Callable[[IncomingBridgeMessage, str], None] | None = None,
        on_media_context: Callable[[IncomingBridgeMessage], None] | None = None,
        on_media_error: Callable[[IncomingBridgeMessage], None] | None = None,
        on_empty_prompt: Callable[[IncomingBridgeMessage, Any], None] | None = None,
        on_before_submit: Callable[[IncomingBridgeMessage, Any, str, bool], None] | None = None,
        on_after_submit: Callable[[IncomingBridgeMessage, Any, BridgeSubmittedTask], None] | None = None,
        interrupt_runtime: Any | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.pending_media = pending_media
        self.resolve_session = resolve_session
        self.prepare_prompt = prepare_prompt
        self.submit_task = submit_task
        self.remember_pending_task = remember_pending_task
        self.send_reply = send_reply
        self.should_ignore = should_ignore or (lambda _message: False)
        self.is_duplicate = is_duplicate or (lambda _message: False)
        self.on_ignored = on_ignored or (lambda _message, _reason: None)
        self.on_media_context = on_media_context or (lambda _message: None)
        self.on_media_error = on_media_error or (lambda _message: None)
        self.on_empty_prompt = on_empty_prompt or (lambda _message, _session: None)
        self.on_before_submit = on_before_submit or (lambda _message, _session, _prompt, _passthrough: None)
        self.on_after_submit = on_after_submit or (lambda _message, _session, _submitted: None)
        self.interrupt_runtime = interrupt_runtime
        self.log = log or (lambda _message: None)

    def handle_message(self, message: IncomingBridgeMessage) -> BridgeSubmittedTask | None:
        text = str(message.text or "").strip()
        self.log(
            f"message sender={message.sender_id} type={message.message_type or '-'} "
            f"text_preview={text[:80]!r} media={len(message.attachments)} media_errors={len(message.attachment_errors)}"
        )
        if not text and not message.attachments and not message.attachment_errors:
            return None
        if self.should_ignore(message):
            self.on_ignored(message, "ignore_prefix")
            return None
        if self.is_duplicate(message):
            self.on_ignored(message, "duplicate")
            return None
        if not text:
            if message.attachments:
                self.pending_media.remember(message.sender_id, message.attachments)
                self.on_media_context(message)
                return None
            if message.attachment_errors:
                self.send_reply(message.reply_target, "附件接收失败：\n" + "\n".join(message.attachment_errors))
                self.on_media_error(message)
            return None

        session = self.resolve_session(message)
        decision = self.prepare_prompt(message, session)
        if decision.handled:
            return None
        prompt = build_prompt_with_media(
            decision.prompt,
            [*self.pending_media.consume(message.sender_id), *message.attachments],
            message.attachment_errors,
        )
        if not prompt:
            self.on_empty_prompt(message, session)
            return None
        if self.interrupt_runtime is not None and self.interrupt_runtime.intercept(message, session, prompt, passthrough=decision.passthrough):
            self.log(f"interrupted active task sender={message.sender_id}")
            return None
        return self.submit_prepared(message, session, prompt, decision.passthrough)

    def submit_prepared(self, message: IncomingBridgeMessage, session: Any, prompt: str, passthrough: bool = False) -> BridgeSubmittedTask | None:
        self.on_before_submit(message, session, prompt, passthrough)
        submitted = self.submit_task(message, session, prompt, passthrough)
        if not submitted.task_id:
            self.send_reply(message.reply_target, "任务提交失败：Hub 没有返回 task id")
            return None
        self.remember_pending_task(message, session, submitted)
        self.on_after_submit(message, session, submitted)
        self.log(f"submitted task_id={submitted.task_id} sender={message.sender_id}")
        return submitted
