from __future__ import annotations


def build_task_followup_hint(
    task_id: str = "",
    session_name: str = "",
    *,
    allow_retry: bool = False,
) -> str:
    lines = ["可继续发送命令查看详情:"]
    if task_id:
        lines.append(f"/task {task_id}")
    if allow_retry and task_id:
        lines.append(f"/retry {task_id}")
    lines.append("/last")
    if session_name:
        lines.append(f"当前会话: {session_name}")
    return "\n".join(lines)
