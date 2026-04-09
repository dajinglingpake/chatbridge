from __future__ import annotations

import base64
import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from bridge_config import BridgeConfig
from core.accounts import DEFAULT_ILINK_BASE_URL
from runtime_stack import BRIDGE_CONVERSATIONS_PATH


ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (1 << 8) | 1


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
    recipients = _load_recipient_ids()
    if not recipients:
        return NoticeResult(sent_count=0, recipient_count=0)
    account = _load_account_payload(Path(cfg.account_file))
    token = str(account.get("token") or "").strip()
    if not token:
        return NoticeResult(sent_count=0, recipient_count=len(recipients), error="active account token is missing")
    base_url = str(account.get("baseUrl") or DEFAULT_ILINK_BASE_URL).strip() or DEFAULT_ILINK_BASE_URL
    message = _build_notice_text(title, detail)
    sent_count = 0
    last_error = ""
    for sender_id in recipients:
        try:
            _send_text(base_url, token, sender_id, message)
            sent_count += 1
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
    return NoticeResult(sent_count=sent_count, recipient_count=len(recipients), error=last_error)


def _build_notice_text(title: str, detail: str) -> str:
    body = (detail or "").strip() or "-"
    return f"[ChatBridge 系统通知]\n操作: {title}\n结果: {body}"


def build_task_followup_hint(task_id: str = "", session_name: str = "") -> str:
    lines = ["可继续发送命令查看详情:"]
    if task_id:
        lines.append(f"/task {task_id}")
    lines.append("/last")
    if session_name:
        lines.append(f"当前会话: {session_name}")
    return "\n".join(lines)


def _load_account_payload(account_path: Path) -> dict[str, object]:
    if not account_path.exists():
        return {}
    try:
        return json.loads(account_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_recipient_ids() -> list[str]:
    if not BRIDGE_CONVERSATIONS_PATH.exists():
        return []
    try:
        payload = json.loads(BRIDGE_CONVERSATIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    return [str(sender_id).strip() for sender_id in payload.keys() if str(sender_id).strip()]


def _send_text(base_url: str, token: str, to_user_id: str, text: str) -> None:
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": f"notice-{int(time.time() * 1000)}-{random.randint(1000, 9999)}",
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": text[:4000]}}],
            "context_token": None,
        },
        "base_info": {"channel_version": "2.1.1"},
    }
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": base64.b64encode(str(random.randint(1, 2**32 - 1)).encode("utf-8")).decode("ascii"),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
    }
    request = urllib.request.Request(
        url=f"{base_url}/ilink/bot/sendmessage",
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
