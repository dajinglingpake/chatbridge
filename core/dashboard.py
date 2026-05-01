from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Callable, Iterable, TypeVar, cast

try:
    import psutil
except ImportError:  # pragma: no cover - optional dependency
    psutil = None

from bridge_config import BridgeConfig, normalize_backend
from core.accounts import account_conversation_path
from env_tools import collect_check_step, collect_lightweight_checks, get_full_check_sequence, get_full_check_step_label
from core.state_models import CheckSnapshot, ExternalAgentProcessState, HubStateSnapshot, RuntimeSnapshot, WeixinBridgeRuntimeState, WeixinConversationBinding
from runtime_stack import (
    BRIDGE_CONVERSATIONS_PATH,
    BRIDGE_ERR_LOG,
    BRIDGE_OUT_LOG,
    BRIDGE_STATE_PATH,
    HUB_ERR_LOG,
    HUB_OUT_LOG,
    HUB_STATE_PATH,
    discover_external_agent_processes,
    get_runtime_snapshot,
    read_json,
)


@dataclass
class DashboardState:
    snapshot: RuntimeSnapshot
    bridge_config: BridgeConfig
    hub_state: HubStateSnapshot
    bridge_state: WeixinBridgeRuntimeState
    bridge_conversations: dict[str, WeixinConversationBinding]
    checks: dict[str, CheckSnapshot]
    checks_in_progress: bool
    checks_progress_text: str
    active_account_id: str
    logs: dict[str, str]
    external_agent_processes: list[ExternalAgentProcessState]


@dataclass(frozen=True)
class PageLoadProfile:
    checks_mode: str = "none"
    logs: bool = False
    external_agent_processes: bool = False
    bridge_conversations: bool = False


@dataclass
class RuntimeCacheEntry:
    cached_at: float
    payload: object

    def is_fresh(self, *, now: float, ttl_seconds: float) -> bool:
        return now - self.cached_at <= ttl_seconds


@dataclass
class FullCheckProgressState:
    results: dict[str, CheckSnapshot]
    next_index: int
    updated_at: float

    @classmethod
    def create(cls, *, now: float) -> "FullCheckProgressState":
        return cls(results={}, next_index=0, updated_at=now)

    @classmethod
    def from_cached_payload(cls, raw: object, *, now: float) -> "FullCheckProgressState":
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            return cls.create(now=now)
        results = raw.get("results")
        return cls(
            results=_coerce_check_map(results),
            next_index=_coerce_int(raw.get("next_index"), default=0),
            updated_at=_coerce_float(raw.get("updated_at"), default=now),
        )

    def is_expired(self, *, now: float, ttl_seconds: float) -> bool:
        return now - self.updated_at > ttl_seconds


_RUNTIME_CACHE: dict[str, RuntimeCacheEntry] = {}
CacheValueT = TypeVar("CacheValueT")
_EXPECTED_LOG_NOISE_MARKERS = ("[bridge] poll error: the read operation timed out",)


def _page_load_profile(page_key: str) -> PageLoadProfile:
    normalized = (page_key or "home").strip().lower()
    return PageLoadProfile(
        checks_mode="light" if normalized == "home" else ("full" if normalized == "diagnostics" else "none"),
        logs=normalized == "diagnostics",
        external_agent_processes=normalized == "diagnostics",
        bridge_conversations=normalized == "sessions",
    )


def _read_cached_payload(cache_key: str, default: CacheValueT) -> CacheValueT:
    cached = _RUNTIME_CACHE.get(cache_key)
    if cached is not None:
        return cast(CacheValueT, cached.payload)
    return default


def _write_cached_payload(cache_key: str, payload: object) -> None:
    _RUNTIME_CACHE[cache_key] = RuntimeCacheEntry(cached_at=time.monotonic(), payload=payload)


def refresh_dashboard_cache(app_dir: Path, cache_key: str) -> None:
    normalized = (cache_key or "").strip().lower()
    bridge_config = BridgeConfig.load()
    if normalized == "checks_light":
        _write_cached_payload("checks:light", _index_checks(collect_lightweight_checks(app_dir, bridge_config)))
        return
    if normalized == "checks_full":
        results = {}
        for step in get_full_check_sequence():
            results.update(_index_checks(collect_check_step(step, app_dir, bridge_config)))
        _write_cached_payload("checks:full", results)
        return
    if normalized == "logs":
        snapshot = get_runtime_snapshot(include_agent_processes=False)
        hub_started_at = _process_started_at(snapshot.hub_pid)
        bridge_started_at = _process_started_at(snapshot.bridge_pid)
        _write_cached_payload("logs", _load_logs(hub_started_at=hub_started_at, bridge_started_at=bridge_started_at))
        return
    if normalized == "external_agent_processes":
        _write_cached_payload("external_agent_processes", discover_external_agent_processes())
        return
    raise ValueError(f"unsupported dashboard cache key: {cache_key}")


def _read_cached_checks(checks_mode: str) -> dict[str, CheckSnapshot]:
    if checks_mode == "full":
        return _read_cached_payload("checks:full", {})
    if checks_mode == "light":
        return _read_cached_payload("checks:light", {})
    return {}


def _load_logs(*, hub_started_at: float | None, bridge_started_at: float | None) -> dict[str, str]:
    return {
        "hub_out": tail_text(HUB_OUT_LOG, start_marker="ChatBridge backend started"),
        "hub_err": tail_text(HUB_ERR_LOG, stale_before=hub_started_at),
        "bridge_out": tail_text(
            BRIDGE_OUT_LOG,
            suppress_expected_noise=True,
            start_marker="Weixin Hub Bridge started at",
        ),
        "bridge_err": tail_text(BRIDGE_ERR_LOG, stale_before=bridge_started_at),
    }


def _get_progressive_full_checks(app_dir: Path, bridge_config: BridgeConfig) -> tuple[dict[str, CheckSnapshot], bool, str]:
    del app_dir, bridge_config
    payload = _read_cached_payload("checks:full", {})
    return payload, False, ""


def _read_cached(cache_key: str, loader: Callable[[], CacheValueT], ttl_seconds: float) -> CacheValueT:
    del loader, ttl_seconds
    payload = _read_cached_payload(cache_key, None)
    if payload is None:
        raise KeyError(cache_key)
    return payload


def _process_started_at(pid: int | None) -> float | None:
    if pid is None or psutil is None:
        return None
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.Error, ProcessLookupError, OSError):
        return None


def _without_expected_log_noise(lines: list[str]) -> list[str]:
    filtered: list[str] = []
    for line in lines:
        lowered = line.lower()
        if any(marker in lowered for marker in _EXPECTED_LOG_NOISE_MARKERS):
            continue
        filtered.append(line)
    return filtered


def tail_text(
    path: Path,
    max_lines: int = 80,
    *,
    stale_before: float | None = None,
    suppress_expected_noise: bool = False,
    start_marker: str = "",
) -> str:
    if not path.exists():
        return "(empty)"
    try:
        if stale_before is not None and path.stat().st_mtime < stale_before:
            return "(empty)"
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(unreadable)"
    if start_marker:
        for index in range(len(lines) - 1, -1, -1):
            if start_marker in lines[index]:
                lines = lines[index:]
                break
    if suppress_expected_noise:
        lines = _without_expected_log_noise(lines)
    return "\n".join(lines[-max_lines:]) if lines else "(empty)"


def load_dashboard_state(app_dir: Path, page_key: str = "home") -> DashboardState:
    profile = _page_load_profile(page_key)
    snapshot = get_runtime_snapshot(include_agent_processes=False)
    bridge_config = BridgeConfig.load()
    hub_state = _read_hub_state(HUB_STATE_PATH, bridge_config)
    bridge_state = _read_bridge_state(BRIDGE_STATE_PATH)
    bridge_conversations = _read_bridge_conversations(BRIDGE_CONVERSATIONS_PATH, bridge_config) if profile.bridge_conversations else {}
    checks_mode = profile.checks_mode
    checks_in_progress = False
    checks_progress_text = ""
    if checks_mode == "full":
        checks, checks_in_progress, checks_progress_text = _get_progressive_full_checks(app_dir, bridge_config)
    elif checks_mode == "light":
        checks = _read_cached_checks("light")
    else:
        checks = {}
    active_account_id = bridge_config.active_account_id
    logs = _read_cached_payload("logs", {}) if profile.logs else {}
    return DashboardState(
        snapshot=snapshot,
        bridge_config=bridge_config,
        hub_state=hub_state,
        bridge_state=bridge_state,
        bridge_conversations=bridge_conversations,
        checks=checks,
        checks_in_progress=checks_in_progress,
        checks_progress_text=checks_progress_text,
        active_account_id=active_account_id,
        logs=logs,
        external_agent_processes=_read_cached_payload("external_agent_processes", []) if profile.external_agent_processes else [],
    )


def _coerce_int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: object, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_hub_state(path: Path, bridge_config: BridgeConfig) -> HubStateSnapshot:
    return HubStateSnapshot.from_dict(
        read_json(path),
        default_backend=bridge_config.default_backend,
        now=_state_now(),
    )


def _read_bridge_state(path: Path) -> WeixinBridgeRuntimeState:
    return WeixinBridgeRuntimeState.from_dict(read_json(path))


def _read_bridge_conversations(path: Path, bridge_config: BridgeConfig) -> dict[str, WeixinConversationBinding]:
    active_path = account_conversation_path(path, bridge_config.active_account_id, bridge_config.account_file)
    payload = read_json(active_path)
    bindings: dict[str, WeixinConversationBinding] = {}
    for sender_id, raw_binding in payload.items():
        cleaned_sender_id = str(sender_id or "").strip()
        if not cleaned_sender_id:
            continue
        bindings[cleaned_sender_id] = WeixinConversationBinding.from_dict(
            raw_binding,
            default_backend=bridge_config.default_backend,
            now=_state_now(),
            normalize_backend=normalize_backend,
        )
    return bindings


def _index_checks(results: Iterable[object]) -> dict[str, CheckSnapshot]:
    checks: dict[str, CheckSnapshot] = {}
    for item in results:
        check = CheckSnapshot.from_result(item)
        if check is None:
            continue
        checks[check.key] = check
    return checks


def _coerce_check_map(raw: object) -> dict[str, CheckSnapshot]:
    if not isinstance(raw, dict):
        return {}
    checks: dict[str, CheckSnapshot] = {}
    for key, value in raw.items():
        check = CheckSnapshot.from_result(value)
        if check is None:
            check = CheckSnapshot.from_dict(value)
        if check is None:
            continue
        checks[str(key or check.key)] = check
    return checks


def _state_now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
