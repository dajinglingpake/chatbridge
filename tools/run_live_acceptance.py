from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bridge_config import BridgeConfig
from core.accounts import load_account_context_tokens, load_account_file_payload
from core.json_store import load_json
from core.runtime_paths import BRIDGE_CONVERSATIONS_PATH, BRIDGE_EVENT_LOG_PATH, BRIDGE_STATE_PATH
from core.weixin_notifier import NoticeResult, broadcast_weixin_notice_by_kind
from runtime_stack import get_runtime_snapshot


@dataclass(frozen=True)
class AcceptanceState:
    hub_running: bool
    bridge_running: bool
    active_account_id: str
    account_file: str
    token_ready: bool
    conversation_count: int
    context_token_count: int
    last_error: str
    handled_messages: int
    failed_messages: int


def _load_state_payload(path: Path) -> dict[str, object]:
    payload = load_json(path, {}, expect_type=dict)
    return payload if isinstance(payload, dict) else {}


def _count_conversations() -> int:
    payload = _load_state_payload(BRIDGE_CONVERSATIONS_PATH)
    return len([key for key in payload.keys() if str(key).strip()])


def _load_recent_events(*, sender_id: str = "", limit: int = 5) -> list[dict[str, object]]:
    if not BRIDGE_EVENT_LOG_PATH.exists():
        return []
    cleaned_sender_id = str(sender_id or "").strip()
    lines = BRIDGE_EVENT_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    events: list[dict[str, object]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if cleaned_sender_id and str(payload.get("sender_id") or "").strip() != cleaned_sender_id:
            continue
        events.append(payload)
        if len(events) >= max(limit, 1):
            break
    return events


def _display_event_name(event: str) -> str:
    mapping = {
        "accepted": "已接收",
        "running": "处理中",
        "succeeded": "已完成",
        "failed": "失败",
        "canceled": "已取消",
    }
    return mapping.get(str(event or "").strip().lower(), str(event or "-"))


def _display_event_detail(event: dict[str, object]) -> str:
    kind = str(event.get("event") or "").strip().lower()
    backend = str(event.get("backend") or "-").strip() or "-"
    result_preview = str(event.get("result_preview") or "").strip()
    error = str(event.get("error") or "").strip()
    if kind == "accepted":
        return f"已提交到 {backend}"
    if kind == "running":
        return f"正在由 {backend} 处理"
    if result_preview:
        return result_preview
    if error:
        return error
    return backend


def collect_acceptance_state() -> AcceptanceState:
    snapshot = get_runtime_snapshot()
    config = BridgeConfig.load()
    account_payload = load_account_file_payload(Path(config.account_file))
    context_tokens = load_account_context_tokens(Path(config.account_file))
    bridge_state = _load_state_payload(BRIDGE_STATE_PATH)
    return AcceptanceState(
        hub_running=bool(snapshot.hub_running),
        bridge_running=bool(snapshot.bridge_running),
        active_account_id=config.active_account_id,
        account_file=config.account_file,
        token_ready=bool(account_payload.token),
        conversation_count=_count_conversations(),
        context_token_count=len(context_tokens),
        last_error=str(bridge_state.get("last_error") or "").strip(),
        handled_messages=int(bridge_state.get("handled_messages") or 0),
        failed_messages=int(bridge_state.get("failed_messages") or 0),
    )


def _print_summary(state: AcceptanceState) -> None:
    print("实时验收概览")
    print(f"- Hub 运行中: {'是' if state.hub_running else '否'}")
    print(f"- Bridge 运行中: {'是' if state.bridge_running else '否'}")
    print(f"- 当前账号: {state.active_account_id}")
    print(f"- 账号文件: {state.account_file}")
    print(f"- Token 就绪: {'是' if state.token_ready else '否'}")
    print(f"- 会话来源数: {state.conversation_count}")
    print(f"- Context Token 数: {state.context_token_count}")
    print(f"- 已处理消息数: {state.handled_messages}")
    print(f"- 失败消息数: {state.failed_messages}")
    print(f"- 最近错误: {state.last_error or '(空)'}")


def _print_recent_events(*, sender_id: str = "", limit: int = 5) -> None:
    events = _load_recent_events(sender_id=sender_id, limit=limit)
    print()
    print("最近异步事件")
    if sender_id:
        print(f"- 目标 sender: {sender_id}")
    if not events:
        print("- (空)")
        return
    for item in events:
        print(
            "- {at} | {event} | task={task_id} | session={session} | detail={detail}".format(
                at=str(item.get("at") or "-"),
                event=_display_event_name(str(item.get("event") or "-")),
                task_id=str(item.get("task_id") or "-"),
                session=str(item.get("session_name") or "-"),
                detail=_display_event_detail(item),
            )
        )


def _print_manual_checklist(*, sender_id: str = "") -> None:
    target_hint = f"（建议使用 sender: {sender_id}）" if sender_id else ""
    print()
    print("真机验收步骤")
    print(f"1. 在微信里发送“列出所有会话” {target_hint}")
    print("   期望: 先收到“我先帮你处理这件事”，再收到“还在处理”，最后收到会话总览。")
    print("2. 在微信里发送“切换到 deep-dive 会话”")
    print("   期望: 管理助手完成切换后，再发“/status”时当前会话变成 deep-dive。")
    print("3. 在微信里发送“/events 3”")
    print("   期望: 能看到已接收 / 处理中 / 已完成三段事件，且详情不是裸 backend。")
    print("4. 在微信里发送“/manage off”，再发一条普通业务消息。")
    print("   期望: 普通消息直接进入当前会话，而不是先走管理助手。")
    print("5. 再发送“/manage on”，恢复默认入口。")
    print("   期望: 后续普通消息重新先进入管理助手。")
    print("6. 如果要验证主动通知，运行本脚本时加 --send-notice。")


def _send_notice() -> NoticeResult:
    return broadcast_weixin_notice_by_kind(
        "service",
        "真机联调验收",
        "这是一条来自 ChatBridge 的验收测试通知。",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="真实微信联调验收助手。")
    parser.add_argument("--sender-id", default="", help="只查看指定 sender 的最近异步事件。")
    parser.add_argument("--events", type=int, default=5, help="最近事件条数。默认 5。")
    parser.add_argument("--send-notice", action="store_true", help="主动发送一条测试通知。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state = collect_acceptance_state()
    _print_summary(state)
    _print_recent_events(sender_id=str(args.sender_id).strip(), limit=int(args.events))
    if args.send_notice:
        notice = _send_notice()
        print()
        print("测试通知结果")
        print(f"- sent_count: {notice.sent_count}")
        print(f"- recipient_count: {notice.recipient_count}")
        print(f"- error: {notice.error or '(空)'}")
        print(f"- summary: {notice.summary}")
    _print_manual_checklist(sender_id=str(args.sender_id).strip())
    if not state.hub_running or not state.bridge_running:
        return 1
    if not state.token_ready:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
