from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from agent_backends.base import AgentBackend, AgentLike, BackendContext
from agent_backends.shared import build_final_prompt, resolve_session_file
from core.platform_compat import terminate_process_tree

PROGRESS_PUSH_INTERVAL_SECONDS = 1.0
CODEX_EXIT_TIMEOUT_SECONDS = 10
CODEX_TRANSIENT_RETRY_ATTEMPTS = 2
TRANSIENT_ERROR_MARKERS = (
    "stream disconnected before completion",
    "error sending request",
    "timeout waiting for child process to exit",
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
)


class _CodexAppServerClient:
    def __init__(self, command: str, *, creationflags: int, start_new_session: bool, slim_exec: bool) -> None:
        self.command = command
        self.creationflags = creationflags
        self.start_new_session = start_new_session
        self.slim_exec = slim_exec
        self.proc: subprocess.Popen | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._next_id = 1
        self._lock = threading.RLock()

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
        request_id = self._send(method, params, expect_response=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self._read_message(deadline)
            if message.get("id") != request_id:
                continue
            if isinstance(message.get("error"), dict):
                error = message["error"]
                raise RuntimeError(str(error.get("message") or error))
            result = message.get("result")
            return result if isinstance(result, dict) else {}
        raise RuntimeError(f"timed out waiting for app-server response: {method}")

    def notify(self, method: str, params: dict[str, object]) -> None:
        self._send(method, params, expect_response=False)

    def wait_for_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        timeout: float,
        on_progress: Callable[[str], None] | None,
    ) -> tuple[str, int | None]:
        deadline = time.time() + timeout
        output = ""
        pending_delta = ""
        last_progress = ""
        last_progress_at = 0.0
        context_left_percent: int | None = None
        while time.time() < deadline:
            message = self._read_message(deadline)
            method = str(message.get("method") or "")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if str(params.get("threadId") or "") not in {"", thread_id}:
                continue
            if str(params.get("turnId") or "") not in {"", turn_id}:
                continue
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            if method == "item/completed" and item.get("type") == "agentMessage":
                text = str(item.get("text") or "").strip()
                if text:
                    output = text
            if method.endswith("/delta"):
                delta = self._extract_delta(message)
                if delta:
                    pending_delta += delta
                    if on_progress is not None and time.time() - last_progress_at >= PROGRESS_PUSH_INTERVAL_SECONDS:
                        progress = pending_delta.strip()
                        if progress and progress != last_progress:
                            on_progress(progress)
                            last_progress = progress
                            last_progress_at = time.time()
            next_context_left = self._extract_context_left_percent(message)
            if next_context_left is not None:
                context_left_percent = next_context_left
            if method == "turn/completed":
                error = ((params.get("turn") if isinstance(params.get("turn"), dict) else {}) or {}).get("error")
                if error:
                    raise RuntimeError(str(error))
                return output, context_left_percent
        raise RuntimeError(f"timed out waiting for app-server turn: {turn_id}")

    def _send(self, method: str, params: dict[str, object], *, expect_response: bool) -> int | None:
        with self._lock:
            if self.proc is None or self.proc.stdin is None or self.proc.poll() is not None:
                raise RuntimeError("Codex app-server is not running")
            message: dict[str, object] = {"method": method, "params": params}
            request_id: int | None = None
            if expect_response:
                request_id = self._next_id
                self._next_id += 1
                message["id"] = request_id
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            return request_id

    def _read_message(self, deadline: float) -> dict[str, Any]:
        timeout = max(0.1, deadline - time.time())
        try:
            return self._messages.get(timeout=timeout)
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
                self._messages.put(message)

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
    def _extract_context_left_percent(message: dict[str, Any]) -> int | None:
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        raw_percent = params.get("contextLeftPercent")
        if isinstance(raw_percent, int):
            return max(0, min(100, raw_percent))
        return None


class CodexBackend(AgentBackend):
    key = "codex"

    def __init__(self) -> None:
        self._app_server_lock = threading.RLock()
        self._app_server: _CodexAppServerClient | None = None

    def invoke(self, agent: AgentLike, prompt: str, session_name: str, context: BackendContext) -> dict[str, str]:
        if context.codex_transport.strip().lower() == "app-server":
            try:
                with self._app_server_lock:
                    return self._invoke_app_server(agent, prompt, session_name, context)
            except RuntimeError as exc:
                if context.on_progress is not None:
                    context.on_progress(f"Codex app-server 调用失败，已回退 exec：{str(exc)[:160]}")
        last_error: RuntimeError | None = None
        for attempt in range(CODEX_TRANSIENT_RETRY_ATTEMPTS + 1):
            try:
                return self._invoke_once(agent, prompt, session_name, context)
            except RuntimeError as exc:
                last_error = exc
                if self._is_cancel_requested(context):
                    raise
                if attempt >= CODEX_TRANSIENT_RETRY_ATTEMPTS or not self._is_transient_error(str(exc)):
                    raise
                if context.on_progress is not None:
                    context.on_progress("Codex 连接中断，正在自动重试...")
                time.sleep(1)
        raise last_error or RuntimeError("Codex failed")

    def _invoke_app_server(self, agent: AgentLike, prompt: str, session_name: str, context: BackendContext) -> dict[str, str]:
        workdir = Path(agent.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        session_file = resolve_session_file(agent, session_name, context.session_dir)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        existing_session = session_file.read_text(encoding="utf-8").strip() if session_file.exists() else ""
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
        )
        if not output.strip():
            raise RuntimeError("Codex app-server returned an empty result")
        session_file.write_text(thread_id, encoding="utf-8")
        result = {"output": output.strip(), "session_id": thread_id}
        if context_left_percent is not None:
            result["context_left_percent"] = str(context_left_percent)
        return result

    def _get_app_server(self, context: BackendContext) -> "_CodexAppServerClient":
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
            if event.get("type") == "error" and event.get("message"):
                error_message = str(event["message"])
            if isinstance(event.get("error"), dict) and event["error"].get("message"):
                error_message = str(event["error"]["message"])
            next_context_left = self._extract_context_left_percent(event)
            if next_context_left is not None:
                context_left_percent = next_context_left
                if context.on_context_left_percent is not None:
                    context.on_context_left_percent(next_context_left)
            delta = self._extract_text_delta(event)
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
        stdout_done_at = time.perf_counter()
        if context.on_progress is not None:
            trailing_chunk, pending_delta = self._take_stream_chunk(pending_delta, force=True)
            if trailing_chunk and trailing_chunk != last_progress:
                last_progress_perf_at = time.perf_counter()
                context.on_progress(trailing_chunk)
        wait_started_at = time.perf_counter()
        completed_returncode = self._wait_for_exit(proc)
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

    def _extract_text_delta(self, value: object) -> str:
        if isinstance(value, dict):
            for key in ("delta", "text_delta", "output_text", "text"):
                raw = value.get(key)
                if isinstance(raw, str) and raw.strip():
                    event_type = str(value.get("type") or value.get("event") or "").lower()
                    if "delta" in event_type or "message" in event_type or "response" in event_type:
                        return raw
            for item in value.values():
                nested = self._extract_text_delta(item)
                if nested:
                    return nested
        if isinstance(value, list):
            for item in value:
                nested = self._extract_text_delta(item)
                if nested:
                    return nested
        return ""

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
