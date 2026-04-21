from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_backends.base import AgentBackend, AgentLike, BackendContext
from agent_backends.shared import build_final_prompt, collect_text_fragments, extract_error_text, extract_session_id, resolve_session_file, run_process


class ClaudeBackend(AgentBackend):
    key = "claude"

    def invoke(self, agent: AgentLike, prompt: str, session_name: str, context: BackendContext) -> dict[str, str]:
        workdir = Path(agent.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        session_file = resolve_session_file(agent, session_name, context.session_dir)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        existing_session = session_file.read_text(encoding="utf-8").strip() if session_file.exists() else ""
        final_prompt = build_final_prompt(agent, prompt)

        argv = [context.claude_command, "-p", final_prompt, "--output-format", "json"]
        if agent.model:
            argv.extend(["--model", agent.model])
        if existing_session:
            argv.extend(["--resume", existing_session])
        mcp_config_path = None
        if context.chatbridge_mcp is not None:
            config_file = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
            json.dump(
                {
                    "mcpServers": {
                        context.chatbridge_mcp.name: {
                            "command": context.chatbridge_mcp.command,
                            "args": context.chatbridge_mcp.args,
                        }
                    }
                },
                config_file,
                ensure_ascii=False,
            )
            config_file.flush()
            config_file.close()
            mcp_config_path = config_file.name
            argv.extend(["--mcp-config", mcp_config_path, "--strict-mcp-config"])

        try:
            completed = run_process(argv, workdir, context)
        finally:
            if mcp_config_path:
                Path(mcp_config_path).unlink(missing_ok=True)

        output, session_id, error_message = self._parse_stdout(completed.stdout)
        if not session_id:
            session_id = existing_session
        if completed.returncode != 0:
            raise RuntimeError(error_message or completed.stderr.strip() or f"Claude exited with code {completed.returncode}")
        if not output:
            raise RuntimeError("Claude returned an empty result")
        if session_id:
            session_file.write_text(session_id, encoding="utf-8")
        return {"output": output, "session_id": session_id}

    def _parse_stdout(self, stdout: str) -> tuple[str, str, str]:
        payload_text = stdout.strip()
        if not payload_text:
            return "", "", ""

        session_id = ""
        error_message = ""
        fragments: list[str] = []
        for line in payload_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = session_id or extract_session_id(payload)
            error_message = error_message or extract_error_text(payload)
            if isinstance(payload.get("result"), str) and payload.get("result", "").strip():
                fragments.append(str(payload["result"]).strip())
            fragments.extend(collect_text_fragments(payload))

        unique_fragments: list[str] = []
        for fragment in fragments:
            text = fragment.strip()
            if text and text not in unique_fragments:
                unique_fragments.append(text)
        return "\n".join(unique_fragments).strip(), session_id, error_message
