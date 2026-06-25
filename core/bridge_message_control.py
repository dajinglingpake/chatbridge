from __future__ import annotations


DEFAULT_PROGRESS_MIN_CHARS = 6
DELTA_LEADING_CONTINUATION_MARKS = "，,、。.!！？?；;：:"


def normalize_message_for_dedupe(text: object) -> str:
    return "\n".join(line.rstrip() for line in str(text or "").strip().splitlines()).strip()


def extract_progress_delta(previous: object, current: object) -> str:
    previous_text = normalize_message_for_dedupe(previous)
    current_text = normalize_message_for_dedupe(current)
    if not current_text:
        return ""
    if previous_text and current_text.startswith(previous_text):
        return _clean_progress_delta(current_text[len(previous_text) :])
    if previous_text == current_text:
        return ""
    return current_text


def _clean_progress_delta(delta: object) -> str:
    return str(delta or "").strip().lstrip(DELTA_LEADING_CONTINUATION_MARKS).strip()


def should_send_progress_delta(delta: object, *, min_chars: int = DEFAULT_PROGRESS_MIN_CHARS) -> bool:
    cleaned = str(delta or "").strip()
    if not cleaned:
        return False
    if len(cleaned) >= min_chars:
        return True
    return any(marker in cleaned for marker in ("\n", "。", "！", "？", ".", "!", "?"))


def normalize_context_left_percent(value: object) -> int | None:
    try:
        if value is None or value == "":
            return None
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return None
