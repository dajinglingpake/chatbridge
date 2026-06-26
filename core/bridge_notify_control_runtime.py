from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from core.bridge_command_catalog import parse_bridge_command
from core.bridge_runtime import BridgeCommandResult


class _NotifyConfig(Protocol):
    service_notice_enabled: bool
    config_notice_enabled: bool
    task_notice_enabled: bool

    def save(self) -> None: ...


class BridgeNotifyControlRuntime:
    def __init__(
        self,
        *,
        config: _NotifyConfig,
        translate: Callable[..., str],
        send_test_notice: Callable[[], str],
        unsupported_message: str | None = None,
    ) -> None:
        self.config = config
        self.translate = translate
        self.send_test_notice = send_test_notice
        self.unsupported_message = unsupported_message

    def handle(self, _sender_id: str, text: str) -> BridgeCommandResult:
        parsed = parse_bridge_command(text)
        if parsed is None or parsed.is_passthrough or parsed.command != "/notify":
            return BridgeCommandResult(False)
        parts = list(parsed.parts)
        if len(parts) < 2:
            return BridgeCommandResult(True, self._render_current())
        desired = parts[1].strip().lower()
        if desired == "test":
            return BridgeCommandResult(True, self.translate("bridge.notify.test", summary=self.send_test_notice()))
        if desired not in {"on", "off", "service-on", "service-off", "config-on", "config-off", "task-on", "task-off"}:
            if self.unsupported_message is not None:
                return BridgeCommandResult(True, self.unsupported_message)
            return BridgeCommandResult(True, self.translate("bridge.notify.usage"))
        if desired == "on":
            self.config.service_notice_enabled = True
            self.config.config_notice_enabled = True
            self.config.task_notice_enabled = True
        elif desired == "off":
            self.config.service_notice_enabled = False
            self.config.config_notice_enabled = False
            self.config.task_notice_enabled = False
        elif desired == "service-on":
            self.config.service_notice_enabled = True
        elif desired == "service-off":
            self.config.service_notice_enabled = False
        elif desired == "config-on":
            self.config.config_notice_enabled = True
        elif desired == "config-off":
            self.config.config_notice_enabled = False
        elif desired == "task-on":
            self.config.task_notice_enabled = True
        elif desired == "task-off":
            self.config.task_notice_enabled = False
        self.config.save()
        return BridgeCommandResult(True, self._render_switched())

    def _render_current(self) -> str:
        return self.translate(
            "bridge.notify.current",
            service=self._enabled_label(self.config.service_notice_enabled),
            config=self._enabled_label(self.config.config_notice_enabled),
            task=self._enabled_label(self.config.task_notice_enabled),
        )

    def _render_switched(self) -> str:
        return self.translate(
            "bridge.notify.switched",
            service=self._enabled_label(self.config.service_notice_enabled),
            config=self._enabled_label(self.config.config_notice_enabled),
            task=self._enabled_label(self.config.task_notice_enabled),
        )

    def _enabled_label(self, enabled: bool) -> str:
        return self.translate("bridge.notify.on") if enabled else self.translate("bridge.notify.off")
