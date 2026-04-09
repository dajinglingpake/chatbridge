from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR / ".runtime"
IPC_DIR = RUNTIME_DIR / "ipc"
REQUEST_DIR = IPC_DIR / "requests"
RESPONSE_DIR = IPC_DIR / "responses"
PROCESSED_DIR = IPC_DIR / "processed"


def ensure_ipc_dirs() -> None:
    for path in [IPC_DIR, REQUEST_DIR, RESPONSE_DIR, PROCESSED_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def create_request(action: str, payload: dict[str, Any]) -> str:
    ensure_ipc_dirs()
    request_id = f"req-{uuid.uuid4().hex}"
    request_path = REQUEST_DIR / f"{request_id}.json"
    request_path.write_text(
        json.dumps({"id": request_id, "action": action, "payload": payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return request_id


def read_request(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_response(request_id: str, payload: dict[str, Any]) -> None:
    ensure_ipc_dirs()
    response_path = RESPONSE_DIR / f"{request_id}.json"
    response_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def wait_for_response(request_id: str, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    response_path = RESPONSE_DIR / f"{request_id}.json"
    while time.time() < deadline:
        if response_path.exists():
            data = json.loads(response_path.read_text(encoding="utf-8"))
            response_path.unlink(missing_ok=True)
            return data
        time.sleep(0.25)
    raise TimeoutError(f"ipc request timed out: {request_id}")


def mark_processed(request_path: Path) -> None:
    ensure_ipc_dirs()
    target = PROCESSED_DIR / request_path.name
    request_path.replace(target)
