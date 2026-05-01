from __future__ import annotations

import json
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
CODEX_TRANSIENT_RETRY_ATTEMPTS = 1
TRANSIENT_ERROR_MARKERS = (
    "stream disconnected before completion",
    "error sending request",
    "timeout waiting for child process to exit",
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
)


class CodexBackend(AgentBackend):
    key = "codex"

    def invoke(self, agent: AgentLike, prompt: str, session_name: str, context: BackendContext) -> dict[str, str]:
        last_error: RuntimeError | None = None
        for attempt in range(CODEX_TRANSIENT_RETRY_ATTEMPTS + 1):
            try:
                return self._invoke_once(agent, prompt, session_name, context)
            except RuntimeError as exc:
                last_error = exc
                if attempt >= CODEX_TRANSIENT_RETRY_ATTEMPTS or not self._is_transient_error(str(exc)):
                    raise
                if context.on_progress is not None:
                    context.on_progress("Codex 连接中断，正在自动重试一次...")
                time.sleep(1)
        raise last_error or RuntimeError("Codex failed")

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
        if agent.model:
            options.extend(["-m", agent.model])
        if context.reasoning_effort:
            options.extend(["-c", f'model_reasoning_effort="{context.reasoning_effort}"'])
        if context.permission_mode == "default":
            options.extend(["-a", "never", "-s", "workspace-write"])
        else:
            options.append("--dangerously-bypass-approvals-and-sandbox")
        if context.mcp_server is not None:
            options.extend(
                [
                    "-c",
                    f'mcp_servers.{context.mcp_server.name}.command="{context.mcp_server.command}"',
                    "-c",
                    f"mcp_servers.{context.mcp_server.name}.args={json.dumps(context.mcp_server.args, ensure_ascii=False)}",
                ]
            )
        if existing_session:
            argv = [context.codex_command, "exec", "resume", *options, existing_session, final_prompt]
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
        pending_delta = ""
        context_left_percent: int | None = None
        assert proc.stderr is not None

        def read_stderr() -> None:
            for raw_line in proc.stderr:
                stderr_lines.append(raw_line)

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
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
                context.on_progress(progress)
        if context.on_progress is not None:
            trailing_chunk, pending_delta = self._take_stream_chunk(pending_delta, force=True)
            if trailing_chunk and trailing_chunk != last_progress:
                context.on_progress(trailing_chunk)
        completed_returncode = self._wait_for_exit(proc)
        stderr_thread.join(timeout=1)
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
