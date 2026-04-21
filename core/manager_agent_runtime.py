from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent_backends import McpServerConfig
from core.json_store import load_json, save_json
from core.runtime_paths import APP_DIR, STATE_DIR
from core.state_models import JsonObject


THREADS_STATE_PATH = STATE_DIR / "chatbridge_manager_threads.json"
DEFAULT_MANAGER_MODEL = "gpt-5.4"
REQUEST_TIMEOUT_SECONDS = 45.0
TURN_TIMEOUT_SECONDS = 180.0
MANAGER_SERVER_NAME = "chatbridge_manager"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _toml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class ManagerThreadRecord:
    sender_id: str
    thread_id: str
    updated_at: str

    @classmethod
    def from_dict(cls, sender_id: str, raw: object) -> "ManagerThreadRecord | None":
        if not isinstance(raw, dict):
            return None
        thread_id = str(raw.get("thread_id") or "").strip()
        if not thread_id:
            return None
        return cls(
            sender_id=sender_id,
            thread_id=thread_id,
            updated_at=str(raw.get("updated_at") or "").strip(),
        )

    def to_dict(self) -> JsonObject:
        return {
            "thread_id": self.thread_id,
            "updated_at": self.updated_at,
        }


class ChatBridgeManagerRuntime:
    def __init__(
        self,
        *,
        codex_command: str,
        app_dir: Path = APP_DIR,
        state_path: Path = THREADS_STATE_PATH,
    ) -> None:
        self.codex_command = codex_command
        self.app_dir = app_dir
        self.state_path = state_path
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._process: subprocess.Popen[str] | None = None
        self._messages: list[JsonObject] = []
        self._stderr_lines: list[str] = []
        self._reader_threads_started = False
        self._request_id = 0
        self._server_signature: tuple[str, str, tuple[str, ...]] | None = None
        self._thread_records = self._load_thread_records()

    def invoke(
        self,
        *,
        sender_id: str,
        prompt: str,
        instructions: str,
        model: str,
        mcp_config: McpServerConfig,
    ) -> dict[str, str]:
        cleaned_sender_id = sender_id.strip()
        if not cleaned_sender_id:
            raise RuntimeError("manager runtime requires sender_id")
        cleaned_prompt = prompt.strip()
        if not cleaned_prompt:
            raise RuntimeError("manager runtime requires prompt")
        selected_model = model.strip() or DEFAULT_MANAGER_MODEL
        with self._lock:
            self._ensure_server(model=selected_model, mcp_config=mcp_config)
            thread_id = self._ensure_thread(
                sender_id=cleaned_sender_id,
                instructions=instructions.strip(),
                model=selected_model,
            )
            output = self._run_turn(thread_id=thread_id, prompt=cleaned_prompt)
            self._remember_thread(sender_id=cleaned_sender_id, thread_id=thread_id)
            return {
                "output": output,
                "session_id": thread_id,
            }

    def close(self) -> None:
        with self._lock:
            self._shutdown_process()

    def _load_thread_records(self) -> dict[str, ManagerThreadRecord]:
        raw = load_json(self.state_path, {}, expect_type=dict)
        records: dict[str, ManagerThreadRecord] = {}
        for sender_id, item in raw.get("threads", {}).items():
            record = ManagerThreadRecord.from_dict(str(sender_id), item)
            if record is not None:
                records[record.sender_id] = record
        return records

    def _save_thread_records(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(
            self.state_path,
            {
                "threads": {sender_id: record.to_dict() for sender_id, record in self._thread_records.items()},
            },
        )

    def _remember_thread(self, *, sender_id: str, thread_id: str) -> None:
        self._thread_records[sender_id] = ManagerThreadRecord(
            sender_id=sender_id,
            thread_id=thread_id,
            updated_at=_now_iso(),
        )
        self._save_thread_records()

    def _forget_thread(self, sender_id: str) -> None:
        if sender_id in self._thread_records:
            self._thread_records.pop(sender_id, None)
            self._save_thread_records()

    def _ensure_server(self, *, model: str, mcp_config: McpServerConfig) -> None:
        signature = (
            model,
            mcp_config.command,
            tuple(mcp_config.args),
        )
        if self._process is not None and self._process.poll() is None and self._server_signature == signature:
            return
        self._shutdown_process()
        self._messages.clear()
        self._stderr_lines.clear()
        self._request_id = 0
        argv = [
            self.codex_command,
            "app-server",
            "-c",
            f"model={_toml_value(model)}",
            "-c",
            f"mcp_servers.{MANAGER_SERVER_NAME}.command={_toml_value(mcp_config.command)}",
            "-c",
            f"mcp_servers.{MANAGER_SERVER_NAME}.args={_toml_value(mcp_config.args)}",
        ]
        self._process = subprocess.Popen(
            argv,
            cwd=str(self.app_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._start_reader_threads()
        self._server_signature = signature
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "chatbridge-manager-runtime",
                    "title": "chatbridge-manager-runtime",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        status_response = self._request("mcpServerStatus/list", {})
        servers = status_response.get("data") or []
        manager_status = next((item for item in servers if str(item.get("name") or "") == MANAGER_SERVER_NAME), None)
        if not isinstance(manager_status, dict):
            raise RuntimeError("manager runtime could not discover chatbridge_manager MCP server")
        tools = manager_status.get("tools") or {}
        if not isinstance(tools, dict) or "get_management_snapshot" not in tools:
            raise RuntimeError("manager runtime did not load chatbridge_manager MCP tools")

    def _ensure_thread(self, *, sender_id: str, instructions: str, model: str) -> str:
        existing = self._thread_records.get(sender_id)
        if existing is not None:
            try:
                response = self._request(
                    "thread/resume",
                    {
                        "threadId": existing.thread_id,
                        "cwd": str(self.app_dir),
                        "approvalPolicy": "never",
                        "sandbox": "danger-full-access",
                        "baseInstructions": instructions or None,
                        "developerInstructions": instructions or None,
                        "personality": "pragmatic",
                        "model": model,
                        "persistExtendedHistory": True,
                    },
                )
                thread = response.get("thread") or {}
                thread_id = str(thread.get("id") or "").strip()
                if thread_id:
                    self._remember_thread(sender_id=sender_id, thread_id=thread_id)
                    return thread_id
            except Exception:
                self._forget_thread(sender_id)
        response = self._request(
            "thread/start",
            {
                "cwd": str(self.app_dir),
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "baseInstructions": instructions or None,
                "developerInstructions": instructions or None,
                "personality": "pragmatic",
                "model": model,
                "experimentalRawEvents": False,
                "persistExtendedHistory": True,
            },
        )
        thread = response.get("thread") or {}
        thread_id = str(thread.get("id") or "").strip()
        if not thread_id:
            raise RuntimeError("manager runtime failed to create thread")
        self._remember_thread(sender_id=sender_id, thread_id=thread_id)
        return thread_id

    def _run_turn(self, *, thread_id: str, prompt: str) -> str:
        start_cursor = len(self._messages)
        response = self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        turn = response.get("turn") or {}
        turn_id = str(turn.get("id") or "").strip()
        if not turn_id:
            raise RuntimeError("manager runtime failed to start turn")
        completion = self._wait_for_notification(
            lambda message: message.get("method") == "turn/completed"
            and str((message.get("params") or {}).get("turn", {}).get("id") or "") == turn_id,
            start_cursor=start_cursor,
            timeout=TURN_TIMEOUT_SECONDS,
        )
        completed_turn = ((completion.get("params") or {}).get("turn") or {})
        if str(completed_turn.get("status") or "") == "failed":
            raise RuntimeError(str((completed_turn.get("error") or {}).get("message") or "manager runtime turn failed"))
        turn_messages = self._messages[start_cursor:]
        final_text = ""
        for message in turn_messages:
            if message.get("method") != "item/completed":
                continue
            item = (message.get("params") or {}).get("item") or {}
            if str(item.get("type") or "") == "agentMessage":
                final_text = str(item.get("text") or "").strip() or final_text
        if final_text:
            return final_text
        raise RuntimeError("manager runtime returned no final agent message")

    def _start_reader_threads(self) -> None:
        if self._process is None or self._reader_threads_started:
            return
        stdout = self._process.stdout
        stderr = self._process.stderr
        if stdout is None or stderr is None:
            raise RuntimeError("manager runtime failed to open stdio")

        def read_stdout() -> None:
            for raw_line in stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    payload = {"_raw": line}
                with self._condition:
                    self._messages.append(payload)
                    self._condition.notify_all()

        def read_stderr() -> None:
            for raw_line in stderr:
                with self._condition:
                    self._stderr_lines.append(raw_line)
                    self._condition.notify_all()

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()
        self._reader_threads_started = True

    def _request(self, method: str, params: JsonObject, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> JsonObject:
        if self._process is None or self._process.poll() is not None:
            raise RuntimeError(self._process_error_message("manager runtime is not running"))
        stdin = self._process.stdin
        if stdin is None:
            raise RuntimeError("manager runtime stdin is unavailable")
        self._request_id += 1
        request_id = self._request_id
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        start_cursor = len(self._messages)
        stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        stdin.flush()
        response = self._wait_for_notification(
            lambda message: int(message.get("id") or 0) == request_id,
            start_cursor=start_cursor,
            timeout=timeout,
        )
        error = response.get("error")
        if isinstance(error, dict):
            raise RuntimeError(str(error.get("message") or f"{method} failed"))
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"{method} returned invalid payload")
        return result

    def _wait_for_notification(
        self,
        predicate,
        *,
        start_cursor: int,
        timeout: float,
    ) -> JsonObject:
        deadline = time.time() + timeout
        cursor = start_cursor
        with self._condition:
            while True:
                while cursor < len(self._messages):
                    message = self._messages[cursor]
                    cursor += 1
                    if predicate(message):
                        return message
                if self._process is not None and self._process.poll() is not None:
                    raise RuntimeError(self._process_error_message("manager runtime exited unexpectedly"))
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise RuntimeError(self._process_error_message("manager runtime request timed out"))
                self._condition.wait(timeout=remaining)

    def _process_error_message(self, prefix: str) -> str:
        stderr_text = "".join(self._stderr_lines).strip()
        if stderr_text:
            return f"{prefix}: {stderr_text.splitlines()[-1]}"
        return prefix

    def _shutdown_process(self) -> None:
        if self._process is None:
            self._reader_threads_started = False
            self._server_signature = None
            return
        process = self._process
        self._process = None
        self._reader_threads_started = False
        self._server_signature = None
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
