from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from core.json_store import load_json, save_json
from core.state_models import IpcRequestEnvelope, IpcResponseEnvelope


APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR / ".runtime"
IPC_DIR = RUNTIME_DIR / "ipc"
REQUEST_DIR = IPC_DIR / "requests"
RESPONSE_DIR = IPC_DIR / "responses"
PROCESSED_DIR = IPC_DIR / "processed"
BRIDGE_REQUEST_DIR = IPC_DIR / "bridge_requests"
BRIDGE_PROCESSED_DIR = IPC_DIR / "bridge_processed"
BRIDGE_CHANNELS_DIR = IPC_DIR / "bridge_channels"
IPC_POLL_INTERVAL_SECONDS = 0.05
PROCESSED_RETENTION_SECONDS = 24 * 60 * 60


def ensure_ipc_dirs() -> None:
    for path in [IPC_DIR, REQUEST_DIR, RESPONSE_DIR, PROCESSED_DIR, BRIDGE_REQUEST_DIR, BRIDGE_PROCESSED_DIR, BRIDGE_CHANNELS_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def _safe_bridge_channel(channel: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(channel or "").strip().lower())
    return cleaned.strip("-_") or "wechat"


def bridge_request_dir(channel: str = "wechat") -> Path:
    ensure_ipc_dirs()
    cleaned = _safe_bridge_channel(channel)
    if cleaned == "wechat":
        return BRIDGE_REQUEST_DIR
    path = BRIDGE_CHANNELS_DIR / cleaned / "requests"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bridge_processed_dir(channel: str = "wechat") -> Path:
    ensure_ipc_dirs()
    cleaned = _safe_bridge_channel(channel)
    if cleaned == "wechat":
        return BRIDGE_PROCESSED_DIR
    path = BRIDGE_CHANNELS_DIR / cleaned / "processed"
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_request(action: str, payload: dict[str, Any]) -> str:
    ensure_ipc_dirs()
    request_id = f"req-{uuid.uuid4().hex}"
    request_path = REQUEST_DIR / f"{request_id}.json"
    save_json(
        request_path,
        IpcRequestEnvelope(id=request_id, action=action, payload=payload).to_dict(),
    )
    return request_id


def create_bridge_request(action: str, payload: dict[str, Any], *, channel: str = "wechat") -> str:
    request_id = f"bridge-req-{time.time_ns()}-{uuid.uuid4().hex}"
    request_path = bridge_request_dir(channel) / f"{request_id}.json"
    save_json(
        request_path,
        IpcRequestEnvelope(id=request_id, action=action, payload=payload).to_dict(),
    )
    return request_id


def read_request(path: Path) -> IpcRequestEnvelope:
    data = load_json(path, {}, expect_type=dict)
    request = IpcRequestEnvelope.from_dict(data)
    if request is None:
        raise ValueError(f"invalid ipc request payload: {path.name}")
    return request


def write_response(request_id: str, payload: IpcResponseEnvelope) -> None:
    ensure_ipc_dirs()
    response_path = RESPONSE_DIR / f"{request_id}.json"
    save_json(response_path, payload.to_dict())


def wait_for_response(request_id: str, timeout_seconds: float) -> IpcResponseEnvelope:
    deadline = time.time() + timeout_seconds
    response_path = RESPONSE_DIR / f"{request_id}.json"
    while time.time() < deadline:
        if response_path.exists():
            data = load_json(response_path, None, expect_type=dict)
            if data is None:
                time.sleep(0.05)
                continue
            response = IpcResponseEnvelope.from_dict(data)
            if response is None:
                time.sleep(0.05)
                continue
            response_path.unlink(missing_ok=True)
            return response
        time.sleep(IPC_POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"ipc request timed out: {request_id}")


def mark_processed(request_path: Path) -> None:
    ensure_ipc_dirs()
    target = PROCESSED_DIR / request_path.name
    request_path.replace(target)


def mark_bridge_processed(request_path: Path, *, channel: str = "wechat") -> None:
    target = bridge_processed_dir(channel) / request_path.name
    request_path.replace(target)


def cleanup_processed_requests(*, max_age_seconds: int = PROCESSED_RETENTION_SECONDS) -> None:
    ensure_ipc_dirs()
    cutoff = time.time() - max_age_seconds
    directories = [PROCESSED_DIR, BRIDGE_PROCESSED_DIR]
    for channel_root in BRIDGE_CHANNELS_DIR.glob("*"):
        processed_dir = channel_root / "processed"
        if processed_dir.is_dir():
            directories.append(processed_dir)
    for directory in directories:
        for path in directory.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
