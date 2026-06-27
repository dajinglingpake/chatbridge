from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.bridge_runtime import IncomingBridgeMessage

@dataclass
class BridgeInterruptChain:
    sender_id: str
    base_prompt: str
    messages: list[str] = field(default_factory=list)
    reply_target: dict[str, Any] = field(default_factory=dict)
    session: Any = None
    passthrough: bool = False
    source: str = ""
    session_name: str = ""
    message_type: str = ""
    context_token: str = ""
    timer: threading.Timer | None = None

class BridgeInterruptRuntime:
    def __init__(
        self,
        *,
        find_active_task: Callable[[str], Any | None],
        cancel_task: Callable[[str], None],
        submit_delayed: Callable[[IncomingBridgeMessage, Any, str, bool], None],
        send_reply: Callable[[dict[str, Any], str], None],
        save_pending_tasks: Callable[[], None],
        translate: Callable[..., str] | None = None,
        delay_seconds: float = 5.0,
        notice_text: str = "",
    ) -> None:
        self.find_active_task = find_active_task
        self.cancel_task = cancel_task
        self.submit_delayed = submit_delayed
        self.send_reply = send_reply
        self.save_pending_tasks = save_pending_tasks
        self.translate = translate
        self.delay_seconds = max(0.1, float(delay_seconds))
        self.notice_text = notice_text
        self._lock = threading.RLock()
        self._chains: dict[str, BridgeInterruptChain] = {}

    def intercept(
        self,
        message: IncomingBridgeMessage,
        session: Any,
        prompt: str,
        *,
        passthrough: bool = False,
    ) -> bool:
        cleaned_prompt = str(prompt or "").strip()
        if not cleaned_prompt:
            return False

        with self._lock:
            existing_chain = self._chains.get(message.sender_id)
            if existing_chain is None:
                active_task = self.find_active_task(message.sender_id)
                if active_task is None:
                    return False
                task_id = str(getattr(active_task, "task_id", "") or "").strip()
                if not task_id:
                    return False
                self.cancel_task(task_id)
                base_prompt = str(getattr(active_task, "interrupt_base_prompt", "") or "").strip()
                if not base_prompt:
                    base_prompt = str(getattr(active_task, "last_submitted_prompt", "") or "").strip()
                if not base_prompt:
                    base_prompt = self._task_prompt(active_task)
                messages = list(getattr(active_task, "interrupt_messages", []) or [])
                chain = BridgeInterruptChain(
                    sender_id=message.sender_id,
                    base_prompt=base_prompt or cleaned_prompt,
                    messages=messages,
                    reply_target=dict(message.reply_target),
                    session=session,
                    passthrough=passthrough,
                    source=message.source,
                    session_name=self._session_name(message, session),
                    message_type=message.message_type,
                    context_token=message.context_token,
                )
                self._chains[message.sender_id] = chain
                notice_text = self._notice_text()
                if notice_text:
                    self.send_reply(message.reply_target, notice_text)
            else:
                active_task = None
                chain = existing_chain
                chain.reply_target = dict(message.reply_target)
                chain.session = session
                chain.passthrough = passthrough
                chain.source = message.source or chain.source
                chain.session_name = self._session_name(message, session) or chain.session_name
                chain.message_type = message.message_type or chain.message_type
                chain.context_token = message.context_token or chain.context_token
                if chain.timer is not None:
                    chain.timer.cancel()

            chain.messages.append(cleaned_prompt)
            if active_task is not None:
                self._store_chain(active_task, chain)
            self._schedule(chain)
            return True

    def _schedule(self, chain: BridgeInterruptChain) -> None:
        timer = threading.Timer(self.delay_seconds, self._flush, args=(chain.sender_id,))
        timer.daemon = True
        chain.timer = timer
        timer.start()

    def _flush(self, sender_id: str) -> None:
        with self._lock:
            chain = self._chains.pop(sender_id, None)
        if chain is None:
            return
        prompt = render_interrupt_prompt(chain.base_prompt, chain.messages, translate=self.translate)
        message = IncomingBridgeMessage(
            sender_id=chain.sender_id,
            text="\n".join(chain.messages),
            reply_target=dict(chain.reply_target),
            source=chain.source,
            session_name=chain.session_name,
            attachments=[],
            attachment_errors=[],
            message_type=chain.message_type,
            context_token=chain.context_token,
            metadata={
                "interrupt_base_prompt": chain.base_prompt,
                "interrupt_messages": list(chain.messages),
                "interrupted": True,
            },
        )
        self.submit_delayed(message, chain.session, prompt, chain.passthrough)

    def _store_chain(self, active_task: Any, chain: BridgeInterruptChain) -> None:
        if hasattr(active_task, "interrupt_base_prompt"):
            setattr(active_task, "interrupt_base_prompt", chain.base_prompt)
        if hasattr(active_task, "interrupt_messages"):
            setattr(active_task, "interrupt_messages", list(chain.messages))
        self.save_pending_tasks()

    def _notice_text(self) -> str:
        if self.notice_text:
            return self.notice_text
        return _translate(
            self.translate,
            "bridge.interrupt.notice",
            "已打断当前任务，浮浮酱会等你继续补充 5 秒后按最新内容重新处理。",
        )

    @staticmethod
    def _task_prompt(active_task: Any) -> str:
        raw = getattr(active_task, "prompt", "")
        if raw:
            return str(raw)
        payload = getattr(active_task, "payload", None)
        if isinstance(payload, dict):
            return str(payload.get("prompt") or "")
        return ""

    @staticmethod
    def _session_name(message: IncomingBridgeMessage, session: Any) -> str:
        if isinstance(session, dict):
            raw = session.get("session_name")
        else:
            raw = getattr(session, "session_name", "")
        return str(raw or message.session_name or "").strip()

def render_interrupt_prompt(base_prompt: str, messages: list[str], *, translate: Callable[..., str] | None = None) -> str:
    cleaned_base = str(base_prompt or "").strip()
    cleaned_messages = [str(item or "").strip() for item in messages if str(item or "").strip()]
    lines = ["<current_user_followups>"]
    lines.extend(f"- [{index}] {message}" for index, message in enumerate(reversed(cleaned_messages), start=1))
    lines.extend([
        "</current_user_followups>",
        "",
        "<previous_user_request_context>",
        cleaned_base or "-",
        "</previous_user_request_context>",
        "",
        _translate(translate, "bridge.interrupt.prompt.instruction", "请以用户补充作为当前主要目标，参考上一轮问题重新处理。"),
        _translate(translate, "bridge.interrupt.prompt.latest_priority", "用户补充是最新指令，优先级高于上一轮问题。"),
        _translate(translate, "bridge.interrupt.prompt.newer_first", "补充内容已按时间倒序排列；越靠前越新、优先级越高。"),
        _translate(
            translate,
            "bridge.interrupt.prompt.no_noop",
            "任何补充都必须视为用户有意输入；不要自行判断补充没有改变目标或可以忽略。",
        ),
        _translate(
            translate,
            "bridge.interrupt.prompt.changed_goal",
            "如果补充改变了方向、提出新的检查/修复要求，必须以补充后的目标为准；不要只完成上一轮问题。",
        ),
        _translate(
            translate,
            "bridge.interrupt.prompt.ambiguous_followup",
            "如果补充含义不明确，必须先回应或澄清补充内容；不要静默继续上一轮任务。",
        ),
        _translate(translate, "bridge.interrupt.prompt.cover_all", "最终回答必须覆盖所有补充内容。"),
        _translate(translate, "bridge.interrupt.prompt.no_interruption_explanation", "不要解释你被打断，直接给出结果。"),
    ])
    return "\n".join(lines).strip()

def _translate(translate: Callable[..., str] | None, key: str, fallback: str) -> str:
    if translate is None:
        return fallback
    value = str(translate(key) or "").strip()
    return fallback if value == key or not value else value
