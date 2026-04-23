from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bridge_config import BridgeConfig
from core.accounts import AccountFilePayload, DEFAULT_ILINK_BASE_URL, load_account_context_tokens, load_account_file_payload
from core.json_store import load_json
from core.weixin_text_outbox import enqueue_text_message
from core.weixin_message_format import format_weixin_reply
from runtime_stack import BRIDGE_CONVERSATIONS_PATH


@dataclass
class NoticeResult:
    sent_count: int
    recipient_count: int
    error: str = ""

    @property
    def summary(self) -> str:
        if self.error == "disabled":
            return "微信系统通知已关闭"
        if self.recipient_count <= 0:
            return "没有可通知的微信会话"
        if self.sent_count == self.recipient_count and not self.error:
            return f"已通知 {self.sent_count} 个微信会话"
        if self.sent_count > 0:
            return f"已通知 {self.sent_count}/{self.recipient_count} 个微信会话，剩余发送失败：{self.error or 'unknown error'}"
        return f"微信通知发送失败：{self.error or 'unknown error'}"


@dataclass(frozen=True)
class NoticeRecipient:
    sender_id: str
    context_token: str = ""


def broadcast_weixin_notice(title: str, detail: str, config: BridgeConfig | None = None) -> NoticeResult:
    return broadcast_weixin_notice_by_kind("config", title, detail, config=config)


def broadcast_weixin_notice_by_kind(kind: str, title: str, detail: str, config: BridgeConfig | None = None) -> NoticeResult:
    cfg = config or BridgeConfig.load()
    if kind == "service" and not cfg.service_notice_enabled:
        return NoticeResult(sent_count=0, recipient_count=0, error="disabled")
    if kind == "task" and not cfg.task_notice_enabled:
        return NoticeResult(sent_count=0, recipient_count=0, error="disabled")
    if kind not in {"service", "task"} and not cfg.config_notice_enabled:
        return NoticeResult(sent_count=0, recipient_count=0, error="disabled")
    recipients = _load_recipients(Path(cfg.account_file))
    if not recipients:
        return NoticeResult(sent_count=0, recipient_count=0)
    account = _load_account_payload(Path(cfg.account_file))
    token = account.token
    if not token:
        return NoticeResult(sent_count=0, recipient_count=len(recipients), error="active account token is missing")
    base_url = account.base_url or DEFAULT_ILINK_BASE_URL
    message = _build_notice_text(title, detail)
    sent_count = 0
    skipped_missing_context = 0
    last_error = ""
    for recipient in recipients:
        if not recipient.context_token:
            skipped_missing_context += 1
            print(f"[notifier] skip recipient={recipient.sender_id} reason=missing_context_token", flush=True)
            continue
        try:
            response = _send_text(base_url, token, recipient.sender_id, recipient.context_token, message)
            if isinstance(response, dict) and response.get("ret") not in (None, 0):
                raise RuntimeError(f"sendmessage returned ret={response.get('ret')}: {response}")
            print(
                f"[notifier] sent recipient={recipient.sender_id} ret={response.get('ret')} errcode={response.get('errcode')} errmsg={response.get('errmsg')}",
                flush=True,
            )
            sent_count += 1
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            print(f"[notifier] failed recipient={recipient.sender_id} error={last_error}", flush=True)
    if skipped_missing_context > 0:
        context_error = f"missing context token for {skipped_missing_context} recipient(s)"
        last_error = context_error if not last_error else f"{last_error}; {context_error}"
    return NoticeResult(sent_count=sent_count, recipient_count=len(recipients), error=last_error)


def _build_notice_text(title: str, detail: str) -> str:
    body = (detail or "").strip() or "-"
    return format_weixin_reply(f"{title}\n{body}", status="notice")


def _is_real_weixin_sender(sender_id: str) -> bool:
    return str(sender_id or "").strip().endswith("@im.wechat")


def build_task_followup_hint(
    task_id: str = "",
    session_name: str = "",
    *,
    allow_retry: bool = False,
) -> str:
    lines = ["可继续发送命令查看详情:"]
    if task_id:
        lines.append(f"/task {task_id}")
    if allow_retry and task_id:
        lines.append(f"/retry {task_id}")
    lines.append("/last")
    if session_name:
        lines.append(f"当前会话: {session_name}")
    return "\n".join(lines)


def _load_account_payload(account_path: Path) -> AccountFilePayload:
    return load_account_file_payload(account_path)


def _load_recipient_ids() -> list[str]:
    payload = load_json(BRIDGE_CONVERSATIONS_PATH, {}, expect_type=dict)
    if not isinstance(payload, dict):
        return []
    return [cleaned for sender_id in payload.keys() if (cleaned := str(sender_id).strip()) and _is_real_weixin_sender(cleaned)]


def _load_recipients(account_path: Path | None) -> list[NoticeRecipient]:
    payload = load_json(BRIDGE_CONVERSATIONS_PATH, {}, expect_type=dict)
    if not isinstance(payload, dict):
        return []
    context_tokens = load_account_context_tokens(account_path) if account_path is not None else {}
    recipients: list[NoticeRecipient] = []
    for sender_id in payload.keys():
        cleaned_sender_id = str(sender_id).strip()
        if not cleaned_sender_id or not _is_real_weixin_sender(cleaned_sender_id):
            continue
        recipients.append(
            NoticeRecipient(
                sender_id=cleaned_sender_id,
                context_token=context_tokens.get(cleaned_sender_id, ""),
            )
        )
    return recipients


def _send_text(base_url: str, token: str, to_user_id: str, context_token: str, text: str) -> dict:
    del base_url, token
    enqueue_text_message(
        to_user_id=to_user_id,
        context_token=context_token,
        text=text[:4000],
        source="notice",
    )
    return {"ret": 0, "queued": True}
