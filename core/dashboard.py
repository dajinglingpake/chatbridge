from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridge_config import BridgeConfig
from env_tools import collect_checks
from runtime_stack import BRIDGE_ERR_LOG, BRIDGE_OUT_LOG, BRIDGE_STATE_PATH, HUB_ERR_LOG, HUB_OUT_LOG, HUB_STATE_PATH, get_runtime_snapshot, read_json


@dataclass
class DashboardState:
    snapshot: Any
    hub_state: dict[str, Any]
    bridge_state: dict[str, Any]
    checks: dict[str, Any]
    active_account_id: str
    logs: dict[str, str]


def tail_text(path: Path, max_lines: int = 80) -> str:
    if not path.exists():
        return "(empty)"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(unreadable)"
    return "\n".join(lines[-max_lines:]) if lines else "(empty)"


def load_dashboard_state(app_dir) -> DashboardState:
    snapshot = get_runtime_snapshot()
    hub_state = read_json(HUB_STATE_PATH)
    bridge_state = read_json(BRIDGE_STATE_PATH)
    checks = {item.key: item for item in collect_checks(app_dir)}
    active_account_id = BridgeConfig.load().active_account_id
    logs = {
        "hub_out": tail_text(HUB_OUT_LOG),
        "hub_err": tail_text(HUB_ERR_LOG),
        "bridge_out": tail_text(BRIDGE_OUT_LOG),
        "bridge_err": tail_text(BRIDGE_ERR_LOG),
    }
    return DashboardState(
        snapshot=snapshot,
        hub_state=hub_state,
        bridge_state=bridge_state,
        checks=checks,
        active_account_id=active_account_id,
        logs=logs,
    )
