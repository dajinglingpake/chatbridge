from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from core.bridge_command_catalog import parse_bridge_command
from core.bridge_runtime import BridgeCommandResult
from core.state_models import HubTask


class _BridgeSessionControlConfig(Protocol):
    default_backend: str
    backend_id: str

class _BridgeAgentInfo(Protocol):
    id: str

class _BridgeSessionControlAdapter(Protocol):
    config: _BridgeSessionControlConfig

    def _ensure_conversation(self, sender_id: str) -> Any: ...
    def _remove_conversation(self, sender_id: str) -> None: ...
    def _save_conversations(self) -> None: ...
    def _now_iso(self) -> str: ...
    def _normalize_backend(self, value: str) -> str: ...
    def _t(self, key: str, **kwargs: Any) -> str: ...
    def _new_session_meta(
        self,
        backend: Any = "",
        *,
        workdir: str = "",
        model: str = "",
        reasoning_effort: str = "",
        permission_mode: str = "",
    ) -> Any: ...
    def _allocate_session_name(self, binding: Any, requested: str) -> str: ...
    def _sanitize_session_name(self, requested: str, *, fallback: str) -> str: ...
    def _sanitize_project_name(self, requested: str) -> str: ...
    def _split_named_path_args(self, raw: str) -> tuple[str, str]: ...
    def _resolve_fallback_session_target(self, binding: Any) -> str: ...
    def _render_context(self, session_name: str, session_meta: Any) -> str: ...
    def _render_session_list(
        self,
        sender_id: str,
        binding: Any,
        *,
        page: int = 1,
        query: str = "",
        project_path: str | None = "",
        scope_label: str = "",
    ) -> str: ...
    def _bulk_delete_sessions(self, binding: Any, raw_names: str) -> tuple[str, bool]: ...
    def _clear_empty_sessions(self, sender_id: str, binding: Any) -> tuple[str, bool]: ...
    def _render_session_preview(self, sender_id: str, session_name: str, binding: Any) -> str: ...
    def _render_session_history(self, sender_id: str, session_name: str, binding: Any) -> str: ...
    def _export_session_history(self, sender_id: str, session_name: str, binding: Any) -> tuple[str, bool]: ...
    def _render_project_file_preview(self, raw_path: str) -> str: ...
    def _render_recent_events(self, sender_id: str, *, limit: int) -> str: ...
    def _resolve_session_workdir(self, session_meta: Any) -> str: ...
    def _resolve_session_model(self, session_meta: Any) -> str: ...
    def _render_model_status(self, session_name: str, session_meta: Any) -> str: ...
    def _render_project_status(self, session_name: str, session_meta: Any) -> str: ...
    def _load_registered_project_spaces(self) -> dict[str, str]: ...
    def _save_registered_project_spaces(self, spaces: dict[str, str]) -> None: ...
    def _resolve_project_workdir(self, project_arg: str) -> str | None: ...
    def _render_project_list(self, session_meta: Any) -> str: ...
    def _render_project_session_list(self, sender_id: str, binding: Any, project_arg: str) -> tuple[str, bool]: ...
    def _render_agent_details(self, agent_id: str) -> str: ...
    def _render_agent_list(self) -> str: ...
    def _render_agent_command_help(self) -> str: ...
    def _load_agents(self) -> list[_BridgeAgentInfo]: ...
    def _set_backend_agent(self, agent_id: str) -> None: ...
    def _clear_current_agent_session(self, sender_id: str, current_session: str) -> str: ...

class BridgeSessionControlRuntime:
    def __init__(
        self,
        *,
        adapter: _BridgeSessionControlAdapter,
        app_dir: Path,
        supported_backends: set[str],
    ) -> None:
        self.adapter = adapter
        self.app_dir = app_dir
        self.supported_backends = supported_backends

    def handle(self, sender_id: str, text: str) -> BridgeCommandResult:
        parsed = parse_bridge_command(text)
        if parsed is None or parsed.is_passthrough:
            return BridgeCommandResult(False)
        raw = parsed.raw
        parts = list(parsed.parts)
        command = parsed.command
        bridge = self.adapter

        binding = bridge._ensure_conversation(sender_id)
        current_session, current_meta = binding.get_current_session(
            default_backend=bridge.config.default_backend,
            now=bridge._now_iso(),
            normalize_backend=bridge._normalize_backend,
        )
        sessions = binding.sessions

        if command == "/new":
            requested = parts[1].strip() if len(parts) >= 2 else ""
            session_name = bridge._allocate_session_name(binding, requested or "session")
            sessions[session_name] = bridge._new_session_meta(
                current_meta.backend,
                workdir=current_meta.workdir,
                model=current_meta.model,
                reasoning_effort=current_meta.reasoning_effort,
                permission_mode=current_meta.permission_mode,
            )
            binding.current_session = session_name
            binding.last_regular_session = session_name
            bridge._save_conversations()
            return BridgeCommandResult(True, bridge._t("bridge.session.created", session=session_name, backend=sessions[session_name].backend))

        if command == "/context":
            return BridgeCommandResult(True, bridge._render_context(current_session, current_meta))

        if command == "/list":
            return BridgeCommandResult(True, bridge._render_session_list(sender_id, binding))

        if command == "/sessions":
            if len(parts) < 2:
                return BridgeCommandResult(True, bridge._render_session_list(sender_id, binding))
            subcommand = parts[1].strip()
            lowered_subcommand = subcommand.lower()
            if lowered_subcommand == "all":
                return BridgeCommandResult(
                    True,
                    bridge._render_session_list(sender_id, binding, project_path=None, scope_label=bridge._t("bridge.session.list.scope.all")),
                )
            if lowered_subcommand in {"search", "find"}:
                keyword = parts[2].strip() if len(parts) >= 3 else ""
                return BridgeCommandResult(True, bridge._render_session_list(sender_id, binding, query=keyword))
            if lowered_subcommand in {"delete", "remove"}:
                raw_names = parts[2].strip() if len(parts) >= 3 else ""
                reply, handled = bridge._bulk_delete_sessions(binding, raw_names)
                return BridgeCommandResult(handled, reply)
            if lowered_subcommand == "clear-empty":
                reply, handled = bridge._clear_empty_sessions(sender_id, binding)
                return BridgeCommandResult(handled, reply)
            try:
                page = int(subcommand)
            except ValueError:
                return BridgeCommandResult(True, bridge._t("bridge.sessions.usage"))
            return BridgeCommandResult(True, bridge._render_session_list(sender_id, binding, page=page))

        if command == "/preview":
            session_name = parts[1].strip() if len(parts) >= 2 else binding.current_session
            if not session_name:
                return BridgeCommandResult(True, bridge._t("bridge.session.preview.usage"))
            if session_name not in sessions:
                return BridgeCommandResult(True, bridge._t("bridge.session.preview.not_found", session=session_name))
            return BridgeCommandResult(True, bridge._render_session_preview(sender_id, session_name, binding))

        if command == "/history":
            session_name = parts[1].strip() if len(parts) >= 2 else binding.current_session
            if not session_name:
                return BridgeCommandResult(True, bridge._t("bridge.session.history.usage"))
            if session_name not in sessions:
                return BridgeCommandResult(True, bridge._t("bridge.session.preview.not_found", session=session_name))
            return BridgeCommandResult(True, bridge._render_session_history(sender_id, session_name, binding))

        if command == "/export":
            session_name = parts[1].strip() if len(parts) >= 2 else binding.current_session
            if not session_name:
                return BridgeCommandResult(True, bridge._t("bridge.session.export.usage"))
            if session_name not in sessions:
                return BridgeCommandResult(True, bridge._t("bridge.session.preview.not_found", session=session_name))
            reply, handled = bridge._export_session_history(sender_id, session_name, binding)
            return BridgeCommandResult(handled, reply)

        if command == "/showfile":
            raw_path = raw[len(command) :].strip()
            return BridgeCommandResult(True, bridge._render_project_file_preview(raw_path))

        if command == "/events":
            raw_limit = parts[1].strip() if len(parts) >= 2 else ""
            try:
                limit = int(raw_limit) if raw_limit else 5
            except ValueError:
                return BridgeCommandResult(True, bridge._t("bridge.events.usage"))
            return BridgeCommandResult(True, bridge._render_recent_events(sender_id, limit=limit))

        if command == "/use":
            if len(parts) < 2:
                return BridgeCommandResult(True, bridge._t("bridge.use.usage"))
            session_name = parts[1].strip()
            if session_name not in sessions:
                return BridgeCommandResult(True, bridge._t("bridge.session.not_found", session=session_name))
            binding.current_session = session_name
            binding.last_regular_session = session_name
            bridge._save_conversations()
            return BridgeCommandResult(True, bridge._t("bridge.session.switched", session=session_name, backend=sessions[session_name].backend))

        if command == "/rename":
            source_session = current_session
            if len(parts) < 2:
                return BridgeCommandResult(True, bridge._t("bridge.rename.usage"))
            requested_name = parts[1].strip()
            if len(parts) >= 3:
                source_session = requested_name
                requested_name = parts[2].strip()
            if not source_session or not requested_name:
                return BridgeCommandResult(True, bridge._t("bridge.rename.usage"))
            if source_session not in sessions:
                return BridgeCommandResult(True, bridge._t("bridge.session.not_found", session=source_session))
            target_session = bridge._sanitize_session_name(requested_name, fallback=source_session)
            if target_session != source_session and target_session in sessions:
                return BridgeCommandResult(True, bridge._t("bridge.session.rename.exists", session=target_session))
            if target_session == source_session:
                return BridgeCommandResult(True, bridge._t("bridge.session.renamed", old=source_session, new=target_session, backend=sessions[target_session].backend))
            session_meta = sessions.pop(source_session)
            session_meta.touch(bridge._now_iso())
            sessions[target_session] = session_meta
            if binding.current_session == source_session:
                binding.current_session = target_session
            if binding.last_regular_session == source_session:
                binding.last_regular_session = target_session
            bridge._save_conversations()
            return BridgeCommandResult(True, bridge._t("bridge.session.renamed", old=source_session, new=target_session, backend=session_meta.backend))

        if command in {"/delete", "/remove"}:
            if len(parts) < 2:
                return BridgeCommandResult(True, bridge._t("bridge.delete.usage"))
            target_session = parts[1].strip()
            if not target_session:
                return BridgeCommandResult(True, bridge._t("bridge.delete.usage"))
            if target_session not in sessions:
                return BridgeCommandResult(True, bridge._t("bridge.session.not_found", session=target_session))
            if target_session == "default":
                return BridgeCommandResult(True, bridge._t("bridge.session.default_delete_blocked"))
            sessions.pop(target_session, None)
            if binding.current_session == target_session:
                next_session = bridge._resolve_fallback_session_target(binding) or "default"
                binding.current_session = next_session
                sessions.setdefault("default", bridge._new_session_meta())
            if binding.last_regular_session == target_session:
                binding.last_regular_session = bridge._resolve_fallback_session_target(binding) or "default"
            bridge._save_conversations()
            return BridgeCommandResult(True, bridge._t("bridge.session.deleted", session=target_session, current=binding.current_session or "default"))

        if command == "/backend":
            if len(parts) < 2:
                return BridgeCommandResult(True, bridge._t("bridge.backend.current", session=current_session, backend=current_meta.backend))
            requested_backend = parts[1].strip().lower()
            if requested_backend not in self.supported_backends:
                return BridgeCommandResult(True, bridge._t("bridge.backend.usage"))
            current_meta.touch(bridge._now_iso(), backend=requested_backend)
            sessions[current_session] = current_meta
            bridge._save_conversations()
            return BridgeCommandResult(True, bridge._t("bridge.backend.switched", backend=requested_backend, session=current_session))

        if command == "/model":
            if len(parts) < 2:
                return BridgeCommandResult(True, bridge._render_model_status(current_session, current_meta))
            model_arg = parts[1].strip()
            if not model_arg:
                return BridgeCommandResult(True, bridge._render_model_status(current_session, current_meta))
            if model_arg.lower() == "reset":
                current_meta.touch(bridge._now_iso(), model="", reasoning_effort="")
                sessions[current_session] = current_meta
                bridge._save_conversations()
                return BridgeCommandResult(True, bridge._t("bridge.model.reset", session=current_session, model=bridge._resolve_session_model(current_meta)))
            current_meta.touch(bridge._now_iso(), model=model_arg, reasoning_effort="")
            sessions[current_session] = current_meta
            bridge._save_conversations()
            return BridgeCommandResult(True, bridge._t("bridge.model.switched", session=current_session, model=bridge._resolve_session_model(current_meta)))

        if command == "/project":
            return self._handle_project(sender_id, binding, current_session, current_meta, parts)

        if command == "/agent":
            return self._handle_agent(parts)

        if command == "/clear":
            return BridgeCommandResult(True, bridge._clear_current_agent_session(sender_id, current_session))

        if command in {"/close", "/end"}:
            if current_session == "default":
                return BridgeCommandResult(True, bridge._t("bridge.session.default_close_blocked"))
            sessions.pop(current_session, None)
            binding.current_session = bridge._resolve_fallback_session_target(binding) or "default"
            sessions.setdefault("default", bridge._new_session_meta())
            binding.last_regular_session = binding.current_session
            bridge._save_conversations()
            return BridgeCommandResult(True, bridge._t("bridge.session.closed", session=current_session))

        if command == "/reset":
            bridge._remove_conversation(sender_id)
            reset = bridge._ensure_conversation(sender_id)
            reset_session, reset_meta = reset.get_current_session(
                default_backend=bridge.config.default_backend,
                now=bridge._now_iso(),
                normalize_backend=bridge._normalize_backend,
            )
            return BridgeCommandResult(True, bridge._t("bridge.session.reset", session=reset_session, backend=reset_meta.backend))

        return BridgeCommandResult(False)

    def _handle_project(self, sender_id: str, binding: Any, current_session: str, current_meta: Any, parts: list[str]) -> BridgeCommandResult:
        bridge = self.adapter
        if len(parts) < 2:
            return BridgeCommandResult(True, bridge._render_project_status(current_session, current_meta))
        project_arg = parts[1].strip()
        lowered_project_arg = project_arg.lower()
        sessions = binding.sessions
        if lowered_project_arg == "add":
            name, path_arg = bridge._split_named_path_args(parts[2].strip() if len(parts) >= 3 else "")
            if not name or not path_arg:
                return BridgeCommandResult(True, bridge._t("bridge.project.add.usage"))
            project_name = bridge._sanitize_project_name(name)
            if not project_name:
                return BridgeCommandResult(True, bridge._t("bridge.project.add.usage"))
            candidate = Path(path_arg).expanduser()
            if not candidate.is_absolute():
                candidate = self.app_dir / candidate
            if not candidate.exists() or not candidate.is_dir():
                return BridgeCommandResult(True, bridge._t("bridge.project.not_found", project=path_arg))
            spaces = bridge._load_registered_project_spaces()
            resolved = str(candidate.resolve())
            spaces[project_name] = resolved
            bridge._save_registered_project_spaces(spaces)
            return BridgeCommandResult(True, bridge._t("bridge.project.added", name=project_name, path=resolved))
        if lowered_project_arg in {"remove", "delete"}:
            name = parts[2].strip() if len(parts) >= 3 else ""
            project_name = bridge._sanitize_project_name(name)
            if not project_name:
                return BridgeCommandResult(True, bridge._t("bridge.project.remove.usage"))
            spaces = bridge._load_registered_project_spaces()
            removed_path = spaces.pop(project_name, "")
            if not removed_path:
                return BridgeCommandResult(True, bridge._t("bridge.project.remove.not_found", name=project_name))
            bridge._save_registered_project_spaces(spaces)
            return BridgeCommandResult(True, bridge._t("bridge.project.removed", name=project_name))
        if lowered_project_arg == "list":
            return BridgeCommandResult(True, bridge._render_project_list(current_meta))
        if lowered_project_arg == "sessions":
            target_project = parts[2].strip() if len(parts) >= 3 else ""
            reply, handled = bridge._render_project_session_list(sender_id, binding, target_project)
            return BridgeCommandResult(handled, reply)
        if lowered_project_arg == "reset":
            current_meta.touch(bridge._now_iso(), workdir="")
            sessions[current_session] = current_meta
            bridge._save_conversations()
            return BridgeCommandResult(True, bridge._t("bridge.project.reset", session=current_session, workdir=bridge._resolve_session_workdir(current_meta)))
        resolved_workdir = bridge._resolve_project_workdir(project_arg)
        if resolved_workdir is None:
            return BridgeCommandResult(True, bridge._t("bridge.project.not_found", project=project_arg))
        current_meta.touch(bridge._now_iso(), workdir=resolved_workdir)
        sessions[current_session] = current_meta
        bridge._save_conversations()
        return BridgeCommandResult(True, bridge._t("bridge.project.switched", session=current_session, workdir=resolved_workdir))

    def _handle_agent(self, parts: list[str]) -> BridgeCommandResult:
        bridge = self.adapter
        if len(parts) < 2:
            return BridgeCommandResult(True, bridge._render_agent_details(bridge.config.backend_id))
        subcommand = parts[1].strip().lower()
        if subcommand == "list":
            return BridgeCommandResult(True, bridge._render_agent_list())
        if subcommand in {"help", "commands"}:
            return BridgeCommandResult(True, bridge._render_agent_command_help())
        requested_agent = parts[1].strip()
        known_agents = {agent.id for agent in bridge._load_agents()}
        if known_agents and requested_agent not in known_agents:
            return BridgeCommandResult(True, bridge._t("bridge.agent.not_found", agent=requested_agent))
        bridge._set_backend_agent(requested_agent)
        return BridgeCommandResult(True, bridge._t("bridge.agent.switched", agent=requested_agent))
