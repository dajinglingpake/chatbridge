from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from core.bridge_command_catalog import parse_bridge_command
from core.bridge_runtime import BridgeCommandResult
from core.json_store import load_json

WEIXIN_RESTART_SCOPES = {
    "all": "restart",
    "bridge": "restart-bridge",
}

QQ_RESTART_SCOPES = {
    "qq": "restart-qq-stack",
    "qq-bridge": "restart-qq-bridge",
    "bridge": "restart-qq-bridge",
    "onebot": "restart-onebot-runtime",
    "onebot-runtime": "restart-onebot-runtime",
    "all": "restart-qq-stack",
}

MCP_RESTART_SCOPES = {
    **WEIXIN_RESTART_SCOPES,
    "weixin": "restart-bridge",
    "wechat": "restart-bridge",
    "qq": "restart-qq-stack",
    "qq-bridge": "restart-qq-bridge",
    "onebot": "restart-onebot-runtime",
    "onebot-runtime": "restart-onebot-runtime",
}

def resolve_restart_action(scope: str, *, default_scope: str, restart_scopes: dict[str, str]) -> str | None:
    cleaned_scope = str(scope or "").strip().lower() or default_scope
    return restart_scopes.get(cleaned_scope)


class BridgeServiceControlRuntime:
    def __init__(
        self,
        *,
        schedule_action: Callable[[str], str],
        render_usage: Callable[[], str],
        state_path: Path,
        default_restart_scope: str,
        restart_scopes: dict[str, str],
        before_restart: Callable[[str, str], None] | None = None,
        render_status: Callable[[], str] | None = None,
        translate: Callable[..., str] | None = None,
    ) -> None:
        self.schedule_action = schedule_action
        self.render_usage = render_usage
        self.state_path = state_path
        self.default_restart_scope = default_restart_scope
        self.restart_scopes = restart_scopes
        self.before_restart = before_restart
        self.render_status = render_status
        self.translate = translate

    def handle(self, sender_id: str, text: str) -> BridgeCommandResult:
        parsed = parse_bridge_command(text)
        if parsed is None or parsed.is_passthrough or parsed.command != "/restart":
            return BridgeCommandResult(False)
        parts = list(parsed.parts)
        requested_scope = parts[1].strip().lower() if len(parts) >= 2 else ""
        if requested_scope == "status":
            renderer = self.render_status or self._render_restart_status
            return BridgeCommandResult(True, renderer())
        scope = requested_scope or self.default_restart_scope
        action = resolve_restart_action(scope, default_scope=self.default_restart_scope, restart_scopes=self.restart_scopes)
        if action is None:
            return BridgeCommandResult(True, self.render_usage())
        if self.before_restart is not None:
            self.before_restart(sender_id, scope)
        return BridgeCommandResult(True, self.schedule_action(action))

    def _render_restart_status(self) -> str:
        payload = load_json(self.state_path, {}, expect_type=dict)
        if not isinstance(payload, dict) or not payload:
            return self._t("bridge.restart.status.empty")
        lines = [
            self._t(
                "bridge.restart.status.header",
                request_id=str(payload.get("request_id") or "-"),
                action=str(payload.get("action") or "-"),
                status=str(payload.get("status") or "-"),
                updated_at=str(payload.get("updated_at") or "-"),
            )
        ]
        before = self._format_pid_snapshot(payload, suffix="_before")
        if before:
            lines.append(self._t("bridge.restart.status.before.generic", pids=before))
        after = self._format_pid_snapshot(payload, suffix="_after")
        if after:
            lines.append(self._t("bridge.restart.status.after.generic", pids=after))
        result_message = str(payload.get("result_message") or "").strip()
        if result_message:
            lines.append(self._t("bridge.restart.status.result", result=result_message))
        error = str(payload.get("error") or "").strip()
        if error:
            lines.append(self._t("bridge.restart.status.error", error=error))
        return "\n".join(lines)

    def _t(self, key: str, **kwargs: object) -> str:
        if self.translate is None:
            templates = {
                "bridge.restart.status.empty": "当前还没有重启记录。",
                "bridge.restart.status.header": "最近重启状态\n请求 ID: {request_id}\n动作: {action}\n状态: {status}\n更新时间: {updated_at}",
                "bridge.restart.status.before.generic": "重启前 PID\n{pids}",
                "bridge.restart.status.after.generic": "重启后 PID\n{pids}",
                "bridge.restart.status.result": "结果: {result}",
                "bridge.restart.status.error": "错误: {error}",
            }
            return templates.get(key, key).format(**kwargs)
        return self.translate(key, **kwargs)

    @staticmethod
    def _format_pid_snapshot(payload: dict[str, object], *, suffix: str) -> str:
        labels = [
            ("hub_pid", "Hub"),
            ("bridge_pid", "Bridge"),
            ("onebot_runtime_pid", "QQ OneBot Runtime"),
            ("qq_bridge_pid", "QQ Bridge"),
        ]
        lines = []
        for key, label in labels:
            value = payload.get(f"{key}{suffix}")
            if value is not None:
                lines.append(f"{label}: {value or '-'}")
        return "\n".join(lines)
