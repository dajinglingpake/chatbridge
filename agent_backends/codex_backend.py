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

PROGRESS_PUSH_INTERVAL_SECONDS = 1.0


class CodexBackend(AgentBackend):
    key = "codex"

    def invoke(self, agent: AgentLike, prompt: str, session_name: str, context: BackendContext) -> dict[str, str]:
        workdir = Path(agent.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        session_file = resolve_session_file(agent, session_name, context.session_dir)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        existing_session = session_file.read_text(encoding="utf-8").strip() if session_file.exists() else ""
        final_prompt = build_final_prompt(agent, prompt)
        output_path = Path(tempfile.gettempdir()) / f"multi-codex-output-{uuid.uuid4().hex}.txt"

        options = ["--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "--json", "-o", str(output_path)]
        if agent.model:
            options.extend(["-m", agent.model])
        if context.chatbridge_mcp is not None:
            options.extend(
                [
                    "-c",
                    f'mcp_servers.{context.chatbridge_mcp.name}.command="{context.chatbridge_mcp.command}"',
                    "-c",
                    f"mcp_servers.{context.chatbridge_mcp.name}.args={json.dumps(context.chatbridge_mcp.args, ensure_ascii=False)}",
                ]
            )
        if existing_session:
            argv = [context.codex_command, "exec", "resume", *options, existing_session, final_prompt]
        else:
            argv = [context.codex_command, "exec", *options, "-C", str(workdir), final_prompt]

        proc = subprocess.Popen(
            argv,
            cwd=str(workdir),
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
        completed_returncode = proc.wait()
        stderr_thread.join(timeout=1)
        if not error_message:
            error_message = "".join(stderr_lines).strip()
        if completed_returncode != 0:
            raise RuntimeError(error_message or f"Codex exited with code {completed_returncode}")
        if not output_path.exists():
            raise RuntimeError("Codex did not produce an output file")
        output = output_path.read_text(encoding="utf-8").strip()
        output_path.unlink(missing_ok=True)
        if not output:
            raise RuntimeError("Codex returned an empty result")
        if session_id:
            session_file.write_text(session_id, encoding="utf-8")
        return {"output": output, "session_id": session_id}

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
