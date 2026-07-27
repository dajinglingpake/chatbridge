from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from agent_backends.base import AgentBackend, AgentLike, BackendContext
from agent_backends.shared import build_final_prompt, collect_text_fragments, resolve_session_file
from core.platform_compat import terminate_process_tree
from localization import Localizer

PROGRESS_PUSH_INTERVAL_SECONDS = 1.0
COMMAND_OUTPUT_LIMIT = 12000
CODEX_EXIT_TIMEOUT_SECONDS = 10
CODEX_TRANSIENT_RETRY_ATTEMPTS = 2
CODEX_INTERRUPTION_RECOVERY_PROMPT_KEY = "agent.codex.recovery_prompt"
CODEX_INTERRUPTION_RECOVERY_PROMPT_FALLBACK = (
    "刚才你突然中断了。请基于当前会话继续上一轮任务，不要从头重做，也不要重复已经完成的步骤。"
    "先检查当前工作区和运行状态，再完成剩余收尾与最终回复。"
    "如果还需要执行耗时步骤，请持续输出简短进度；超过 10 分钟没有输出会被超时终止。\n\n"
    "上一轮用户指令：\n{prompt}"
)
TRANSIENT_ERROR_MARKERS = (
    "stream disconnected before completion",
    "error sending request",
    "timeout waiting for child process to exit",
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
    "codex request timed out",
    "codex stdout idle timed out",
)
INTERRUPTION_RECOVERY_MARKERS = (
    "codex request timed out",
    "codex stdout idle timed out",
)


class _CodexAppServerClient:
    def __init__(self, command: str, *, creationflags: int, start_new_session: bool, slim_exec: bool) -> None:
        self.command = command
        self.creationflags = creationflags
        self.start_new_session = start_new_session
        self.slim_exec = slim_exec
        self.proc: subprocess.Popen | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._response_queues: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._turn_queues: dict[tuple[str, str], queue.Queue[dict[str, Any]]] = {}
        self._stderr_lines: list[str] = []
        self._next_id = 1
        self._lock = threading.RLock()
        self._dispatch_lock = threading.RLock()

    def start(self) -> None:
        argv = [self.command, "app-server", "--listen", "stdio://"]
        if self.slim_exec:
            argv.extend(["--disable", "plugins"])
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=self.creationflags,
            start_new_session=self.start_new_session,
            shell=False,
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True, name="codex-app-server-stdout").start()
        threading.Thread(target=self._read_stderr, daemon=True, name="codex-app-server-stderr").start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "chatbridge",
                    "title": "ChatBridge",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=60,
        )
        self.notify("initialized", {})

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def request(self, method: str, params: dict[str, object], *, timeout: float) -> dict[str, Any]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            with self._dispatch_lock:
                self._response_queues[request_id] = response_queue
            try:
                self._write_message({"id": request_id, "method": method, "params": params})
            except Exception:
                with self._dispatch_lock:
                    self._response_queues.pop(request_id, None)
                raise
        try:
            message = response_queue.get(timeout=max(0.1, timeout))
        except queue.Empty as exc:
            stderr_tail = "\n".join(self._stderr_lines[-8:])
            raise RuntimeError(
                f"timed out waiting for app-server response: {method}; stderr={stderr_tail}"
            ) from exc
        finally:
            with self._dispatch_lock:
                self._response_queues.pop(request_id, None)
        if isinstance(message.get("error"), dict):
            error = message["error"]
            raise RuntimeError(str(error.get("message") or error))
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict[str, object]) -> None:
        self._send(method, params)

    def wait_for_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        timeout: float,
        on_progress: Callable[[str], None] | None,
        on_live_output: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        on_activity: Callable[[dict[str, object]], None] | None = None,
        reasoning_progress_label: Callable[[str], str] | None = None,
    ) -> tuple[str, int | None]:
        deadline = time.time() + timeout
        message_queue = self._subscribe_turn(thread_id, turn_id)
        output = ""
        pending_answer_delta = ""
        pending_reasoning_delta = ""
        last_output_progress = ""
        last_output_progress_at = 0.0
        last_reasoning_progress = ""
        last_reasoning_progress_at = 0.0
        last_reasoning_item_id = ""
        reasoning_activities: dict[str, str] = {}
        last_reasoning_activity_at: dict[str, float] = {}
        last_reasoning_activity_text: dict[str, str] = {}
        command_activities: dict[str, dict[str, object]] = {}
        last_command_output_at: dict[str, float] = {}
        context_left_percent: int | None = None

        def push_live_output(text: str, *, force: bool = False) -> None:
            nonlocal last_output_progress, last_output_progress_at
            cleaned = text.strip()
            callback = on_live_output or on_progress
            now = time.time()
            if not cleaned or callback is None or cleaned == last_output_progress:
                return
            if not force and now - last_output_progress_at < PROGRESS_PUSH_INTERVAL_SECONDS:
                return
            callback(cleaned)
            last_output_progress = cleaned
            last_output_progress_at = now

        def push_reasoning(text: str, *, force: bool = False) -> None:
            nonlocal last_reasoning_progress, last_reasoning_progress_at
            cleaned = text.strip()
            callback = on_reasoning or on_progress
            now = time.time()
            if not cleaned or callback is None or cleaned == last_reasoning_progress:
                return
            if not force and now - last_reasoning_progress_at < PROGRESS_PUSH_INTERVAL_SECONDS:
                return
            if on_reasoning is None and reasoning_progress_label is not None:
                cleaned = reasoning_progress_label(cleaned)
            callback(cleaned)
            last_reasoning_progress = text.strip()
            last_reasoning_progress_at = now

        def emit_command_activity(activity: dict[str, object]) -> None:
            item_id = str(activity.get("id") or "")
            if not item_id or on_activity is None:
                return
            on_activity(dict(activity))

        def emit_reasoning_activity(item_id: str, text: str, *, force: bool = False) -> None:
            cleaned = text.strip()
            if not item_id or not cleaned or on_activity is None:
                return
            if last_reasoning_activity_text.get(item_id) == cleaned:
                return
            now = time.time()
            if not force and now - last_reasoning_activity_at.get(item_id, 0.0) < PROGRESS_PUSH_INTERVAL_SECONDS:
                return
            last_reasoning_activity_at[item_id] = now
            last_reasoning_activity_text[item_id] = cleaned
            on_activity(self._reasoning_activity_payload(item_id, cleaned))

        def push_command_activity(item: dict[str, Any], *, at_ms: object = None) -> None:
            activity = self._command_activity_payload(item, at_ms=at_ms)
            item_id = str(activity.get("id") or "")
            if not item_id:
                return
            existing = command_activities.get(item_id)
            if existing is not None:
                activity = self._merge_command_activity(existing, activity)
            command_activities[item_id] = activity
            emit_command_activity(activity)

        def push_command_output(item_id: str, delta: str) -> None:
            activity = command_activities.get(item_id)
            if activity is None or not delta:
                return
            metadata = dict(activity.get("metadata")) if isinstance(activity.get("metadata"), dict) else {}
            metadata["output"] = f"{metadata.get('output') or ''}{delta}"[-COMMAND_OUTPUT_LIMIT:]
            activity = {**activity, "metadata": metadata}
            command_activities[item_id] = activity
            now = time.time()
            if on_activity is not None and now - last_command_output_at.get(item_id, 0.0) >= PROGRESS_PUSH_INTERVAL_SECONDS:
                last_command_output_at[item_id] = now
                on_activity(dict(activity))

        try:
            while time.time() < deadline:
                message = self._read_message(deadline, message_queue=message_queue)
                method = str(message.get("method") or "")
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                if str(params.get("threadId") or "") not in {"", thread_id}:
                    continue
                message_turn_id = str(params.get("turnId") or "")
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                if not message_turn_id and isinstance(turn, dict):
                    message_turn_id = str(turn.get("id") or "")
                if message_turn_id not in {"", turn_id}:
                    continue
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                if method == "item/started" and item.get("type") == "commandExecution":
                    push_command_activity(item, at_ms=params.get("startedAtMs"))
                if method == "item/commandExecution/outputDelta":
                    push_command_output(str(params.get("itemId") or "").strip(), self._extract_delta(message))
                if method == "item/completed" and item.get("type") == "commandExecution":
                    push_command_activity(item, at_ms=params.get("completedAtMs"))
                if method == "item/completed" and item.get("type") == "agentMessage":
                    text = self._extract_agent_message_text(item)
                    if text:
                        output = text
                if method == "item/agentMessage/delta":
                    delta = self._extract_delta(message)
                    if delta:
                        pending_answer_delta += delta
                        push_live_output(pending_answer_delta)
                if method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
                    delta = self._extract_delta(message)
                    if delta:
                        reasoning_item_id = str(params.get("itemId") or params.get("item_id") or "reasoning").strip() or "reasoning"
                        reasoning_activities[reasoning_item_id] = f"{reasoning_activities.get(reasoning_item_id, '')}{delta}"
                        emit_reasoning_activity(reasoning_item_id, reasoning_activities[reasoning_item_id])
                        if last_reasoning_item_id and reasoning_item_id != last_reasoning_item_id and pending_reasoning_delta:
                            pending_reasoning_delta = f"{pending_reasoning_delta.rstrip()}\n\n"
                        last_reasoning_item_id = reasoning_item_id
                        pending_reasoning_delta += delta
                        push_reasoning(pending_reasoning_delta)
                next_context_left = self._extract_context_left_percent(message)
                if next_context_left is not None:
                    context_left_percent = next_context_left
                if method == "turn/completed":
                    for reasoning_item_id, reasoning_text in reasoning_activities.items():
                        emit_reasoning_activity(reasoning_item_id, reasoning_text, force=True)
                    push_reasoning(pending_reasoning_delta, force=True)
                    error = turn.get("error") if isinstance(turn, dict) else None
                    if error:
                        raise RuntimeError(self._extract_error_message(error))
                    status = str(turn.get("status") or "") if isinstance(turn, dict) else ""
                    if status in {"failed", "interrupted"}:
                        raise RuntimeError(f"Codex app-server turn {status}")
                    if not output.strip() and pending_answer_delta.strip():
                        output = pending_answer_delta.strip()
                    push_live_output(output or pending_answer_delta, force=True)
                    return output, context_left_percent
            raise RuntimeError(f"timed out waiting for app-server turn: {turn_id}")
        finally:
            self._unsubscribe_turn(thread_id, turn_id, message_queue)

    def list_threads(
        self,
        *,
        limit: int = 50,
        cursor: str = "",
        search_term: str = "",
        cwd: str = "",
        archived: bool | None = False,
    ) -> dict[str, Any]:
        params: dict[str, object] = {
            "limit": max(1, min(200, int(limit or 50))),
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "useStateDbOnly": True,
            "sourceKinds": [],
        }
        if cursor.strip():
            params["cursor"] = cursor.strip()
        if search_term.strip():
            params["searchTerm"] = search_term.strip()
        if cwd.strip():
            params["cwd"] = cwd.strip()
        if archived is not None:
            params["archived"] = bool(archived)
        return self.request("thread/list", params, timeout=60)

    def read_thread(self, thread_id: str, *, include_turns: bool = True) -> dict[str, Any]:
        return self.request(
            "thread/read",
            {"threadId": thread_id.strip(), "includeTurns": bool(include_turns)},
            timeout=60,
        )

    def _send(self, method: str, params: dict[str, object]) -> None:
        with self._lock:
            self._write_message({"method": method, "params": params})

    def _write_message(self, message: dict[str, object]) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.poll() is not None:
            raise RuntimeError("Codex app-server is not running")
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _read_message(
        self,
        deadline: float,
        *,
        message_queue: queue.Queue[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        timeout = max(0.1, deadline - time.time())
        try:
            return (message_queue or self._messages).get(timeout=timeout)
        except queue.Empty as exc:
            stderr_tail = "\n".join(self._stderr_lines[-8:])
            raise RuntimeError(f"timed out reading Codex app-server message; stderr={stderr_tail}") from exc

    def _read_stdout(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self._dispatch_message(message)

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        with self._dispatch_lock:
            response_queue = self._response_queues.get(request_id) if isinstance(request_id, int) else None
            if response_queue is not None and not message.get("method"):
                response_queue.put(message)
                return
            thread_id, turn_id = self._message_turn_identity(message)
            turn_queue = self._turn_queues.get((thread_id, turn_id)) if thread_id and turn_id else None
            if turn_queue is None and thread_id and not turn_id:
                matching = [item for (candidate_thread_id, _), item in self._turn_queues.items() if candidate_thread_id == thread_id]
                turn_queue = matching[0] if len(matching) == 1 else None
            if turn_queue is not None:
                turn_queue.put(message)
                return
            self._messages.put(message)
            while self._messages.qsize() > 512:
                try:
                    self._messages.get_nowait()
                except queue.Empty:
                    break

    def _subscribe_turn(self, thread_id: str, turn_id: str) -> queue.Queue[dict[str, Any]]:
        turn_key = (thread_id, turn_id)
        turn_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._dispatch_lock:
            if turn_key in self._turn_queues:
                raise RuntimeError(f"Codex app-server turn is already being monitored: {turn_id}")
            self._turn_queues[turn_key] = turn_queue
            unmatched: list[dict[str, Any]] = []
            while True:
                try:
                    message = self._messages.get_nowait()
                except queue.Empty:
                    break
                message_thread_id, message_turn_id = self._message_turn_identity(message)
                if message_thread_id == thread_id and message_turn_id in {"", turn_id}:
                    turn_queue.put(message)
                else:
                    unmatched.append(message)
            for message in unmatched:
                self._messages.put(message)
        return turn_queue

    def _unsubscribe_turn(
        self,
        thread_id: str,
        turn_id: str,
        turn_queue: queue.Queue[dict[str, Any]],
    ) -> None:
        with self._dispatch_lock:
            if self._turn_queues.get((thread_id, turn_id)) is turn_queue:
                self._turn_queues.pop((thread_id, turn_id), None)

    @staticmethod
    def _message_turn_identity(message: dict[str, Any]) -> tuple[str, str]:
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        thread_id = str(params.get("threadId") or "").strip()
        turn_id = str(params.get("turnId") or "").strip()
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        if not turn_id:
            turn_id = str(turn.get("id") or "").strip()
        return thread_id, turn_id

    def _read_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        for raw_line in proc.stderr:
            line = raw_line.strip()
            if line:
                self._stderr_lines.append(line)
                del self._stderr_lines[:-30]

    @staticmethod
    def _extract_delta(message: dict[str, Any]) -> str:
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        for key in ("delta", "textDelta", "text_delta", "text"):
            raw = params.get(key)
            if isinstance(raw, str) and raw:
                return raw
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        raw = item.get("text")
        return raw if isinstance(raw, str) else ""

    @staticmethod
    def _extract_agent_message_text(item: dict[str, Any]) -> str:
        raw = item.get("text")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return "\n".join(fragment.strip() for fragment in collect_text_fragments(item) if fragment.strip()).strip()

    @classmethod
    def _reasoning_activity_payload(cls, item_id: str, text: str, *, at_ms: object = None) -> dict[str, object]:
        return {
            "id": item_id,
            "event": "codex_reasoning",
            "type": "reasoning",
            "at": cls._activity_timestamp(at_ms),
            "detail": text.strip(),
            "metadata": {},
        }

    @classmethod
    def _command_activity_payload(cls, item: dict[str, Any], *, at_ms: object = None) -> dict[str, object]:
        item_id = str(item.get("id") or item.get("itemId") or "").strip()
        status = str(item.get("status") or "inProgress").strip() or "inProgress"
        exit_code = item.get("exitCode") if "exitCode" in item else item.get("exit_code")
        output = item.get("aggregatedOutput") if "aggregatedOutput" in item else item.get("aggregated_output")
        metadata: dict[str, object] = {
            "command": str(item.get("command") or "").strip(),
            "cwd": str(item.get("cwd") or "").strip(),
            "status": status,
            "output": str(output or "")[-COMMAND_OUTPUT_LIMIT:],
        }
        if exit_code is not None:
            metadata["exit_code"] = exit_code
        duration_ms = item.get("durationMs") if "durationMs" in item else item.get("duration_ms")
        if duration_ms is not None:
            metadata["duration_ms"] = duration_ms
        normalized_status = status.replace("_", "").lower()
        activity_type = "error" if normalized_status in {"failed", "declined"} or (isinstance(exit_code, int) and exit_code != 0) else "success" if normalized_status == "completed" else "info"
        return {
            "id": item_id,
            "event": "codex_command",
            "type": activity_type,
            "at": cls._activity_timestamp(at_ms),
            "detail": str(item.get("command") or "").strip(),
            "metadata": metadata,
        }

    @staticmethod
    def _merge_command_activity(existing: dict[str, object], current: dict[str, object]) -> dict[str, object]:
        existing_metadata = dict(existing.get("metadata")) if isinstance(existing.get("metadata"), dict) else {}
        current_metadata = dict(current.get("metadata")) if isinstance(current.get("metadata"), dict) else {}
        if not current_metadata.get("output") and existing_metadata.get("output"):
            current_metadata["output"] = existing_metadata["output"]
        return {
            **existing,
            **current,
            "at": existing.get("at") or current.get("at") or "",
            "metadata": {**existing_metadata, **current_metadata},
        }

    @staticmethod
    def _activity_timestamp(value: object) -> str:
        if isinstance(value, (int, float)) and value > 0:
            try:
                return datetime.fromtimestamp(float(value) / 1000).isoformat(timespec="milliseconds")
            except (OSError, OverflowError, ValueError):
                pass
        return datetime.now().isoformat(timespec="milliseconds")

    @staticmethod
    def _extract_error_message(error: object) -> str:
        if isinstance(error, dict):
            for key in ("message", "additionalDetails"):
                raw = error.get(key)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
        return str(error)

    @staticmethod
    def _extract_context_left_percent(message: dict[str, Any]) -> int | None:
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        raw_percent = params.get("contextLeftPercent")
        if isinstance(raw_percent, int):
            return max(0, min(100, raw_percent))
        token_usage = params.get("tokenUsage")
        if isinstance(token_usage, dict):
            total = token_usage.get("total")
            context_window = token_usage.get("modelContextWindow")
            if isinstance(total, dict) and isinstance(context_window, int) and context_window > 0:
                total_tokens = total.get("totalTokens")
                if isinstance(total_tokens, int):
                    return max(0, min(100, round((1 - (total_tokens / context_window)) * 100)))
        return None


class CodexBackend(AgentBackend):
    key = "codex"

    def __init__(self) -> None:
        self._app_server_lock = threading.RLock()
        self._app_server: _CodexAppServerClient | None = None

    def invoke(self, agent: AgentLike, prompt: str, session_name: str, context: BackendContext) -> dict[str, str]:
        translate = Localizer().translate
        if context.codex_transport.strip().lower() == "app-server":
            return self._invoke_app_server(agent, prompt, session_name, context)
        last_error: RuntimeError | None = None
        current_prompt = prompt
        for attempt in range(CODEX_TRANSIENT_RETRY_ATTEMPTS + 1):
            try:
                return self._invoke_once(agent, current_prompt, session_name, context)
            except RuntimeError as exc:
                last_error = exc
                if self._is_cancel_requested(context):
                    raise
                message = str(exc)
                if attempt >= CODEX_TRANSIENT_RETRY_ATTEMPTS or not self._is_transient_error(message):
                    raise
                if context.on_progress is not None:
                    if self._is_interruption_timeout(message):
                        context.on_progress(
                            _translate(
                                translate,
                                "agent.codex.progress.interruption_recovery",
                                "Codex 执行中断，正在自动续跑...",
                            )
                        )
                    else:
                        context.on_progress(
                            _translate(
                                translate,
                                "agent.codex.progress.transient_retry",
                                "Codex 连接中断，正在自动重试...",
                            )
                        )
                if self._is_interruption_timeout(message):
                    current_prompt = _translate(
                        translate,
                        CODEX_INTERRUPTION_RECOVERY_PROMPT_KEY,
                        CODEX_INTERRUPTION_RECOVERY_PROMPT_FALLBACK,
                        prompt=prompt.strip() or _translate(translate, "agent.codex.prompt.continue", "继续"),
                    )
                time.sleep(1)
        raise last_error or RuntimeError("Codex failed")

    def _invoke_app_server(self, agent: AgentLike, prompt: str, session_name: str, context: BackendContext) -> dict[str, str]:
        workdir = Path(agent.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        session_file = resolve_session_file(agent, session_name, context.session_dir)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        existing_session = context.codex_thread_id.strip() or (session_file.read_text(encoding="utf-8").strip() if session_file.exists() else "")
        final_prompt = build_final_prompt(agent, prompt)
        client = self._get_app_server(context)
        thread_params = self._app_server_thread_params(agent, workdir, context)
        if existing_session:
            thread_payload = client.request("thread/resume", {**thread_params, "threadId": existing_session}, timeout=90)
        else:
            thread_payload = client.request("thread/start", thread_params, timeout=90)
        thread = thread_payload.get("thread") if isinstance(thread_payload.get("thread"), dict) else {}
        thread_id = str(thread.get("id") or existing_session).strip()
        if not thread_id:
            raise RuntimeError("Codex app-server did not return a thread id")
        turn_payload = client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": final_prompt}],
                "model": agent.model or None,
                "cwd": str(workdir),
                "approvalPolicy": self._approval_policy(context),
                "sandboxPolicy": self._sandbox_policy(context),
                "effort": context.reasoning_effort or None,
            },
            timeout=30,
        )
        turn = turn_payload.get("turn") if isinstance(turn_payload.get("turn"), dict) else {}
        turn_id = str(turn.get("id") or "").strip()
        if not turn_id:
            raise RuntimeError("Codex app-server did not return a turn id")
        output, context_left_percent = client.wait_for_turn(
            thread_id,
            turn_id,
            timeout=max(60, int(getattr(context, "hub_task_timeout_seconds", 0) or 0), 60 * 30),
            on_progress=context.on_progress,
            on_live_output=context.on_live_output,
            on_reasoning=context.on_reasoning,
            on_activity=context.on_activity,
            reasoning_progress_label=lambda text: _translate(
                Localizer().translate,
                "agent.codex.progress.reasoning",
                "思考：{text}",
                text=text,
            ),
        )
        if not output.strip():
            raise RuntimeError("Codex app-server returned an empty result")
        session_file.write_text(thread_id, encoding="utf-8")
        result = {"output": output.strip(), "session_id": thread_id}
        if context_left_percent is not None:
            result["context_left_percent"] = str(context_left_percent)
        return result

    def _get_app_server(self, context: BackendContext) -> "_CodexAppServerClient":
        with self._app_server_lock:
            if self._app_server is not None and self._app_server.is_alive():
                return self._app_server
            self._app_server = _CodexAppServerClient(
                context.codex_command,
                creationflags=context.creationflags,
                start_new_session=context.start_new_session,
                slim_exec=context.codex_slim_exec,
            )
            self._app_server.start()
            return self._app_server

    def _app_server_thread_params(self, agent: AgentLike, workdir: Path, context: BackendContext) -> dict[str, object]:
        config: dict[str, object] = {}
        if context.codex_slim_exec:
            config["features"] = {"plugins": False}
        if context.mcp_server is not None:
            config["mcp_servers"] = {
                context.mcp_server.name: {
                    "command": context.mcp_server.command,
                    "args": context.mcp_server.args,
                    "default_tools_approval_mode": "approve",
                }
            }
        return {
            "model": agent.model or None,
            "cwd": str(workdir),
            "approvalPolicy": self._approval_policy(context),
            "sandbox": self._sandbox_mode(context),
            "config": config or None,
            "serviceName": "chatbridge",
        }

    @staticmethod
    def _approval_policy(context: BackendContext) -> str:
        return "never"

    @staticmethod
    def _sandbox_mode(context: BackendContext) -> str:
        permission_mode = context.permission_mode.strip().lower()
        if permission_mode in {"default", "workspace-write"}:
            return "workspace-write"
        if permission_mode == "read-only":
            return "read-only"
        return "danger-full-access"

    @classmethod
    def _sandbox_policy(cls, context: BackendContext) -> dict[str, str]:
        sandbox_mode = cls._sandbox_mode(context)
        if sandbox_mode == "workspace-write":
            return {"type": "workspaceWrite"}
        if sandbox_mode == "read-only":
            return {"type": "readOnly"}
        return {"type": "dangerFullAccess"}

    def _invoke_once(
        self,
        agent: AgentLike,
        prompt: str,
        session_name: str,
        context: BackendContext,
    ) -> dict[str, str]:
        workdir = Path(agent.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        session_file = resolve_session_file(agent, session_name, context.session_dir)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        existing_session = session_file.read_text(encoding="utf-8").strip() if session_file.exists() else ""
        final_prompt = build_final_prompt(agent, prompt)
        output_path = Path(tempfile.gettempdir()) / f"multi-codex-output-{uuid.uuid4().hex}.txt"

        options = ["--skip-git-repo-check", "--json", "-o", str(output_path)]
        if context.codex_slim_exec:
            options.extend(["--disable", "plugins", "--ignore-rules"])
        if agent.model:
            options.extend(["-m", agent.model])
        if context.reasoning_effort:
            options.extend(["-c", f'model_reasoning_effort="{context.reasoning_effort}"'])
        if context.codex_search_enabled:
            options.extend(["-c", 'web_search="live"'])
        image_options = self._image_options(context)
        sandbox_mode = self._sandbox_mode(context)
        resume_options: list[str] | None = None
        permission_profile_options = self._permission_profile_options(context)
        if permission_profile_options:
            options.extend(["-c", f'approval_policy="{self._approval_policy(context)}"', *permission_profile_options])
            resume_options = [
                "--skip-git-repo-check",
                "--json",
                "-o",
                str(output_path),
                "-c",
                f'approval_policy="{self._approval_policy(context)}"',
                *permission_profile_options,
            ]
            if context.codex_slim_exec:
                resume_options.extend(["--disable", "plugins", "--ignore-rules"])
            if agent.model:
                resume_options.extend(["-m", agent.model])
            if context.reasoning_effort:
                resume_options.extend(["-c", f'model_reasoning_effort="{context.reasoning_effort}"'])
            if context.codex_search_enabled:
                resume_options.extend(["-c", 'web_search="live"'])
        elif sandbox_mode in {"workspace-write", "read-only"}:
            options.extend(["-c", f'approval_policy="{self._approval_policy(context)}"', "-s", sandbox_mode])
            resume_options = [
                "--skip-git-repo-check",
                "--json",
                "-o",
                str(output_path),
                "-c",
                f'approval_policy="{self._approval_policy(context)}"',
                "-c",
                f'sandbox_mode="{sandbox_mode}"',
            ]
            if context.codex_slim_exec:
                resume_options.extend(["--disable", "plugins", "--ignore-rules"])
            if agent.model:
                resume_options.extend(["-m", agent.model])
            if context.reasoning_effort:
                resume_options.extend(["-c", f'model_reasoning_effort="{context.reasoning_effort}"'])
            if context.codex_search_enabled:
                resume_options.extend(["-c", 'web_search="live"'])
        else:
            options.append("--dangerously-bypass-approvals-and-sandbox")
            resume_options = list(options)
        options.extend(image_options)
        resume_options.extend(image_options)
        if context.mcp_server is not None:
            mcp_options = [
                "-c",
                f'mcp_servers.{context.mcp_server.name}.command="{context.mcp_server.command}"',
                "-c",
                f"mcp_servers.{context.mcp_server.name}.args={json.dumps(context.mcp_server.args, ensure_ascii=False)}",
                "-c",
                f'mcp_servers.{context.mcp_server.name}.default_tools_approval_mode="approve"',
            ]
            options.extend(mcp_options)
            resume_options.extend(mcp_options)
        if existing_session:
            argv = [context.codex_command, "exec", "resume", *resume_options, existing_session, final_prompt]
        else:
            argv = [context.codex_command, "exec", *options, "-C", str(workdir), final_prompt]

        proc = subprocess.Popen(
            argv,
            cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=context.creationflags,
            start_new_session=context.start_new_session,
            shell=False,
            bufsize=1,
        )
        if context.on_process_started is not None:
            context.on_process_started(proc.pid)
        stderr_lines: list[str] = []
        session_id = existing_session
        error_message = ""
        last_progress = ""
        last_progress_at = 0.0
        last_progress_perf_at = 0.0
        pending_delta = ""
        pending_reasoning = ""
        last_reasoning = ""
        last_reasoning_at = 0.0
        last_reasoning_item_id = ""
        reasoning_activities: dict[str, str] = {}
        last_reasoning_activity_at: dict[str, float] = {}
        last_reasoning_activity_text: dict[str, str] = {}
        context_left_percent: int | None = None
        assert proc.stderr is not None
        stdout_events: queue.Queue[dict[str, object] | None] = queue.Queue()

        def read_stderr() -> None:
            for raw_line in proc.stderr:
                stderr_lines.append(raw_line)

        def read_stdout() -> None:
            stdout = proc.stdout
            if stdout is None:
                stdout_events.put(None)
                return
            for raw_line in stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    stdout_events.put(event)
            stdout_events.put(None)

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()
        assert proc.stdout is not None
        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stdout_thread.start()
        timeout_seconds = max(1, int(getattr(context, "hub_task_timeout_seconds", 600) or 600))
        last_stdout_at = time.time()
        completed_by_event = False
        exit_error: RuntimeError | None = None
        while True:
            if self._is_cancel_requested(context):
                terminate_process_tree(int(getattr(proc, "pid", 0) or 0))
                raise RuntimeError("Task canceled during execution.")
            remaining = (last_stdout_at + timeout_seconds) - time.time()
            if remaining <= 0:
                terminate_process_tree(int(getattr(proc, "pid", 0) or 0))
                raise RuntimeError(f"Codex stdout idle timed out after {timeout_seconds} seconds")
            try:
                event = stdout_events.get(timeout=min(0.5, remaining))
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if event is None:
                break
            last_stdout_at = time.time()
            if event.get("type") == "thread.started" and event.get("thread_id"):
                session_id = str(event["thread_id"])
                session_file.write_text(session_id, encoding="utf-8")
            if event.get("type") == "error" and event.get("message"):
                error_message = str(event["message"])
            if isinstance(event.get("error"), dict) and event["error"].get("message"):
                error_message = str(event["error"]["message"])
            next_context_left = self._extract_context_left_percent(event)
            if next_context_left is not None:
                context_left_percent = next_context_left
                if context.on_context_left_percent is not None:
                    context.on_context_left_percent(next_context_left)
            command_activity = self._extract_exec_command_activity(event)
            if command_activity and context.on_activity is not None:
                context.on_activity(command_activity)
            reasoning_update, reasoning_is_delta = self._extract_reasoning_update(event)
            if reasoning_update:
                cleaned_reasoning = reasoning_update.strip()
                reasoning_item_id = self._extract_reasoning_item_id(event)
                if not reasoning_item_id:
                    reasoning_item_id = "reasoning" if reasoning_is_delta else f"reasoning-{len(reasoning_activities) + 1}"
                if reasoning_is_delta:
                    reasoning_activities[reasoning_item_id] = f"{reasoning_activities.get(reasoning_item_id, '')}{reasoning_update}"
                else:
                    reasoning_activities[reasoning_item_id] = cleaned_reasoning
                now = time.time()
                if (
                    context.on_activity is not None
                    and last_reasoning_activity_text.get(reasoning_item_id) != reasoning_activities[reasoning_item_id].strip()
                    and (not reasoning_is_delta or now - last_reasoning_activity_at.get(reasoning_item_id, 0.0) >= PROGRESS_PUSH_INTERVAL_SECONDS)
                ):
                    last_reasoning_activity_at[reasoning_item_id] = now
                    last_reasoning_activity_text[reasoning_item_id] = reasoning_activities[reasoning_item_id].strip()
                    context.on_activity(
                        _CodexAppServerClient._reasoning_activity_payload(
                            reasoning_item_id,
                            reasoning_activities[reasoning_item_id],
                        )
                    )
                if reasoning_is_delta:
                    if last_reasoning_item_id and reasoning_item_id != last_reasoning_item_id and pending_reasoning:
                        pending_reasoning = f"{pending_reasoning.rstrip()}\n\n"
                    last_reasoning_item_id = reasoning_item_id
                    pending_reasoning += reasoning_update
                elif not pending_reasoning:
                    pending_reasoning = cleaned_reasoning
                elif cleaned_reasoning.startswith(pending_reasoning):
                    pending_reasoning = cleaned_reasoning
                elif cleaned_reasoning not in pending_reasoning:
                    pending_reasoning = f"{pending_reasoning.rstrip()}\n\n{cleaned_reasoning}"
                reasoning_callback = context.on_reasoning or context.on_progress
                now = time.time()
                force_reasoning = not reasoning_is_delta
                if (
                    reasoning_callback is not None
                    and pending_reasoning.strip()
                    and pending_reasoning.strip() != last_reasoning
                    and (force_reasoning or now - last_reasoning_at >= PROGRESS_PUSH_INTERVAL_SECONDS)
                ):
                    last_reasoning = pending_reasoning.strip()
                    last_reasoning_at = now
                    last_progress_perf_at = time.perf_counter()
                    reasoning_callback(last_reasoning)
            delta = "" if reasoning_update else self._extract_text_delta(event)
            progress = ""
            if delta:
                pending_delta += delta
                force_chunk = time.time() - last_progress_at >= PROGRESS_PUSH_INTERVAL_SECONDS
                progress, pending_delta = self._take_stream_chunk(pending_delta, force=force_chunk)
            if context.on_progress is not None and progress:
                now = time.time()
                if progress == last_progress:
                    continue
                if now - last_progress_at < PROGRESS_PUSH_INTERVAL_SECONDS:
                    continue
                last_progress = progress
                last_progress_at = now
                last_progress_perf_at = time.perf_counter()
                context.on_progress(progress)
            if self._is_task_complete_event(event):
                completed_by_event = True
                break
        stdout_done_at = time.perf_counter()
        if context.on_progress is not None:
            trailing_chunk, pending_delta = self._take_stream_chunk(pending_delta, force=True)
            if trailing_chunk and trailing_chunk != last_progress:
                last_progress_perf_at = time.perf_counter()
                context.on_progress(trailing_chunk)
        reasoning_callback = context.on_reasoning or context.on_progress
        trailing_reasoning = pending_reasoning.strip()
        if context.on_activity is not None:
            for reasoning_item_id, reasoning_text in reasoning_activities.items():
                if last_reasoning_activity_text.get(reasoning_item_id) == reasoning_text.strip():
                    continue
                context.on_activity(_CodexAppServerClient._reasoning_activity_payload(reasoning_item_id, reasoning_text))
        if reasoning_callback is not None and trailing_reasoning and trailing_reasoning != last_reasoning:
            last_progress_perf_at = time.perf_counter()
            reasoning_callback(trailing_reasoning)
        wait_started_at = time.perf_counter()
        try:
            completed_returncode = self._wait_for_exit(proc)
        except RuntimeError as exc:
            if not completed_by_event:
                raise
            exit_error = exc
            completed_returncode = 0
        process_exit_at = time.perf_counter()
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        threads_joined_at = time.perf_counter()
        if not error_message:
            error_message = "".join(stderr_lines).strip()
        if completed_returncode != 0:
            output_path.unlink(missing_ok=True)
            raise RuntimeError(error_message or f"Codex exited with code {completed_returncode}")
        if not output_path.exists():
            if exit_error is not None:
                raise exit_error
            raise RuntimeError("Codex did not produce an output file")
        output = output_path.read_text(encoding="utf-8").strip()
        output_path.unlink(missing_ok=True)
        if not output:
            raise RuntimeError("Codex returned an empty result")
        if session_id:
            session_file.write_text(session_id, encoding="utf-8")
        now_perf = time.perf_counter()
        progress_to_stdout_done_ms = int((stdout_done_at - last_progress_perf_at) * 1000) if last_progress_perf_at else -1
        print(
            "[codex-perf] exec_finalize "
            f"pid={getattr(proc, 'pid', '-')} "
            f"returncode={completed_returncode} "
            f"completed_by_event={int(completed_by_event)} "
            f"forced_cleanup_after_complete={int(exit_error is not None)} "
            f"progress_to_stdout_done_ms={progress_to_stdout_done_ms} "
            f"wait_exit_ms={int((process_exit_at - wait_started_at) * 1000)} "
            f"join_threads_ms={int((threads_joined_at - process_exit_at) * 1000)} "
            f"read_output_ms={int((now_perf - threads_joined_at) * 1000)}",
            flush=True,
        )
        result = {"output": output, "session_id": session_id}
        if context_left_percent is not None:
            result["context_left_percent"] = str(context_left_percent)
        return result

    def list_app_server_threads(
        self,
        context: BackendContext,
        *,
        limit: int = 50,
        cursor: str = "",
        search_term: str = "",
        cwd: str = "",
        archived: bool | None = False,
    ) -> dict[str, object]:
        with self._app_server_lock:
            payload = self._get_app_server(context).list_threads(
                limit=limit,
                cursor=cursor,
                search_term=search_term,
                cwd=cwd,
                archived=archived,
            )
        data = payload.get("data") if isinstance(payload.get("data"), list) else []
        threads = [self._normalize_app_server_thread(item) for item in data if isinstance(item, dict)]
        if archived is not None:
            for thread in threads:
                thread["archived"] = bool(archived)
        return {
            "threads": threads,
            "next_cursor": str(payload.get("nextCursor") or ""),
            "backwards_cursor": str(payload.get("backwardsCursor") or ""),
        }

    def read_app_server_thread(self, context: BackendContext, thread_id: str) -> dict[str, object]:
        cleaned_thread_id = thread_id.strip()
        if not cleaned_thread_id:
            raise ValueError("thread_id is required")
        with self._app_server_lock:
            payload = self._get_app_server(context).read_thread(cleaned_thread_id, include_turns=True)
        thread = payload.get("thread") if isinstance(payload.get("thread"), dict) else {}
        normalized = self._normalize_app_server_thread(thread)
        normalized["messages"] = self._normalize_app_server_thread_messages(thread)
        return normalized

    def send_app_server_thread_message(
        self,
        context: BackendContext,
        thread_id: str,
        prompt: str,
        *,
        images: list[str] | None = None,
        model: str = "",
        reasoning_effort: str = "",
        timeout_seconds: float = 15.0,
    ) -> dict[str, object]:
        cleaned_thread_id = str(thread_id or "").strip()
        cleaned_prompt = str(prompt or "").strip()
        if not cleaned_thread_id:
            raise ValueError("thread_id is required")
        if not cleaned_prompt:
            raise ValueError("prompt is required")
        cleaned_images = list(
            dict.fromkeys(str(item or "").strip() for item in (images or []) if str(item or "").strip())
        )
        client_user_message_id = f"chatbridge:{uuid.uuid4()}"
        input_items: list[dict[str, str]] = [{"type": "text", "text": cleaned_prompt}]
        input_items.extend({"type": "localImage", "path": image} for image in cleaned_images)
        deadline = time.monotonic() + max(3.0, float(timeout_seconds or 15.0))

        def request(method: str, params: dict[str, object]) -> dict[str, Any]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"timed out preparing Codex app-server message: {method}")
            return client.request(method, params, timeout=max(0.5, remaining))

        client = self._get_app_server(context)
        thread_payload = request(
            "thread/read",
            {"threadId": cleaned_thread_id, "includeTurns": False},
        )
        thread = thread_payload.get("thread") if isinstance(thread_payload.get("thread"), dict) else {}
        resume_params: dict[str, object] = {
            "threadId": cleaned_thread_id,
            "excludeTurns": True,
        }
        rollout_path = str(thread.get("path") or "").strip()
        if rollout_path:
            resume_params["path"] = rollout_path
        request("thread/resume", resume_params)
        turns_payload = request(
            "thread/turns/list",
            {
                "threadId": cleaned_thread_id,
                "limit": 1,
                "sortDirection": "desc",
                "itemsView": "notLoaded",
            },
        )
        turns = turns_payload.get("data") if isinstance(turns_payload.get("data"), list) else []
        active_turn = next(
            (
                item
                for item in turns
                if isinstance(item, dict)
                and str(item.get("status") or "").replace("_", "").lower() in {"inprogress", "running"}
            ),
            None,
        )
        active_turn_id = str(active_turn.get("id") or "").strip() if isinstance(active_turn, dict) else ""
        if active_turn_id:
            mode = "steer"
            result = request(
                "turn/steer",
                {
                    "threadId": cleaned_thread_id,
                    "expectedTurnId": active_turn_id,
                    "input": input_items,
                    "clientUserMessageId": client_user_message_id,
                },
            )
            turn_id = str(result.get("turnId") or active_turn_id).strip()
        else:
            mode = "start"
            params: dict[str, object] = {
                "threadId": cleaned_thread_id,
                "input": input_items,
                "clientUserMessageId": client_user_message_id,
            }
            if str(model or "").strip():
                params["model"] = str(model).strip()
            if str(reasoning_effort or "").strip():
                params["effort"] = str(reasoning_effort).strip()
            result = request("turn/start", params)
            turn = result.get("turn") if isinstance(result.get("turn"), dict) else {}
            turn_id = str(turn.get("id") or "").strip()
        if not turn_id:
            raise RuntimeError("Codex app-server did not return a turn id")
        threading.Thread(
            target=self._drain_app_server_turn,
            args=(client, cleaned_thread_id, turn_id),
            daemon=True,
            name=f"codex-history-turn-{turn_id[:8]}",
        ).start()
        return {
            "thread_id": cleaned_thread_id,
            "turn_id": turn_id,
            "mode": mode,
            "client_user_message_id": client_user_message_id,
            "reconciled": False,
        }

    @staticmethod
    def _drain_app_server_turn(client: _CodexAppServerClient, thread_id: str, turn_id: str) -> None:
        try:
            client.wait_for_turn(
                thread_id,
                turn_id,
                timeout=60 * 60 * 6,
                on_progress=None,
            )
        except RuntimeError:
            pass

    @classmethod
    def _normalize_app_server_thread(cls, thread: dict[str, Any]) -> dict[str, object]:
        thread_id = str(thread.get("id") or thread.get("sessionId") or "").strip()
        cwd = str(thread.get("cwd") or "").strip()
        git_info = thread.get("gitInfo") if isinstance(thread.get("gitInfo"), dict) else {}
        status = thread.get("status") if isinstance(thread.get("status"), dict) else {}
        preview = str(thread.get("preview") or "").strip()
        name = str(thread.get("name") or "").strip()
        title = name or preview or (Path(cwd).name if cwd else thread_id)
        return {
            "id": thread_id,
            "session_id": str(thread.get("sessionId") or thread_id).strip(),
            "title": title,
            "preview": preview,
            "cwd": cwd,
            "source": str(thread.get("source") or thread.get("threadSource") or "").strip(),
            "model_provider": str(thread.get("modelProvider") or "").strip(),
            "created_at": cls._format_app_server_timestamp(thread.get("createdAt")),
            "updated_at": cls._format_app_server_timestamp(thread.get("updatedAt")),
            "recency_at": cls._format_app_server_timestamp(thread.get("recencyAt")),
            "status": str(status.get("type") or thread.get("status") or "").strip(),
            "branch": str(git_info.get("branch") or "").strip(),
            "sha": str(git_info.get("sha") or "").strip(),
            "path": str(thread.get("path") or "").strip(),
        }

    @classmethod
    def _normalize_app_server_thread_messages(cls, thread: dict[str, Any]) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []
        turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
        for turn_index, turn in enumerate(turns, start=1):
            if not isinstance(turn, dict):
                continue
            turn_id = str(turn.get("id") or "").strip()
            turn_at = cls._app_server_item_timestamp(turn)
            items = turn.get("items") if isinstance(turn.get("items"), list) else []
            for item_index, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    continue
                message = cls._normalize_app_server_item(item, turn_id=turn_id, turn_order=turn_index, item_order=item_index, fallback_at=turn_at)
                if message is not None:
                    messages.append(message)
        return messages

    @classmethod
    def _normalize_app_server_item(
        cls,
        item: dict[str, Any],
        *,
        turn_id: str,
        turn_order: int = 0,
        item_order: int = 0,
        fallback_at: str = "",
    ) -> dict[str, object] | None:
        item_type = str(item.get("type") or "").strip()
        item_id = str(item.get("id") or "").strip()
        if item_type == "userMessage":
            content = item.get("content") if isinstance(item.get("content"), list) else []
            text = "\n".join(
                str(part.get("text") or "").strip()
                for part in content
                if isinstance(part, dict) and str(part.get("text") or "").strip()
            ).strip()
            role = "user"
        elif item_type == "agentMessage":
            text = str(item.get("text") or "").strip()
            role = "assistant"
        elif item_type == "reasoning":
            summary = item.get("summary") if isinstance(item.get("summary"), list) else []
            text = "\n".join(str(part).strip() for part in summary if str(part).strip()).strip()
            role = "reasoning"
        else:
            return cls._normalize_app_server_activity_item(
                item,
                turn_id=turn_id,
                item_type=item_type,
                item_id=item_id,
                turn_order=turn_order,
                item_order=item_order,
                fallback_at=fallback_at,
            )
        if not text:
            return None
        return {
            "id": item_id,
            "turn_id": turn_id,
            "role": role,
            "phase": str(item.get("phase") or "").strip(),
            "at": cls._app_server_item_timestamp(item) or fallback_at,
            "turn_order": turn_order,
            "item_order": item_order,
            "text": text,
        }

    @classmethod
    def _normalize_app_server_activity_item(
        cls,
        item: dict[str, Any],
        *,
        turn_id: str,
        item_type: str,
        item_id: str,
        turn_order: int = 0,
        item_order: int = 0,
        fallback_at: str = "",
    ) -> dict[str, object] | None:
        activity = cls._app_server_activity_payload(item, item_type=item_type, item_id=item_id, fallback_at=fallback_at)
        if not activity:
            return None
        return {
            "id": item_id,
            "turn_id": turn_id,
            "role": "activity",
            "phase": str(item.get("phase") or "").strip(),
            "at": str(activity.get("at") or cls._app_server_item_timestamp(item) or fallback_at),
            "turn_order": turn_order,
            "item_order": item_order,
            "text": str(activity.get("detail") or activity.get("event") or item_type).strip(),
            "activity": activity,
        }

    @classmethod
    def _app_server_activity_payload(cls, item: dict[str, Any], *, item_type: str, item_id: str, fallback_at: str = "") -> dict[str, object]:
        normalized_type = item_type.replace("-", "_").replace(".", "_")
        event = {
            "commandExecution": "codex_command",
            "mcpToolCall": "codex_tool_call",
            "toolCall": "codex_tool_call",
            "tool_call": "codex_tool_call",
            "function_call": "codex_tool_call",
            "todo": "codex_todo",
            "todo_list": "codex_todo",
            "activity_log": "codex_activity",
            "activityLog": "codex_activity",
            "compaction": "codex_compaction",
            "error": "codex_error",
        }.get(item_type) or {
            "commandexecution": "codex_command",
            "command_execution": "codex_command",
            "mcptoolcall": "codex_tool_call",
            "toolcall": "codex_tool_call",
            "tool_call": "codex_tool_call",
            "todo": "codex_todo",
            "todo_list": "codex_todo",
            "activity_log": "codex_activity",
            "activitylog": "codex_activity",
            "compaction": "codex_compaction",
            "error": "codex_error",
        }.get(normalized_type.lower(), "codex_item")
        normalized_status = str(item.get("status") or "").replace("_", "").lower()
        activity_type = "error" if event == "codex_error" or normalized_status in {"failed", "declined"} else "success" if normalized_status == "completed" else "info"
        detail = cls._app_server_activity_detail(item, item_type=item_type)
        metadata = cls._app_server_activity_metadata(item, item_type=item_type, item_id=item_id)
        if not detail and event == "codex_item" and not metadata:
            return {}
        return {
            "event": event,
            "type": activity_type,
            "at": cls._app_server_item_timestamp(item) or fallback_at,
            "detail": detail or item_type or "Codex item",
            "metadata": metadata,
        }

    @classmethod
    def _app_server_activity_detail(cls, item: dict[str, Any], *, item_type: str) -> str:
        detail = item.get("detail")
        normalized_item_type = item_type.replace("-", "_").replace(".", "_").lower()
        if normalized_item_type in {"commandexecution", "command_execution"}:
            return str(item.get("command") or item_type).strip()
        if normalized_item_type in {"mcptoolcall", "toolcall", "tool_call", "function_call"}:
            return cls._app_server_tool_call_command(item)
        if isinstance(detail, dict) and normalized_item_type in {"toolcall", "tool_call", "function_call"}:
            detail_type = str(detail.get("type") or item.get("name") or "").strip()
            for key in ("command", "description", "text", "log", "message"):
                value = detail.get(key)
                if isinstance(value, str) and value.strip():
                    return f"{detail_type}: {value.strip()}" if detail_type else value.strip()
            if detail_type:
                return detail_type
        for key in ("message", "text", "summary", "label", "name", "title", "status", "error"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if isinstance(detail, dict):
            detail_type = str(detail.get("type") or "").strip()
            for key in ("command", "description", "text", "log", "message"):
                value = detail.get(key)
                if isinstance(value, str) and value.strip():
                    return f"{detail_type}: {value.strip()}" if detail_type else value.strip()
            if detail_type:
                return detail_type
        for key in ("items", "todos"):
            value = item.get(key)
            if isinstance(value, list):
                lines = []
                for entry in value[:8]:
                    if isinstance(entry, dict):
                        text = str(entry.get("text") or entry.get("content") or entry.get("title") or "").strip()
                        if text:
                            prefix = "[x]" if bool(entry.get("completed")) else "[ ]"
                            lines.append(f"{prefix} {text}")
                    elif str(entry).strip():
                        lines.append(str(entry).strip())
                if lines:
                    return "\n".join(lines)
        compact = cls._compact_app_server_value(
            {
                key: value
                for key, value in item.items()
                if key not in {"id", "type", "turn_id", "turnId"}
            },
            limit=500,
        )
        return compact if compact != "{}" else item_type

    @classmethod
    def _app_server_activity_metadata(cls, item: dict[str, Any], *, item_type: str, item_id: str) -> dict[str, str]:
        metadata: dict[str, str] = {"item_type": item_type}
        if item_id:
            metadata["item_id"] = item_id
        for key in ("callId", "name", "server", "tool", "status", "phase", "trigger", "preTokens", "command", "cwd", "exitCode", "durationMs", "source"):
            value = item.get(key)
            if value is not None and str(value).strip():
                metadata[key] = str(value).strip()
        normalized_item_type = item_type.replace("-", "_").replace(".", "_").lower()
        if normalized_item_type in {"mcptoolcall", "toolcall", "tool_call", "function_call"}:
            command = cls._app_server_tool_call_command(item)
            output = cls._app_server_tool_call_output(item)
            if command:
                metadata["command"] = command
            if output:
                metadata["output"] = output
        aggregated_output = item.get("aggregatedOutput")
        if isinstance(aggregated_output, str) and aggregated_output:
            metadata["output"] = aggregated_output[-COMMAND_OUTPUT_LIMIT:]
        raw_metadata = item.get("metadata")
        if isinstance(raw_metadata, dict):
            for key, value in raw_metadata.items():
                cleaned_key = str(key or "").strip()
                if cleaned_key and value is not None:
                    metadata[f"metadata.{cleaned_key}"] = cls._compact_app_server_value(value, limit=220)
        detail = item.get("detail")
        if isinstance(detail, dict):
            for key, value in detail.items():
                cleaned_key = str(key or "").strip()
                if cleaned_key and value is not None and cleaned_key not in {"log"}:
                    metadata[f"detail.{cleaned_key}"] = cls._compact_app_server_value(value, limit=220)
        return metadata

    @classmethod
    def _app_server_tool_call_command(cls, item: dict[str, Any]) -> str:
        server = str(item.get("server") or item.get("name") or "").strip()
        tool = str(item.get("tool") or "").strip()
        label = ".".join(part for part in (server, tool) if part) or "tool"
        arguments = item.get("arguments")
        if not isinstance(arguments, dict):
            detail = item.get("detail")
            if isinstance(detail, dict):
                detail_type = str(detail.get("type") or item.get("name") or label).strip() or label
                for key in ("command", "description", "text", "message"):
                    value = detail.get(key)
                    if isinstance(value, str) and value.strip():
                        return f"{detail_type}: {value.strip()}"
            compact = cls._compact_app_server_value(arguments, limit=4000) if arguments not in (None, "") else ""
            return f"{label} {compact}".strip()
        title = str(arguments.get("title") or "").strip()
        header = f"{label} - {title}" if title else label
        for key in ("cmd", "command", "code"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return f"{header}\n{value.strip()}"[:8000]
        compact = cls._compact_app_server_value(arguments, limit=4000)
        return f"{header}\n{compact}" if compact and compact != "{}" else header

    @staticmethod
    def _app_server_tool_call_output(item: dict[str, Any]) -> str:
        fragments = [fragment.strip() for fragment in collect_text_fragments(item.get("result")) if fragment.strip()]
        detail = item.get("detail")
        if isinstance(detail, dict):
            for key in ("log", "output", "result"):
                value = detail.get(key)
                if isinstance(value, str) and value.strip():
                    fragments.append(value.strip())
        error = item.get("error")
        if isinstance(error, str) and error.strip():
            fragments.append(error.strip())
        elif isinstance(error, dict):
            message = str(error.get("message") or error.get("error") or "").strip()
            if message:
                fragments.append(message)
        return "\n".join(fragments)[-COMMAND_OUTPUT_LIMIT:]

    @classmethod
    def _app_server_item_timestamp(cls, item: dict[str, Any]) -> str:
        for key in ("timestamp", "createdAt", "updatedAt", "completedAt", "completedAtMs"):
            value = item.get(key)
            if value not in (None, ""):
                return cls._format_app_server_timestamp(value)
        return ""

    @staticmethod
    def _compact_app_server_value(value: object, *, limit: int = 800) -> str:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return f"{compact[: max(0, limit - 1)]}..."

    @staticmethod
    def _format_app_server_timestamp(value: object) -> str:
        if isinstance(value, (int, float)) and value > 0:
            try:
                return datetime.fromtimestamp(float(value)).isoformat(timespec="seconds")
            except (OSError, OverflowError, ValueError):
                return str(value)
        return str(value or "").strip()

    def _wait_for_exit(self, proc: subprocess.Popen) -> int:
        try:
            return int(proc.wait(timeout=CODEX_EXIT_TIMEOUT_SECONDS) or 0)
        except TypeError:
            return int(proc.wait() or 0)
        except subprocess.TimeoutExpired as exc:
            terminate_process_tree(int(getattr(proc, "pid", 0) or 0))
            try:
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError("timeout waiting for child process to exit") from exc

    @staticmethod
    def _is_transient_error(message: str) -> bool:
        lowered = str(message or "").lower()
        return any(marker in lowered for marker in TRANSIENT_ERROR_MARKERS)

    @staticmethod
    def _is_interruption_timeout(message: str) -> bool:
        lowered = str(message or "").lower()
        return any(marker in lowered for marker in INTERRUPTION_RECOVERY_MARKERS)

    @staticmethod
    def _is_task_complete_event(event: dict[str, object]) -> bool:
        if str(event.get("type") or "") == "turn.completed":
            return True
        if str(event.get("type") or "") != "event_msg":
            return False
        payload = event.get("payload")
        return isinstance(payload, dict) and str(payload.get("type") or "") == "task_complete"

    @staticmethod
    def _is_cancel_requested(context: BackendContext) -> bool:
        return bool(context.is_cancel_requested and context.is_cancel_requested())

    @staticmethod
    def _image_options(context: BackendContext) -> list[str]:
        options: list[str] = []
        for image in context.images or []:
            path = str(image or "").strip()
            if path:
                options.extend(["-i", path])
        return options

    @staticmethod
    def _permission_profile_options(context: BackendContext) -> list[str]:
        profile = str(context.permission_profile or "").strip()
        if not profile:
            return []
        profile_key = profile.replace("\\", "\\\\").replace('"', '\\"')
        filesystem = '{":minimal"="read",":workspace_roots"={"."="read",".chatbridge_attachments"="read"}}'
        options = [
            "-c",
            f'default_permissions="{profile_key}"',
            "-c",
            f"permissions.{profile_key}.filesystem={filesystem}",
        ]
        if context.codex_search_enabled:
            options.extend(
                [
                    "-c",
                    f'permissions.{profile_key}.network={{enabled=true,domains={{"*"="allow"}}}}',
                ]
            )
        return options

    def _take_stream_chunk(self, buffer: str, *, force: bool) -> tuple[str, str]:
        normalized = buffer.replace("\r", "")
        if not normalized.strip():
            return "", ""
        if not force:
            for separator in ("\n", "。", "！", "？", ". ", "! ", "? ", "；", ";"):
                index = normalized.rfind(separator)
                if index >= 0:
                    cut = index + len(separator)
                    chunk = normalized[:cut].strip()
                    remainder = normalized[cut:]
                    if chunk:
                        return chunk, remainder
            return "", buffer
        chunk = normalized.strip()
        return chunk, ""

    def _extract_text_delta(self, value: object, event_type: str = "") -> str:
        if isinstance(value, dict):
            current_event_type = str(value.get("type") or value.get("event") or value.get("method") or event_type).lower()
            for key in ("delta", "text_delta", "output_text", "text"):
                raw = value.get(key)
                if isinstance(raw, str) and raw.strip():
                    if "delta" in current_event_type or "message" in current_event_type or "response" in current_event_type:
                        return raw
            for item in value.values():
                nested = self._extract_text_delta(item, current_event_type)
                if nested:
                    return nested
        if isinstance(value, list):
            for item in value:
                nested = self._extract_text_delta(item, event_type)
                if nested:
                    return nested
        return ""

    @staticmethod
    def _extract_exec_command_activity(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            return {}
        event_type = str(value.get("type") or "").strip()
        if event_type not in {"item.started", "item.updated", "item.completed"}:
            return {}
        item = value.get("item")
        if not isinstance(item, dict) or str(item.get("type") or "") != "command_execution":
            return {}
        return _CodexAppServerClient._command_activity_payload(item)

    @staticmethod
    def _extract_reasoning_item_id(value: object) -> str:
        if not isinstance(value, dict):
            return ""
        params = value.get("params") if isinstance(value.get("params"), dict) else {}
        item = value.get("item") if isinstance(value.get("item"), dict) else {}
        if not item and isinstance(params.get("item"), dict):
            item = params["item"]
        return str(
            params.get("itemId")
            or params.get("item_id")
            or item.get("id")
            or item.get("itemId")
            or value.get("item_id")
            or ""
        ).strip()

    @staticmethod
    def _extract_reasoning_update(value: object) -> tuple[str, bool]:
        if not isinstance(value, dict):
            return "", False
        event_type = str(value.get("method") or value.get("type") or value.get("event") or "").lower()
        candidates = [value]
        for key in ("params", "item", "payload"):
            nested = value.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
                nested_item = nested.get("item")
                if isinstance(nested_item, dict):
                    candidates.append(nested_item)
        params = value.get("params")
        reasoning_candidate = params if "reasoning" in event_type and isinstance(params, dict) else next(
            (
                candidate
                for candidate in candidates[1:]
                if "reasoning" in str(candidate.get("type") or candidate.get("event") or "").lower()
            ),
            None,
        )
        if reasoning_candidate is None and "reasoning" not in event_type:
            return "", False
        target = reasoning_candidate or value
        for key in ("delta", "textDelta", "text_delta", "text"):
            text = target.get(key)
            if isinstance(text, str) and text.strip():
                return text, "delta" in event_type
        summary = target.get("summary")
        if isinstance(summary, list):
            parts = []
            for item in summary:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            if parts:
                return "\n".join(parts), "delta" in event_type
        if target is not value:
            for key in ("delta", "textDelta", "text_delta", "text"):
                text = value.get(key)
                if isinstance(text, str) and text.strip():
                    return text, "delta" in event_type
        return "", False

    def _extract_context_left_percent(self, value: object) -> int | None:
        if not isinstance(value, dict):
            return None
        if str(value.get("type") or "") != "event_msg":
            return None
        payload = value.get("payload")
        if not isinstance(payload, dict) or str(payload.get("type") or "") != "token_count":
            return None
        info = payload.get("info")
        if not isinstance(info, dict):
            return None
        usage = info.get("last_token_usage")
        if not isinstance(usage, dict):
            return None
        total_tokens = usage.get("total_tokens")
        context_window = info.get("model_context_window")
        if not isinstance(total_tokens, int) or not isinstance(context_window, int) or context_window <= 0:
            return None
        return max(0, min(100, round((1 - (total_tokens / context_window)) * 100)))

def _translate(translate: Callable[..., str], key: str, fallback: str, **kwargs: object) -> str:
    value = str(translate(key, **kwargs) or "")
    if value and value != key:
        return value
    return fallback.format(**kwargs)
