from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from codex_wechat_ipc import REQUEST_DIR, ensure_ipc_dirs, mark_processed, read_request, write_response


APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR / ".runtime"
STATE_DIR = RUNTIME_DIR / "state"
SESSION_DIR = RUNTIME_DIR / "sessions"
WORKSPACE_DIR = APP_DIR / "workspace"
CONFIG_PATH = APP_DIR / "multi_codex_hub_config.json"
STATE_PATH = STATE_DIR / "multi_codex_hub_state.json"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SUPPORTED_BACKENDS = {"codex", "opencode"}


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
    backend = (value or "codex").strip().lower()
    return backend if backend in SUPPORTED_BACKENDS else "codex"


def discover_agent_processes() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and ( "
        "$_.CommandLine -like '*codex*' -or $_.Name -like 'codex*' -or "
        "$_.CommandLine -like '*opencode*' -or $_.Name -like 'opencode*' "
        ") } | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    result = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "pid": item.get("ProcessId"),
                "name": item.get("Name") or "",
                "command_line": item.get("CommandLine") or "",
            }
        )
    return result


@dataclass
class AgentConfig:
    id: str
    name: str
    workdir: str
    session_file: str
    backend: str = "codex"
    model: str = ""
    prompt_prefix: str = ""
    enabled: bool = True


@dataclass
class HubConfig:
    codex_command: str = "codex.cmd"
    opencode_command: str = "opencode.cmd"
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
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        raw["agents"] = [AgentConfig(**a) for a in raw.get("agents", [])]
        raw.pop("host", None)
        raw.pop("port", None)
        raw.pop("auto_open_browser", None)
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
            agent["backend"] = normalize_backend(str(agent.get("backend") or "codex"))
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class MultiCodexHub:
    def __init__(self, config: HubConfig) -> None:
        self.config = config
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
                task["backend"] = "codex"
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
            self._ensure_agent(agent)
            self.config.save()
            self._save_state()
            return agent

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
            result = self._invoke_backend(agent, task["prompt"], task.get("session_name", ""), task.get("backend", "codex"))
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
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                task["status"] = "failed"
                task["finished_at"] = now_iso()
                task["error"] = str(exc)
                self.runtimes[agent_id]["status"] = "failed"
                self.runtimes[agent_id]["failure_count"] += 1
                self.runtimes[agent_id]["last_error"] = str(exc)
                self._save_state()

    def _invoke_backend(self, agent: AgentConfig, prompt: str, session_name: str = "", backend: str = "") -> dict[str, str]:
        if normalize_backend(backend or agent.backend) == "opencode":
            return self._invoke_opencode(agent, prompt, session_name)
        return self._invoke_codex(agent, prompt, session_name)

    def _invoke_codex(self, agent: AgentConfig, prompt: str, session_name: str = "") -> dict[str, str]:
        workdir = Path(agent.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        session_file = self._resolve_session_file(agent, session_name)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        existing_session = session_file.read_text(encoding="utf-8").strip() if session_file.exists() else ""
        final_prompt = prompt if not agent.prompt_prefix else f"{agent.prompt_prefix}\n\n{prompt}"
        output_path = Path(tempfile.gettempdir()) / f"multi-codex-output-{uuid.uuid4().hex}.txt"

        options = ["--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "--json", "-o", str(output_path)]
        if agent.model:
            options.extend(["-m", agent.model])
        if existing_session:
            argv = [self.config.codex_command, "exec", "resume", *options, existing_session, final_prompt]
        else:
            argv = [self.config.codex_command, "exec", *options, "-C", str(workdir), final_prompt]

        completed = subprocess.run(
            argv,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            check=False,
            shell=False,
        )
        session_id = existing_session
        error_message = completed.stderr.strip()
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started" and event.get("thread_id"):
                session_id = str(event["thread_id"])
            if event.get("type") == "error" and event.get("message"):
                error_message = str(event["message"])
            if isinstance(event.get("error"), dict) and event["error"].get("message"):
                error_message = str(event["error"]["message"])
        if completed.returncode != 0:
            raise RuntimeError(error_message or f"Codex exited with code {completed.returncode}")
        if not output_path.exists():
            raise RuntimeError("Codex did not produce an output file")
        output = output_path.read_text(encoding="utf-8").strip()
        output_path.unlink(missing_ok=True)
        if not output:
            raise RuntimeError("Codex returned an empty result")
        if session_id:
            session_file.write_text(session_id, encoding="utf-8")
        return {"output": output, "session_id": session_id}

    def _invoke_opencode(self, agent: AgentConfig, prompt: str, session_name: str = "") -> dict[str, str]:
        workdir = Path(agent.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        session_file = self._resolve_session_file(agent, session_name)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        existing_session = session_file.read_text(encoding="utf-8").strip() if session_file.exists() else ""
        final_prompt = prompt if not agent.prompt_prefix else f"{agent.prompt_prefix}\n\n{prompt}"

        argv = [self.config.opencode_command, "run", "--format", "json"]
        if agent.model:
            argv.extend(["--model", agent.model])
        if existing_session:
            argv.extend(["--session", existing_session])
        argv.append(final_prompt)

        completed = subprocess.run(
            argv,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            check=False,
            shell=False,
        )

        output, session_id, error_message = self._parse_opencode_stdout(completed.stdout)
        if not session_id:
            session_id = existing_session or self._find_latest_opencode_session(workdir)
        if completed.returncode != 0:
            raise RuntimeError(error_message or completed.stderr.strip() or f"OpenCode exited with code {completed.returncode}")
        if not output:
            output = completed.stdout.strip()
        if not output:
            raise RuntimeError("OpenCode returned an empty result")
        if session_id:
            session_file.write_text(session_id, encoding="utf-8")
        return {"output": output, "session_id": session_id}

    def _parse_opencode_stdout(self, stdout: str) -> tuple[str, str, str]:
        fragments: list[str] = []
        session_id = ""
        error_message = ""
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = session_id or self._extract_session_id(payload)
            error_message = error_message or self._extract_error_text(payload)
            fragments.extend(self._collect_text_fragments(payload))
        unique_fragments: list[str] = []
        for fragment in fragments:
            text = fragment.strip()
            if text and text not in unique_fragments:
                unique_fragments.append(text)
        return "\n".join(unique_fragments).strip(), session_id, error_message

    def _collect_text_fragments(self, value: Any) -> list[str]:
        if isinstance(value, list):
            fragments: list[str] = []
            for item in value:
                fragments.extend(self._collect_text_fragments(item))
            return fragments
        if not isinstance(value, dict):
            return []

        fragments: list[str] = []
        text_keys = {"text", "message", "content", "output", "response"}
        role = str(value.get("role") or "").lower()
        event_type = str(value.get("type") or value.get("event") or "").lower()
        for key, item in value.items():
            if isinstance(item, str) and key in text_keys:
                if role in {"assistant", ""} or "assistant" in event_type or "message" in event_type or "response" in event_type:
                    fragments.append(item)
                continue
            if isinstance(item, (dict, list)):
                fragments.extend(self._collect_text_fragments(item))
        return fragments

    @staticmethod
    def _extract_session_id(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("session_id", "sessionId", "thread_id", "threadId"):
                raw = value.get(key)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
            session = value.get("session")
            if isinstance(session, dict):
                for key in ("id", "session_id", "sessionId"):
                    raw = session.get(key)
                    if isinstance(raw, str) and raw.strip():
                        return raw.strip()
            for item in value.values():
                found = MultiCodexHub._extract_session_id(item)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = MultiCodexHub._extract_session_id(item)
                if found:
                    return found
        return ""

    @staticmethod
    def _extract_error_text(value: Any) -> str:
        if isinstance(value, dict):
            event_type = str(value.get("type") or value.get("event") or "").lower()
            if "error" in event_type:
                for key in ("message", "error", "content"):
                    raw = value.get(key)
                    if isinstance(raw, str) and raw.strip():
                        return raw.strip()
            for item in value.values():
                nested = MultiCodexHub._extract_error_text(item)
                if nested:
                    return nested
        if isinstance(value, list):
            for item in value:
                nested = MultiCodexHub._extract_error_text(item)
                if nested:
                    return nested
        return ""

    def _find_latest_opencode_session(self, workdir: Path) -> str:
        completed = subprocess.run(
            [self.config.opencode_command, "session", "list", "-n", "1", "--format", "json"],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            check=False,
            shell=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return ""
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return ""
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("id", "session_id", "sessionId"):
                raw = item.get(key)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
        return ""

    def _resolve_session_file(self, agent: AgentConfig, session_name: str) -> Path:
        raw_name = (session_name or "").strip()
        if not raw_name:
            return Path(agent.session_file)
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in raw_name).strip("-_") or "default"
        return SESSION_DIR / f"{agent.id}__{safe}.txt"

    def _save_state(self) -> None:
        ensure_ipc_dirs()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(
                {
                    "generated_at": now_iso(),
                    "config": {
                        "codex_command": self.config.codex_command,
                        "opencode_command": self.config.opencode_command,
                    },
                    "agents": self.list_agents(),
                    "tasks": self.list_tasks(),
                    "external_agent_processes": discover_agent_processes(),
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
        if action == "state":
            return {
                "ok": True,
                "generated_at": now_iso(),
                "config": {
                    "codex_command": self.config.codex_command,
                    "opencode_command": self.config.opencode_command,
                },
                "agents": self.list_agents(),
                "tasks": self.list_tasks(),
                "external_agent_processes": discover_agent_processes(),
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
