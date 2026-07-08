from __future__ import annotations

from datetime import datetime


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_iso_datetime(value: str) -> datetime | None:
    try:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def format_duration_since(started_at: str, *, ended_at: str | None = None) -> str:
    start = parse_iso_datetime(started_at)
    if start is None:
        return "-"
    end = parse_iso_datetime(ended_at or "") or (datetime.now(start.tzinfo) if start.tzinfo else datetime.now())
    if start.tzinfo is None and end.tzinfo is not None:
        start = start.replace(tzinfo=end.tzinfo)
    elif start.tzinfo is not None and end.tzinfo is None:
        end = end.replace(tzinfo=start.tzinfo)
    seconds = max(0, int((end - start).total_seconds()))
    minutes, remainder = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m{remainder:02d}s"
    return f"{remainder}s"


def format_output_time(value: str | None) -> str:
    parsed = parse_iso_datetime(value or "")
    if parsed is None:
        parsed = datetime.now()
    return parsed.strftime("%H:%M:%S")


def _format_context_left(value: int | None) -> str:
    if value is None:
        return ""
    percent = max(0, min(100, int(value)))
    return f"ctx {percent}%"


def prefix_bridge_output(
    status: str,
    elapsed: str,
    text: str,
    *,
    at: str | None = None,
    context_left_percent: int | None = None,
) -> str:
    cleaned_text = str(text or "").strip()
    parts = [status, elapsed]
    context_text = _format_context_left(context_left_percent)
    if context_text:
        parts.append(context_text)
    parts.append(format_output_time(at))
    header = " · ".join(parts)
    return f"{header}\n\n{cleaned_text}" if cleaned_text else header


def has_bridge_reply_header(text: str) -> bool:
    first_line = str(text or "").strip().splitlines()[0:1]
    if not first_line:
        return False
    parts = first_line[0].split(" · ")
    status = parts[0].split(maxsplit=1)[0]
    return len(parts) in {3, 4} and status in {"running", "done", "reply", "notice"}


def format_bridge_reply(text: str, *, status: str = "reply", elapsed: str = "-", at: str | None = None) -> str:
    cleaned = str(text or "").strip() or "(empty reply)"
    if has_bridge_reply_header(cleaned):
        return cleaned
    return prefix_bridge_output(status, elapsed, cleaned, at=at or now_iso())
