from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

USAGE_URL = "https://chatgpt.com/codex/settings/usage"


@dataclass(frozen=True)
class _TokenUsage:
    total_tokens: int
    model_context_window: int | None


@dataclass(frozen=True)
class _RateLimitWindow:
    used_percent: int
    resets_at: int | None
    window_minutes: int | None


@dataclass(frozen=True)
class _RateLimitBucket:
    limit_id: str
    limit_name: str
    primary: _RateLimitWindow | None
    secondary: _RateLimitWindow | None


@dataclass(frozen=True)
class _StatusSnapshot:
    cli_version: str
    model: str
    reasoning_effort: str
    service_tier: str
    directory: str
    permissions: str
    agents_path: str
    account_label: str
    collaboration_mode: str
    session_id: str
    token_usage: _TokenUsage | None
    primary_bucket: _RateLimitBucket | None
    extra_buckets: tuple[_RateLimitBucket, ...]


class _AppServerClient:
    def __init__(self, codex_command: str) -> None:
        argv = [*shlex.split(codex_command), "app-server"]
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("failed to start codex app-server")
        self._next_id = 1

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {"name": "chatbridge-status", "version": "0.1.0"},
                "capabilities": {},
            },
        )

    def request(self, method: str, params: object) -> dict:
        request_id = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        while True:
            line = self._proc.stdout.readline()
            if not line:
                stderr = self._proc.stderr.read().strip() if self._proc.stderr is not None else ""
                raise RuntimeError(stderr or f"codex app-server closed while waiting for {method}")
            message = json.loads(line)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"codex app-server {method} failed: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"codex app-server {method} returned invalid result")
            return result


def query_codex_status_panel(codex_command: str, session_file: Path, workdir: Path) -> str | None:
    del workdir
    session_id = _read_session_id(session_file)
    if not session_id:
        return None
    client = _AppServerClient(codex_command)
    try:
        client.initialize()
        account_payload = client.request("account/read", {"refresh": False})
        limits_payload = client.request("account/rateLimits/read", None)
        resume_payload = client.request("thread/resume", {"threadId": session_id})
    finally:
        client.close()
    snapshot = _build_snapshot(session_id, account_payload, limits_payload, resume_payload)
    return _render_status_panel(snapshot)


def _read_session_id(session_file: Path) -> str:
    if not session_file.exists():
        return ""
    return session_file.read_text(encoding="utf-8").strip()


def _build_snapshot(
    session_id: str,
    account_payload: dict,
    limits_payload: dict,
    resume_payload: dict,
) -> _StatusSnapshot:
    thread = dict(resume_payload.get("thread") or {})
    token_usage = _load_latest_token_usage(Path(str(thread.get("path") or "")))
    account_label = _format_account_label(dict(account_payload.get("account") or {}))
    primary_bucket, extra_buckets = _parse_rate_limit_buckets(dict(limits_payload))
    return _StatusSnapshot(
        cli_version=str(thread.get("cliVersion") or "unknown"),
        model=str(resume_payload.get("model") or "-"),
        reasoning_effort=str(resume_payload.get("reasoningEffort") or "").strip(),
        service_tier=str(resume_payload.get("serviceTier") or "").strip(),
        directory=_abbreviate_path(str(resume_payload.get("cwd") or thread.get("cwd") or "-")),
        permissions=_format_permissions(
            approval_policy=resume_payload.get("approvalPolicy"),
            sandbox=resume_payload.get("sandbox"),
        ),
        agents_path=_abbreviate_path(_pick_agents_path(list(resume_payload.get("instructionSources") or []))),
        account_label=account_label,
        collaboration_mode="Default",
        session_id=session_id,
        token_usage=token_usage,
        primary_bucket=primary_bucket,
        extra_buckets=extra_buckets,
    )


def _format_account_label(account: dict) -> str:
    if not account:
        return "Unknown"
    if str(account.get("type") or "") != "chatgpt":
        return str(account.get("type") or "Unknown")
    email = str(account.get("email") or "").strip() or "Unknown"
    plan = _titleize_plan(str(account.get("planType") or "unknown"))
    return f"{email} ({plan})"


def _titleize_plan(plan_type: str) -> str:
    cleaned = plan_type.strip().lower()
    if not cleaned:
        return "Unknown"
    if cleaned == "plus":
        return "Plus"
    if cleaned == "pro":
        return "Pro"
    if cleaned == "prolite":
        return "Prolite"
    if cleaned == "free":
        return "Free"
    return cleaned.replace("_", " ").title()


def _format_permissions(*, approval_policy: object, sandbox: object) -> str:
    sandbox_type = ""
    network_access = False
    if isinstance(sandbox, dict):
        sandbox_type = str(sandbox.get("type") or "").strip()
        network_access = bool(sandbox.get("networkAccess"))
    approval = str(approval_policy or "").strip()
    if sandbox_type == "dangerFullAccess":
        return "Full Access"
    if sandbox_type == "workspaceWrite":
        if approval == "never" and network_access:
            return "Full Access"
        return "Workspace Write"
    if sandbox_type == "readOnly":
        return "Read Only"
    return approval or sandbox_type or "Unknown"


def _pick_agents_path(instruction_sources: list[object]) -> str:
    for raw in instruction_sources:
        candidate = str(raw or "").strip()
        if candidate.endswith("AGENTS.md"):
            return candidate
    return str(instruction_sources[0]).strip() if instruction_sources else "-"


def _abbreviate_path(path: str) -> str:
    cleaned = str(path or "").strip()
    if not cleaned:
        return "-"
    home = str(Path.home())
    if cleaned == home:
        return "~"
    if cleaned.startswith(home + "/"):
        return "~/" + cleaned[len(home) + 1 :]
    return cleaned


def _parse_rate_limit_buckets(payload: dict) -> tuple[_RateLimitBucket | None, tuple[_RateLimitBucket, ...]]:
    raw_buckets = payload.get("rateLimitsByLimitId")
    primary_raw = payload.get("rateLimits")
    buckets: list[_RateLimitBucket] = []
    if isinstance(raw_buckets, dict):
        for value in raw_buckets.values():
            bucket = _parse_rate_limit_bucket(value)
            if bucket is not None:
                buckets.append(bucket)
    primary = _parse_rate_limit_bucket(primary_raw)
    if primary is None and buckets:
        primary = buckets[0]
    extras = tuple(bucket for bucket in buckets if primary is None or bucket.limit_id != primary.limit_id)
    return primary, extras


def _parse_rate_limit_bucket(raw: object) -> _RateLimitBucket | None:
    if not isinstance(raw, dict):
        return None
    limit_id = str(raw.get("limitId") or "").strip()
    if not limit_id:
        return None
    return _RateLimitBucket(
        limit_id=limit_id,
        limit_name=str(raw.get("limitName") or "").strip(),
        primary=_parse_rate_limit_window(raw.get("primary")),
        secondary=_parse_rate_limit_window(raw.get("secondary")),
    )


def _parse_rate_limit_window(raw: object) -> _RateLimitWindow | None:
    if not isinstance(raw, dict):
        return None
    try:
        used_percent = int(raw.get("usedPercent"))
    except (TypeError, ValueError):
        return None
    resets_at = raw.get("resetsAt")
    window_minutes = raw.get("windowDurationMins")
    return _RateLimitWindow(
        used_percent=used_percent,
        resets_at=int(resets_at) if isinstance(resets_at, int) else None,
        window_minutes=int(window_minutes) if isinstance(window_minutes, int) else None,
    )


def _load_latest_token_usage(session_log_path: Path) -> _TokenUsage | None:
    if not session_log_path.exists():
        return None
    latest_total_tokens: int | None = None
    latest_context_window: int | None = None
    with session_log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if str(record.get("type") or "") != "event_msg":
                continue
            payload = dict(record.get("payload") or {})
            if str(payload.get("type") or "") != "token_count":
                continue
            info = dict(payload.get("info") or {})
            total = dict(info.get("total_token_usage") or {})
            total_tokens = total.get("total_tokens")
            if isinstance(total_tokens, int):
                latest_total_tokens = total_tokens
            context_window = info.get("model_context_window")
            if isinstance(context_window, int):
                latest_context_window = context_window
    if latest_total_tokens is None:
        return None
    return _TokenUsage(
        total_tokens=latest_total_tokens,
        model_context_window=latest_context_window,
    )


def _render_status_panel(snapshot: _StatusSnapshot) -> str:
    lines = [
        f"OpenAI Codex v{snapshot.cli_version}",
        "",
        f"Model: {_format_model_value(snapshot.model, snapshot.reasoning_effort, snapshot.service_tier)}",
        f"Directory: {snapshot.directory}",
        f"Permissions: {snapshot.permissions}",
        f"Agents.md: {snapshot.agents_path}",
        f"Account: {snapshot.account_label}",
        f"Collaboration mode: {snapshot.collaboration_mode}",
        f"Session: {snapshot.session_id}",
    ]
    if snapshot.token_usage is not None:
        lines.extend(["", f"Context window: {_format_context_window(snapshot.token_usage)}"])
    if snapshot.primary_bucket is not None:
        lines.extend(_format_rate_limit_lines("5h limit", snapshot.primary_bucket.primary))
        lines.extend(_format_rate_limit_lines("Weekly limit", snapshot.primary_bucket.secondary))
    for bucket in snapshot.extra_buckets:
        title = bucket.limit_name.strip() or bucket.limit_id
        lines.extend(
            [
                "",
                f"{title} limit:",
            ]
        )
        lines.extend(_format_rate_limit_lines("5h limit", bucket.primary))
        lines.extend(_format_rate_limit_lines("Weekly limit", bucket.secondary))
    lines.extend(["", f"Usage details: {USAGE_URL}", "Warning: limits may be stale - run //status again shortly."])
    return "\n".join(lines)


def _format_model_value(model: str, reasoning_effort: str, service_tier: str) -> str:
    parts = [model.strip() or "-"]
    details: list[str] = []
    if reasoning_effort.strip():
        details.append(f"reasoning {reasoning_effort.strip()}")
    if service_tier.strip():
        details.append(service_tier.strip())
    if details:
        parts.append(f"({', '.join(details)})")
    return " ".join(parts)


def _format_context_window(token_usage: _TokenUsage) -> str:
    if not token_usage.model_context_window:
        return f"used {token_usage.total_tokens:_}".replace("_", ",")
    used = token_usage.total_tokens
    total = token_usage.model_context_window
    left_percent = max(0, min(100, round((1 - (used / total)) * 100)))
    used_display = _format_compact_tokens(used)
    total_display = _format_compact_tokens(total)
    return f"{left_percent}% left ({used_display} used / {total_display})"


def _format_compact_tokens(value: int) -> str:
    if value >= 1000:
        return f"{round(value / 1000)}K"
    return str(value)


def _format_rate_limit_lines(label: str, window: _RateLimitWindow | None) -> list[str]:
    if window is None:
        return [f"{label}: -"]
    left_percent = max(0, min(100, 100 - int(window.used_percent)))
    text = f"{label}: {left_percent}% left"
    if window.resets_at is not None:
        reset_text = _format_reset_time(window.resets_at, weekly=(window.window_minutes or 0) >= 24 * 60)
        text += f", resets {reset_text}"
    return [text]


def _format_reset_time(timestamp: int, *, weekly: bool) -> str:
    moment = datetime.fromtimestamp(timestamp)
    if weekly:
        return f"{moment:%H:%M} on {moment.day} {moment:%b}"
    return moment.strftime("%H:%M")
