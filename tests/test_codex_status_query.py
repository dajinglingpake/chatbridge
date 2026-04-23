from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_backends.codex_status_query import (
    _build_snapshot,
    _format_context_window,
    _format_rate_limit_lines,
    _load_latest_token_usage,
    _render_status_panel,
    query_codex_context_left_percent,
)
from unittest.mock import patch


class CodexStatusQueryTests(unittest.TestCase):
    def test_load_latest_token_usage_reads_last_token_count_event(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            log_path = Path(tempdir) / "session.jsonl"
            log_path.write_text(
                "\n".join(
                    [
                        '{"type":"event_msg","payload":{"type":"other"}}',
                        '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"total_tokens":900000},"last_token_usage":{"total_tokens":120000},"model_context_window":258400}}}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            usage = _load_latest_token_usage(log_path)
        self.assertIsNotNone(usage)
        assert usage is not None
        self.assertEqual(120000, usage.total_tokens)
        self.assertEqual(258400, usage.model_context_window)

    def test_format_context_window_uses_left_percent(self) -> None:
        class _Usage:
            total_tokens = 108000
            model_context_window = 258400

        text = _format_context_window(_Usage())
        self.assertIn("% left", text)
        self.assertIn("108K used / 258K", text)

    def test_format_rate_limit_lines_use_separate_lines(self) -> None:
        class _Window:
            used_percent = 34
            resets_at = 1776943234
            window_minutes = 300

        lines = _format_rate_limit_lines("5h limit", _Window())
        self.assertEqual(["5h limit: 66% left, resets 19:20"], lines)

    def test_render_status_panel_contains_authoritative_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            log_path = Path(tempdir) / "session.jsonl"
            log_path.write_text(
                '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"total_tokens":900000},"last_token_usage":{"total_tokens":108000},"model_context_window":258400}}}\n',
                encoding="utf-8",
            )
            snapshot = _build_snapshot(
                "019db7f5-26b3-78b2-80d7-062b07144f1e",
                {
                    "account": {
                        "type": "chatgpt",
                        "email": "1753473884@qq.com",
                        "planType": "plus",
                    }
                },
                {
                    "rateLimits": {
                        "limitId": "codex",
                        "limitName": None,
                        "primary": {"usedPercent": 34, "windowDurationMins": 300, "resetsAt": 1776943234},
                        "secondary": {"usedPercent": 41, "windowDurationMins": 10080, "resetsAt": 1777425766},
                    },
                    "rateLimitsByLimitId": {
                        "codex_bengalfox": {
                            "limitId": "codex_bengalfox",
                            "limitName": "GPT-5.3-Codex-Spark",
                            "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1776953291},
                            "secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 1777540091},
                        }
                    },
                },
                {
                    "model": "gpt-5.4",
                    "reasoningEffort": "high",
                    "serviceTier": "fast",
                    "approvalPolicy": "on-request",
                    "sandbox": {"type": "workspaceWrite", "networkAccess": False},
                    "cwd": "/home/dajingling/PythonProjects/chatbridge",
                    "instructionSources": ["/home/dajingling/.codex/AGENTS.md"],
                    "thread": {
                        "cliVersion": "0.122.0",
                        "path": str(log_path),
                    },
                },
            )
        panel = _render_status_panel(snapshot)
        self.assertIn("OpenAI Codex v0.122.0", panel)
        self.assertIn("gpt-5.4 (reasoning high, fast)", panel)
        self.assertIn("Workspace Write", panel)
        self.assertIn("1753473884@qq.com (Plus)", panel)
        self.assertIn("Collaboration mode:", panel)
        self.assertIn("Context window:", panel)
        self.assertIn("GPT-5.3-Codex-Spark limit:", panel)
        self.assertIn("Warning:", panel)
        self.assertIn("5h limit: 66% left, resets", panel)
        self.assertNotIn("█", panel)
        self.assertNotIn("░", panel)
        self.assertNotIn("╭", panel)
        self.assertNotIn("│", panel)

    def test_render_status_panel_reports_unavailable_rate_limits(self) -> None:
        snapshot = _build_snapshot(
            "019db7f5-26b3-78b2-80d7-062b07144f1e",
            {
                "account": {
                    "type": "chatgpt",
                    "email": "1753473884@qq.com",
                    "planType": "plus",
                }
            },
            {},
            {
                "model": "gpt-5.4",
                "reasoningEffort": "high",
                "serviceTier": "fast",
                "approvalPolicy": "never",
                "sandbox": {"type": "dangerFullAccess", "networkAccess": True},
                "cwd": "/home/dajingling/PythonProjects/chatbridge",
                "instructionSources": ["/home/dajingling/.codex/AGENTS.md"],
                "thread": {
                    "cliVersion": "0.122.0",
                    "path": "/path/not/found.jsonl",
                },
            },
        )
        panel = _render_status_panel(snapshot)
        self.assertIn("OpenAI Codex v0.122.0", panel)
        self.assertIn("1753473884@qq.com (Plus)", panel)
        self.assertIn("Rate limits: unavailable", panel)

    def test_query_codex_context_left_percent_reads_local_codex_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            codex_home = temp_path / ".codex"
            codex_home.mkdir(parents=True, exist_ok=True)
            session_file = temp_path / "session.txt"
            log_path = temp_path / "session.jsonl"
            state_db_path = codex_home / "state_5.sqlite"
            session_file.write_text("thread-1", encoding="utf-8")
            log_path.write_text(
                '{"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"total_tokens":108000},"model_context_window":258400}}}\n',
                encoding="utf-8",
            )
            with sqlite3.connect(str(state_db_path)) as connection:
                connection.execute("create table threads (id text primary key, rollout_path text not null)")
                connection.execute(
                    "insert into threads (id, rollout_path) values (?, ?)",
                    ("thread-1", str(log_path)),
                )
                connection.commit()
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                percent = query_codex_context_left_percent("codex", session_file, temp_path)
        self.assertEqual(58, percent)


if __name__ == "__main__":
    unittest.main()
