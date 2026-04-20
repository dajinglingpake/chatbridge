from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable


JsonObject = dict[str, object]


@dataclass
class AgentRuntimeState:
    status: str = "idle"
    queue_size: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_output: str = ""
    last_error: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, raw: object, *, now: str) -> "AgentRuntimeState":
        if not isinstance(raw, dict):
            return cls(updated_at=now)
        return cls(
            status=str(raw.get("status") or "idle"),
            queue_size=int(raw.get("queue_size") or 0),
            success_count=int(raw.get("success_count") or 0),
            failure_count=int(raw.get("failure_count") or 0),
            last_output=str(raw.get("last_output") or ""),
            last_error=str(raw.get("last_error") or ""),
            updated_at=str(raw.get("updated_at") or now),
        )

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass
class ExternalAgentProcessState:
    pid: int
    name: str
    backend: str
    session_hint: str = ""
    command_line: str = ""

    @classmethod
    def from_dict(cls, raw: object) -> "ExternalAgentProcessState | None":
        if not isinstance(raw, dict):
            return None
        pid = int(raw.get("pid") or 0)
        if pid <= 0:
            return None
        return cls(
            pid=pid,
            name=str(raw.get("name") or ""),
            backend=str(raw.get("backend") or "").strip().lower() or "unknown",
            session_hint=str(raw.get("session_hint") or ""),
            command_line=str(raw.get("command_line") or ""),
        )

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass
class RuntimeSnapshot:
    hub_running: bool
    bridge_running: bool
    hub_pid: int | None
    bridge_pid: int | None
    codex_processes: list[str]
    log_dir: str

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass
class CheckSnapshot:
    key: str
    label: str
    ok: bool
    detail: str

    @classmethod
    def from_result(cls, raw: object) -> "CheckSnapshot | None":
        if raw is None:
            return None
        key = str(getattr(raw, "key", "") or "").strip()
        label = str(getattr(raw, "label", "") or "").strip()
        if not key or not label:
            return None
        return cls(
            key=key,
            label=label,
            ok=bool(getattr(raw, "ok", False)),
            detail=str(getattr(raw, "detail", "") or ""),
        )

    @classmethod
    def from_dict(cls, raw: object) -> "CheckSnapshot | None":
        if not isinstance(raw, dict):
            return None
        key = str(raw.get("key") or "").strip()
        label = str(raw.get("label") or "").strip()
        if not key or not label:
            return None
        return cls(
            key=key,
            label=label,
            ok=bool(raw.get("ok", False)),
            detail=str(raw.get("detail") or ""),
        )

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass
class HubTask:
    id: str
    agent_id: str
    agent_name: str
    backend: str
    source: str
    sender_id: str
    prompt: str
    status: str
    created_at: str
    started_at: str = ""
    finished_at: str = ""
    output: str = ""
    error: str = ""
    session_id: str = ""
    session_name: str = ""

    @classmethod
    def from_dict(cls, raw: object, *, default_backend: str) -> "HubTask | None":
        if not isinstance(raw, dict):
            return None
        task_id = str(raw.get("id") or "").strip()
        agent_id = str(raw.get("agent_id") or raw.get("agent_name") or "").strip()
        created_at = str(raw.get("created_at") or "").strip()
        if not task_id:
            return None
        return cls(
            id=task_id,
            agent_id=agent_id or "main",
            agent_name=str(raw.get("agent_name") or agent_id).strip() or agent_id,
            backend=str(raw.get("backend") or default_backend).strip() or default_backend,
            source=str(raw.get("source") or "desktop").strip() or "desktop",
            sender_id=str(raw.get("sender_id") or "").strip(),
            prompt=str(raw.get("prompt") or ""),
            status=str(raw.get("status") or "queued").strip() or "queued",
            created_at=created_at,
            started_at=str(raw.get("started_at") or "").strip(),
            finished_at=str(raw.get("finished_at") or "").strip(),
            output=str(raw.get("output") or ""),
            error=str(raw.get("error") or ""),
            session_id=str(raw.get("session_id") or "").strip(),
            session_name=str(raw.get("session_name") or "").strip(),
        )

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass
class HubAgentSnapshot:
    id: str
    name: str
    workdir: str
    session_file: str
    backend: str
    model: str = ""
    prompt_prefix: str = ""
    enabled: bool = True
    runtime: AgentRuntimeState = field(default_factory=AgentRuntimeState)

    @classmethod
    def from_dict(cls, raw: object, *, now: str) -> "HubAgentSnapshot | None":
        if not isinstance(raw, dict):
            return None
        agent_id = str(raw.get("id") or "").strip()
        if not agent_id:
            return None
        return cls(
            id=agent_id,
            name=str(raw.get("name") or agent_id).strip() or agent_id,
            workdir=str(raw.get("workdir") or ""),
            session_file=str(raw.get("session_file") or ""),
            backend=str(raw.get("backend") or ""),
            model=str(raw.get("model") or ""),
            prompt_prefix=str(raw.get("prompt_prefix") or ""),
            enabled=bool(raw.get("enabled", True)),
            runtime=AgentRuntimeState.from_dict(raw.get("runtime"), now=now),
        )

    def to_dict(self) -> JsonObject:
        payload = asdict(self)
        payload["runtime"] = self.runtime.to_dict()
        return payload


@dataclass
class HubStateSnapshot:
    generated_at: str = ""
    agents: list[HubAgentSnapshot] = field(default_factory=list)
    tasks: list[HubTask] = field(default_factory=list)
    external_agent_processes: list[ExternalAgentProcessState] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: object, *, default_backend: str, now: str) -> "HubStateSnapshot":
        if not isinstance(raw, dict):
            return cls()
        agents = [
            agent
            for item in (raw.get("agents") or [])
            if (agent := HubAgentSnapshot.from_dict(item, now=now)) is not None
        ]
        tasks = [
            task
            for item in (raw.get("tasks") or [])
            if (task := HubTask.from_dict(item, default_backend=default_backend)) is not None
        ]
        external_agent_processes = [
            process
            for item in (raw.get("external_agent_processes") or [])
            if (process := ExternalAgentProcessState.from_dict(item)) is not None
        ]
        return cls(
            generated_at=str(raw.get("generated_at") or ""),
            agents=agents,
            tasks=tasks,
            external_agent_processes=external_agent_processes,
        )

    def to_dict(self) -> JsonObject:
        return {
            "generated_at": self.generated_at,
            "agents": [agent.to_dict() for agent in self.agents],
            "tasks": [task.to_dict() for task in self.tasks],
            "external_agent_processes": [process.to_dict() for process in self.external_agent_processes],
        }


@dataclass
class WeixinSessionMeta:
    backend: str
    created_at: str
    updated_at: str

    @classmethod
    def from_dict(
        cls,
        raw: object,
        *,
        default_backend: str,
        now: str,
        normalize_backend: Callable[[str], str],
    ) -> "WeixinSessionMeta":
        if not isinstance(raw, dict):
            return cls(
                backend=normalize_backend(default_backend),
                created_at=now,
                updated_at=now,
            )
        return cls(
            backend=normalize_backend(str(raw.get("backend") or default_backend)),
            created_at=str(raw.get("created_at") or now),
            updated_at=str(raw.get("updated_at") or now),
        )

    def touch(self, now: str, backend: str | None = None) -> None:
        if backend is not None:
            self.backend = backend
        self.updated_at = now

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass
class WeixinConversationBinding:
    current_session: str = "default"
    sessions: dict[str, WeixinSessionMeta] = field(default_factory=dict)

    @classmethod
    def create(cls, *, default_backend: str, now: str) -> "WeixinConversationBinding":
        return cls(
            current_session="default",
            sessions={
                "default": WeixinSessionMeta(
                    backend=default_backend,
                    created_at=now,
                    updated_at=now,
                )
            },
        )

    @classmethod
    def from_dict(
        cls,
        raw: object,
        *,
        default_backend: str,
        now: str,
        normalize_backend: Callable[[str], str],
    ) -> "WeixinConversationBinding":
        if not isinstance(raw, dict):
            return cls.create(default_backend=default_backend, now=now)
        sessions: dict[str, WeixinSessionMeta] = {}
        raw_sessions = raw.get("sessions")
        if isinstance(raw_sessions, dict):
            for name, meta in raw_sessions.items():
                session_name = str(name or "").strip()
                if not session_name:
                    continue
                sessions[session_name] = WeixinSessionMeta.from_dict(
                    meta,
                    default_backend=default_backend,
                    now=now,
                    normalize_backend=normalize_backend,
                )
        current_session = str(raw.get("current_session") or "default").strip() or "default"
        if not sessions:
            sessions["default"] = WeixinSessionMeta(
                backend=normalize_backend(default_backend),
                created_at=now,
                updated_at=now,
            )
        if current_session not in sessions:
            sessions[current_session] = WeixinSessionMeta(
                backend=normalize_backend(default_backend),
                created_at=now,
                updated_at=now,
            )
        return cls(current_session=current_session, sessions=sessions)

    def ensure_session(
        self,
        session_name: str,
        *,
        default_backend: str,
        now: str,
        normalize_backend: Callable[[str], str],
    ) -> WeixinSessionMeta:
        cleaned_name = session_name.strip() or "default"
        session = self.sessions.get(cleaned_name)
        if session is None:
            session = WeixinSessionMeta(
                backend=normalize_backend(default_backend),
                created_at=now,
                updated_at=now,
            )
            self.sessions[cleaned_name] = session
        self.current_session = cleaned_name
        return session

    def get_current_session(
        self,
        *,
        default_backend: str,
        now: str,
        normalize_backend: Callable[[str], str],
    ) -> tuple[str, WeixinSessionMeta]:
        session = self.ensure_session(
            self.current_session,
            default_backend=default_backend,
            now=now,
            normalize_backend=normalize_backend,
        )
        return self.current_session, session

    def to_dict(self) -> JsonObject:
        return {
            "current_session": self.current_session,
            "sessions": {name: meta.to_dict() for name, meta in self.sessions.items()},
        }


@dataclass
class WeixinBridgeRuntimeState:
    started_at: str
    last_poll_at: str = ""
    last_message_at: str = ""
    last_sender_id: str = ""
    last_error: str = ""
    handled_messages: int = 0
    failed_messages: int = 0
    managed_conversations: int = 0
    account_file: str = ""
    sync_file: str = ""
    using_local_account_storage: bool = True

    @classmethod
    def create(cls, *, now: str, managed_conversations: int, account_file: str, sync_file: str) -> "WeixinBridgeRuntimeState":
        return cls(
            started_at=now,
            managed_conversations=managed_conversations,
            account_file=account_file,
            sync_file=sync_file,
        )

    @classmethod
    def from_dict(cls, raw: object) -> "WeixinBridgeRuntimeState":
        if not isinstance(raw, dict):
            return cls(started_at="")
        return cls(
            started_at=str(raw.get("started_at") or ""),
            last_poll_at=str(raw.get("last_poll_at") or ""),
            last_message_at=str(raw.get("last_message_at") or ""),
            last_sender_id=str(raw.get("last_sender_id") or ""),
            last_error=str(raw.get("last_error") or ""),
            handled_messages=int(raw.get("handled_messages") or 0),
            failed_messages=int(raw.get("failed_messages") or 0),
            managed_conversations=int(raw.get("managed_conversations") or 0),
            account_file=str(raw.get("account_file") or ""),
            sync_file=str(raw.get("sync_file") or ""),
            using_local_account_storage=bool(raw.get("using_local_account_storage", True)),
        )

    def sync_files(self, *, managed_conversations: int, account_file: str, sync_file: str) -> None:
        self.managed_conversations = managed_conversations
        self.account_file = account_file
        self.sync_file = sync_file

    def mark_poll(self, *, now: str) -> None:
        self.last_poll_at = now

    def mark_message(self, *, now: str, sender_id: str) -> None:
        self.last_message_at = now
        self.last_sender_id = sender_id

    def record_handled(self) -> None:
        self.handled_messages += 1

    def record_failed(self) -> None:
        self.failed_messages += 1

    def set_error(self, message: str) -> None:
        self.last_error = message

    def clear_error(self) -> None:
        self.last_error = ""

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass
class IpcRequestEnvelope:
    id: str
    action: str
    payload: JsonObject = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: object) -> "IpcRequestEnvelope | None":
        if not isinstance(raw, dict):
            return None
        request_id = str(raw.get("id") or "").strip()
        action = str(raw.get("action") or "").strip()
        payload = raw.get("payload")
        if not request_id or not action or not isinstance(payload, dict):
            return None
        return cls(id=request_id, action=action, payload=payload)

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "action": self.action,
            "payload": dict(self.payload),
        }


@dataclass
class IpcResponseEnvelope:
    ok: bool
    error: str = ""
    payload: JsonObject = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: object) -> "IpcResponseEnvelope | None":
        if not isinstance(raw, dict):
            return None
        ok = bool(raw.get("ok", False))
        error = str(raw.get("error") or "")
        payload = {key: value for key, value in raw.items() if key not in {"ok", "error"}}
        return cls(ok=ok, error=error, payload=payload)

    def to_dict(self) -> JsonObject:
        data = {"ok": self.ok}
        if self.error:
            data["error"] = self.error
        data.update(self.payload)
        return data
