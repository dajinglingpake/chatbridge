from __future__ import annotations

from datetime import datetime


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_iso_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def format_duration_since(started_at: str, *, ended_at: str | None = None) -> str:
    start = parse_iso_datetime(started_at)
    if start is None:
        return "-"
    end = parse_iso_datetime(ended_at or "") or datetime.now()
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


def prefix_weixin_output(status: str, elapsed: str, text: str, *, at: str | None = None) -> str:
    cleaned_text = str(text or "").strip()
    header = f"{status} · {elapsed} · {format_output_time(at)}"
    return f"{header}\n\n{cleaned_text}" if cleaned_text else header


def has_weixin_reply_header(text: str) -> bool:
    first_line = str(text or "").strip().splitlines()[0:1]
    if not first_line:
        return False
    parts = first_line[0].split(" · ")
    return len(parts) == 3 and parts[0] in {"running", "done", "reply", "notice"}


def format_weixin_reply(text: str, *, status: str = "reply", elapsed: str = "-", at: str | None = None) -> str:
    cleaned = str(text or "").strip() or "(empty reply)"
    if has_weixin_reply_header(cleaned):
        return cleaned
    return prefix_weixin_output(status, elapsed, cleaned, at=at or now_iso())
