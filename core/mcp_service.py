from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from agent_backends.shared import resolve_session_file
from agent_backends import supported_backend_keys
from agent_hub import HubConfig
from bridge_config import APP_DIR, BridgeConfig, normalize_backend
from core.app_service import schedule_named_action, submit_hub_task
from core.bridge_service_control_runtime import MCP_RESTART_SCOPES, resolve_restart_action
from core.context_relations import build_context_relation_lines
from core.dashboard import load_dashboard_state
from core.json_store import load_json
from core.runtime_paths import ONEBOT_RUNTIME_DIR, PROJECT_SPACES_PATH, RUNTIME_DIR, WORKSPACE_DIR
from core.state_models import HubTask, JsonObject, WeixinConversationBinding, WeixinSessionMeta
from runtime_stack import get_runtime_snapshot
from weixin_hub_bridge import DEFAULT_WEIXIN_BASE_URL, EVENT_LOG_PATH, WeixinBridge


def _state_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _stamp_for_export() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _summarize_text(value: str, *, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    return text[: limit - 1] + "..." if len(text) > limit else text

QQ_NAPCAT_LOG_DIR = ONEBOT_RUNTIME_DIR / "napcat-shell" / "logs"
QQ_OB11_LOG_MARKER = "转化为 OB11Message"
QQ_HISTORY_MAX_LIMIT = 100
QQ_HISTORY_DEFAULT_LIMIT = 20
_CQ_CODE_PATTERN = re.compile(r"\[CQ:[^\]]+\]")


def _prepare_media_delivery_copy(file_path: Path) -> Path:
    exports_dir = RUNTIME_DIR / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    stamped_name = f"{file_path.stem}-{_stamp_for_export()}{file_path.suffix}"
    target_path = exports_dir / stamped_name
    shutil.copyfile(file_path, target_path)
    return target_path


def _select_latest_display_task(session_tasks: list[HubTask]) -> HubTask | None:
    terminal_statuses = {"succeeded", "failed", "canceled"}
    terminal_tasks = [task for task in session_tasks if str(getattr(task, "status", "") or "").strip() in terminal_statuses]
    return max(terminal_tasks or session_tasks, key=lambda item: item.created_at, default=None)


def _build_latest_round_summary(session_tasks: list[HubTask]) -> str:
    latest_task = _select_latest_display_task(session_tasks)
    if latest_task is None:
        return "暂无历史"
    if latest_task.error.strip():
        error_text = _summarize_text(latest_task.error, limit=72) or "（空错误）"
        return f"报错：{error_text}"
    if latest_task.output.strip():
        output_text = _summarize_text(latest_task.output, limit=72) or "（空回复）"
        return f"结果：{output_text}"
    if latest_task.status == "running":
        prompt_text = _summarize_text(latest_task.prompt, limit=48) or "（空输入）"
        return f"处理中：{prompt_text}"
    if latest_task.status == "queued":
        prompt_text = _summarize_text(latest_task.prompt, limit=48) or "（空输入）"
        return f"排队中：{prompt_text}"
    prompt_text = _summarize_text(latest_task.prompt, limit=48) or "（空输入）"
    return f"请求：{prompt_text}"


def _build_latest_sender_reply_summary(sender_tasks: list[HubTask]) -> str:
    visible_tasks = [task for task in sender_tasks if str(getattr(task, "session_name", "") or "").strip()]
    if not visible_tasks:
        return "暂无历史"
    terminal_statuses = {"succeeded", "failed", "canceled"}
    completed_tasks = [task for task in visible_tasks if str(getattr(task, "status", "") or "").strip() in terminal_statuses]
    latest_task = max(completed_tasks or visible_tasks, key=lambda item: item.created_at)
    if latest_task.error.strip():
        error_text = _summarize_text(latest_task.error, limit=88) or "（空错误）"
        return f"最近报错：{error_text}"
    if latest_task.output.strip():
        output = latest_task.output.strip()
        if output.startswith("你的会话："):
            return "最近回复：已返回会话总览"
        output_text = _summarize_text(latest_task.output, limit=88) or "（空回复）"
        return f"最近回复：{output_text}"
    if latest_task.status == "running":
        prompt_text = _summarize_text(latest_task.prompt, limit=56) or "（空输入）"
        return f"处理中：{prompt_text}"
    if latest_task.status == "queued":
        prompt_text = _summarize_text(latest_task.prompt, limit=56) or "（空输入）"
        return f"排队中：{prompt_text}"
    prompt_text = _summarize_text(latest_task.prompt, limit=56) or "（空输入）"
    return f"最近请求：{prompt_text}"

@dataclass
class ToolActionResult:
    ok: bool
    summary: str
    data: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "data": self.data,
        }

def _clamp_history_limit(limit: int, *, default: int = QQ_HISTORY_DEFAULT_LIMIT) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = default
    return max(1, min(QQ_HISTORY_MAX_LIMIT, value))

def _safe_media_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    posix_name = PurePosixPath(normalized).name
    windows_name = PureWindowsPath(text).name
    name = windows_name if len(windows_name) < len(posix_name) else posix_name
    return name.strip()

def _strip_cq_codes(value: str) -> str:
    return " ".join(_CQ_CODE_PATTERN.sub(" ", str(value or "")).split())

def _qq_message_text_and_media(message: JsonObject) -> tuple[str, list[JsonObject]]:
    segments = message.get("message")
    text_parts: list[str] = []
    media: list[JsonObject] = []
    if isinstance(segments, list):
        for item in segments:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "").strip().lower()
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            if kind == "text":
                text = str(data.get("text") or "").strip()
                if text:
                    text_parts.append(text)
                continue
            if kind == "at":
                qq = str(data.get("qq") or "").strip()
                if qq and qq != "all":
                    text_parts.append(f"@{qq}")
                elif qq:
                    text_parts.append("@all")
                continue
            if kind in {"image", "file", "video", "record", "audio"}:
                raw_name = data.get("name") or data.get("filename") or data.get("file") or data.get("file_name")
                entry: JsonObject = {"type": kind}
                name = _safe_media_name(raw_name)
                if name:
                    entry["name"] = name
                raw_size = data.get("file_size") or data.get("size")
                if isinstance(raw_size, int):
                    entry["size"] = raw_size
                else:
                    try:
                        if str(raw_size or "").strip():
                            entry["size"] = int(str(raw_size).strip())
                    except ValueError:
                        pass
                media.append(entry)
    text = " ".join(" ".join(text_parts).split())
    if not text:
        text = _strip_cq_codes(str(message.get("raw_message") or ""))
    return text, media

def _sanitize_qq_group_message(message: JsonObject) -> JsonObject | None:
    if str(message.get("message_type") or "").strip().lower() != "group":
        return None
    group_id = str(message.get("group_id") or "").strip()
    if not group_id:
        return None
    sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    text, media = _qq_message_text_and_media(message)
    created_at = ""
    raw_time = message.get("time")
    try:
        if raw_time is not None:
            created_at = datetime.fromtimestamp(int(raw_time)).isoformat(timespec="seconds")
    except (OSError, OverflowError, TypeError, ValueError):
        created_at = ""
    payload: JsonObject = {
        "time": int(raw_time) if isinstance(raw_time, int) else raw_time,
        "created_at": created_at,
        "group_id": group_id,
        "group_name": str(message.get("group_name") or "").strip(),
        "user_id": str(message.get("user_id") or sender.get("user_id") or "").strip(),
        "display_name": str(sender.get("card") or sender.get("nickname") or "").strip(),
        "text": text,
    }
    if media:
        payload["media"] = media
    return payload

def _extract_qq_ob11_payload(line: str) -> JsonObject | None:
    marker_index = line.find(QQ_OB11_LOG_MARKER)
    if marker_index < 0:
        return None
    json_index = line.find("{", marker_index)
    if json_index < 0:
        return None
    try:
        parsed = json.loads(line[json_index:])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    candidate = parsed.get("arrayMsg") if isinstance(parsed.get("arrayMsg"), dict) else parsed
    return candidate if isinstance(candidate, dict) else None

def _iter_qq_group_history_messages(log_dir: Path | None = None) -> list[JsonObject]:
    root = log_dir or QQ_NAPCAT_LOG_DIR
    if not root.exists() or not root.is_dir():
        return []
    paths = sorted((item for item in root.glob("*.log") if item.is_file()), key=lambda item: item.stat().st_mtime)
    messages: list[JsonObject] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            payload = _extract_qq_ob11_payload(line)
            if payload is None:
                continue
            if str(payload.get("post_type") or "message").strip().lower() not in {"", "message"}:
                continue
            sanitized = _sanitize_qq_group_message(payload)
            if sanitized is not None:
                messages.append(sanitized)
    messages.sort(key=lambda item: int(item.get("time") or 0))
    return messages

def _qq_history_summary(messages: list[JsonObject], *, prefix: str) -> str:
    if not messages:
        return f"{prefix}：暂无记录。"
    lines = [f"{prefix}：共 {len(messages)} 条。"]
    for item in messages[-10:]:
        speaker = str(item.get("display_name") or item.get("user_id") or "unknown")
        text = _summarize_text(str(item.get("text") or ""), limit=80)
        media = item.get("media") if isinstance(item.get("media"), list) else []
        media_suffix = f" [媒体 {len(media)} 个]" if media else ""
        lines.append(f"- {item.get('created_at') or item.get('time')}: {speaker}: {text or '(无文字)'}{media_suffix}")
    return "\n".join(lines)

def get_qq_current_group_recent_messages(current_group_id: str, limit: int = QQ_HISTORY_DEFAULT_LIMIT) -> ToolActionResult:
    group_id = str(current_group_id or "").strip()
    if not group_id:
        return ToolActionResult(ok=False, summary="当前 QQ 群上下文缺少 group_id")
    count = _clamp_history_limit(limit)
    messages = [item for item in _iter_qq_group_history_messages() if str(item.get("group_id") or "") == group_id][-count:]
    return ToolActionResult(
        ok=True,
        summary=_qq_history_summary(messages, prefix=f"当前 QQ 群 {group_id} 最近消息"),
        data={"group_id": group_id, "limit": count, "messages": messages},
    )

def search_qq_current_group_messages(current_group_id: str, query: str, limit: int = QQ_HISTORY_DEFAULT_LIMIT) -> ToolActionResult:
    group_id = str(current_group_id or "").strip()
    cleaned_query = str(query or "").strip()
    if not group_id:
        return ToolActionResult(ok=False, summary="当前 QQ 群上下文缺少 group_id")
    if not cleaned_query:
        return ToolActionResult(ok=False, summary="query 不能为空")
    count = _clamp_history_limit(limit)
    query_lower = cleaned_query.lower()
    matches = [
        item
        for item in _iter_qq_group_history_messages()
        if str(item.get("group_id") or "") == group_id
        and query_lower in f"{item.get('display_name') or ''} {item.get('user_id') or ''} {item.get('text') or ''}".lower()
    ][-count:]
    return ToolActionResult(
        ok=True,
        summary=_qq_history_summary(matches, prefix=f"当前 QQ 群 {group_id} 搜索 {cleaned_query!r}"),
        data={"group_id": group_id, "query": cleaned_query, "limit": count, "messages": matches},
    )

def get_qq_admin_group_recent_messages(group_id: str, limit: int = QQ_HISTORY_DEFAULT_LIMIT) -> ToolActionResult:
    cleaned_group_id = str(group_id or "").strip()
    if not cleaned_group_id:
        return ToolActionResult(ok=False, summary="group_id 不能为空")
    count = _clamp_history_limit(limit)
    messages = [item for item in _iter_qq_group_history_messages() if str(item.get("group_id") or "") == cleaned_group_id][-count:]
    return ToolActionResult(
        ok=True,
        summary=_qq_history_summary(messages, prefix=f"QQ 群 {cleaned_group_id} 最近消息"),
        data={"group_id": cleaned_group_id, "limit": count, "messages": messages},
    )

def search_qq_admin_group_messages(group_id: str, query: str, limit: int = QQ_HISTORY_DEFAULT_LIMIT) -> ToolActionResult:
    cleaned_group_id = str(group_id or "").strip()
    cleaned_query = str(query or "").strip()
    if not cleaned_group_id:
        return ToolActionResult(ok=False, summary="group_id 不能为空")
    if not cleaned_query:
        return ToolActionResult(ok=False, summary="query 不能为空")
    count = _clamp_history_limit(limit)
    query_lower = cleaned_query.lower()
    matches = [
        item
        for item in _iter_qq_group_history_messages()
        if str(item.get("group_id") or "") == cleaned_group_id
        and query_lower in f"{item.get('display_name') or ''} {item.get('user_id') or ''} {item.get('text') or ''}".lower()
    ][-count:]
    return ToolActionResult(
        ok=True,
        summary=_qq_history_summary(matches, prefix=f"QQ 群 {cleaned_group_id} 搜索 {cleaned_query!r}"),
        data={"group_id": cleaned_group_id, "query": cleaned_query, "limit": count, "messages": matches},
    )

def list_qq_admin_groups() -> ToolActionResult:
    groups: dict[str, JsonObject] = {}
    for item in _iter_qq_group_history_messages():
        group_id = str(item.get("group_id") or "").strip()
        if not group_id:
            continue
        existing = groups.setdefault(
            group_id,
            {
                "group_id": group_id,
                "group_name": str(item.get("group_name") or "").strip(),
                "message_count": 0,
                "latest_time": "",
                "latest_created_at": "",
            },
        )
        existing["message_count"] = int(existing.get("message_count") or 0) + 1
        existing["latest_time"] = item.get("time") or ""
        existing["latest_created_at"] = item.get("created_at") or ""
        if item.get("group_name"):
            existing["group_name"] = item.get("group_name")
    group_list = sorted(groups.values(), key=lambda item: int(item.get("latest_time") or 0), reverse=True)
    return ToolActionResult(
        ok=True,
        summary=f"已返回 {len(group_list)} 个 QQ 群的历史摘要。",
        data={"groups": group_list},
    )


def _resolve_session_model(session_meta: WeixinSessionMeta, agent_model: str) -> str:
    if session_meta.model.strip():
        return session_meta.model.strip()
    return agent_model.strip() or "-"


def _resolve_session_workdir(session_meta: WeixinSessionMeta, agent_workdir: str) -> str:
    if session_meta.workdir.strip():
        return session_meta.workdir.strip()
    return agent_workdir.strip() or str((APP_DIR / "workspace").resolve())


def _project_name_for_workdir(workdir: str) -> str:
    resolved = str(Path(workdir).expanduser().resolve())
    registered = load_json(PROJECT_SPACES_PATH, {}, expect_type=dict)
    payload = registered.get("projects") if isinstance(registered, dict) else {}
    if isinstance(payload, dict):
        for raw_name, raw_path in payload.items():
            candidate = Path(str(raw_path or "").strip()).expanduser()
            if candidate.exists() and candidate.is_dir() and str(candidate.resolve()) == resolved:
                return str(raw_name).strip()
    if WORKSPACE_DIR.exists():
        for project_dir in sorted(item for item in WORKSPACE_DIR.iterdir() if item.is_dir()):
            if str(project_dir.resolve()) == resolved:
                return project_dir.name
    bridge_config = BridgeConfig.load()
    hub_config = HubConfig.load()
    bridge_agent = next((agent for agent in hub_config.agents if agent.id == bridge_config.backend_id), None)
    if bridge_agent is not None and str(Path(bridge_agent.workdir).expanduser().resolve()) == resolved:
        return Path(resolved).name or "agent-default"
    return Path(resolved).name or resolved


def _find_agent_config(agent_id: str):
    cleaned_agent_id = agent_id.strip()
    return next((agent for agent in HubConfig.load().agents if agent.id == cleaned_agent_id), None)


def get_tool_guide() -> ToolActionResult:
    backend_choices = ", ".join(sorted(supported_backend_keys()))
    lines = [
        "内置工具直接作用于当前发送方的当前会话。",
        "只读查询: get_sender_snapshot | list_agents | get_task | get_command_catalog。",
        "目标发送方操作: execute_sender_command(target_sender_id, command)。",
        "服务重启: restart_services(scope='current'|'all'|'weixin'|'bridge'|'qq'|'qq-bridge'|'onebot')，current/all 会按当前运行模式重启对应平台，Hub 重启中断的任务会由桥接层自动续跑。",
        "媒体发送: send_bridge_media(target_sender_id, path)，发送项目内允许的图片或文件。",
        "新 Agent 会话: start_agent_session(agent_id, session_name, prompt, ...)。",
        "Agent 委派: delegate_task(agent_id, prompt, ...)。这不会隐式切换当前发送方的会话。",
        f"当前支持的会话后端: {backend_choices}",
        "推荐流程: 先读取快照，再显式指定目标发送方或 Agent。",
    ]
    lines.extend(
        [
            "",
            *build_context_relation_lines(
                lambda key, **kwargs: _translate_context_key(key, **kwargs),
                agent_id="bridge-agent",
                agent_backend="codex/claude/opencode",
                agent_model="follow agent default unless session overrides it",
                agent_workdir="follow agent default unless session overrides it",
                session_name="current sender session",
                session_backend="session backend",
                session_model="session model",
                session_workdir="session project",
            ),
        ]
    )
    return ToolActionResult(
        ok=True,
        summary="\n".join(lines),
        data={
            "mutating_tools": [
                "execute_sender_command",
                "restart_services",
                "send_bridge_media",
                "start_agent_session",
                "delegate_task",
            ],
        },
    )


def get_command_catalog() -> ToolActionResult:
    backend_choices = "|".join(sorted(supported_backend_keys()))
    commands = [
        {"command": "/help", "category": "help", "description": "查看桥接层帮助"},
        {"command": "/status", "category": "context", "description": "查看当前会话、模型、工程目录和桥 Agent"},
        {"command": "/new <name>", "category": "session", "description": "新建并切换会话"},
        {"command": "/list", "category": "session", "description": "列出会话摘要"},
        {"command": "/preview [name]", "category": "session", "description": "查看当前或指定会话最近摘要"},
        {"command": "/use <name>", "category": "session", "description": "切换到指定会话"},
        {"command": "/rename <new>", "category": "session", "description": "重命名当前会话"},
        {"command": "/delete <name>", "category": "session", "description": "删除指定会话"},
        {"command": "/clear", "category": "session", "description": "清空当前会话绑定的底层 Agent 会话 ID"},
        {"command": "/cancel [task_id]", "category": "task", "description": "取消排队任务"},
        {"command": "/retry [task_id]", "category": "task", "description": "重试最近任务或指定任务"},
        {"command": "/task <id>", "category": "task", "description": "查看任务详情"},
        {"command": "/last", "category": "task", "description": "查看最近任务"},
        {"command": f"/backend <{backend_choices}>", "category": "session", "description": "切换当前会话后端"},
        {"command": "/model <name>", "category": "session", "description": "切换当前会话模型"},
        {"command": "/model reset", "category": "session", "description": "恢复跟随 Agent 默认模型"},
        {"command": "/project <name|path>", "category": "session", "description": "切换当前项目；该项目会成为后续会话的工程目录"},
        {"command": "/project add <name> <path>", "category": "session", "description": "注册项目路径"},
        {"command": "/project remove <name>", "category": "session", "description": "删除已注册项目"},
        {"command": "/project sessions [name]", "category": "session", "description": "列出当前或指定项目下的会话"},
        {"command": "/project reset", "category": "session", "description": "恢复跟随 Agent 默认工程目录"},
        {"command": "/sessions all", "category": "session", "description": "列出全部项目下的会话"},
        {"command": "/showfile <path>", "category": "file", "description": "预览项目内非敏感文本文件内容"},
        {"command": "/sendfile <path>", "category": "file", "description": "发送项目内非敏感图片或文件到当前微信会话"},
        {"command": "/events [count]", "category": "observe", "description": "查看最近异步回执事件"},
        {"command": "/agent", "category": "agent", "description": "查看当前微信桥默认 Agent"},
        {"command": "/agent list", "category": "agent", "description": "查看所有 Agent 摘要"},
        {"command": "/agent help", "category": "agent", "description": "查看当前 Agent CLI 能力说明"},
        {"command": "/agent <name>", "category": "agent", "description": "切换微信桥默认 Agent"},
        {"command": "/notify", "category": "notify", "description": "查看通知状态"},
        {"command": "/reset", "category": "session", "description": "重置发送方回默认会话"},
        {"command": "//status", "category": "passthrough", "description": "把 /status 原样透传给当前 Agent"},
    ]
    return ToolActionResult(
        ok=True,
        summary="已返回 ChatBridge 命令清单。优先使用结构化 MCP 工具，只有在需要完整桥命令覆盖时再调用 execute_sender_command。",
        data={"commands": commands},
    )


def list_agents() -> ToolActionResult:
    dashboard = load_dashboard_state(APP_DIR, page_key="sessions")
    agents = [
        {
            "id": agent.id,
            "name": agent.name,
            "backend": agent.backend,
            "model": agent.model.strip() or "-",
            "workdir": agent.workdir or "-",
            "enabled": agent.enabled,
            "runtime_status": agent.runtime.status,
            "queue_size": agent.runtime.queue_size,
            "success_count": agent.runtime.success_count,
            "failure_count": agent.runtime.failure_count,
        }
        for agent in dashboard.hub_state.agents
    ]
    if not agents:
        agents = [
            {
                "id": agent.id,
                "name": agent.name,
                "backend": agent.backend,
                "model": agent.model.strip() or "-",
                "workdir": agent.workdir or "-",
                "enabled": agent.enabled,
                "runtime_status": "unknown",
                "queue_size": 0,
                "success_count": 0,
                "failure_count": 0,
            }
            for agent in HubConfig.load().agents
        ]
    return ToolActionResult(
        ok=True,
        summary=f"已返回 {len(agents)} 个 Agent 的摘要。",
        data={"agents": agents},
    )


def _build_session_overview_line(
    session_name: str,
    *,
    is_current: bool,
    backend: str,
    latest_status: str,
    latest_summary: str,
) -> str:
    current_marker = " [当前]" if is_current else ""
    summary = latest_summary.strip() or "暂无历史"
    return f"- {session_name}{current_marker} | 后端 {backend} | 最近状态 {_display_task_status(latest_status)} | 该会话最后回复 {summary}"


def _display_task_status(status: str) -> str:
    mapping = {
        "idle": "空闲",
        "queued": "排队中",
        "running": "处理中",
        "succeeded": "已完成",
        "failed": "失败",
        "canceled": "已取消",
    }
    cleaned = str(status or "").strip().lower()
    return mapping.get(cleaned, cleaned or "未知")


def _build_sender_overview_header(
    *,
    current_session: str,
    session_count: int,
    current_project: str,
    focus_sender: bool,
    sender_index: int,
) -> str:
    if focus_sender:
        return f"你的会话：当前项目 {current_project}，当前会话 {current_session}，共 {session_count} 个会话"
    return f"其他会话来源 {sender_index} 的会话：当前项目 {current_project}，当前会话 {current_session}，共 {session_count} 个会话"


def list_senders(*, focus_sender_id: str = "") -> ToolActionResult:
    config = BridgeConfig.load()
    dashboard = load_dashboard_state(APP_DIR, page_key="sessions")
    hub_config = HubConfig.load()
    bridge_agent = next((agent for agent in hub_config.agents if agent.id == config.backend_id), None)
    bridge_agent_model = bridge_agent.model.strip() if bridge_agent is not None else ""
    bridge_agent_workdir = bridge_agent.workdir if bridge_agent is not None else ""
    cleaned_focus_sender_id = focus_sender_id.strip()

    senders: list[JsonObject] = []
    summary_lines: list[str] = []
    for index, (sender_id, binding) in enumerate(sorted(dashboard.bridge_conversations.items()), start=1):
        current_session, current_meta = binding.get_current_session(
            default_backend=config.default_backend,
            now=_state_now(),
            normalize_backend=normalize_backend,
        )
        sender_tasks = [task for task in dashboard.hub_state.tasks if task.sender_id == sender_id]
        sessions: list[JsonObject] = []
        is_focus_sender = sender_id == cleaned_focus_sender_id
        sender_label = "你的会话" if is_focus_sender else f"其他会话来源 {index} 的会话"
        sender_summary_lines = [
            _build_sender_overview_header(
                current_session=current_session,
                session_count=len(binding.sessions),
                current_project=_project_name_for_workdir(_resolve_session_workdir(current_meta, bridge_agent_workdir)),
                focus_sender=is_focus_sender,
                sender_index=index,
            ),
        ]
        for session_name, session_meta in sorted(binding.sessions.items()):
            session_tasks = [task for task in sender_tasks if (task.session_name or "default") == session_name]
            latest_task = _select_latest_display_task(session_tasks)
            latest_summary = _build_latest_round_summary(session_tasks)
            overview_line = _build_session_overview_line(
                session_name,
                is_current=session_name == current_session,
                backend=session_meta.backend,
                latest_status=latest_task.status if latest_task is not None else "idle",
                latest_summary=latest_summary,
            )
            sender_summary_lines.append(overview_line)
            sessions.append(
                {
                    "name": session_name,
                    "is_current": session_name == current_session,
                    "backend": session_meta.backend,
                    "model": _resolve_session_model(session_meta, bridge_agent_model),
                    "workdir": _resolve_session_workdir(session_meta, bridge_agent_workdir),
                    "task_count": len(session_tasks),
                    "latest_task_id": latest_task.id if latest_task is not None else "",
                    "latest_status": latest_task.status if latest_task is not None else "idle",
                    "latest_summary": latest_summary,
                    "overview_line": overview_line,
                }
            )
        summary_lines.extend(sender_summary_lines)
        senders.append(
            {
                "sender_id": sender_id,
                "label": sender_label,
                "current_session": current_session,
                "session_count": len(binding.sessions),
                "sessions": sessions,
                "summary_lines": sender_summary_lines,
                "latest_sender_reply_summary": _build_latest_sender_reply_summary(sender_tasks) if is_focus_sender else "",
            }
        )
    return ToolActionResult(
        ok=True,
        summary="\n".join(summary_lines) if summary_lines else f"已返回 {len(senders)} 个发送方的会话总览。",
        data={
            "conversation_count": len(dashboard.bridge_conversations),
            "senders": senders,
            "summary_lines": summary_lines,
        },
    )


def get_task(task_id: str) -> ToolActionResult:
    cleaned_task_id = task_id.strip()
    if not cleaned_task_id:
        return ToolActionResult(ok=False, summary="task_id 不能为空")
    dashboard = load_dashboard_state(APP_DIR, page_key="sessions")
    task = next((item for item in dashboard.hub_state.tasks if item.id == cleaned_task_id), None)
    if task is None:
        return ToolActionResult(ok=False, summary=f"未找到任务: {cleaned_task_id}")
    task_payload = task.to_dict()
    task_payload["prompt_summary"] = _summarize_text(task.prompt, limit=240)
    task_payload["result_summary"] = _summarize_text(task.output or task.error, limit=360) or "(empty)"
    return ToolActionResult(
        ok=True,
        summary=f"已返回任务 {cleaned_task_id} 的详情。",
        data={"task": task_payload},
    )


def get_sender_snapshot(target_sender_id: str = "") -> ToolActionResult:
    config = BridgeConfig.load()
    dashboard = load_dashboard_state(APP_DIR, page_key="sessions")
    hub_config = HubConfig.load()
    bridge_agent = next((agent for agent in hub_config.agents if agent.id == config.backend_id), None)
    bridge_agent_model = bridge_agent.model.strip() if bridge_agent is not None else ""
    bridge_agent_workdir = bridge_agent.workdir if bridge_agent is not None else ""
    payload: JsonObject = {
        "bridge": {
            "agent_id": config.backend_id,
            "default_backend": config.default_backend,
            "active_account_id": config.active_account_id,
            "bridge_running": dashboard.snapshot.bridge_running,
            "hub_running": dashboard.snapshot.hub_running,
            "conversation_count": len(dashboard.bridge_conversations),
            "agent_model": bridge_agent_model or "-",
            "agent_workdir": bridge_agent_workdir or "-",
        },
        "agents": [
            {
                "id": agent.id,
                "backend": agent.backend,
                "model": agent.model.strip() or "-",
                "runtime_status": agent.runtime.status,
                "queue_size": agent.runtime.queue_size,
            }
            for agent in dashboard.hub_state.agents
        ],
        "task_counts": {
            "total": len(dashboard.hub_state.tasks),
            "queued": len([task for task in dashboard.hub_state.tasks if task.status == "queued"]),
            "running": len([task for task in dashboard.hub_state.tasks if task.status == "running"]),
            "failed": len([task for task in dashboard.hub_state.tasks if task.status == "failed"]),
        },
        "recent_events": _load_recent_bridge_events(limit=5),
    }
    cleaned_sender_id = target_sender_id.strip()
    if cleaned_sender_id:
        binding = dashboard.bridge_conversations.get(cleaned_sender_id)
        if binding is None:
            binding = WeixinConversationBinding.create(default_backend=normalize_backend(config.default_backend), now=_state_now())
        current_session, current_meta = binding.get_current_session(
            default_backend=config.default_backend,
            now=_state_now(),
            normalize_backend=normalize_backend,
        )
        sender_tasks = [task for task in dashboard.hub_state.tasks if task.sender_id == cleaned_sender_id]
        session_summaries: list[dict[str, object]] = []
        summary_lines = [
            _build_sender_overview_header(
                current_session=current_session,
                session_count=len(binding.sessions),
                current_project=_project_name_for_workdir(_resolve_session_workdir(current_meta, bridge_agent_workdir)),
                focus_sender=True,
                sender_index=1,
            ),
        ]
        for session_name, session_meta in sorted(binding.sessions.items()):
            session_tasks = [task for task in sender_tasks if (task.session_name or "default") == session_name]
            latest_task = _select_latest_display_task(session_tasks)
            latest_summary = _build_latest_round_summary(session_tasks)
            overview_line = _build_session_overview_line(
                session_name,
                is_current=session_name == current_session,
                backend=session_meta.backend,
                latest_status=latest_task.status if latest_task is not None else "idle",
                latest_summary=latest_summary,
            )
            summary_lines.append(overview_line)
            session_summaries.append(
                {
                    "name": session_name,
                    "is_current": session_name == current_session,
                    "backend": session_meta.backend,
                    "model": _resolve_session_model(session_meta, bridge_agent_model),
                    "workdir": _resolve_session_workdir(session_meta, bridge_agent_workdir),
                    "task_count": len(session_tasks),
                    "latest_task_id": latest_task.id if latest_task is not None else "",
                    "latest_status": latest_task.status if latest_task is not None else "idle",
                    "latest_summary": latest_summary,
                    "overview_line": overview_line,
                }
            )
        payload["target_sender"] = {
            "sender_id": cleaned_sender_id,
            "current_session": current_session,
            "current_backend": current_meta.backend,
            "current_model": _resolve_session_model(current_meta, bridge_agent_model),
            "current_workdir": _resolve_session_workdir(current_meta, bridge_agent_workdir),
            "session_count": len(binding.sessions),
            "sessions": session_summaries,
            "summary_lines": summary_lines,
            "latest_sender_reply_summary": _build_latest_sender_reply_summary(sender_tasks),
            "relation_lines": build_context_relation_lines(
                lambda key, **kwargs: _translate_context_key(key, **kwargs),
                agent_id=config.backend_id,
                agent_backend=bridge_agent.backend if bridge_agent is not None and bridge_agent.backend else "-",
                agent_model=bridge_agent_model or "-",
                agent_workdir=bridge_agent_workdir or "-",
                session_name=current_session,
                session_backend=current_meta.backend,
                session_model=_resolve_session_model(current_meta, bridge_agent_model),
                session_workdir=_resolve_session_workdir(current_meta, bridge_agent_workdir),
            ),
            "recent_events": _load_recent_bridge_events(limit=5, sender_id=cleaned_sender_id),
        }
        return ToolActionResult(
            ok=True,
            summary="\n".join(summary_lines),
            data=payload,
        )
    return ToolActionResult(ok=True, summary="已返回 ChatBridge 总览快照。", data=payload)


def _load_recent_bridge_events(*, limit: int = 5, sender_id: str = "") -> list[JsonObject]:
    if not EVENT_LOG_PATH.exists():
        return []
    cleaned_sender_id = sender_id.strip()
    entries: list[JsonObject] = []
    for line in reversed(EVENT_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        raw_sender_id = str(parsed.get("sender_id") or "").strip()
        if cleaned_sender_id and raw_sender_id != cleaned_sender_id:
            continue
        entries.append({str(key): value for key, value in parsed.items() if value is not None})
        if len(entries) >= max(limit, 1):
            break
    return entries


def _translate_context_key(key: str, **kwargs: object) -> str:
    templates = {
        "bridge.context.title": "上下文关系:",
        "bridge.context.agent": "Agent {agent}: 默认 backend={backend} | model={model} | project={workdir}",
        "bridge.context.session": "Session {session}: 当前 backend={backend} | model={model} | project={workdir}",
        "bridge.context.rule.agent": "1. Agent 定义默认值和提示词前缀，是整个会话执行链的基础配置。",
        "bridge.context.rule.session": "2. Session 只属于当前发送方；切换 /use 只影响这个发送方，不影响其他发送方。",
        "bridge.context.rule.backend": "3. Session backend 决定这次任务实际走哪个后端；若未单独切换，就沿用该 Session 当前 backend。",
        "bridge.context.rule.model": "4. Session model 为空时跟随当前 Agent 默认 model；设置 /model 后，Session 覆盖优先。",
        "bridge.context.rule.project": "5. Session project 为空时跟随当前 Agent 默认 workdir；设置 /project 后，Session 覆盖优先。",
    }
    return templates.get(key, key).format(**kwargs)


def execute_sender_command(
    target_sender_id: str,
    command: str,
) -> ToolActionResult:
    cleaned_sender_id = target_sender_id.strip()
    cleaned_command = command.strip()
    if not cleaned_sender_id:
        return ToolActionResult(ok=False, summary="target_sender_id 不能为空")
    if not cleaned_command.startswith("/"):
        return ToolActionResult(ok=False, summary="command 必须以 / 开头")
    if _is_qq_sender_id(cleaned_sender_id):
        from qq_onebot_bridge import QQOneBotBridge

        bridge = QQOneBotBridge(BridgeConfig.load())
    else:
        bridge = WeixinBridge(BridgeConfig.load())
    reply, handled = bridge._handle_control_command(cleaned_sender_id, cleaned_command)
    if not handled:
        return ToolActionResult(ok=False, summary=f"桥接层未处理命令: {cleaned_command}")
    return ToolActionResult(
        ok=True,
        summary=f"已对发送方 {cleaned_sender_id} 执行桥命令 {cleaned_command}。",
        data={
            "target_sender_id": cleaned_sender_id,
            "command": cleaned_command,
            "reply": reply,
        },
    )


def _is_qq_sender_id(sender_id: str) -> bool:
    return sender_id.strip().lower().startswith("qq:")

def restart_services(scope: str = "all") -> ToolActionResult:
    cleaned_scope = str(scope or "").strip().lower() or "all"
    action = _resolve_mcp_restart_action(cleaned_scope)
    if action is None:
        return ToolActionResult(ok=False, summary=f"scope 不支持：{cleaned_scope}")
    result = schedule_named_action(action, delay_seconds=1.0)
    return ToolActionResult(
        ok=result.ok,
        summary=result.message,
        data={"scope": cleaned_scope, "action": action},
    )

def _resolve_mcp_restart_action(scope: str) -> str | None:
    cleaned_scope = str(scope or "").strip().lower() or "all"
    if cleaned_scope in {"all", "current"}:
        snapshot = get_runtime_snapshot(include_agent_processes=False)
        if (snapshot.qq_bridge_running or snapshot.onebot_runtime_running) and not snapshot.bridge_running:
            return "restart-qq-stack"
        return "restart"
    return resolve_restart_action(cleaned_scope, default_scope="all", restart_scopes=MCP_RESTART_SCOPES)


def send_bridge_media(target_sender_id: str, path: str) -> ToolActionResult:
    cleaned_sender_id = str(target_sender_id or "").strip()
    cleaned_path = str(path or "").strip()
    if not cleaned_sender_id:
        return ToolActionResult(ok=False, summary="target_sender_id 不能为空")
    if not cleaned_path:
        return ToolActionResult(ok=False, summary="path 不能为空")
    if _is_qq_sender_id(cleaned_sender_id):
        return _send_qq_media(cleaned_sender_id, cleaned_path)
    return _send_weixin_media(cleaned_sender_id, cleaned_path)


def send_current_qq_private_admin_media(
    current_sender_id: str,
    admin_user_id: str,
    path: str,
) -> ToolActionResult:
    cleaned_sender_id = str(current_sender_id or "").strip()
    cleaned_admin_user_id = str(admin_user_id or "").strip()
    cleaned_path = str(path or "").strip()
    current_user_id = _qq_private_user_id_from_sender_id(cleaned_sender_id)

    if not cleaned_path:
        return ToolActionResult(ok=False, summary="path 不能为空")
    if not current_user_id or current_user_id != cleaned_admin_user_id:
        return ToolActionResult(ok=False, summary="媒体发送仅允许当前 QQ 私聊管理员使用")

    config = BridgeConfig.load()
    allowed_user_ids = {str(item or "").strip() for item in config.qq_allowed_private_user_ids}
    if current_user_id not in allowed_user_ids:
        return ToolActionResult(ok=False, summary="当前 QQ 私聊管理员已不在允许名单中")
    return _send_qq_media(cleaned_sender_id, cleaned_path)


def send_weixin_media(target_sender_id: str, path: str) -> ToolActionResult:
    return send_bridge_media(target_sender_id, path)

def _send_weixin_media(cleaned_sender_id: str, cleaned_path: str) -> ToolActionResult:
    bridge = WeixinBridge(BridgeConfig.load())
    try:
        account = bridge._load_account()
        token = str(account.get("token") or "").strip()
        if not token:
            return ToolActionResult(ok=False, summary="微信账号 token 为空，请先登录")
        base_url = str(account.get("baseUrl") or DEFAULT_WEIXIN_BASE_URL).strip()
        original_file_path = bridge._resolve_shareable_project_file(cleaned_path)
        file_path = _prepare_media_delivery_copy(original_file_path)
        context_token = bridge.context_tokens.get(cleaned_sender_id, "")
        response = bridge._send_media_file(base_url, token, cleaned_sender_id, context_token, file_path)
    except Exception as exc:  # noqa: BLE001
        return ToolActionResult(ok=False, summary=f"发送媒体失败：{exc}")
    return ToolActionResult(
        ok=True,
        summary=f"已发送 {file_path.name} 到 {cleaned_sender_id}。",
        data={
            "target_sender_id": cleaned_sender_id,
            "path": str(file_path),
            "source_path": str(original_file_path),
            "file_name": file_path.name,
            "response": response,
        },
    )

def _send_qq_media(cleaned_sender_id: str, cleaned_path: str) -> ToolActionResult:
    from qq_onebot_bridge import QQOneBotBridge

    bridge = QQOneBotBridge(BridgeConfig.load())
    try:
        original_file_path = bridge._resolve_shareable_project_file(cleaned_path)
        file_path = _prepare_media_delivery_copy(original_file_path)
        reply_target = _qq_reply_target_from_sender_id(cleaned_sender_id)
        response = bridge._send_media_to_reply_target(reply_target, file_path)
    except Exception as exc:  # noqa: BLE001
        return ToolActionResult(ok=False, summary=f"发送媒体失败：{exc}")
    return ToolActionResult(
        ok=True,
        summary=f"已发送 {file_path.name} 到 {cleaned_sender_id}。",
        data={
            "target_sender_id": cleaned_sender_id,
            "path": str(file_path),
            "source_path": str(original_file_path),
            "file_name": file_path.name,
            "response": response,
        },
    )

def _qq_reply_target_from_sender_id(sender_id: str) -> JsonObject:
    parts = sender_id.split(":")
    if len(parts) >= 4 and parts[0] == "qq" and parts[1] == "group":
        return {"message_type": "group", "group_id": parts[2], "user_id": parts[3]}
    if len(parts) >= 3 and parts[0] == "qq" and parts[1] == "private":
        return {"message_type": "private", "user_id": parts[2]}
    raise ValueError(f"不支持的 QQ sender_id: {sender_id}")


def _qq_private_user_id_from_sender_id(sender_id: str) -> str:
    parts = str(sender_id or "").strip().split(":")
    if len(parts) >= 3 and parts[0].lower() == "qq" and parts[1].lower() == "private":
        return parts[2].strip()
    return ""


def delegate_task(
    agent_id: str,
    prompt: str,
    *,
    session_name: str = "",
    backend: str = "",
    target_sender_id: str = "",
    workdir: str = "",
    model: str = "",
) -> ToolActionResult:
    result = submit_hub_task(
        agent_id=agent_id,
        prompt=prompt,
        session_name=session_name,
        backend=backend,
        source="mcp-manager",
        sender_id=target_sender_id,
        workdir=workdir,
        model=model,
    )
    return ToolActionResult(
        ok=result.ok,
        summary=result.message,
        data={
            "agent_id": agent_id.strip() or "main",
            "session_name": session_name.strip(),
            "backend": backend.strip(),
            "target_sender_id": target_sender_id.strip(),
            "workdir": workdir.strip(),
            "model": model.strip(),
        },
    )


def start_agent_session(
    agent_id: str,
    session_name: str,
    prompt: str,
    *,
    backend: str = "",
    target_sender_id: str = "",
    workdir: str = "",
    model: str = "",
) -> ToolActionResult:
    cleaned_agent_id = agent_id.strip()
    cleaned_session_name = session_name.strip()
    cleaned_prompt = prompt.strip()
    if not cleaned_agent_id:
        return ToolActionResult(ok=False, summary="agent_id 不能为空")
    if not cleaned_session_name:
        return ToolActionResult(ok=False, summary="session_name 不能为空")
    if not cleaned_prompt:
        return ToolActionResult(ok=False, summary="prompt 不能为空")
    agent = _find_agent_config(cleaned_agent_id)
    if agent is None:
        return ToolActionResult(ok=False, summary=f"未找到 Agent: {cleaned_agent_id}")
    session_file = resolve_session_file(agent, cleaned_session_name, APP_DIR / "sessions")
    if session_file.exists() and session_file.read_text(encoding="utf-8").strip():
        return ToolActionResult(
            ok=False,
            summary=f"Agent 会话已存在，请更换 session_name: {cleaned_session_name}",
            data={"agent_id": cleaned_agent_id, "session_name": cleaned_session_name, "session_file": str(session_file)},
        )
    result = submit_hub_task(
        agent_id=cleaned_agent_id,
        prompt=cleaned_prompt,
        session_name=cleaned_session_name,
        backend=backend,
        source="mcp-manager",
        sender_id=target_sender_id,
        workdir=workdir,
        model=model,
    )
    return ToolActionResult(
        ok=result.ok,
        summary=result.message,
        data={
            "agent_id": cleaned_agent_id,
            "session_name": cleaned_session_name,
            "session_file": str(session_file),
            "backend": backend.strip(),
            "target_sender_id": target_sender_id.strip(),
            "workdir": workdir.strip(),
            "model": model.strip(),
        },
    )
