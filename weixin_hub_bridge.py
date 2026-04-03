from __future__ import annotations

import base64
import hashlib
import json
import random
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from codex_wechat_ipc import create_request, wait_for_response


APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR / ".runtime"
STATE_DIR = RUNTIME_DIR / "state"
WEIXIN_ACCOUNTS_DIR = APP_DIR / "accounts"
CONFIG_PATH = APP_DIR / "weixin_hub_bridge_config.json"
STATE_PATH = STATE_DIR / "weixin_hub_bridge_state.json"
CONVERSATION_PATH = STATE_DIR / "weixin_conversations.json"
DEFAULT_WEIXIN_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (1 << 8) | 1


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


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


@dataclass
class BridgeConfig:
    account_id: str = "wechat-bot"
    account_file: str = "accounts/wechat-bot.json"
    sync_file: str = "accounts/wechat-bot.sync.json"
    backend_id: str = "main"
    poll_timeout_ms: int = 35000
    hub_task_timeout_seconds: int = 600
    bridge_name: str = "weixin-bridge"
    auto_reply_prefix: str = ""
    ignore_prefixes: list[str] = field(default_factory=lambda: ["/ignore"])

    @classmethod
    def load(cls) -> "BridgeConfig":
        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg.save()
            return cfg
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if "default_agent_id" in raw and "backend_id" not in raw:
            raw["backend_id"] = raw.pop("default_agent_id")
        raw["account_file"] = _to_abs_path(str(raw.get("account_file") or "accounts/wechat-bot.json"), WEIXIN_ACCOUNTS_DIR / "wechat-bot.json")
        raw["sync_file"] = _to_abs_path(str(raw.get("sync_file") or "accounts/wechat-bot.sync.json"), WEIXIN_ACCOUNTS_DIR / "wechat-bot.sync.json")
        return cls(**raw)

    def save(self) -> None:
        data = asdict(self)
        data["account_file"] = _to_rel_path(str(data.get("account_file") or (WEIXIN_ACCOUNTS_DIR / "wechat-bot.json")))
        data["sync_file"] = _to_rel_path(str(data.get("sync_file") or (WEIXIN_ACCOUNTS_DIR / "wechat-bot.sync.json")))
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class WeixinBridge:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.account_path = Path(config.account_file)
        self.sync_path = Path(config.sync_file)
        self._ensure_local_account_storage()
        self.conversations = self._load_conversations()
        self._recent_message_keys: list[str] = []
        self.state: dict[str, Any] = {
            "started_at": now_iso(),
            "last_poll_at": "",
            "last_message_at": "",
            "last_sender_id": "",
            "last_error": "",
            "handled_messages": 0,
            "failed_messages": 0,
            "managed_conversations": len(self.conversations),
            "account_file": str(self.account_path),
            "sync_file": str(self.sync_path),
            "using_local_account_storage": True,
        }

    def run(self) -> None:
        print(f"Weixin Hub Bridge started at {now_iso()}")
        print(f"Config: {CONFIG_PATH}")
        print(f"State: {STATE_PATH}")
        while True:
            try:
                self.poll_once()
                self.state["last_error"] = ""
            except Exception as exc:  # noqa: BLE001
                self.state["last_error"] = str(exc)
                self._save_state()
                print(f"[bridge] poll error: {exc}")
                time.sleep(3)

    def poll_once(self) -> None:
        account = self._load_account()
        token = (account.get("token") or "").strip()
        if not token:
            raise RuntimeError("weixin account token is missing; please log in first")
        base_url = (account.get("baseUrl") or DEFAULT_WEIXIN_BASE_URL).strip()
        buf = self._load_sync_buf()

        payload = {"get_updates_buf": buf, "base_info": {"channel_version": "2.1.1"}}
        response = self._post_json(f"{base_url}/ilink/bot/getupdates", payload, token=token, timeout_ms=self.config.poll_timeout_ms)
        self.state["last_poll_at"] = now_iso()
        if response.get("ret") not in (None, 0):
            raise RuntimeError(f"weixin getupdates failed: ret={response.get('ret')} errcode={response.get('errcode')} errmsg={response.get('errmsg')}")

        next_buf = response.get("get_updates_buf")
        if isinstance(next_buf, str) and next_buf:
            self._save_sync_buf(next_buf)

        for msg in response.get("msgs") or []:
            self._handle_message(base_url, token, msg)

        self._save_state()

    def _handle_message(self, base_url: str, token: str, msg: dict[str, Any]) -> None:
        if msg.get("message_type") != 1:
            return

        sender_id = str(msg.get("from_user_id") or "").strip()
        if not sender_id:
            return

        text = self._extract_text(msg)
        if not text:
            return
        if any(text.startswith(prefix) for prefix in self.config.ignore_prefixes):
            return
        message_key = self._message_key(msg, text)
        if self._is_duplicate_message(message_key):
            return

        reply, handled = self._handle_control_command(sender_id, text)
        if handled:
            if reply:
                self._send_text(base_url, token, sender_id, msg.get("context_token"), reply)
                self.state["handled_messages"] += 1
            self._save_state()
            return

        binding = self._ensure_conversation(sender_id)
        session_name = str(binding.get("current_session") or "default")
        session_meta = (binding.get("sessions") or {}).get(session_name) or {}
        prompt = text.strip()
        if not prompt:
            return

        self.state["last_message_at"] = now_iso()
        self.state["last_sender_id"] = sender_id

        response = self._ipc_request(
            "submit_task",
            {
                "agent_id": self.config.backend_id,
                "prompt": prompt,
                "source": "wechat",
                "sender_id": sender_id,
                "session_name": session_name,
            },
            timeout_seconds=15,
        )
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "submit_task failed"))
        task = response["task"]

        result = self._wait_for_task(task["id"])
        if result["status"] == "succeeded":
            reply = str(result.get("output") or "").strip()
            if self.config.auto_reply_prefix:
                reply = f"{self.config.auto_reply_prefix}{reply}"
            self._send_text(base_url, token, sender_id, msg.get("context_token"), reply)
            self.state["handled_messages"] += 1
        else:
            error_text = str(result.get("error") or "task failed").strip()
            self._send_text(base_url, token, sender_id, msg.get("context_token"), f"Codex task failed:\n{error_text}")
            self.state["failed_messages"] += 1

    def _wait_for_task(self, task_id: str) -> dict[str, Any]:
        deadline = time.time() + max(self.config.hub_task_timeout_seconds, 10)
        while time.time() < deadline:
            data = self._ipc_request("get_task", {"task_id": task_id}, timeout_seconds=5)
            if not data.get("ok"):
                raise RuntimeError(str(data.get("error") or "get_task failed"))
            task = data["task"]
            if task["status"] in {"succeeded", "failed"}:
                return task
            time.sleep(2)
        raise TimeoutError(f"task timed out: {task_id}")

    def _send_text(self, base_url: str, token: str, to_user_id: str, context_token: Any, text: str) -> None:
        text = (text or "").strip()
        if not text:
            text = "(empty reply)"
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"bridge-{int(time.time() * 1000)}-{random.randint(1000, 9999)}",
                "message_type": 2,
                "message_state": 2,
                "item_list": [
                    {
                        "type": 1,
                        "text_item": {"text": text[:4000]},
                    }
                ],
                "context_token": context_token or None,
            },
            "base_info": {"channel_version": "2.1.1"},
        }
        self._post_json(f"{base_url}/ilink/bot/sendmessage", body, token=token, timeout_ms=15000)

    def _extract_text(self, msg: dict[str, Any]) -> str:
        parts = []
        for item in msg.get("item_list") or []:
            if item.get("type") == 1:
                text_item = item.get("text_item") or {}
                text = str(text_item.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()

    def _message_key(self, msg: dict[str, Any], text: str) -> str:
        payload = {
            "id": msg.get("msg_id") or msg.get("message_id") or msg.get("client_id") or "",
            "context_token": msg.get("context_token") or "",
            "sender_id": msg.get("from_user_id") or "",
            "create_time": msg.get("create_time") or msg.get("create_timestamp") or "",
            "text": text,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha1(encoded).hexdigest()

    def _is_duplicate_message(self, message_key: str) -> bool:
        if message_key in self._recent_message_keys:
            return True
        self._recent_message_keys.append(message_key)
        if len(self._recent_message_keys) > 200:
            self._recent_message_keys = self._recent_message_keys[-200:]
        return False

    def _handle_control_command(self, sender_id: str, text: str) -> tuple[str, bool]:
        raw = self._normalize_command_text(text)
        if not raw.startswith("/"):
            return "", False

        binding = self._ensure_conversation(sender_id)
        current_session = str(binding.get("current_session") or "default")
        sessions = binding.setdefault("sessions", {})
        sessions.setdefault(current_session, self._new_session_meta())

        parts = raw.split(maxsplit=2)
        command = parts[0].lower()

        if command in {"/help", "/h", "/?"}:
            help_lines = [
                "可用命令:",
                "/help 查看帮助",
                "/status 查看当前会话状态",
                "/new <name> 新建会话并切换",
                "/list 列出所有会话",
                "/use <name> 切换到指定会话",
                "/close 结束当前会话",
                "/reset 重置回默认会话",
                "",
                "普通消息:",
                "不带斜杠的消息会继续发送到当前会话。",
            ]
            return "\n".join(help_lines), True

        if command == "/new":
            requested = parts[1].strip() if len(parts) >= 2 else ""
            session_name = self._allocate_session_name(binding, requested or "session")
            sessions[session_name] = self._new_session_meta()
            binding["current_session"] = session_name
            self._save_conversations()
            return f"已创建并切换到会话: {session_name}", True

        if command == "/list":
            lines = ["会话列表:"]
            for name in sorted(sessions):
                marker = "*" if name == binding.get("current_session") else "-"
                lines.append(f"{marker} {name}")
            return "\n".join(lines), True

        if command == "/use":
            if len(parts) < 2:
                return "用法: /use <session>", True
            session_name = parts[1].strip()
            if session_name not in sessions:
                return f"未找到会话: {session_name}", True
            binding["current_session"] = session_name
            self._save_conversations()
            return f"已切换到会话: {session_name}", True

        if command in {"/close", "/end"}:
            if current_session == "default":
                return "默认会话不能结束。可以发送 /new <name> 创建新会话，或 /reset 重置状态。", True
            sessions.pop(current_session, None)
            binding["current_session"] = "default"
            sessions.setdefault("default", self._new_session_meta())
            self._save_conversations()
            return f"已结束会话: {current_session}\n已切回默认会话: default", True

        if command == "/status":
            return (
                f"当前会话: {binding.get('current_session')}\n"
                f"会话数量: {len(sessions)}"
            ), True

        if command == "/reset":
            self.conversations.pop(sender_id, None)
            self._save_conversations()
            reset = self._ensure_conversation(sender_id)
            return f"已重置到默认会话: {reset.get('current_session')}", True

        return "未知命令。发送 /help 查看当前支持的命令。", True

    @staticmethod
    def _normalize_command_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(text or ""))
        normalized = normalized.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        if not lines:
            return ""
        return lines[0]

    def _ensure_conversation(self, sender_id: str) -> dict[str, Any]:
        existing = self.conversations.get(sender_id)
        if existing:
            existing.setdefault("current_session", "default")
            existing.setdefault("sessions", {})
            existing.pop("current_agent_id", None)
            for meta in (existing.get("sessions") or {}).values():
                if isinstance(meta, dict):
                    meta.pop("agent_id", None)
            if existing["current_session"] not in existing["sessions"]:
                existing["sessions"][existing["current_session"]] = self._new_session_meta()
            return existing

        created = {
            "current_session": "default",
            "sessions": {"default": self._new_session_meta()},
        }
        self.conversations[sender_id] = created
        self._save_conversations()
        return created

    def _new_session_meta(self) -> dict[str, Any]:
        return {
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }

    def _allocate_session_name(self, binding: dict[str, Any], requested: str) -> str:
        sessions = binding.setdefault("sessions", {})
        base = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in requested).strip("-_") or "session"
        if base not in sessions:
            return base
        index = 2
        while f"{base}-{index}" in sessions:
            index += 1
        return f"{base}-{index}"

    def _load_account(self) -> dict[str, Any]:
        self._ensure_local_account_storage()
        if not self.account_path.exists():
            raise FileNotFoundError(f"account file not found: {self.account_path}")
        return json.loads(self.account_path.read_text(encoding="utf-8"))

    def _load_sync_buf(self) -> str:
        self._ensure_local_account_storage()
        try:
            data = json.loads(self.sync_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ""
        except json.JSONDecodeError:
            return ""
        return str(data.get("get_updates_buf") or "")

    def _save_sync_buf(self, buf: str) -> None:
        self.sync_path.parent.mkdir(parents=True, exist_ok=True)
        self.sync_path.write_text(json.dumps({"get_updates_buf": buf}, ensure_ascii=False), encoding="utf-8")

    def _ensure_local_account_storage(self) -> None:
        self.account_path.parent.mkdir(parents=True, exist_ok=True)
        self.sync_path.parent.mkdir(parents=True, exist_ok=True)

    def _request(self, method: str, url: str, body: dict[str, Any] | None = None, token: str = "", timeout_ms: int = 15000) -> dict[str, Any]:
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {}
        if url.startswith("https://ilinkai.weixin.qq.com") or "/ilink/bot/" in url:
            headers["AuthorizationType"] = "ilink_bot_token"
            headers["X-WECHAT-UIN"] = base64.b64encode(str(random.randint(1, 2**32 - 1)).encode("utf-8")).decode("ascii")
            headers["iLink-App-Id"] = ILINK_APP_ID
            headers["iLink-App-ClientVersion"] = str(ILINK_APP_CLIENT_VERSION)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        req = urllib.request.Request(url=url, data=payload, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout_ms / 1000) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_json(self, url: str, body: dict[str, Any], token: str = "", timeout_ms: int = 15000) -> dict[str, Any]:
        try:
            return self._request("POST", url, body=body, token=token, timeout_ms=timeout_ms)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"POST {url} failed: {exc.code} {detail}") from exc

    def _ipc_request(self, action: str, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        request_id = create_request(action, payload)
        return wait_for_response(request_id, timeout_seconds)

    def _save_state(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.state["managed_conversations"] = len(self.conversations)
        self.state["account_file"] = str(self.account_path)
        self.state["sync_file"] = str(self.sync_path)
        STATE_PATH.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_conversations(self) -> dict[str, Any]:
        try:
            return json.loads(CONVERSATION_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_conversations(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CONVERSATION_PATH.write_text(json.dumps(self.conversations, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    cfg = BridgeConfig.load()
    WeixinBridge(cfg).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
