from __future__ import annotations

from pathlib import Path

from bridge_config import BridgeConfig
from core.accounts import AccountFilePayload, DEFAULT_ILINK_BASE_URL, account_conversation_path, load_account_context_tokens, load_account_file_payload
from core.bridge_notifier import NoticeRecipient, NoticeResult, build_notice_text, disabled_notice_result, notice_enabled
from core.bridge_followup_hint import build_task_followup_hint
from core.json_store import load_json
from core.weixin_text_outbox import enqueue_text_message
from runtime_stack import BRIDGE_CONVERSATIONS_PATH

def broadcast_weixin_notice(title: str, detail: str, config: BridgeConfig | None = None) -> NoticeResult:
    return broadcast_weixin_notice_by_kind("config", title, detail, config=config)


def broadcast_weixin_notice_by_kind(kind: str, title: str, detail: str, config: BridgeConfig | None = None) -> NoticeResult:
    cfg = config or BridgeConfig.load()
    if not notice_enabled(cfg, kind):
        return disabled_notice_result("微信")
    recipients = _load_recipients(Path(cfg.account_file), account_id=cfg.active_account_id)
    if not recipients:
        return NoticeResult(sent_count=0, recipient_count=0, platform_label="微信")
    account = _load_account_payload(Path(cfg.account_file))
    token = account.token
    if not token:
        return NoticeResult(sent_count=0, recipient_count=len(recipients), error="active account token is missing", platform_label="微信")
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
            response = _send_text(
                base_url,
                token,
                recipient.sender_id,
                recipient.context_token,
                message,
                account_id=cfg.active_account_id,
                account_file=cfg.account_file,
            )
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
    return NoticeResult(sent_count=sent_count, recipient_count=len(recipients), error=last_error, platform_label="微信")


def _build_notice_text(title: str, detail: str) -> str:
    return build_notice_text(title, detail)


def _is_real_weixin_sender(sender_id: str) -> bool:
    return str(sender_id or "").strip().endswith("@im.wechat")

def _load_account_payload(account_path: Path) -> AccountFilePayload:
    return load_account_file_payload(account_path)


def _load_recipient_ids() -> list[str]:
    payload = load_json(BRIDGE_CONVERSATIONS_PATH, {}, expect_type=dict)
    if not isinstance(payload, dict):
        return []
    return [cleaned for sender_id in payload.keys() if (cleaned := str(sender_id).strip()) and _is_real_weixin_sender(cleaned)]


def _load_recipients(account_path: Path | None, *, account_id: str = "") -> list[NoticeRecipient]:
    conversation_path = account_conversation_path(BRIDGE_CONVERSATIONS_PATH, account_id, account_path or "")
    payload = load_json(conversation_path, {}, expect_type=dict)
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


def _send_text(
    base_url: str,
    token: str,
    to_user_id: str,
    context_token: str,
    text: str,
    *,
    account_id: str = "",
    account_file: str = "",
) -> dict:
    del base_url, token
    enqueue_text_message(
        to_user_id=to_user_id,
        context_token=context_token,
        text=text[:4000],
        source="notice",
        account_id=account_id,
        account_file=account_file,
    )
    return {"ret": 0, "queued": True}
