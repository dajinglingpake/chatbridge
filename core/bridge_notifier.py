from __future__ import annotations

from dataclasses import dataclass

from bridge_config import BridgeConfig
from core.bridge_message_format import format_bridge_reply


@dataclass
class NoticeResult:
    sent_count: int
    recipient_count: int
    error: str = ""
    platform_label: str = "桥接"

    @property
    def summary(self) -> str:
        if self.error == "disabled":
            return f"{self.platform_label}系统通知已关闭"
        if self.error == "unsupported":
            return f"{self.platform_label}系统通知暂不支持"
        if self.recipient_count <= 0:
            return f"没有可通知的{self.platform_label}会话"
        if self.sent_count == self.recipient_count and not self.error:
            return f"已通知 {self.sent_count} 个{self.platform_label}会话"
        if self.sent_count > 0:
            return f"已通知 {self.sent_count}/{self.recipient_count} 个{self.platform_label}会话，剩余发送失败：{self.error or 'unknown error'}"
        return f"{self.platform_label}通知发送失败：{self.error or 'unknown error'}"


@dataclass(frozen=True)
class NoticeRecipient:
    sender_id: str
    context_token: str = ""


def build_notice_text(title: str, detail: str) -> str:
    body = (detail or "").strip() or "-"
    return format_bridge_reply(f"{title}\n{body}", status="notice")


def notice_enabled(config: BridgeConfig, kind: str) -> bool:
    if kind == "service":
        return bool(config.service_notice_enabled)
    if kind == "task":
        return bool(config.task_notice_enabled)
    return bool(config.config_notice_enabled)


def disabled_notice_result(platform_label: str) -> NoticeResult:
    return NoticeResult(sent_count=0, recipient_count=0, error="disabled", platform_label=platform_label)


def unsupported_notice_result(platform_label: str) -> NoticeResult:
    return NoticeResult(sent_count=0, recipient_count=0, error="unsupported", platform_label=platform_label)


def normalize_notice_channel(value: str) -> str:
    cleaned = str(value or "").strip().lower()
    if cleaned.startswith("qq"):
        return "qq"
    if cleaned.startswith("wechat") or cleaned.startswith("weixin"):
        return "wechat"
    return "wechat"


def broadcast_bridge_notice_by_kind(
    kind: str,
    title: str,
    detail: str,
    *,
    channel: str = "wechat",
    config: BridgeConfig | None = None,
) -> NoticeResult:
    normalized_channel = normalize_notice_channel(channel)
    if normalized_channel == "qq":
        return unsupported_notice_result("QQ")
    from core.weixin_notifier import broadcast_weixin_notice_by_kind

    return broadcast_weixin_notice_by_kind(kind, title, detail, config=config)
