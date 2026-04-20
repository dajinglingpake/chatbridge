from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BackendCommandGuide:
    title: str
    summary: str
    command_groups: tuple[str, ...]
    footer: str = ""


_GUIDES: dict[str, BackendCommandGuide] = {
    "claude": BackendCommandGuide(
        title="Claude Code CLI",
        summary="Claude 默认进入交互会话，也支持 print 模式和管理类子命令。",
        command_groups=(
            "核心用法: claude [prompt] | claude -p [prompt] | claude -c | claude -r",
            "常用选项: --model | --agent | --permission-mode | --output-format | --add-dir | --settings | --mcp-config",
            "管理命令: claude agents | auth | mcp | plugin | doctor | install | update",
        ),
        footer="如需完整说明，请在终端执行 claude --help。",
    ),
    "codex": BackendCommandGuide(
        title="Codex CLI",
        summary="Codex 没有聊天态 /help 命令，主要通过 CLI 子命令工作。",
        command_groups=(
            "核心命令: codex exec | codex review | codex login | codex logout | codex resume | codex fork | codex apply",
            "扩展命令: codex mcp | codex marketplace | codex mcp-server | codex app-server | codex cloud | codex exec-server | codex features",
            "常用子命令: codex exec resume | codex exec review | codex login status | codex mcp list|get|add|remove|login|logout",
        ),
        footer="如需完整说明，请在终端执行 codex --help、codex exec --help、codex resume --help。",
    ),
    "opencode": BackendCommandGuide(
        title="OpenCode CLI",
        summary="OpenCode 同时支持 TUI、单次 run、会话管理和服务端模式。",
        command_groups=(
            "核心命令: opencode [project] | opencode run [message] | opencode session | opencode models | opencode providers",
            "扩展命令: opencode agent | mcp | plugin | debug | stats | export | import | serve | web",
            "常用选项: --model | --agent | --continue | --session | --fork | --format json | --dir",
        ),
        footer="如需完整说明，请在终端执行 opencode --help、opencode run --help。",
    ),
}


def get_backend_command_guide(backend: str) -> BackendCommandGuide | None:
    return _GUIDES.get((backend or "").strip().lower())
