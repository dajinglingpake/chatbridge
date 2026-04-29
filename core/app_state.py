from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agent_backends import supported_backend_keys
from core.state_models import CheckSnapshot, RuntimeSnapshot, WeixinBridgeRuntimeState

Translator = Callable[[str], str]


@dataclass
class BadgeState:
    text: str
    style: str


@dataclass
class IssueItem:
    kind: str
    title: str
    detail: str


def _fallback_text(key: str, **kwargs: object) -> str:
    templates = {
        "ui.status.running": "运行中",
        "ui.status.partial": "部分运行",
        "ui.status.stopped": "已停止",
        "ui.overview.hub": "Hub: {status} {pid}",
        "ui.overview.bridge": "Bridge: {status} {pid}",
        "ui.overview.agent_processes": "Agent 进程数: {count}",
        "ui.overview.active_account": "当前账号: {account}",
        "ui.overview.none_agents": "没有检测到残留 Agent 进程。",
        "ui.overview.bridge_state": "微信桥状态:",
        "ui.summary.missing": "当前有 {count} 项需要处理，详情集中在诊断页。",
        "ui.summary.ready_running": "环境已就绪，微信桥和会话后台都在运行。",
        "ui.summary.ready_waiting": "环境已就绪，等待启动后台服务。",
        "ui.primary.stop.label": "停止服务",
        "ui.primary.stop.hint": "关闭界面前优先通过这里正常停止，避免残留后台进程。",
        "ui.primary.manual.label": "查看诊断",
        "ui.primary.manual.hint": "基础环境不完整，先处理缺失项。",
        "ui.primary.repair.label": "一键补齐依赖",
        "ui.primary.repair.hint": "按顺序补齐桌面依赖、Node 和 CLI。",
        "ui.primary.login.label": "打开微信账号目录",
        "ui.primary.login.hint": "当前缺少项目内微信账号文件，先把 json/sync 文件放到项目目录。",
        "ui.primary.start.label": "启动服务",
        "ui.primary.start.hint": "环境检测已通过，直接启动微信桥和会话后台即可。",
        "ui.quickstart.step.desktop": "1. 桌面依赖",
        "ui.quickstart.step.node": "2. Node / Agent CLI",
        "ui.quickstart.step.accounts": "3. 微信账号文件",
        "ui.quickstart.step.start": "4. 后台启动",
        "ui.quickstart.commands": "微信命令:",
        "ui.quickstart.accounts_dir": "微信账号目录:",
        "ui.quickstart.repair": "先补齐依赖，再继续。",
        "ui.quickstart.login": "依赖已经就绪，现在补微信账号文件。",
        "ui.quickstart.start": "环境已准备好，现在只差启动后台服务。",
        "ui.quickstart.stop": "后台已经在运行，优先正常停止。",
        "ui.quickstart.manual": "先看自动诊断区域，确认缺失项。",
        "ui.issue.dependencies.title": "依赖未就绪",
        "ui.issue.dependencies.detail": "先处理缺失依赖，再继续导入微信账号文件或启动后台。",
        "ui.issue.login.title": "微信账号文件缺失",
        "ui.issue.login.detail": "当前没有可用的微信账号 json/sync 文件，需要先放入项目目录。",
        "ui.issue.process_mismatch.title": "后台状态不一致",
        "ui.issue.process_mismatch.detail": "Hub 和 Bridge 没有同时处于运行状态，建议先停止，再重新启动。",
        "ui.issue.logs.title": "微信桥最近报错",
        "ui.issue.residual.title": "检测到残留进程",
        "ui.issue.residual.detail": "后台已经停止，但仍检测到相关进程，建议清理残留进程。",
        "ui.issue.none.summary": "当前没有需要手动处理的异常。",
        "ui.issue.none.detail": "服务状态和基础依赖看起来正常。出现新问题时，这里会列出具体异常和对应操作。",
        "ui.issue.summary.count": "当前检测到 {count} 个需要处理的问题。",
        "ui.step.done": "[完成] {title}",
        "ui.step.pending": "[待处理] {title}",
    }
    template = templates.get(key, key)
    return template.format(**kwargs)


def _t(translator: Callable[..., str] | None, key: str, **kwargs: object) -> str:
    if translator is None:
        return _fallback_text(key, **kwargs)
    try:
        return translator(key, **kwargs)
    except TypeError:
        return translator(key)


def _check_ok(checks: dict[str, CheckSnapshot], key: str) -> bool:
    item = checks.get(key)
    return bool(item and item.ok)


def _check_missing(checks: dict[str, CheckSnapshot], key: str) -> bool:
    item = checks.get(key)
    return bool(item and not item.ok)


def pid_text(pid: int | None) -> str:
    return f"(PID {pid})" if pid else ""


def build_badge(snapshot: RuntimeSnapshot, translator: Callable[..., str] | None = None) -> BadgeState:
    if snapshot.hub_running and snapshot.bridge_running:
        return BadgeState(
            text=_t(translator, "ui.status.running"),
            style="background:#d9f3e4;color:#12633b;border:1px solid #b9dfca;border-radius:16px;padding:10px 12px;font-weight:700;",
        )
    if snapshot.hub_running or snapshot.bridge_running:
        return BadgeState(
            text=_t(translator, "ui.status.partial"),
            style="background:#fff2cc;color:#946200;border:1px solid #eadcb0;border-radius:16px;padding:10px 12px;font-weight:700;",
        )
    return BadgeState(
        text=_t(translator, "ui.status.stopped"),
        style="background:#f8d7da;color:#8a1c2b;border:1px solid #efbac2;border-radius:16px;padding:10px 12px;font-weight:700;",
    )


def build_overview_lines(
    snapshot: RuntimeSnapshot,
    bridge_state: WeixinBridgeRuntimeState,
    active_account_id: str,
    translator: Callable[..., str] | None = None,
) -> list[str]:
    lines = [
        _t(translator, "ui.overview.hub", status=_t(translator, "ui.status.running") if snapshot.hub_running else _t(translator, "ui.status.stopped"), pid=pid_text(snapshot.hub_pid)),
        _t(translator, "ui.overview.bridge", status=_t(translator, "ui.status.running") if snapshot.bridge_running else _t(translator, "ui.status.stopped"), pid=pid_text(snapshot.bridge_pid)),
        _t(translator, "ui.overview.agent_processes", count=len(snapshot.codex_processes)),
        _t(translator, "ui.overview.active_account", account=active_account_id),
        "",
    ]
    if snapshot.codex_processes:
        lines.extend(snapshot.codex_processes[:8])
    else:
        lines.append(_t(translator, "ui.overview.none_agents"))
    bridge_state_items = list(bridge_state.to_dict().items())
    if bridge_state_items:
        lines.extend(["", _t(translator, "ui.overview.bridge_state")])
        lines.extend(f"{key}: {value}" for key, value in bridge_state_items[:8])
    return lines


def decide_primary_action(
    snapshot: RuntimeSnapshot,
    checks: dict[str, CheckSnapshot],
    translator: Callable[..., str] | None = None,
) -> tuple[str, str, str]:
    if snapshot.hub_running or snapshot.bridge_running:
        return "stop", _t(translator, "ui.primary.stop.label"), _t(translator, "ui.primary.stop.hint")

    blocking = [key for key in ["python", "project_files"] if _check_missing(checks, key)]
    if _check_missing(checks, "nvm") and _check_missing(checks, "winget"):
        blocking.append("nvm")
    if blocking:
        return "manual", _t(translator, "ui.primary.manual.label"), _t(translator, "ui.primary.manual.hint")

    auto_fixable = [
        key
        for key in ["psutil", "nvm", "node", "npm", "codex", "claude", "opencode"]
        if _check_missing(checks, key)
    ]
    if auto_fixable:
        return "repair", _t(translator, "ui.primary.repair.label"), _t(translator, "ui.primary.repair.hint")

    if _check_missing(checks, "weixin_account"):
        return "login", _t(translator, "ui.primary.login.label"), _t(translator, "ui.primary.login.hint")

    return "start", _t(translator, "ui.primary.start.label"), _t(translator, "ui.primary.start.hint")


def build_summary_text(
    snapshot: RuntimeSnapshot,
    checks: dict[str, CheckSnapshot],
    translator: Callable[..., str] | None = None,
) -> str:
    missing_count = sum(1 for item in checks.values() if not item.ok)
    if missing_count:
        return _t(translator, "ui.summary.missing", count=missing_count)
    if snapshot.hub_running and snapshot.bridge_running:
        return _t(translator, "ui.summary.ready_running")
    return _t(translator, "ui.summary.ready_waiting")


def build_quickstart_lines(
    snapshot: RuntimeSnapshot,
    checks: dict[str, CheckSnapshot],
    accounts_dir: Path,
    translator: Callable[..., str] | None = None,
) -> tuple[list[str], str]:
    backend_choices = "|".join(supported_backend_keys())
    stage_lines = [
        step_line(_t(translator, "ui.quickstart.step.desktop"), _check_ok(checks, "psutil"), translator),
        step_line(_t(translator, "ui.quickstart.step.node"), not any(_check_missing(checks, key) for key in ["node", "npm", "codex", "claude", "opencode"]), translator),
        step_line(_t(translator, "ui.quickstart.step.accounts"), not _check_missing(checks, "weixin_account"), translator),
        step_line(_t(translator, "ui.quickstart.step.start"), snapshot.hub_running and snapshot.bridge_running, translator),
    ]
    body = stage_lines + [
        "",
        _t(translator, "ui.quickstart.commands"),
        "/help",
        "/status",
        "/sessions 2",
        "/sessions search bug",
        "/new <name>",
        "/list",
        "/preview [name]",
        "/use <name>",
        "/rename <new>",
        "/delete <name>",
        "/sessions delete a,b,c",
        "/sessions clear-empty",
        "/history",
        "/export",
        "/cancel [task_id]",
        "/retry [task_id]",
        "/model",
        "/model <name>",
        "/project",
        "/project list",
        "/project <name|path>",
        "/agent",
        "/agent list",
        "/backend",
        f"/backend <{backend_choices}>",
        "//status",
        "",
        _t(translator, "ui.quickstart.relations"),
        _t(translator, "ui.quickstart.relation.agent"),
        _t(translator, "ui.quickstart.relation.session"),
        _t(translator, "ui.quickstart.relation.override"),
        "/close",
        "/reset",
        "",
        _t(translator, "ui.quickstart.accounts_dir"),
        str(accounts_dir.resolve()),
    ]
    action_key, _, _ = decide_primary_action(snapshot, checks, translator)
    status_map = {
        "repair": _t(translator, "ui.quickstart.repair"),
        "login": _t(translator, "ui.quickstart.login"),
        "start": _t(translator, "ui.quickstart.start"),
        "stop": _t(translator, "ui.quickstart.stop"),
    }
    return body, status_map.get(action_key, _t(translator, "ui.quickstart.manual"))


def build_issues(
    snapshot: RuntimeSnapshot,
    bridge_state: WeixinBridgeRuntimeState,
    checks: dict[str, CheckSnapshot],
    translator: Callable[..., str] | None = None,
) -> list[IssueItem]:
    issues: list[IssueItem] = []
    if any(_check_missing(checks, key) for key in ["psutil", "nvm", "node", "npm", "codex", "claude", "opencode"]):
        issues.append(
            IssueItem(
                kind="dependencies",
                title=_t(translator, "ui.issue.dependencies.title"),
                detail=_t(translator, "ui.issue.dependencies.detail"),
            )
        )
    if _check_missing(checks, "weixin_account"):
        issues.append(
            IssueItem(
                kind="login",
                title=_t(translator, "ui.issue.login.title"),
                detail=_t(translator, "ui.issue.login.detail"),
            )
        )
    if snapshot.hub_running != snapshot.bridge_running:
        issues.append(
            IssueItem(
                kind="processes",
                title=_t(translator, "ui.issue.process_mismatch.title"),
                detail=_t(translator, "ui.issue.process_mismatch.detail"),
            )
        )
    if bridge_state.last_error:
        issues.append(
            IssueItem(
                kind="logs",
                title=_t(translator, "ui.issue.logs.title"),
                detail=bridge_state.last_error.strip(),
            )
        )
    if snapshot.codex_processes and not (snapshot.hub_running or snapshot.bridge_running):
        issues.append(
            IssueItem(
                kind="processes",
                title=_t(translator, "ui.issue.residual.title"),
                detail=_t(translator, "ui.issue.residual.detail"),
            )
        )
    return issues


def step_line(title: str, done: bool, translator: Callable[..., str] | None = None) -> str:
    key = "ui.step.done" if done else "ui.step.pending"
    return _t(translator, key, title=title)
