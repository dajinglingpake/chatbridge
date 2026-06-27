from __future__ import annotations


def should_send_typing_keepalive(
    last_sent_at: float | int,
    *,
    now_seconds: float | int,
    keepalive_seconds: float | int,
) -> bool:
    if not last_sent_at:
        return True
    return float(now_seconds) - float(last_sent_at) >= float(keepalive_seconds)


def should_refresh_typing_ticket(
    ticket: str,
    refreshed_at: float | int,
    *,
    now_seconds: float | int,
    ttl_seconds: float | int,
) -> bool:
    if not str(ticket or "").strip():
        return True
    if not refreshed_at:
        return True
    return float(now_seconds) - float(refreshed_at) >= float(ttl_seconds)
