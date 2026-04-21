from __future__ import annotations

import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent.parent


def _resolve_path_override(env_key: str, default: Path) -> Path:
    raw = str(os.environ.get(env_key) or "").strip()
    if not raw:
        return default
    return Path(raw).expanduser().resolve()


_runtime_override = str(os.environ.get("CHATBRIDGE_RUNTIME_ROOT") or "").strip()
RUNTIME_DIR = Path(_runtime_override).expanduser().resolve() if _runtime_override else APP_DIR / ".runtime"
STATE_DIR = RUNTIME_DIR / "state"
LOG_DIR = RUNTIME_DIR / "logs"
SESSION_DIR = APP_DIR / "sessions"
WORKSPACE_DIR = APP_DIR / "workspace"

HUB_PID_FILE = RUNTIME_DIR / "agent_hub.pid"
BRIDGE_PID_FILE = RUNTIME_DIR / "weixin_hub_bridge.pid"
HUB_OUT_LOG = LOG_DIR / "agent_hub.out.log"
HUB_ERR_LOG = LOG_DIR / "agent_hub.err.log"
BRIDGE_OUT_LOG = LOG_DIR / "weixin_hub_bridge.out.log"
BRIDGE_ERR_LOG = LOG_DIR / "weixin_hub_bridge.err.log"
HUB_STATE_PATH = STATE_DIR / "agent_hub_state.json"
BRIDGE_STATE_PATH = _resolve_path_override("CHATBRIDGE_BRIDGE_STATE_PATH", STATE_DIR / "weixin_hub_bridge_state.json")
BRIDGE_CONVERSATIONS_PATH = _resolve_path_override("CHATBRIDGE_BRIDGE_CONVERSATIONS_PATH", STATE_DIR / "weixin_conversations.json")
BRIDGE_PENDING_TASKS_PATH = _resolve_path_override("CHATBRIDGE_BRIDGE_PENDING_TASKS_PATH", STATE_DIR / "weixin_pending_tasks.json")
PROJECT_SPACES_PATH = _resolve_path_override("CHATBRIDGE_PROJECT_SPACES_PATH", STATE_DIR / "project_spaces.json")
BRIDGE_EVENT_LOG_PATH = _resolve_path_override("CHATBRIDGE_BRIDGE_EVENT_LOG_PATH", LOG_DIR / "weixin_bridge_events.jsonl")
BRIDGE_MESSAGE_AUDIT_LOG_PATH = _resolve_path_override("CHATBRIDGE_BRIDGE_MESSAGE_AUDIT_LOG_PATH", LOG_DIR / "weixin_bridge_message_audit.jsonl")
