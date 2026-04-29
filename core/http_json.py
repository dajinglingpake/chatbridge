from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

from core.state_models import JsonObject


def decode_json_bytes(payload: bytes) -> JsonObject:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid JSON response payload") from exc
    if not isinstance(data, dict):
        raise RuntimeError("unexpected JSON response type")
    return data


def request_json(request: urllib.request.Request, *, timeout: float) -> JsonObject:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return decode_json_bytes(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError("timed out") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason or exc)) from exc
