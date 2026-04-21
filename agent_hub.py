from __future__ import annotations

import json
import queue
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_backends import DEFAULT_BACKEND_KEY, BackendContext, McpServerConfig, build_backend_registry, supported_backend_keys
from bridge_config import BridgeConfig
from core.json_store import load_json, save_json
from core.manager_agent_runtime import ChatBridgeManagerRuntime
from core.state_models import AgentRuntimeState, HubTask, IpcRequestEnvelope, IpcResponseEnvelope
from core.weixin_notifier import broadcast_weixin_notice_by_kind, build_task_followup_hint
from local_ipc import REQUEST_DIR, ensure_ipc_dirs, mark_processed, read_request, write_response
from core.platform_compat import IS_WINDOWS, creationflags, resolve_command, terminate_process_tree
from runtime_stack import discover_external_agent_processes


APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR / ".runtime"
STATE_DIR = RUNTIME_DIR / "state"
SESSION_DIR = APP_DIR / "sessions"
WORKSPACE_DIR = APP_DIR / "workspace"
CONFIG_PATH = APP_DIR / "config" / "agent_hub.json"
STATE_PATH = STATE_DIR / "agent_hub_state.json"
SUPPORTED_BACKENDS = set(supported_backend_keys())
WECHAT_SOURCE = "wechat"
WECHAT_MANAGER_SOURCE = "wechat-manager"
MANAGER_SESSION_PREFIX = "__manager__"
CHATBRIDGE_MANAGER_MCP_NAME = "chatbridge_manager"
CHATBRIDGE_MANAGER_TOOL_PATH = APP_DIR / "tools" / "chatbridge_mcp_server.py"
PROMPTS_DIR = APP_DIR / "prompts"
CHATBRIDGE_MANAGER_PROMPT_PATH = PROMPTS_DIR / "chatbridge_manager_prompt.txt"
CHATBRIDGE_MANAGER_PROMPT_FALLBACK = """你是 ChatBridge 的管理 Agent。

你的职责是通过 ChatBridge MCP 工具管理当前微信发送方，而不是直接假设结果。

必须遵守：
1. 优先使用结构化 MCP 工具，不要编造会话、任务、Agent、模型或工程目录状态。
2. 只读查询优先用 get_management_snapshot、list_sender_conversations、list_agents、get_task、get_command_catalog。
2.1 ChatBridge 管理能力通过 MCP tools 暴露，不通过 resources 或 resource templates 暴露；不要为了确认工具是否存在而调用 list_mcp_resources 或 list_mcp_resource_templates，直接按工具名调用即可。
3. 你当前运行在 ChatBridge 内置管理 Agent 中。对当前发送方的写操作可以直接执行；只有在用户明确要求演示外部 MCP 控制流程时，才需要 enter_control_mode / exit_control_mode。
4. 默认目标发送方固定为：{sender_id}。除非用户明确要求，否则不要对其他发送方执行写操作。
5. 管理会话与普通业务会话隔离；你做的 /use、/agent、start_agent_session、delegate_task 等操作不会改变你自己的控制平面身份。
6. 回答必须使用简体中文，简洁直接。默认只给用户结果、状态和下一步，不要主动罗列调用过的 MCP 工具。
6.1 只有在用户明确问“你怎么做的/调用了什么/用了哪些工具/步骤是什么”，或者排障确实需要解释失败原因时，才展开 MCP 工具调用细节。
7. 如果用户要求退出管理模式，明确告诉他发送 /manage off；如果要重新进入，发送 /manage on。
8. 如果用户说“列出所有会话/全部会话/有哪些会话”，默认理解为“列出当前这个微信联系人的全部会话”，优先调用 get_management_snapshot(target_sender_id="{sender_id}")。
9. 只有当用户明确说“所有发送方/所有来源/系统里所有人的会话”时，才调用 list_sender_conversations(focus_sender_id="{sender_id}")。
10. 回答会话总览时，优先直接复述 MCP 返回里的 summary_lines，不要自行重算会话数量、当前会话或最近状态；不要默认输出 sender_id、task_count、model、workdir、latest_task_id 这类工程字段，除非用户明确要求。
11. 如果用户的问题同时包含“所有会话”和“所有发送方”，先明确区分“你的会话”和“全局来源”，默认先回答“你的会话”。
12. 不要把 MCP 快照里的 control_mode/manager_mode 字段误解成微信当前是否在管理 Agent。微信里的普通消息是否进入管理 Agent，由 /manage 控制；当前这条管理会话本身就是管理入口。
13. 不要汇报与结果无关的工具探测、能力枚举或内部思考过程。默认不要出现“我调用了某某工具”这类句子，除非用户明确要求。
14. 如果某个管理工具已经返回了适合直接展示的人类摘要，优先直接引用该摘要，不要再自行压缩、改写或补充未经工具确认的结论。
15. 默认不要使用“发送方”“全局来源”“control_mode”“manager_mode”这类内部术语。面向用户时优先说“你这边”“当前会话”“全部会话”“其他人的会话”。
16. 默认不要在结果末尾追加泛泛的教学式引导，例如“如果你要看系统里所有发送方……”。只有当用户明确表示还想继续看更大范围时，才补一句简短的下一步建议。
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_manager_prompt_template() -> str:
    if CHATBRIDGE_MANAGER_PROMPT_PATH.exists():
        text = CHATBRIDGE_MANAGER_PROMPT_PATH.read_text(encoding="utf-8").strip()
        if text:
            return text
    return CHATBRIDGE_MANAGER_PROMPT_FALLBACK.strip()


def _to_abs_path(value: str, default: Path) -> str:
    raw = (value or "").strip()
    path = Path(raw) if raw else default
    if not path.is_absolute():
        path = APP_DIR / path
    return str(path.resolve())


def _to_rel_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(APP_DIR.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def normalize_backend(value: str) -> str:
    backend = (value or DEFAULT_BACKEND_KEY).strip().lower()
    return backend if backend in SUPPORTED_BACKENDS else DEFAULT_BACKEND_KEY


@dataclass
class AgentConfig:
    id: str
    name: str
    workdir: str
    session_file: str
    backend: str = DEFAULT_BACKEND_KEY
    model: str = ""
    prompt_prefix: str = ""
    enabled: bool = True


def _normalize_agent(raw: object) -> AgentConfig | None:
    if not isinstance(raw, dict):
        return None
    agent_id = str(raw.get("id") or "").strip()
    if not agent_id:
        return None
    return AgentConfig(
        id=agent_id,
        name=str(raw.get("name") or agent_id).strip() or agent_id,
        workdir=_to_abs_path(str(raw.get("workdir") or ""), WORKSPACE_DIR),
        session_file=_to_abs_path(str(raw.get("session_file") or ""), SESSION_DIR / f"{agent_id}.txt"),
        backend=normalize_backend(str(raw.get("backend") or DEFAULT_BACKEND_KEY)),
        model=str(raw.get("model") or "").strip(),
        prompt_prefix=str(raw.get("prompt_prefix") or "").strip(),
        enabled=bool(raw.get("enabled", True)),
    )


@dataclass
class HubConfig:
    codex_command: str = field(default_factory=lambda: resolve_command(DEFAULT_BACKEND_KEY))
    claude_command: str = field(default_factory=lambda: resolve_command("claude"))
    opencode_command: str = field(default_factory=lambda: resolve_command("opencode"))
    agents: list[AgentConfig] = field(default_factory=list)

    @classmethod
    def load(cls) -> "HubConfig":
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            cfg = cls(
                agents=[
                    AgentConfig("main", "默认会话", str(WORKSPACE_DIR), str(SESSION_DIR / "main.txt")),
                ]
            )
            cfg.save()
            return cfg
        raw = load_json(CONFIG_PATH, None, expect_type=dict)
        if raw is None:
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            cfg = cls(
                agents=[
                    AgentConfig("main", "默认会话", str(WORKSPACE_DIR), str(SESSION_DIR / "main.txt")),
                ]
            )
            cfg.save()
            return cfg
        raw["agents"] = [agent for item in raw.get("agents", []) if (agent := _normalize_agent(item)) is not None]
        raw.pop("host", None)
        raw.pop("port", None)
        raw.pop("auto_open_browser", None)
        raw["codex_command"] = resolve_command(str(raw.get("codex_command") or DEFAULT_BACKEND_KEY))
        raw["claude_command"] = resolve_command(str(raw.get("claude_command") or "claude"))
        raw["opencode_command"] = resolve_command(str(raw.get("opencode_command") or "opencode"))
        if not raw["agents"]:
            raw["agents"] = [
                AgentConfig("main", "默认会话", str(WORKSPACE_DIR), str(SESSION_DIR / "main.txt")),
            ]
        for agent in raw["agents"]:
            agent.name = (agent.name or "默认会话").strip()
            agent.workdir = _to_abs_path(agent.workdir, WORKSPACE_DIR)
            agent.session_file = _to_abs_path(agent.session_file, SESSION_DIR / f"{agent.id}.txt")
            agent.backend = normalize_backend(agent.backend)
            Path(agent.workdir).mkdir(parents=True, exist_ok=True)
        return cls(**raw)

    def save(self) -> None:
        data = asdict(self)
        for agent in data.get("agents", []):
            agent["workdir"] = _to_rel_path(str(agent.get("workdir") or WORKSPACE_DIR))
            agent["session_file"] = _to_rel_path(str(agent.get("session_file") or (SESSION_DIR / "main.txt")))
            agent["backend"] = normalize_backend(str(agent.get("backend") or DEFAULT_BACKEND_KEY))
        save_json(CONFIG_PATH, data)


class MultiCodexHub:
    def __init__(self, config: HubConfig) -> None:
        self.config = config
        self.backend_registry = build_backend_registry()
        self.manager_runtime = ChatBridgeManagerRuntime(codex_command=self.config.codex_command)
        self.lock = threading.RLock()
        self.tasks: list[HubTask] = []
        self.runtimes: dict[str, AgentRuntimeState] = {}
        self.queues: dict[str, queue.Queue[HubTask]] = {}
        self.started_workers: set[str] = set()
        self.running_task_pids: dict[str, int] = {}
        self.cancel_requested_task_ids: set[str] = set()
        self._restore_previous_state()
        for agent in self.config.agents:
            self._ensure_agent(agent)
        self._save_state()

    def _restore_previous_state(self) -> None:
        previous = load_json(STATE_PATH, None, expect_type=dict)
        if previous is None:
            return
        for raw_task in previous.get("tasks", []):
            task = HubTask.from_dict(raw_task, default_backend=DEFAULT_BACKEND_KEY)
            if task is None:
                continue
            task.backend = normalize_backend(task.backend)
            if task.status == "running":
                task.status = "unknown_after_restart"
                task.error = "Hub restarted while this task was running."
                task.finished_at = now_iso()
            self.tasks.append(task)
        for raw_agent in previous.get("agents", []):
            if not isinstance(raw_agent, dict):
                continue
            agent_id = str(raw_agent.get("id") or "").strip()
            if not agent_id:
                continue
            self.runtimes[agent_id] = AgentRuntimeState.from_dict(raw_agent.get("runtime"), now=now_iso())

    def _ensure_agent(self, agent: AgentConfig) -> None:
        self.runtimes.setdefault(
            agent.id,
            AgentRuntimeState(updated_at=now_iso()),
        )
        self.queues.setdefault(agent.id, queue.Queue())
        if agent.id not in self.started_workers:
            threading.Thread(target=self._worker, args=(agent.id,), daemon=True).start()
            self.started_workers.add(agent.id)

    def list_agents(self) -> list[dict[str, Any]]:
        return [{**asdict(agent), "runtime": self.runtimes.get(agent.id, AgentRuntimeState()).to_dict()} for agent in self.config.agents]

    def list_tasks(self) -> list[dict[str, Any]]:
        return [task.to_dict() for task in sorted(self.tasks, key=lambda item: item.created_at, reverse=True)[:50]]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        for task in self.tasks:
            if task.id == task_id:
                return task.to_dict()
        return None

    def _find_task(self, task_id: str) -> HubTask | None:
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def _queued_task_count(self, agent_id: str) -> int:
        return sum(1 for task in self.tasks if task.agent_id == agent_id and task.status == "queued")

    def _refresh_runtime_queue_size(self, agent_id: str) -> None:
        runtime = self.runtimes.setdefault(agent_id, AgentRuntimeState(updated_at=now_iso()))
        runtime.queue_size = self._queued_task_count(agent_id)
        runtime.updated_at = now_iso()

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        cleaned_id = task_id.strip()
        if not cleaned_id:
            raise ValueError("task_id is required")
        pid = 0
        with self.lock:
            task = self._find_task(cleaned_id)
            if task is None:
                raise ValueError("task not found")
            if task.status == "queued":
                task.status = "canceled"
                task.finished_at = now_iso()
                task.error = "Task canceled before execution."
                self._refresh_runtime_queue_size(task.agent_id)
                self._save_state()
                return task.to_dict()
            if task.status == "running":
                pid = int(self.running_task_pids.get(cleaned_id) or 0)
                if pid <= 0:
                    raise ValueError("running task cannot be canceled right now")
                self.cancel_requested_task_ids.add(cleaned_id)
                task_payload = task.to_dict()
            else:
                if task.status == "canceled":
                    raise ValueError("task already canceled")
                raise ValueError(f"task cannot be canceled from status: {task.status}")
        terminate_process_tree(pid)
        return task_payload

    def retry_task(
        self,
        task_id: str,
        *,
        source: str = "",
        sender_id: str = "",
    ) -> dict[str, Any]:
        cleaned_id = task_id.strip()
        if not cleaned_id:
            raise ValueError("task_id is required")
        with self.lock:
            task = self._find_task(cleaned_id)
            if task is None:
                raise ValueError("task not found")
            if task.status in {"queued", "running"}:
                raise ValueError(f"task cannot be retried from status: {task.status}")
            agent_id = task.agent_id
            prompt = task.prompt
            task_source = source.strip() or task.source or "desktop"
            task_sender_id = sender_id.strip() or task.sender_id
            session_name = task.session_name
            backend = task.backend
        return self.submit_task(
            agent_id,
            prompt,
            task_source,
            task_sender_id,
            session_name,
            backend,
            task.workdir,
            task.model,
            task.bridge_conversations_path,
            task.bridge_event_log_path,
            task.manager_state_path,
        )

    def create_or_update_agent(self, payload: dict[str, Any]) -> AgentConfig:
        with self.lock:
            agent_id = (payload.get("id") or "").strip() or f"agent-{uuid.uuid4().hex[:8]}"
            agent = next((a for a in self.config.agents if a.id == agent_id), None)
            if agent is None:
                agent = AgentConfig(
                    agent_id,
                    payload.get("name") or agent_id,
                    payload.get("workdir") or str(WORKSPACE_DIR),
                    payload.get("session_file") or str(SESSION_DIR / f"{agent_id}.txt"),
                )
                self.config.agents.append(agent)
            agent.name = (payload.get("name") or agent.name).strip()
            agent.workdir = (payload.get("workdir") or agent.workdir).strip()
            agent.session_file = (payload.get("session_file") or agent.session_file).strip()
            agent.backend = normalize_backend(str(payload.get("backend") or agent.backend))
            agent.model = (payload.get("model") or "").strip()
            agent.prompt_prefix = (payload.get("prompt_prefix") or "").strip()
            agent.enabled = bool(payload.get("enabled", agent.enabled))
            if not agent.name:
                raise ValueError("agent name is required")
            if not agent.workdir:
                raise ValueError("agent workdir is required")
            if not agent.session_file:
                raise ValueError("agent session_file is required")
            if not agent.enabled and not any(item.id != agent.id and item.enabled for item in self.config.agents):
                raise ValueError("at least one enabled agent is required")
            Path(agent.workdir).mkdir(parents=True, exist_ok=True)
            Path(agent.session_file).parent.mkdir(parents=True, exist_ok=True)
            self._ensure_agent(agent)
            self.config.save()
            self._save_state()
            return agent

    def delete_agent(self, agent_id: str) -> None:
        with self.lock:
            cleaned_id = agent_id.strip()
            if not cleaned_id:
                raise ValueError("agent_id is required")
            agent = next((item for item in self.config.agents if item.id == cleaned_id), None)
            if agent is None:
                raise ValueError(f"agent not found: {cleaned_id}")
            if len(self.config.agents) <= 1:
                raise ValueError("cannot delete the last agent")
            bridge_agent_id = BridgeConfig.load().backend_id.strip()
            if bridge_agent_id and bridge_agent_id == cleaned_id:
                raise ValueError(f"agent is in use by weixin bridge: {cleaned_id}")
            runtime = self.runtimes.get(cleaned_id) or AgentRuntimeState()
            if runtime.status == "running" or runtime.queue_size > 0:
                raise ValueError(f"agent still has active work: {cleaned_id}")
            self.config.agents = [item for item in self.config.agents if item.id != cleaned_id]
            self.queues.pop(cleaned_id, None)
            self.runtimes.pop(cleaned_id, None)
            self.started_workers.discard(cleaned_id)
            self.tasks = [task for task in self.tasks if task.agent_id != cleaned_id]
            self.config.save()
            self._save_state()

    def submit_task(
        self,
        agent_id: str,
        prompt: str,
        source: str = "desktop",
        sender_id: str = "",
        session_name: str = "",
        backend: str = "",
        workdir: str = "",
        model: str = "",
        bridge_conversations_path: str = "",
        bridge_event_log_path: str = "",
        manager_state_path: str = "",
    ) -> dict[str, Any]:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        agent = next((a for a in self.config.agents if a.id == agent_id and a.enabled), None)
        if agent is None:
            raise ValueError(f"agent not found or disabled: {agent_id}")
        task = HubTask(
            id=f"task-{uuid.uuid4().hex[:10]}",
            agent_id=agent.id,
            agent_name=agent.name,
            backend=normalize_backend(backend or agent.backend),
            source=source,
            sender_id=sender_id,
            prompt=prompt,
            status="queued",
            created_at=now_iso(),
            session_name=session_name.strip(),
            workdir=workdir.strip(),
            model=model.strip(),
            bridge_conversations_path=bridge_conversations_path.strip(),
            bridge_event_log_path=bridge_event_log_path.strip(),
            manager_state_path=manager_state_path.strip(),
        )
        with self.lock:
            self.tasks.append(task)
            self.queues[agent.id].put(task)
            self._refresh_runtime_queue_size(agent.id)
            self._save_state()
        return task.to_dict()

    def handle_wechat_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        preferred = (payload.get("preferred_agent_id") or "").strip()
        enabled = [agent for agent in self.config.agents if agent.enabled]
        agent_id = preferred or (enabled[0].id if enabled else "")
        if not agent_id:
            raise ValueError("no enabled agent configured")
        return self.submit_task(
            agent_id,
            str(payload.get("text") or ""),
            source="wechat",
            sender_id=str(payload.get("sender_id") or ""),
            session_name=str(payload.get("session_name") or ""),
            backend=str(payload.get("backend") or ""),
            workdir=str(payload.get("workdir") or ""),
            model=str(payload.get("model") or ""),
        )

    def _worker(self, agent_id: str) -> None:
        q = self.queues[agent_id]
        while True:
            task = q.get()
            try:
                if task.status == "canceled":
                    continue
                self._run_task(agent_id, task)
            finally:
                with self.lock:
                    runtime = self.runtimes[agent_id]
                    self._refresh_runtime_queue_size(agent_id)
                    if runtime.status == "running" and not any(
                        item.agent_id == agent_id and item.status == "running" for item in self.tasks
                    ):
                        runtime.status = "idle"
                    self._save_state()
                q.task_done()

    def _run_task(self, agent_id: str, task: HubTask) -> None:
        agent = next(a for a in self.config.agents if a.id == agent_id)
        with self.lock:
            task.status = "running"
            task.started_at = now_iso()
            self.runtimes[agent_id].status = "running"
            self._save_state()
        try:
            result = self._invoke_backend(agent, task)
            canceled = self._consume_cancel_request(task.id)
            self._clear_running_task_pid(task.id)
            with self.lock:
                runtime = self.runtimes[agent_id]
                task.finished_at = now_iso()
                runtime.status = "idle"
                if canceled:
                    task.status = "canceled"
                    task.error = "Task canceled during execution."
                    task.output = ""
                    runtime.last_error = task.error
                else:
                    task.status = "succeeded"
                    task.output = result["output"]
                    task.session_id = result["session_id"]
                    runtime.success_count += 1
                    runtime.last_output = result["output"][:1800]
                    runtime.last_error = ""
                self._save_state()
            if canceled:
                self._notify_task_canceled(task)
                return
            self._notify_task_result(task, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            canceled = self._consume_cancel_request(task.id)
            self._clear_running_task_pid(task.id)
            with self.lock:
                runtime = self.runtimes[agent_id]
                task.finished_at = now_iso()
                if canceled:
                    task.status = "canceled"
                    task.error = "Task canceled during execution."
                    runtime.status = "idle"
                    runtime.last_error = task.error
                else:
                    task.status = "failed"
                    task.error = str(exc)
                    runtime.status = "failed"
                    runtime.failure_count += 1
                    runtime.last_error = str(exc)
                self._save_state()
            if canceled:
                self._notify_task_canceled(task)
                return
            self._notify_task_result(task, succeeded=False)

    def _register_running_task_pid(self, task_id: str, pid: int) -> None:
        if pid <= 0:
            return
        with self.lock:
            self.running_task_pids[task_id] = pid

    def _clear_running_task_pid(self, task_id: str) -> None:
        with self.lock:
            self.running_task_pids.pop(task_id, None)

    def _consume_cancel_request(self, task_id: str) -> bool:
        with self.lock:
            if task_id not in self.cancel_requested_task_ids:
                return False
            self.cancel_requested_task_ids.remove(task_id)
            return True

    def _notify_task_result(self, task: HubTask, succeeded: bool) -> None:
        if task.source.strip().lower().startswith("wechat"):
            return
        task_id = task.id
        agent_name = task.agent_name or task.agent_id
        session_name = task.session_name or "default"
        backend = task.backend or DEFAULT_BACKEND_KEY
        if succeeded:
            output = task.output.strip() or "(empty)"
            detail = (
                f"任务 ID: {task_id}\n"
                f"Agent: {agent_name}\n"
                f"会话: {session_name}\n"
                f"后端: {backend}\n"
                f"状态: 成功\n"
                f"输出摘要: {output[:600]}\n"
                f"{build_task_followup_hint(task_id=task_id, session_name=session_name)}"
            )
            broadcast_weixin_notice_by_kind("task", "任务执行完成", detail)
            return
        error_text = task.error.strip() or "unknown error"
        detail = (
            f"任务 ID: {task_id}\n"
            f"Agent: {agent_name}\n"
            f"会话: {session_name}\n"
            f"后端: {backend}\n"
            f"状态: 失败\n"
            f"错误: {error_text[:600]}\n"
            f"{build_task_followup_hint(task_id=task_id, session_name=session_name)}"
        )
        broadcast_weixin_notice_by_kind("task", "任务执行失败", detail)

    def _notify_task_canceled(self, task: HubTask) -> None:
        if task.source.strip().lower().startswith("wechat"):
            return
        task_id = task.id
        agent_name = task.agent_name or task.agent_id
        session_name = task.session_name or "default"
        backend = task.backend or DEFAULT_BACKEND_KEY
        detail = (
            f"任务 ID: {task_id}\n"
            f"Agent: {agent_name}\n"
            f"会话: {session_name}\n"
            f"后端: {backend}\n"
            f"状态: 已取消\n"
            f"说明: {(task.error or 'Task canceled during execution.')[:600]}\n"
            f"{build_task_followup_hint(task_id=task_id, session_name=session_name, allow_retry=True)}"
        )
        broadcast_weixin_notice_by_kind("task", "任务已取消", detail)

    def _invoke_backend(self, agent: AgentConfig, task: HubTask) -> dict[str, str]:
        manager_mcp = self._build_manager_mcp_config(task)
        if task.source.strip().lower() == WECHAT_MANAGER_SOURCE:
            if manager_mcp is None:
                raise RuntimeError("manager runtime missing MCP configuration")
            return self.manager_runtime.invoke(
                sender_id=task.sender_id,
                prompt=task.prompt,
                instructions=self._resolve_task_prompt_prefix(agent, task),
                model=task.model.strip() or agent.model,
                mcp_config=manager_mcp,
            )
        normalized_backend = normalize_backend(task.backend or agent.backend)
        backend = self.backend_registry.get(normalized_backend)
        if backend is None:
            raise ValueError(f"unsupported backend: {normalized_backend}")
        if manager_mcp is not None and normalized_backend == "opencode":
            raise RuntimeError("当前管理 Agent 暂不支持 opencode 后端，请改用 codex 或 claude")
        effective_agent = AgentConfig(
            id=agent.id,
            name=agent.name,
            workdir=self._resolve_task_workdir(agent, task),
            session_file=agent.session_file,
            backend=agent.backend,
            model=task.model.strip() or agent.model,
            prompt_prefix=self._resolve_task_prompt_prefix(agent, task),
            enabled=agent.enabled,
        )
        return backend.invoke(
            agent=effective_agent,
            prompt=task.prompt,
            session_name=task.session_name,
            context=BackendContext(
                codex_command=self.config.codex_command,
                claude_command=self.config.claude_command,
                opencode_command=self.config.opencode_command,
                session_dir=SESSION_DIR,
                creationflags=creationflags(),
                start_new_session=not IS_WINDOWS,
                on_process_started=lambda pid: self._register_running_task_pid(task.id, pid),
                chatbridge_mcp=manager_mcp,
            ),
        )

    def _resolve_task_workdir(self, agent: AgentConfig, task: HubTask) -> str:
        if task.source.strip().lower() == WECHAT_MANAGER_SOURCE:
            return str(APP_DIR)
        return task.workdir.strip() or agent.workdir

    def _resolve_task_prompt_prefix(self, agent: AgentConfig, task: HubTask) -> str:
        if task.source.strip().lower() != WECHAT_MANAGER_SOURCE:
            return agent.prompt_prefix
        return load_manager_prompt_template().format(sender_id=task.sender_id or "(unknown sender)")

    def _build_manager_mcp_config(self, task: HubTask) -> McpServerConfig | None:
        if task.source.strip().lower() != WECHAT_MANAGER_SOURCE:
            return None
        args = [str(CHATBRIDGE_MANAGER_TOOL_PATH), "--trusted-internal-manager"]
        if task.bridge_conversations_path.strip():
            args.extend(["--bridge-conversations-path", task.bridge_conversations_path.strip()])
        if task.bridge_event_log_path.strip():
            args.extend(["--bridge-event-log-path", task.bridge_event_log_path.strip()])
        if task.manager_state_path.strip():
            args.extend(["--manager-state-path", task.manager_state_path.strip()])
        return McpServerConfig(
            name=CHATBRIDGE_MANAGER_MCP_NAME,
            command=sys.executable,
            args=args,
        )

    def _save_state(self) -> None:
        ensure_ipc_dirs()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        external_agent_processes = [item.to_dict() for item in discover_external_agent_processes()]
        save_json(
            STATE_PATH,
            {
                "generated_at": now_iso(),
                "config": {
                    "codex_command": self.config.codex_command,
                    "claude_command": self.config.claude_command,
                    "opencode_command": self.config.opencode_command,
                },
                "agents": self.list_agents(),
                "tasks": self.list_tasks(),
                "external_agent_processes": external_agent_processes,
            },
        )

    def process_ipc_once(self) -> None:
        ensure_ipc_dirs()
        for request_path in sorted(REQUEST_DIR.glob("*.json")):
            try:
                request = read_request(request_path)
                response = self._dispatch_request(request)
            except Exception as exc:  # noqa: BLE001
                request_id = request_path.stem
                response = IpcResponseEnvelope(ok=False, error=str(exc))
            else:
                request_id = request.id or request_path.stem
            write_response(request_id, response)
            mark_processed(request_path)

    def _dispatch_request(self, request: IpcRequestEnvelope) -> IpcResponseEnvelope:
        action = request.action
        payload = request.payload
        if action == "submit_task":
            return IpcResponseEnvelope(
                ok=True,
                payload={
                    "task": self.submit_task(
                        str(payload.get("agent_id") or ""),
                        str(payload.get("prompt") or ""),
                        str(payload.get("source") or "desktop"),
                        str(payload.get("sender_id") or ""),
                        str(payload.get("session_name") or ""),
                        str(payload.get("backend") or ""),
                        str(payload.get("workdir") or ""),
                        str(payload.get("model") or ""),
                        str(payload.get("bridge_conversations_path") or ""),
                        str(payload.get("bridge_event_log_path") or ""),
                        str(payload.get("manager_state_path") or ""),
                    ),
                },
            )
        if action == "get_task":
            task = self.get_task(str(payload.get("task_id") or ""))
            if task is None:
                return IpcResponseEnvelope(ok=False, error="task not found")
            return IpcResponseEnvelope(ok=True, payload={"task": task})
        if action == "cancel_task":
            return IpcResponseEnvelope(ok=True, payload={"task": self.cancel_task(str(payload.get("task_id") or ""))})
        if action == "retry_task":
            return IpcResponseEnvelope(
                ok=True,
                payload={
                    "task": self.retry_task(
                        str(payload.get("task_id") or ""),
                        source=str(payload.get("source") or ""),
                        sender_id=str(payload.get("sender_id") or ""),
                    )
                },
            )
        if action == "wechat_message":
            return IpcResponseEnvelope(ok=True, payload={"task": self.handle_wechat_message(payload)})
        if action == "save_agent":
            return IpcResponseEnvelope(ok=True, payload={"agent": asdict(self.create_or_update_agent(payload))})
        if action == "delete_agent":
            self.delete_agent(str(payload.get("agent_id") or ""))
            return IpcResponseEnvelope(ok=True)
        if action == "state":
            external_agent_processes = [item.to_dict() for item in discover_external_agent_processes()]
            return IpcResponseEnvelope(
                ok=True,
                payload={
                    "generated_at": now_iso(),
                    "config": {
                        "codex_command": self.config.codex_command,
                        "claude_command": self.config.claude_command,
                        "opencode_command": self.config.opencode_command,
                    },
                    "agents": self.list_agents(),
                    "tasks": self.list_tasks(),
                    "external_agent_processes": external_agent_processes,
                },
            )
        raise ValueError(f"unsupported action: {action}")


def run() -> int:
    ensure_ipc_dirs()
    config = HubConfig.load()
    hub = MultiCodexHub(config)
    print("ChatBridge backend started in local IPC mode")
    print(f"Config: {CONFIG_PATH}")
    print(f"State: {STATE_PATH}")
    while True:
        hub.process_ipc_once()
        time.sleep(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
