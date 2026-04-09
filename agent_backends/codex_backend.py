from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from pathlib import Path

from agent_backends.base import AgentBackend, AgentLike, BackendContext
from agent_backends.shared import build_final_prompt, resolve_session_file


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
        if existing_session:
            argv = [context.codex_command, "exec", "resume", *options, existing_session, final_prompt]
        else:
            argv = [context.codex_command, "exec", *options, "-C", str(workdir), final_prompt]

        completed = subprocess.run(
            argv,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=context.creationflags,
            check=False,
            shell=False,
        )
        session_id = existing_session
        error_message = completed.stderr.strip()
        for line in completed.stdout.splitlines():
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
        if completed.returncode != 0:
            raise RuntimeError(error_message or f"Codex exited with code {completed.returncode}")
        if not output_path.exists():
            raise RuntimeError("Codex did not produce an output file")
        output = output_path.read_text(encoding="utf-8").strip()
        output_path.unlink(missing_ok=True)
        if not output:
            raise RuntimeError("Codex returned an empty result")
        if session_id:
            session_file.write_text(session_id, encoding="utf-8")
        return {"output": output, "session_id": session_id}
