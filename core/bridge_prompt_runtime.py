from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.bridge_runtime import BridgeCommandResult, BridgePromptDecision, IncomingBridgeMessage


class BridgePromptRuntime:
    def __init__(
        self,
        *,
        native_menu: Any,
        media_control: Any,
        handle_control: Callable[[str, str], tuple[str, bool]],
        send_reply: Callable[[dict[str, Any], str], None],
        save_conversations: Callable[[], None],
        unsupported_agent_slash_reply: Callable[[str], str],
        unsupported_bridge_command_reply: Callable[[str], str],
        render_local_passthrough: Callable[[str, Any, str], str | None] | None = None,
        on_handled: Callable[[IncomingBridgeMessage, str, str, Any], None] | None = None,
        save_state: Callable[[], None] | None = None,
        reject_unknown_bridge_slash: bool = False,
        reject_unknown_passthrough_slash: bool = True,
        submit_raw_passthrough: bool = False,
    ) -> None:
        self.native_menu = native_menu
        self.media_control = media_control
        self.handle_control = handle_control
        self.send_reply = send_reply
        self.save_conversations = save_conversations
        self.unsupported_agent_slash_reply = unsupported_agent_slash_reply
        self.unsupported_bridge_command_reply = unsupported_bridge_command_reply
        self.render_local_passthrough = render_local_passthrough
        self.on_handled = on_handled or (lambda _message, _route, _command, _session: None)
        self.save_state = save_state or (lambda: None)
        self.reject_unknown_bridge_slash = reject_unknown_bridge_slash
        self.reject_unknown_passthrough_slash = reject_unknown_passthrough_slash
        self.submit_raw_passthrough = submit_raw_passthrough

    def prepare(
        self,
        message: IncomingBridgeMessage,
        *,
        session_name: str,
        session_meta: Any,
        session: Any = None,
    ) -> BridgePromptDecision:
        text = str(message.text or "")
        passthrough_prompt = self.native_menu.passthrough_prompt(text)
        session_context = session if session is not None else session_meta

        if self.native_menu.has_active_menu(session_meta) and (passthrough_prompt is None or not self.native_menu.is_special_command(passthrough_prompt)):
            result = self.native_menu.handle_reply(session_name, session_meta, text)
            if result.handled:
                self._reply_if_present(message.reply_target, result)
                self.save_conversations()
                self._mark_handled(message, "native_menu_reply", self._active_menu_command(session_meta), session_context)
                return BridgePromptDecision(handled=True)

        if passthrough_prompt is None:
            media_result = self.media_control.handle(message.reply_target, text)
            if media_result.handled:
                self._reply_if_present(message.reply_target, media_result)
                self._mark_handled(message, "media_command", self._command_text(text), session_context)
                return BridgePromptDecision(handled=True)

            reply, handled = self.handle_control(message.sender_id, text)
            if handled:
                if reply:
                    self.send_reply(message.reply_target, reply)
                self._mark_handled(message, "control_command", self._command_text(text), session_context, count_handled=bool(reply))
                return BridgePromptDecision(handled=True)

            cleaned = text.strip()
            if self.reject_unknown_bridge_slash and cleaned.startswith("/") and not cleaned.startswith("//"):
                self.send_reply(message.reply_target, self.unsupported_bridge_command_reply(cleaned))
                self._mark_handled(message, "unsupported_bridge_command", self._command_text(cleaned), session_context)
                return BridgePromptDecision(handled=True)

            return BridgePromptDecision(prompt=text.strip())

        if self.render_local_passthrough is not None:
            local_reply = self.render_local_passthrough(session_name, session_meta, passthrough_prompt)
            if local_reply is not None:
                self.send_reply(message.reply_target, local_reply)
                self._mark_handled(message, "passthrough_local_status", passthrough_prompt.strip().lower(), session_context)
                return BridgePromptDecision(handled=True)

        reply, handled = self.handle_control(message.sender_id, text)
        if handled:
            if reply:
                self.send_reply(message.reply_target, reply)
            self._mark_handled(message, "passthrough_control_command", passthrough_prompt.strip().lower(), session_context, count_handled=bool(reply))
            return BridgePromptDecision(handled=True)

        native_start = self.native_menu.start(session_name, session_meta, passthrough_prompt)
        if native_start.handled:
            self._reply_if_present(message.reply_target, native_start)
            self.save_conversations()
            self._mark_handled(message, "native_menu_start", passthrough_prompt.strip().lower(), session_context)
            return BridgePromptDecision(handled=True)

        if self.reject_unknown_passthrough_slash and self.native_menu.looks_like_agent_slash_command(passthrough_prompt):
            self.send_reply(message.reply_target, self.unsupported_agent_slash_reply(passthrough_prompt.strip()))
            self._mark_handled(message, "passthrough_unsupported", passthrough_prompt.strip().lower(), session_context)
            return BridgePromptDecision(handled=True)

        submitted_prompt = text.strip() if self.submit_raw_passthrough else passthrough_prompt
        return BridgePromptDecision(prompt=submitted_prompt, passthrough=True)

    def prepare_for_session(self, message: IncomingBridgeMessage, session: dict[str, Any]) -> BridgePromptDecision:
        return self.prepare(
            message,
            session_name=str(session["session_name"] or ""),
            session_meta=session["session_meta"],
            session=session,
        )

    def _reply_if_present(self, reply_target: dict[str, Any], result: BridgeCommandResult) -> None:
        if result.reply:
            self.send_reply(reply_target, result.reply)

    def _mark_handled(
        self,
        message: IncomingBridgeMessage,
        route: str,
        command: str,
        session: Any,
        *,
        count_handled: bool = True,
    ) -> None:
        self.on_handled(message, route, command, session)
        if count_handled:
            self.save_state()

    @staticmethod
    def _command_text(text: str) -> str:
        return str(text or "").strip().split(maxsplit=1)[0].lower()

    @staticmethod
    def _active_menu_command(session_meta: Any) -> str:
        return str(getattr(session_meta, "native_menu_command", "") or "").strip().lower()
