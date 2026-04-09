from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_backends import DEFAULT_BACKEND_KEY, BackendContext, build_backend_registry, supported_backend_keys
from bridge_config import BridgeConfig
from core.weixin_notifier import broadcast_weixin_notice_by_kind, build_task_followup_hint
from local_ipc import REQUEST_DIR, ensure_ipc_dirs, mark_processed, read_request, write_response
from core.platform_compat import creationflags, resolve_command
from runtime_stack import discover_external_agent_processes


APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR / ".runtime"
STATE_DIR = RUNTIME_DIR / "state"
SESSION_DIR = RUNTIME_DIR / "sessions"
WORKSPACE_DIR = APP_DIR / "workspace"
CONFIG_PATH = APP_DIR / "agent_hub_config.json"
LEGACY_CONFIG_PATH = APP_DIR / "multi_codex_hub_config.json"
STATE_PATH = STATE_DIR / "multi_codex_hub_state.json"
SUPPORTED_BACKENDS = set(supported_backend_keys())


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


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


@dataclass
class HubConfig:
    codex_command: str = field(default_factory=lambda: resolve_command(DEFAULT_BACKEND_KEY))
    claude_command: str = field(default_factory=lambda: resolve_command("claude"))
    opencode_command: str = field(default_factory=lambda: resolve_command("opencode"))
    agents: list[AgentConfig] = field(default_factory=list)

    @classmethod
    def load(cls) -> "HubConfig":
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        config_path = CONFIG_PATH if CONFIG_PATH.exists() else LEGACY_CONFIG_PATH
        if not config_path.exists():
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            cfg = cls(
                agents=[
                    AgentConfig("main", "默认会话", str(WORKSPACE_DIR), str(SESSION_DIR / "main.txt")),
                ]
            )
            cfg.save()
            return cfg
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["agents"] = [AgentConfig(**a) for a in raw.get("agents", [])]
        raw.pop("host", None)
        raw.pop("port", None)
        raw.pop("auto_open_browser", None)
        raw["codex_command"] = resolve_command(str(raw.get("codex_command") or DEFAULT_BACKEND_KEY))
        raw["claude_command"] = resolve_command(str(raw.get("claude_command") or "claude"))
        raw["opencode_command"] = resolve_command(str(raw.get("opencode_command") or "opencode"))
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
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class MultiCodexHub:
    def __init__(self, config: HubConfig) -> None:
        self.config = config
        self.backend_registry = build_backend_registry()
        self.lock = threading.RLock()
        self.tasks: list[dict[str, Any]] = []
        self.runtimes: dict[str, dict[str, Any]] = {}
        self.queues: dict[str, queue.Queue[dict[str, Any]]] = {}
        self.started_workers: set[str] = set()
        self._restore_previous_state()
        for agent in self.config.agents:
            self._ensure_agent(agent)
        self._save_state()

    def _restore_previous_state(self) -> None:
        if not STATE_PATH.exists():
            return
        try:
            previous = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        for task in previous.get("tasks", []):
            if not isinstance(task, dict):
                continue
            if task.get("status") == "running":
                task["status"] = "unknown_after_restart"
                task["error"] = "Hub restarted while this task was running."
                task["finished_at"] = now_iso()
            if not task.get("backend"):
                task["backend"] = DEFAULT_BACKEND_KEY
            self.tasks.append(task)
        for agent in previous.get("agents", []):
            agent_id = agent.get("id")
            runtime = agent.get("runtime")
            if isinstance(agent_id, str) and isinstance(runtime, dict):
                self.runtimes[agent_id] = runtime

    def _ensure_agent(self, agent: AgentConfig) -> None:
        self.runtimes.setdefault(
            agent.id,
            {
                "status": "idle",
                "queue_size": 0,
                "success_count": 0,
                "failure_count": 0,
                "last_output": "",
                "last_error": "",
                "updated_at": now_iso(),
            },
        )
        self.queues.setdefault(agent.id, queue.Queue())
        if agent.id not in self.started_workers:
            threading.Thread(target=self._worker, args=(agent.id,), daemon=True).start()
            self.started_workers.add(agent.id)

    def list_agents(self) -> list[dict[str, Any]]:
        return [{**asdict(agent), "runtime": dict(self.runtimes.get(agent.id, {}))} for agent in self.config.agents]

    def list_tasks(self) -> list[dict[str, Any]]:
        return sorted(self.tasks, key=lambda x: x["created_at"], reverse=True)[:50]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        for task in self.tasks:
            if task["id"] == task_id:
                return dict(task)
        return None

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
            runtime = self.runtimes.get(cleaned_id) or {}
            if str(runtime.get("status") or "") == "running" or int(runtime.get("queue_size") or 0) > 0:
                raise ValueError(f"agent still has active work: {cleaned_id}")
            self.config.agents = [item for item in self.config.agents if item.id != cleaned_id]
            self.queues.pop(cleaned_id, None)
            self.runtimes.pop(cleaned_id, None)
            self.started_workers.discard(cleaned_id)
            self.tasks = [task for task in self.tasks if str(task.get('agent_id') or '') != cleaned_id]
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
    ) -> dict[str, Any]:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        agent = next((a for a in self.config.agents if a.id == agent_id and a.enabled), None)
        if agent is None:
            raise ValueError(f"agent not found or disabled: {agent_id}")
        task = {
            "id": f"task-{uuid.uuid4().hex[:10]}",
            "agent_id": agent.id,
            "agent_name": agent.name,
            "backend": normalize_backend(backend or agent.backend),
            "source": source,
            "sender_id": sender_id,
            "prompt": prompt,
            "status": "queued",
            "created_at": now_iso(),
            "started_at": "",
            "finished_at": "",
            "output": "",
            "error": "",
            "session_id": "",
            "session_name": session_name.strip(),
        }
        with self.lock:
            self.tasks.append(task)
            self.queues[agent.id].put(task)
            self.runtimes[agent.id]["queue_size"] = self.queues[agent.id].qsize()
            self._save_state()
        return task

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
        )

    def _worker(self, agent_id: str) -> None:
        q = self.queues[agent_id]
        while True:
            task = q.get()
            try:
                self._run_task(agent_id, task)
            finally:
                with self.lock:
                    self.runtimes[agent_id]["queue_size"] = q.qsize()
                    self.runtimes[agent_id]["updated_at"] = now_iso()
                    self._save_state()
                q.task_done()

    def _run_task(self, agent_id: str, task: dict[str, Any]) -> None:
        agent = next(a for a in self.config.agents if a.id == agent_id)
        with self.lock:
            task["status"] = "running"
            task["started_at"] = now_iso()
            self.runtimes[agent_id]["status"] = "running"
            self._save_state()
        try:
            result = self._invoke_backend(agent, task["prompt"], task.get("session_name", ""), task.get("backend", DEFAULT_BACKEND_KEY))
            with self.lock:
                task["status"] = "succeeded"
                task["finished_at"] = now_iso()
                task["output"] = result["output"]
                task["session_id"] = result["session_id"]
                self.runtimes[agent_id]["status"] = "idle"
                self.runtimes[agent_id]["success_count"] += 1
                self.runtimes[agent_id]["last_output"] = result["output"][:1800]
                self.runtimes[agent_id]["last_error"] = ""
                self._save_state()
            self._notify_task_result(task, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                task["status"] = "failed"
                task["finished_at"] = now_iso()
                task["error"] = str(exc)
                self.runtimes[agent_id]["status"] = "failed"
                self.runtimes[agent_id]["failure_count"] += 1
                self.runtimes[agent_id]["last_error"] = str(exc)
                self._save_state()
            self._notify_task_result(task, succeeded=False)

    def _notify_task_result(self, task: dict[str, Any], succeeded: bool) -> None:
        if str(task.get("source") or "").strip().lower() == "wechat":
            return
        task_id = str(task.get("id") or "")
        agent_name = str(task.get("agent_name") or task.get("agent_id") or "")
        session_name = str(task.get("session_name") or "default")
        backend = str(task.get("backend") or DEFAULT_BACKEND_KEY)
        if succeeded:
            output = str(task.get("output") or "").strip() or "(empty)"
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
        error_text = str(task.get("error") or "unknown error").strip()
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

    def _invoke_backend(self, agent: AgentConfig, prompt: str, session_name: str = "", backend: str = "") -> dict[str, str]:
        normalized_backend = normalize_backend(backend or agent.backend)
        backend = self.backend_registry.get(normalized_backend)
        if backend is None:
            raise ValueError(f"unsupported backend: {normalized_backend}")
        return backend.invoke(
            agent=agent,
            prompt=prompt,
            session_name=session_name,
            context=BackendContext(
                codex_command=self.config.codex_command,
                claude_command=self.config.claude_command,
                opencode_command=self.config.opencode_command,
                session_dir=SESSION_DIR,
                creationflags=creationflags(),
            ),
        )

    def _save_state(self) -> None:
        ensure_ipc_dirs()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(
                {
                    "generated_at": now_iso(),
                    "config": {
                        "codex_command": self.config.codex_command,
                        "claude_command": self.config.claude_command,
                        "opencode_command": self.config.opencode_command,
                    },
                    "agents": self.list_agents(),
                    "tasks": self.list_tasks(),
                    "external_agent_processes": discover_external_agent_processes(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def process_ipc_once(self) -> None:
        ensure_ipc_dirs()
        for request_path in sorted(REQUEST_DIR.glob("*.json")):
            try:
                request = read_request(request_path)
                response = self._dispatch_request(request)
            except Exception as exc:  # noqa: BLE001
                request_id = request_path.stem
                response = {"ok": False, "error": str(exc)}
            else:
                request_id = str(request.get("id") or request_path.stem)
            write_response(request_id, response)
            mark_processed(request_path)

    def _dispatch_request(self, request: dict[str, Any]) -> dict[str, Any]:
        action = str(request.get("action") or "").strip()
        payload = request.get("payload") or {}
        if action == "submit_task":
            return {
                "ok": True,
                "task": self.submit_task(
                    str(payload.get("agent_id") or ""),
                    str(payload.get("prompt") or ""),
                    str(payload.get("source") or "desktop"),
                    str(payload.get("sender_id") or ""),
                    str(payload.get("session_name") or ""),
                    str(payload.get("backend") or ""),
                ),
            }
        if action == "get_task":
            task = self.get_task(str(payload.get("task_id") or ""))
            if task is None:
                return {"ok": False, "error": "task not found"}
            return {"ok": True, "task": task}
        if action == "wechat_message":
            return {"ok": True, "task": self.handle_wechat_message(payload)}
        if action == "save_agent":
            return {"ok": True, "agent": asdict(self.create_or_update_agent(payload))}
        if action == "delete_agent":
            self.delete_agent(str(payload.get("agent_id") or ""))
            return {"ok": True}
        if action == "state":
            return {
                "ok": True,
                "generated_at": now_iso(),
                "config": {
                    "codex_command": self.config.codex_command,
                    "claude_command": self.config.claude_command,
                    "opencode_command": self.config.opencode_command,
                },
                "agents": self.list_agents(),
                "tasks": self.list_tasks(),
                "external_agent_processes": discover_external_agent_processes(),
            }
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
