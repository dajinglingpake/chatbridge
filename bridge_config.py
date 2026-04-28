from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from agent_backends import DEFAULT_BACKEND_KEY, supported_backend_keys
from core.json_store import load_json, save_json

APP_DIR = Path(__file__).resolve().parent
WEIXIN_ACCOUNTS_DIR = APP_DIR / "accounts"
CONFIG_PATH = APP_DIR / "config" / "weixin_bridge.json"
ACCOUNT_STATE_PATH = WEIXIN_ACCOUNTS_DIR / "bridge-account-state.local.json"
SUPPORTED_BACKENDS = set(supported_backend_keys())


def _to_abs_path(value: str, default: Path) -> str:
    raw = (value or "").strip()
    path = Path(raw) if raw else default
    if not path.is_absolute():
        path = APP_DIR / path
    return str(path.resolve())


def _to_rel_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(APP_DIR.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def normalize_backend(value: str) -> str:
    backend = (value or DEFAULT_BACKEND_KEY).strip().lower()
    return backend if backend in SUPPORTED_BACKENDS else DEFAULT_BACKEND_KEY


@dataclass
class WeixinAccountProfile:
    account_id: str
    account_file: str
    sync_file: str

    @property
    def account_path(self) -> Path:
        return Path(self.account_file)

    @property
    def sync_path(self) -> Path:
        return Path(self.sync_file)

    @property
    def is_usable(self) -> bool:
        return self.account_path.exists() and self.sync_path.exists()


def default_account_profile() -> WeixinAccountProfile:
    return WeixinAccountProfile(
        account_id="wechat-bot",
        account_file=_to_abs_path("accounts/wechat-bot.json", WEIXIN_ACCOUNTS_DIR / "wechat-bot.json"),
        sync_file=_to_abs_path("accounts/wechat-bot.sync.json", WEIXIN_ACCOUNTS_DIR / "wechat-bot.sync.json"),
    )


def discover_account_profiles(account_dir: Path = WEIXIN_ACCOUNTS_DIR) -> list[WeixinAccountProfile]:
    if not account_dir.exists():
        return []
    profiles: list[WeixinAccountProfile] = []
    for account_path in sorted(account_dir.glob("*.json")):
        if account_path.name.endswith(".sync.json") or account_path.name.endswith(".context-tokens.json"):
            continue
        sync_path = account_path.with_name(f"{account_path.stem}.sync.json")
        if not sync_path.exists():
            continue
        profiles.append(
            WeixinAccountProfile(
                account_id=account_path.stem,
                account_file=str(account_path.resolve()),
                sync_file=str(sync_path.resolve()),
            )
        )
    return profiles


def _is_qr_bot_account(profile: WeixinAccountProfile) -> bool:
    return profile.account_id.endswith("@im.bot")


def _profile_latest_mtime(profile: WeixinAccountProfile) -> float:
    paths = [
        Path(profile.account_file),
        Path(profile.sync_file),
        Path(profile.account_file).with_name(f"{Path(profile.account_file).stem}.context-tokens.json"),
    ]
    mtimes = [path.stat().st_mtime for path in paths if path.exists()]
    return max(mtimes) if mtimes else 0.0


def collapse_qr_bot_profiles(accounts: list[WeixinAccountProfile], preferred_account_id: str = "") -> list[WeixinAccountProfile]:
    qr_accounts = [profile for profile in accounts if _is_qr_bot_account(profile)]
    if len(qr_accounts) <= 1:
        return accounts
    preferred = str(preferred_account_id or "").strip()
    preferred_qr_account = next((profile for profile in qr_accounts if profile.account_id == preferred and profile.is_usable), None)
    latest_qr_account = preferred_qr_account or max(qr_accounts, key=_profile_latest_mtime)
    collapsed = [profile for profile in accounts if not _is_qr_bot_account(profile)]
    collapsed.append(latest_qr_account)
    return [collapsed[key] for key in sorted(range(len(collapsed)), key=lambda index: collapsed[index].account_id)]


def _normalize_profile(raw: object) -> WeixinAccountProfile | None:
    if not isinstance(raw, dict):
        return None
    account_id = str(raw.get("account_id") or raw.get("id") or "").strip()
    account_file = str(raw.get("account_file") or "").strip()
    sync_file = str(raw.get("sync_file") or "").strip()
    if not account_id or not account_file or not sync_file:
        return None
    return WeixinAccountProfile(
        account_id=account_id,
        account_file=_to_abs_path(account_file, WEIXIN_ACCOUNTS_DIR / f"{account_id}.json"),
        sync_file=_to_abs_path(sync_file, WEIXIN_ACCOUNTS_DIR / f"{account_id}.sync.json"),
    )


def merge_account_profiles(*groups: list[WeixinAccountProfile]) -> list[WeixinAccountProfile]:
    merged: dict[str, WeixinAccountProfile] = {}
    for group in groups:
        for profile in group:
            merged[profile.account_id] = profile
    return [merged[key] for key in sorted(merged)]


def select_active_account_id(accounts: list[WeixinAccountProfile], preferred: str = "") -> str:
    if not accounts:
        return default_account_profile().account_id
    usable_ids = [profile.account_id for profile in accounts if profile.is_usable]
    if preferred and any(profile.account_id == preferred and profile.is_usable for profile in accounts):
        return preferred
    if usable_ids:
        return usable_ids[0]
    if preferred and any(profile.account_id == preferred for profile in accounts):
        return preferred
    return accounts[0].account_id


def build_account_profiles(raw: dict[str, object]) -> tuple[list[WeixinAccountProfile], str]:
    runtime_state = load_account_runtime_state()
    preferred = str(runtime_state.get("active_account_id") or raw.get("active_account_id") or raw.get("account_id") or "").strip()
    configured = [_normalize_profile(item) for item in raw.get("accounts", []) or []]
    configured_profiles = [profile for profile in configured if profile is not None]
    legacy_account_id = str(raw.get("account_id") or "").strip() or "wechat-bot"
    legacy_profile = _normalize_profile(
        {
            "account_id": legacy_account_id,
            "account_file": raw.get("account_file") or f"accounts/{legacy_account_id}.json",
            "sync_file": raw.get("sync_file") or f"accounts/{legacy_account_id}.sync.json",
        }
    )
    discovered_profiles = discover_account_profiles()
    accounts = collapse_qr_bot_profiles(
        merge_account_profiles(configured_profiles, [legacy_profile] if legacy_profile else [], discovered_profiles),
        preferred_account_id=preferred,
    )
    if not accounts:
        accounts = [default_account_profile()]
    active_account_id = select_active_account_id(accounts, preferred)
    return accounts, active_account_id


def load_account_runtime_state() -> dict[str, object]:
    raw = load_json(ACCOUNT_STATE_PATH, {}, expect_type=dict)
    return raw if isinstance(raw, dict) else {}


@dataclass
class BridgeConfig:
    active_account_id: str = "wechat-bot"
    accounts: list[WeixinAccountProfile] = field(default_factory=lambda: [default_account_profile()])
    account_id: str = "wechat-bot"
    account_file: str = "accounts/wechat-bot.json"
    sync_file: str = "accounts/wechat-bot.sync.json"
    backend_id: str = "main"
    default_backend: str = DEFAULT_BACKEND_KEY
    service_notice_enabled: bool = True
    config_notice_enabled: bool = True
    task_notice_enabled: bool = False
    language: str = "auto"
    poll_timeout_ms: int = 35000
    hub_task_timeout_seconds: int = 600
    bridge_name: str = "weixin-bridge"
    auto_reply_prefix: str = ""
    ignore_prefixes: list[str] = field(default_factory=lambda: ["/ignore"])

    @classmethod
    def load(cls) -> "BridgeConfig":
        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg._sync_active_account_fields()
            cfg.save()
            return cfg
        raw = load_json(CONFIG_PATH, None, expect_type=dict)
        if raw is None:
            cfg = cls()
            cfg._sync_active_account_fields()
            cfg.save()
            return cfg
        accounts, active_account_id = build_account_profiles(raw)
        raw["accounts"] = accounts
        raw["active_account_id"] = active_account_id
        raw["default_backend"] = normalize_backend(str(raw.get("default_backend") or DEFAULT_BACKEND_KEY))
        raw["service_notice_enabled"] = bool(raw.get("service_notice_enabled", True))
        raw["config_notice_enabled"] = bool(raw.get("config_notice_enabled", True))
        raw["task_notice_enabled"] = bool(raw.get("task_notice_enabled", False))
        raw["language"] = str(raw.get("language") or "auto")
        cfg = cls(**raw)
        cfg._sync_active_account_fields()
        return cfg

    def _sync_active_account_fields(self) -> None:
        self.active_account_id = select_active_account_id(self.accounts, self.active_account_id or self.account_id)
        active = self.get_active_account()
        self.account_id = active.account_id
        self.account_file = active.account_file
        self.sync_file = active.sync_file

    def get_active_account(self) -> WeixinAccountProfile:
        for profile in self.accounts:
            if profile.account_id == self.active_account_id:
                return profile
        fallback = self.accounts[0] if self.accounts else default_account_profile()
        self.active_account_id = fallback.account_id
        return fallback

    def set_active_account(self, account_id: str) -> None:
        self.active_account_id = account_id
        self._sync_active_account_fields()

    def add_account(self, account_id: str, account_file: str, sync_file: str) -> WeixinAccountProfile | None:
        existing = [p for p in self.accounts if p.account_id == account_id]
        for p in existing:
            self.accounts.remove(p)
        
        profile = WeixinAccountProfile(
            account_id=account_id,
            account_file=account_file,
            sync_file=sync_file,
        )
        self.accounts.append(profile)
        return profile

    def set_backend_agent(self, agent_id: str) -> None:
        cleaned = str(agent_id or "").strip()
        if not cleaned:
            raise ValueError("backend_id is required")
        self.backend_id = cleaned

    def save(self) -> None:
        self._sync_active_account_fields()
        data = {
            "backend_id": self.backend_id,
            "default_backend": normalize_backend(self.default_backend),
            "service_notice_enabled": bool(self.service_notice_enabled),
            "config_notice_enabled": bool(self.config_notice_enabled),
            "task_notice_enabled": bool(self.task_notice_enabled),
            "language": str(self.language or "auto"),
            "poll_timeout_ms": int(self.poll_timeout_ms),
            "hub_task_timeout_seconds": int(self.hub_task_timeout_seconds),
            "bridge_name": self.bridge_name,
            "auto_reply_prefix": self.auto_reply_prefix,
            "ignore_prefixes": list(self.ignore_prefixes),
        }
        save_json(CONFIG_PATH, data)
        self._save_account_runtime_state()

    def _save_account_runtime_state(self) -> None:
        save_json(
            ACCOUNT_STATE_PATH,
            {
                "active_account_id": self.active_account_id,
            },
        )
