from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from core.bridge_command_catalog import parse_bridge_command
from core.bridge_runtime import BridgeCommandResult
from core.codex_model_catalog import display_model, display_reasoning_effort

PERMISSION_MODE_PRESETS: tuple[tuple[str, str], ...] = (
    ("default", "Default"),
    ("full-access", "Full Access"),
)
SPECIAL_NATIVE_MENU_COMMANDS = frozenset({"/model", "/permission", "/permissions"})


class _NativeMenuSessionMeta(Protocol):
    native_menu_command: str
    native_menu_stage: str
    native_menu_options: list[str]
    native_menu_context: str
    reasoning_effort: str
    permission_mode: str

    def set_native_menu(self, *, command: str, stage: str, options: list[str], context: str = "") -> None: ...
    def clear_native_menu(self) -> None: ...
    def touch(
        self,
        now: str,
        backend: str | None = None,
        workdir: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        permission_mode: str | None = None,
    ) -> None: ...


class BridgeNativeMenuRuntime:
    def __init__(
        self,
        *,
        translate: Callable[..., str],
        now: Callable[[], str],
        load_model_catalog: Callable[[], list[dict[str, Any]]],
        resolve_session_model: Callable[[_NativeMenuSessionMeta], str],
        resolve_session_permission_mode: Callable[[_NativeMenuSessionMeta], str],
        display_permission_mode: Callable[[str], str] | None = None,
    ) -> None:
        self.translate = translate
        self.now = now
        self.load_model_catalog = load_model_catalog
        self.resolve_session_model = resolve_session_model
        self.resolve_session_permission_mode = resolve_session_permission_mode
        self.display_permission_mode = display_permission_mode or self._display_permission_mode

    def has_active_menu(self, session_meta: _NativeMenuSessionMeta) -> bool:
        return bool(session_meta.native_menu_command and session_meta.native_menu_options)

    def is_special_command(self, prompt: str | None) -> bool:
        return str(prompt or "").strip().lower() in SPECIAL_NATIVE_MENU_COMMANDS

    def handle_reply(self, session_name: str, session_meta: _NativeMenuSessionMeta, text: str) -> BridgeCommandResult:
        if not self.has_active_menu(session_meta):
            return BridgeCommandResult(False)
        raw = self._normalize_text(text)
        lowered = raw.lower()
        if lowered in {"取消", "cancel", "/cancel", "q", "quit"}:
            session_meta.clear_native_menu()
            session_meta.touch(self.now())
            return BridgeCommandResult(True, self.translate("bridge.native_menu.canceled", session=session_name))
        if lowered in {"返回", "back"}:
            if session_meta.native_menu_command == "/model" and session_meta.native_menu_stage == "select_reasoning":
                context = self._parse_context(session_meta)
                entries = context.get("entries") or []
                session_meta.set_native_menu(
                    command="/model",
                    stage="select_model",
                    options=[entry["slug"] for entry in entries if entry.get("slug")],
                    context=json.dumps({"entries": entries}, ensure_ascii=False),
                )
                session_meta.touch(self.now())
                return BridgeCommandResult(True, self._render_model_selection_menu(session_name, session_meta))
            session_meta.clear_native_menu()
            session_meta.touch(self.now())
            return BridgeCommandResult(True, self.translate("bridge.native_menu.canceled", session=session_name))
        if not raw.isdigit():
            return BridgeCommandResult(True, self._render_invalid(session_name, session_meta))
        option_index = int(raw) - 1
        if option_index < 0 or option_index >= len(session_meta.native_menu_options):
            return BridgeCommandResult(True, self._render_invalid(session_name, session_meta))
        selected = session_meta.native_menu_options[option_index]
        if session_meta.native_menu_command == "/model":
            reply, handled = self._apply_model_selection(session_name, session_meta, selected)
            return BridgeCommandResult(handled, reply)
        if session_meta.native_menu_command == "/permissions":
            reply, handled = self._apply_permission_selection(session_name, session_meta, selected)
            return BridgeCommandResult(handled, reply)
        session_meta.clear_native_menu()
        session_meta.touch(self.now())
        return BridgeCommandResult(True, self.translate("bridge.native_menu.canceled", session=session_name))

    def start(self, session_name: str, session_meta: _NativeMenuSessionMeta, prompt: str | None) -> BridgeCommandResult:
        command = str(prompt or "").strip().lower()
        if command == "/model":
            entries = self.load_model_catalog()
            if not entries:
                session_meta.clear_native_menu()
                session_meta.touch(self.now())
                return BridgeCommandResult(True, self.translate("bridge.native_menu.model.empty"))
            session_meta.set_native_menu(
                command="/model",
                stage="select_model",
                options=[entry["slug"] for entry in entries],
                context=json.dumps({"entries": entries}, ensure_ascii=False),
            )
            session_meta.touch(self.now())
            return BridgeCommandResult(True, self._render_model_selection_menu(session_name, session_meta))
        if command in {"/permission", "/permissions"}:
            session_meta.set_native_menu(
                command="/permissions",
                stage="select_permission",
                options=[value for value, _ in PERMISSION_MODE_PRESETS],
                context="",
            )
            session_meta.touch(self.now())
            return BridgeCommandResult(True, self._render_permission_selection_menu(session_name, session_meta))
        return BridgeCommandResult(False)

    @staticmethod
    def passthrough_prompt(text: str) -> str | None:
        parsed = parse_bridge_command(text)
        if parsed is None or not parsed.is_passthrough:
            return None
        return parsed.passthrough_prompt or "/"

    @staticmethod
    def looks_like_agent_slash_command(prompt: str | None) -> bool:
        return str(prompt or "").strip().startswith("/")

    @staticmethod
    def _normalize_text(text: str) -> str:
        return str(text or "").strip()

    def _apply_model_selection(self, session_name: str, session_meta: _NativeMenuSessionMeta, selected: str) -> tuple[str, bool]:
        context = self._parse_context(session_meta)
        entries = context.get("entries") or []
        entries_by_slug = {
            str(entry.get("slug") or "").strip(): entry
            for entry in entries
            if str(entry.get("slug") or "").strip()
        }
        if session_meta.native_menu_stage == "select_model":
            entry = entries_by_slug.get(selected)
            if entry is None:
                return self._render_invalid(session_name, session_meta), True
            reasoning_levels = [str(item).strip() for item in entry.get("reasoning_levels") or [] if str(item).strip()]
            if len(reasoning_levels) <= 1:
                chosen_effort = str(entry.get("default_reasoning") or "").strip() or (reasoning_levels[0] if reasoning_levels else "")
                session_meta.touch(self.now(), model=str(entry.get("slug") or "").strip(), reasoning_effort=chosen_effort)
                session_meta.clear_native_menu()
                return self.translate(
                    "bridge.native_menu.model.updated",
                    session=session_name,
                    model=display_model(str(entry.get("display_name") or entry.get("slug") or "")),
                    reasoning=display_reasoning_effort(chosen_effort),
                ), True
            session_meta.set_native_menu(
                command="/model",
                stage="select_reasoning",
                options=reasoning_levels,
                context=json.dumps({"entries": entries, "selected_model": str(entry.get("slug") or "").strip()}, ensure_ascii=False),
            )
            session_meta.touch(self.now())
            return self._render_reasoning_selection_menu(session_name, session_meta), True
        if session_meta.native_menu_stage == "select_reasoning":
            selected_model = str(context.get("selected_model") or "").strip()
            entry = entries_by_slug.get(selected_model)
            if entry is None:
                session_meta.clear_native_menu()
                session_meta.touch(self.now())
                return self.translate("bridge.native_menu.canceled", session=session_name), True
            session_meta.touch(self.now(), model=selected_model, reasoning_effort=selected)
            session_meta.clear_native_menu()
            return self.translate(
                "bridge.native_menu.model.updated",
                session=session_name,
                model=display_model(str(entry.get("display_name") or entry.get("slug") or "")),
                reasoning=display_reasoning_effort(selected),
            ), True
        return self._render_invalid(session_name, session_meta), True

    def _apply_permission_selection(self, session_name: str, session_meta: _NativeMenuSessionMeta, selected: str) -> tuple[str, bool]:
        session_meta.touch(self.now(), permission_mode=selected)
        session_meta.clear_native_menu()
        return self.translate(
            "bridge.native_menu.permissions.updated",
            session=session_name,
            mode=self.display_permission_mode(selected),
        ), True

    def _render_invalid(self, session_name: str, session_meta: _NativeMenuSessionMeta) -> str:
        return self.translate("bridge.native_menu.invalid") + "\n\n" + self._render_menu(session_name, session_meta)

    def _render_menu(self, session_name: str, session_meta: _NativeMenuSessionMeta) -> str:
        if session_meta.native_menu_command == "/model":
            if session_meta.native_menu_stage == "select_reasoning":
                return self._render_reasoning_selection_menu(session_name, session_meta)
            return self._render_model_selection_menu(session_name, session_meta)
        if session_meta.native_menu_command == "/permissions":
            return self._render_permission_selection_menu(session_name, session_meta)
        return self.translate("bridge.native_menu.canceled", session=session_name)

    def _render_model_selection_menu(self, session_name: str, session_meta: _NativeMenuSessionMeta) -> str:
        entries = self._parse_context(session_meta).get("entries") or []
        lines = [
            self.translate(
                "bridge.native_menu.model.title",
                session=session_name,
                current=self.resolve_session_model(session_meta),
                reasoning=display_reasoning_effort(session_meta.reasoning_effort),
            )
        ]
        for index, entry in enumerate(entries, start=1):
            model_name = display_model(str(entry.get("display_name") or entry.get("slug") or ""))
            description = str(entry.get("description") or "").strip()
            if description:
                lines.append(self.translate("bridge.native_menu.model.option.detail", index=index, model=model_name, detail=description))
            else:
                lines.append(self.translate("bridge.native_menu.model.option", index=index, model=model_name))
        lines.append(self.translate("bridge.native_menu.help"))
        return "\n".join(lines)

    def _render_reasoning_selection_menu(self, session_name: str, session_meta: _NativeMenuSessionMeta) -> str:
        context = self._parse_context(session_meta)
        selected_model = str(context.get("selected_model") or "").strip()
        entries = {
            str(entry.get("slug") or "").strip(): entry
            for entry in (context.get("entries") or [])
            if str(entry.get("slug") or "").strip()
        }
        entry = entries.get(selected_model, {})
        model_name = display_model(str(entry.get("display_name") or selected_model))
        lines = [self.translate("bridge.native_menu.reasoning.title", session=session_name, model=model_name)]
        for index, effort in enumerate(session_meta.native_menu_options, start=1):
            lines.append(self.translate("bridge.native_menu.reasoning.option", index=index, reasoning=display_reasoning_effort(effort)))
        lines.append(self.translate("bridge.native_menu.help.back"))
        return "\n".join(lines)

    def _render_permission_selection_menu(self, session_name: str, session_meta: _NativeMenuSessionMeta) -> str:
        current_mode = self.display_permission_mode(self.resolve_session_permission_mode(session_meta))
        lines = [self.translate("bridge.native_menu.permissions.title", session=session_name, current=current_mode)]
        option_labels = dict(PERMISSION_MODE_PRESETS)
        for index, option in enumerate(session_meta.native_menu_options, start=1):
            lines.append(self.translate("bridge.native_menu.permissions.option", index=index, mode=option_labels.get(option, option)))
        lines.append(self.translate("bridge.native_menu.help"))
        return "\n".join(lines)

    @staticmethod
    def _parse_context(session_meta: _NativeMenuSessionMeta) -> dict[str, Any]:
        raw = session_meta.native_menu_context.strip()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        entries: list[dict[str, Any]] = []
        for item in payload.get("entries") or []:
            if not isinstance(item, dict):
                continue
            entries.append(
                {
                    "slug": str(item.get("slug") or "").strip(),
                    "display_name": str(item.get("display_name") or "").strip(),
                    "description": str(item.get("description") or "").strip(),
                    "default_reasoning": str(item.get("default_reasoning") or "").strip(),
                    "reasoning_levels": [str(level).strip() for level in (item.get("reasoning_levels") or []) if str(level).strip()],
                }
            )
        return {"entries": entries, "selected_model": str(payload.get("selected_model") or "").strip()}

    @staticmethod
    def _display_permission_mode(mode: str) -> str:
        cleaned = str(mode or "").strip().lower()
        for value, label in PERMISSION_MODE_PRESETS:
            if value == cleaned:
                return label
        return cleaned or "Full Access"
