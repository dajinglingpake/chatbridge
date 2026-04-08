from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from bridge_config import APP_DIR, BridgeConfig, WeixinAccountProfile


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


def resolve_ilink_base_url(config: BridgeConfig | None = None) -> str:
    try:
        config = config or BridgeConfig.load()
        if config.accounts:
            first_account = config.accounts[0]
            if first_account.account_path.exists():
                data = json.loads(first_account.account_path.read_text(encoding="utf-8"))
                base_url = str(data.get("baseUrl") or "").strip()
                if base_url:
                    return base_url
    except Exception:
        pass
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


def save_account_from_qr_payload(data: dict, base_url: str = "") -> SavedAccount | None:
    account_id = str(data.get("ilink_bot_id") or f"wechat-{datetime.now().strftime('%Y%m%d%H%M%S')}").strip()
    resolved_base_url = str(data.get("baseurl") or base_url or DEFAULT_ILINK_BASE_URL).strip()
    bot_token = str(data.get("bot_token") or "").strip()
    if not bot_token:
        return None

    account_file = APP_DIR / "accounts" / f"{account_id}.json"
    sync_file = APP_DIR / "accounts" / f"{account_id}.sync.json"
    account_file.parent.mkdir(parents=True, exist_ok=True)

    account_file.write_text(
        json.dumps(
            {
                "token": bot_token,
                "baseUrl": resolved_base_url,
                "name": account_id,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    sync_file.write_text(json.dumps({"get_updates_buf": ""}, ensure_ascii=False), encoding="utf-8")

    config = BridgeConfig.load()
    new_profile = config.add_account(account_id, str(account_file), str(sync_file))
    if new_profile is None:
        return None
    config.set_active_account(new_profile.account_id)
    config.save()
    return SavedAccount(account_id=account_id, account_file=account_file, sync_file=sync_file)


def activate_account(account_id: str) -> BridgeConfig:
    config = BridgeConfig.load()
    config.set_active_account(account_id)
    config.save()
    return config
