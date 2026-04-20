from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from bridge_config import APP_DIR, BridgeConfig, WeixinAccountProfile
from core.json_store import load_json, save_json
from core.state_models import JsonObject


DEFAULT_ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"


@dataclass
class AccountOption:
    key: str
    text: str
    account: WeixinAccountProfile | None = None


@dataclass
class SavedAccount:
    account_id: str
    account_file: Path
    sync_file: Path


@dataclass
class AccountFilePayload:
    token: str = ""
    base_url: str = ""
    name: str = ""

    @classmethod
    def from_dict(cls, raw: object) -> "AccountFilePayload":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            token=str(raw.get("token") or "").strip(),
            base_url=str(raw.get("baseUrl") or "").strip(),
            name=str(raw.get("name") or "").strip(),
        )

    def to_dict(self) -> JsonObject:
        return {
            "token": self.token,
            "baseUrl": self.base_url,
            "name": self.name,
        }


@dataclass
class QRConfirmedPayload:
    account_id: str
    base_url: str
    bot_token: str

    @classmethod
    def from_dict(cls, raw: object, *, fallback_base_url: str = "") -> "QRConfirmedPayload":
        if not isinstance(raw, dict):
            return cls(account_id="", base_url=(fallback_base_url or DEFAULT_ILINK_BASE_URL).strip(), bot_token="")
        account_id = str(raw.get("ilink_bot_id") or f"wechat-{datetime.now().strftime('%Y%m%d%H%M%S')}").strip()
        base_url = str(raw.get("baseurl") or fallback_base_url or DEFAULT_ILINK_BASE_URL).strip() or DEFAULT_ILINK_BASE_URL
        bot_token = str(raw.get("bot_token") or "").strip()
        return cls(account_id=account_id, base_url=base_url, bot_token=bot_token)


def load_account_file_payload(account_path: Path) -> AccountFilePayload:
    data = load_json(account_path, {}, expect_type=dict)
    return AccountFilePayload.from_dict(data)


def context_tokens_path_for_account(account_path: Path) -> Path:
    return account_path.with_name(f"{account_path.stem}.context-tokens.json")


def load_account_context_tokens(account_path: Path) -> dict[str, str]:
    payload = load_json(context_tokens_path_for_account(account_path), {}, expect_type=dict)
    if not isinstance(payload, dict):
        return {}
    tokens: dict[str, str] = {}
    for sender_id, context_token in payload.items():
        cleaned_sender_id = str(sender_id or "").strip()
        cleaned_context_token = str(context_token or "").strip()
        if cleaned_sender_id and cleaned_context_token:
            tokens[cleaned_sender_id] = cleaned_context_token
    return tokens


def save_account_context_tokens(account_path: Path, tokens: dict[str, str]) -> None:
    save_json(
        context_tokens_path_for_account(account_path),
        {str(sender_id).strip(): str(context_token).strip() for sender_id, context_token in tokens.items() if str(sender_id).strip() and str(context_token).strip()},
    )


def resolve_ilink_base_url(config: BridgeConfig | None = None) -> str:
    resolved_config = config or BridgeConfig.load()
    active_account = resolved_config.get_active_account()
    payload = load_account_file_payload(active_account.account_path)
    if payload.base_url:
        return payload.base_url
    return DEFAULT_ILINK_BASE_URL


def account_option_text(config: BridgeConfig, account: WeixinAccountProfile, t: Callable[[str], str] | Callable[..., str]) -> str:
    status = t("ui.account.status.ready") if account.is_usable else t("ui.account.status.missing")
    marker = t("ui.account.option.active") if account.account_id == config.active_account_id else t("ui.account.option.inactive")
    return t(
        "ui.account.option.label",
        marker=marker,
        account=account.account_id,
        file=Path(account.account_file).name,
        status=status,
    )


def build_account_options(config: BridgeConfig, t: Callable[[str], str] | Callable[..., str]) -> tuple[list[AccountOption], int]:
    options: list[AccountOption] = []
    current_index = 0

    for index, account in enumerate(config.accounts):
        options.append(AccountOption(key="existing", text=account_option_text(config, account, t), account=account))
        if account.account_id == config.active_account_id:
            current_index = index

    options.append(AccountOption(key="qr_login", text=t("ui.account.qr_login.option")))
    return options, current_index


def save_account_from_qr_payload(data: object, base_url: str = "", config: BridgeConfig | None = None) -> SavedAccount | None:
    payload = QRConfirmedPayload.from_dict(data, fallback_base_url=base_url)
    if not payload.bot_token:
        return None

    account_file = APP_DIR / "accounts" / f"{payload.account_id}.json"
    sync_file = APP_DIR / "accounts" / f"{payload.account_id}.sync.json"
    account_file.parent.mkdir(parents=True, exist_ok=True)

    save_json(
        account_file,
        AccountFilePayload(
            token=payload.bot_token,
            base_url=payload.base_url,
            name=payload.account_id,
        ).to_dict(),
    )
    save_json(sync_file, {"get_updates_buf": ""})

    resolved_config = config or BridgeConfig.load()
    new_profile = resolved_config.add_account(payload.account_id, str(account_file), str(sync_file))
    if new_profile is None:
        return None
    resolved_config.set_active_account(new_profile.account_id)
    resolved_config.save()
    return SavedAccount(account_id=payload.account_id, account_file=account_file, sync_file=sync_file)


def activate_account(account_id: str, config: BridgeConfig | None = None) -> BridgeConfig:
    resolved_config = config or BridgeConfig.load()
    resolved_config.set_active_account(account_id)
    resolved_config.save()
    return resolved_config
