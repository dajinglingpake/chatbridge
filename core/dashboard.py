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
from core.state_models import CheckSnapshot, ExternalAgentProcessState, HubStateSnapshot, RuntimeSnapshot, BridgeRuntimeState, WeixinConversationBinding
from runtime_stack import (
    BRIDGE_CONVERSATIONS_PATH,
    BRIDGE_ERR_LOG,
    BRIDGE_OUT_LOG,
    BRIDGE_STATE_PATH,
    HUB_ERR_LOG,
    HUB_OUT_LOG,
    HUB_STATE_PATH,
    ONEBOT_RUNTIME_ERR_LOG,
    ONEBOT_RUNTIME_OUT_LOG,
    QQ_BRIDGE_ERR_LOG,
    QQ_BRIDGE_OUT_LOG,
    discover_external_agent_processes,
    get_runtime_snapshot,
    read_json,
)


@dataclass
class DashboardState:
    snapshot: RuntimeSnapshot
    bridge_config: BridgeConfig
    hub_state: HubStateSnapshot
    bridge_state: BridgeRuntimeState
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
    runtime_process_discovery: bool = False
    runtime_qq_login_status: bool = False


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
_STATE_FILE_CACHE: dict[tuple[str, str, object], tuple[tuple[int, int], object]] = {}
CacheValueT = TypeVar("CacheValueT")
_EXPECTED_LOG_NOISE_MARKERS = ("[bridge] poll error: the read operation timed out",)
_RUNTIME_SNAPSHOT_CACHE_SECONDS = 2.0
_LOG_TAIL_READ_CHUNK_BYTES = 64 * 1024
_LOG_TAIL_MAX_BYTES = 256 * 1024


def _page_load_profile(page_key: str) -> PageLoadProfile:
    normalized = (page_key or "home").strip().lower()
    return PageLoadProfile(
        checks_mode="light" if normalized == "home" else ("full" if normalized == "diagnostics" else "none"),
        logs=normalized == "diagnostics",
        external_agent_processes=normalized == "diagnostics",
        bridge_conversations=normalized == "sessions",
        runtime_process_discovery=normalized in {"home", "diagnostics"},
        runtime_qq_login_status=normalized == "home",
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
        snapshot = _read_runtime_snapshot(include_agent_processes=False)
        hub_started_at = _process_started_at(snapshot.hub_pid)
        bridge_started_at = _process_started_at(snapshot.bridge_pid)
        onebot_runtime_started_at = _process_started_at(snapshot.onebot_runtime_pid)
        qq_bridge_started_at = _process_started_at(snapshot.qq_bridge_pid)
        _write_cached_payload(
            "logs",
            _load_logs(
                hub_started_at=hub_started_at,
                bridge_started_at=bridge_started_at,
                onebot_runtime_started_at=onebot_runtime_started_at,
                qq_bridge_started_at=qq_bridge_started_at,
            ),
        )
        return
    if normalized == "external_agent_processes":
        _write_cached_payload("external_agent_processes", discover_external_agent_processes())
        return
    if normalized in {"runtime", "runtime_snapshot"}:
        _write_cached_payload(
            "runtime_snapshot:False:True:True",
            get_runtime_snapshot(
                include_agent_processes=False,
                include_qq_login_status=True,
                discover_missing_processes=True,
            ),
        )
        return
    raise ValueError(f"unsupported dashboard cache key: {cache_key}")


def _read_cached_checks(checks_mode: str) -> dict[str, CheckSnapshot]:
    if checks_mode == "full":
        return _read_cached_payload("checks:full", {})
    if checks_mode == "light":
        return _read_cached_payload("checks:light", {})
    return {}


def _load_logs(*, hub_started_at: float | None, bridge_started_at: float | None, onebot_runtime_started_at: float | None, qq_bridge_started_at: float | None) -> dict[str, str]:
    return {
        "hub_out": tail_text(HUB_OUT_LOG, start_marker="ChatBridge backend started"),
        "hub_err": tail_text(HUB_ERR_LOG, stale_before=hub_started_at),
        "bridge_out": tail_text(
            BRIDGE_OUT_LOG,
            suppress_expected_noise=True,
            start_marker="Weixin Hub Bridge started at",
        ),
        "bridge_err": tail_text(BRIDGE_ERR_LOG, stale_before=bridge_started_at),
        "onebot_runtime_out": tail_text(ONEBOT_RUNTIME_OUT_LOG),
        "onebot_runtime_err": tail_text(ONEBOT_RUNTIME_ERR_LOG, stale_before=onebot_runtime_started_at),
        "qq_bridge_out": tail_text(QQ_BRIDGE_OUT_LOG, start_marker="QQ OneBot Bridge listening on"),
        "qq_bridge_err": tail_text(QQ_BRIDGE_ERR_LOG, stale_before=qq_bridge_started_at),
    }


def _get_progressive_full_checks(app_dir: Path, bridge_config: BridgeConfig) -> tuple[dict[str, CheckSnapshot], bool, str]:
    del app_dir, bridge_config
    payload = _read_cached_payload("checks:full", {})
    return payload, False, ""


def _read_cached(cache_key: str, loader: Callable[[], CacheValueT], ttl_seconds: float) -> CacheValueT:
    now = time.monotonic()
    cached = _RUNTIME_CACHE.get(cache_key)
    if cached is not None and cached.is_fresh(now=now, ttl_seconds=ttl_seconds):
        return cast(CacheValueT, cached.payload)
    payload = loader()
    _RUNTIME_CACHE[cache_key] = RuntimeCacheEntry(cached_at=now, payload=payload)
    return payload

def _read_runtime_snapshot(
    *,
    include_agent_processes: bool = False,
    include_qq_login_status: bool = True,
    discover_missing_processes: bool = True,
) -> RuntimeSnapshot:
    cache_key = f"runtime_snapshot:{include_agent_processes}:{include_qq_login_status}:{discover_missing_processes}"
    return _read_cached(
        cache_key,
        lambda: get_runtime_snapshot(
            include_agent_processes=include_agent_processes,
            include_qq_login_status=include_qq_login_status,
            discover_missing_processes=discover_missing_processes,
        ),
        _RUNTIME_SNAPSHOT_CACHE_SECONDS,
    )


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
        stat = path.stat()
        if stale_before is not None and stat.st_mtime < stale_before:
            return "(empty)"
    except OSError:
        return "(unreadable)"
    lines = _read_tail_lines(path, max_lines=max_lines, start_marker=start_marker, file_size=stat.st_size)
    if start_marker:
        for index in range(len(lines) - 1, -1, -1):
            if start_marker in lines[index]:
                lines = lines[index:]
                break
    if suppress_expected_noise:
        lines = _without_expected_log_noise(lines)
    return "\n".join(lines[-max_lines:]) if lines else "(empty)"

def _read_tail_lines(path: Path, *, max_lines: int, start_marker: str = "", file_size: int = -1) -> list[str]:
    if file_size == 0:
        return []
    safe_max_lines = max(1, int(max_lines))
    if file_size < 0:
        try:
            file_size = path.stat().st_size
        except OSError:
            return []
    chunks: list[bytes] = []
    bytes_read = 0
    remaining = file_size
    try:
        with path.open("rb") as handle:
            while remaining > 0 and bytes_read < _LOG_TAIL_MAX_BYTES:
                read_size = min(_LOG_TAIL_READ_CHUNK_BYTES, remaining, _LOG_TAIL_MAX_BYTES - bytes_read)
                remaining -= read_size
                handle.seek(remaining)
                chunk = handle.read(read_size)
                if not chunk:
                    break
                chunks.append(chunk)
                bytes_read += len(chunk)
                text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
                lines = text.splitlines()
                if start_marker and any(start_marker in line for line in lines):
                    return lines
                if len(lines) > safe_max_lines:
                    return lines
    except OSError:
        return []
    if not chunks:
        return []
    return b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines()


def load_dashboard_state(
    app_dir: Path,
    page_key: str = "home",
    load_bridge_conversations: bool | None = None,
    include_hub_tasks: bool = True,
    include_hub_task_text: bool = True,
) -> DashboardState:
    profile = _page_load_profile(page_key)
    should_load_bridge_conversations = profile.bridge_conversations if load_bridge_conversations is None else load_bridge_conversations
    snapshot = _read_runtime_snapshot(
        include_agent_processes=False,
        include_qq_login_status=profile.runtime_qq_login_status,
        discover_missing_processes=profile.runtime_process_discovery,
    )
    bridge_config = BridgeConfig.load()
    hub_state = _read_hub_state(HUB_STATE_PATH, bridge_config, include_tasks=include_hub_tasks, include_task_text=include_hub_task_text)
    bridge_state = _read_bridge_state(BRIDGE_STATE_PATH)
    bridge_conversations = _read_bridge_conversations(BRIDGE_CONVERSATIONS_PATH, bridge_config) if should_load_bridge_conversations else {}
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


def _file_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (-1, -1)
    return int(stat.st_mtime_ns), int(stat.st_size)

def _read_cached_state_file(
    cache_name: str,
    path: Path,
    variant: object,
    loader: Callable[[], CacheValueT],
) -> CacheValueT:
    signature = _file_signature(path)
    cache_key = (cache_name, str(path), variant)
    cached = _STATE_FILE_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return cast(CacheValueT, cached[1])
    payload = loader()
    _STATE_FILE_CACHE[cache_key] = (signature, payload)
    return payload

def _read_hub_state(path: Path, bridge_config: BridgeConfig, *, include_tasks: bool = True, include_task_text: bool = True) -> HubStateSnapshot:
    default_backend = bridge_config.default_backend
    return _read_cached_state_file(
        "hub",
        path,
        (default_backend, bool(include_tasks), bool(include_task_text)),
        lambda: HubStateSnapshot.from_dict(
            read_json(path),
            default_backend=default_backend,
            now=_state_now(),
            include_tasks=include_tasks,
            include_task_text=include_task_text,
        ),
    )


def _read_bridge_state(path: Path) -> BridgeRuntimeState:
    return _read_cached_state_file(
        "bridge",
        path,
        "",
        lambda: BridgeRuntimeState.from_dict(read_json(path)),
    )


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
