from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class SessionRow:
    name: str
    status: str
    queue_size: int
    success_count: int
    failure_count: int


@dataclass
class SessionDetail:
    rows: list[SessionRow]
    detail_lines: list[str]
    conversation_lines: list[str]


@dataclass
class SessionAggregate:
    last_task: dict[str, Any]
    queue_size: int = 0
    success_count: int = 0
    failure_count: int = 0
    has_running: bool = False


_SESSION_ROWS_CACHE: dict[tuple, list[SessionRow]] = {}


def session_name_from_file(agent_id: str, session_file: Path) -> str:
    stem = session_file.stem
    if "__" in stem:
        return stem.split("__", 1)[1] or "default"
    if not agent_id or stem == agent_id:
        return "default"
    prefix = f"{agent_id}__"
    if stem.startswith(prefix):
        return stem[len(prefix) :] or "default"
    return stem


def session_file_for_name(session_dir: Path, session_name: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in session_name).strip("-_") or "default"
    if safe == "default":
        direct = session_dir / "main.txt"
        if direct.exists():
            return direct
        fallback = sorted(session_dir.glob("*.txt"))
        return fallback[0] if fallback else session_dir / "main.txt"
    exact_matches = sorted(session_dir.glob(f"*__{safe}.txt"))
    if exact_matches:
        return exact_matches[0]
    return session_dir / f"main__{safe}.txt"


def normalize_task_session_name(task: dict[str, Any]) -> str:
    return str(task.get("session_name") or "default")


def build_hub_signature(hub_state: dict[str, Any]) -> tuple:
    agents = tuple(
        (
            agent.get("id"),
            (agent.get("runtime") or {}).get("status"),
            (agent.get("runtime") or {}).get("queue_size"),
            (agent.get("runtime") or {}).get("success_count"),
            (agent.get("runtime") or {}).get("failure_count"),
            (agent.get("runtime") or {}).get("updated_at"),
        )
        for agent in hub_state.get("agents", [])
    )
    tasks = tuple(
        (
            task.get("id"),
            task.get("status"),
            task.get("finished_at"),
            task.get("session_name"),
            task.get("agent_id"),
        )
        for task in hub_state.get("tasks", [])[:20]
    )
    return agents, tasks


def build_session_dir_signature(session_dir: Path) -> tuple:
    if not session_dir.exists():
        return ()
    return tuple(
        sorted(
            (
                session_file.name,
                int(session_file.stat().st_mtime_ns),
            )
            for session_file in session_dir.glob("*.txt")
        )
    )


def build_session_rows(hub_state: dict[str, Any], session_dir: Path) -> list[SessionRow]:
    cache_key = (build_hub_signature(hub_state), build_session_dir_signature(session_dir))
    cached_rows = _SESSION_ROWS_CACHE.get(cache_key)
    if cached_rows is not None:
        return cached_rows

    tasks = hub_state.get("tasks", [])
    session_names: set[str] = {"default"}
    aggregates: dict[str, SessionAggregate] = {}
    for task in tasks:
        session_name = normalize_task_session_name(task)
        session_names.add(session_name)
        aggregate = aggregates.get(session_name)
        if aggregate is None:
            aggregate = SessionAggregate(last_task=task)
            aggregates[session_name] = aggregate
        status = str(task.get("status") or "")
        if status in {"queued", "running"}:
            aggregate.queue_size += 1
        if status == "running":
            aggregate.has_running = True
        elif status == "succeeded":
            aggregate.success_count += 1
        elif status == "failed":
            aggregate.failure_count += 1
    if session_dir.exists():
        for session_file in session_dir.glob("*.txt"):
            session_names.add(session_name_from_file("", session_file))

    rows: list[SessionRow] = []
    for session_name in sorted(session_names):
        aggregate = aggregates.get(session_name)
        if aggregate is None:
            queue_size = 0
            success_count = 0
            failure_count = 0
            status = "idle"
        else:
            queue_size = aggregate.queue_size
            success_count = aggregate.success_count
            failure_count = aggregate.failure_count
            if queue_size:
                status = "running" if aggregate.has_running else "queued"
            else:
                status = str(aggregate.last_task.get("status") or "idle")
        rows.append(
            SessionRow(
                name=session_name,
                status=status,
                queue_size=queue_size,
                success_count=success_count,
                failure_count=failure_count,
            )
        )
    _SESSION_ROWS_CACHE.clear()
    _SESSION_ROWS_CACHE[cache_key] = rows
    return rows


def build_session_detail(
    hub_state: dict[str, Any],
    session_dir: Path,
    session_name: str,
    task_status_text: Callable[[str], str] | None = None,
    t: Callable[[str], str] | None = None,
) -> SessionDetail:
    if not session_name:
        empty_detail = t("ui.agent.select_session") if t else "先在上方选中一个会话。"
        empty_conversation = t("ui.agent.select_preview") if t else "这里会显示该会话最近几轮对话。"
        return SessionDetail(rows=[], detail_lines=[empty_detail], conversation_lines=[empty_conversation])

    all_tasks = hub_state.get("tasks", [])
    matching_tasks: list[dict[str, Any]] = []
    queue_size = 0
    success_count = 0
    failure_count = 0
    has_running = False
    for task in all_tasks:
        if normalize_task_session_name(task) != session_name:
            continue
        matching_tasks.append(task)
        status = str(task.get("status") or "")
        if status in {"queued", "running"}:
            queue_size += 1
        if status == "running":
            has_running = True
        elif status == "succeeded":
            success_count += 1
        elif status == "failed":
            failure_count += 1
    tasks = sorted(matching_tasks[:8], key=lambda item: str(item.get("created_at") or ""), reverse=True)

    selected_session_file = session_file_for_name(session_dir, session_name)
    selected_session_id = selected_session_file.read_text(encoding="utf-8").strip() if selected_session_file.exists() else ""

    if queue_size:
        status = "running" if has_running else "queued"
    elif tasks:
        status = str(tasks[0].get("status") or "idle")
    else:
        status = "idle"

    render_status = task_status_text or (lambda value: value)
    if t:
        detail_lines = [
            t("ui.agent.detail.session", value=session_name),
            t("ui.agent.detail.status", value=render_status(status)),
            t("ui.agent.detail.queue", value=queue_size),
            t("ui.agent.detail.result", success=success_count, failure=failure_count),
            t("ui.agent.detail.file", value=selected_session_file),
            t("ui.agent.detail.id", value=selected_session_id or "(empty)"),
        ]
    else:
        detail_lines = [
            f"会话名: {session_name}",
            f"状态: {render_status(status)}",
            f"队列: {queue_size}",
            f"成功/失败: {success_count}/{failure_count}",
            f"会话文件: {selected_session_file}",
            f"当前会话 ID: {selected_session_id or '(empty)'}",
        ]

    if tasks:
        detail_lines.extend(["", t("ui.agent.detail.recent") if t else "最近任务:"])
        for task in tasks:
            detail_lines.append(
                f"[{render_status(str(task.get('status') or 'idle'))}] {task.get('created_at')}  "
                f"session={normalize_task_session_name(task)}  source={task.get('source') or '-'}"
            )
            detail_lines.append(str(task.get("prompt") or ""))
            if task.get("output"):
                detail_lines.append(f"output: {str(task.get('output'))[:240]}")
            if task.get("error"):
                detail_lines.append(f"error: {str(task.get('error'))[:240]}")
            detail_lines.append("")

        conversation_lines = [t("ui.agent.preview.title") if t else "会话预览:"]
        for index, task in enumerate(reversed(tasks[-6:]), start=1):
            if t:
                conversation_lines.append(t("ui.agent.preview.round", index=index, time=task.get("created_at")))
                conversation_lines.append(t("ui.agent.preview.user", text=str(task.get("prompt") or "(empty)")[:320]))
                if task.get("output"):
                    conversation_lines.append(t("ui.agent.preview.assistant", text=str(task.get("output") or "")[:320]))
                elif task.get("error"):
                    conversation_lines.append(t("ui.agent.preview.error", text=str(task.get("error") or "")[:320]))
                else:
                    conversation_lines.append(t("ui.agent.preview.no_output"))
            else:
                conversation_lines.append(f"第 {index} 轮 | {task.get('created_at')}")
                conversation_lines.append(f"用户: {str(task.get('prompt') or '(empty)')[:320]}")
                if task.get("output"):
                    conversation_lines.append(f"Codex: {str(task.get('output') or '')[:320]}")
                elif task.get("error"):
                    conversation_lines.append(f"错误: {str(task.get('error') or '')[:320]}")
                else:
                    conversation_lines.append("Codex: (no output)")
            conversation_lines.append("")
    else:
        conversation_lines = [t("ui.agent.preview.title") if t else "会话预览:", t("ui.agent.preview.none") if t else "当前选择下还没有任务记录。"]

    return SessionDetail(rows=[], detail_lines=detail_lines, conversation_lines=conversation_lines)
