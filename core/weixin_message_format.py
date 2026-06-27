from __future__ import annotations

from core.bridge_message_format import (
    format_bridge_reply,
    format_duration_since,
    has_bridge_reply_header,
    now_iso,
    parse_iso_datetime,
    prefix_bridge_output,
)

format_weixin_reply = format_bridge_reply
has_weixin_reply_header = has_bridge_reply_header
prefix_weixin_output = prefix_bridge_output
