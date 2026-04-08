from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bridge_config import BridgeConfig
from codex_wechat_bootstrap import collect_checks
from codex_wechat_runtime import BRIDGE_STATE_PATH, HUB_STATE_PATH, get_runtime_snapshot, read_json


@dataclass
class DashboardState:
    snapshot: Any
    hub_state: dict[str, Any]
    bridge_state: dict[str, Any]
    checks: dict[str, Any]
    active_account_id: str


def load_dashboard_state(app_dir) -> DashboardState:
    snapshot = get_runtime_snapshot()
    hub_state = read_json(HUB_STATE_PATH)
    bridge_state = read_json(BRIDGE_STATE_PATH)
    checks = {item.key: item for item in collect_checks(app_dir)}
    active_account_id = BridgeConfig.load().active_account_id
    return DashboardState(
        snapshot=snapshot,
        hub_state=hub_state,
        bridge_state=bridge_state,
        checks=checks,
        active_account_id=active_account_id,
    )
