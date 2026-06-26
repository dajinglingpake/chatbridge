from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.bridge_runtime import BridgeMessageRuntime, BridgePromptDecision, BridgeSubmittedTask, IncomingBridgeMessage, PendingMediaContextStore

class BridgeMessageRuntimeTests(unittest.TestCase):
    def _runtime(self, media_store: PendingMediaContextStore, *, submitted: list[tuple[str, str]], replies: list[str], remembered: list[str]) -> BridgeMessageRuntime:
        return BridgeMessageRuntime(
            pending_media=media_store,
            resolve_session=lambda message: message.session_name,
            prepare_prompt=lambda message, _session: BridgePromptDecision(
                handled=message.text == "/status",
                prompt=message.text.strip(),
            ),
            submit_task=lambda message, _session, prompt, _passthrough: submitted.append((message.sender_id, prompt)) or BridgeSubmittedTask("task-001", {"id": "task-001"}),
            remember_pending_task=lambda _message, _session, task: remembered.append(task.task_id),
            send_reply=lambda _target, text: replies.append(text),
        )

    def test_media_only_message_is_cached_for_next_text_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            media_store = PendingMediaContextStore(Path(temp_dir) / "media.json", ttl_seconds=60, now_seconds=lambda: 100)
            submitted: list[tuple[str, str]] = []
            replies: list[str] = []
            remembered: list[str] = []
            runtime = self._runtime(media_store, submitted=submitted, replies=replies, remembered=remembered)

            runtime.handle_message(
                IncomingBridgeMessage(
                    sender_id="sender-a",
                    text="",
                    reply_target={"id": "sender-a"},
                    source="test",
                    session_name="session-a",
                    attachments=[{"kind": "image", "name": "a.png", "path": "/tmp/a.png"}],
                    attachment_errors=[],
                )
            )
            runtime.handle_message(
                IncomingBridgeMessage(
                    sender_id="sender-a",
                    text="看图",
                    reply_target={"id": "sender-a"},
                    source="test",
                    session_name="session-a",
                    attachments=[],
                    attachment_errors=[],
                )
            )

        self.assertEqual([], replies)
        self.assertEqual("sender-a", submitted[0][0])
        self.assertIn("看图", submitted[0][1])
        self.assertIn("用户发送了以下附件", submitted[0][1])
        self.assertIn("图片: a.png", submitted[0][1])
        self.assertEqual(["task-001"], remembered)

    def test_control_command_replies_without_submitting_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            media_store = PendingMediaContextStore(Path(temp_dir) / "media.json", ttl_seconds=60)
            submitted: list[tuple[str, str]] = []
            replies: list[str] = []
            remembered: list[str] = []

            runtime = BridgeMessageRuntime(
                pending_media=media_store,
                resolve_session=lambda message: message.session_name,
                prepare_prompt=lambda message, _session: (
                    replies.append("ok") or BridgePromptDecision(handled=True)
                    if message.text == "/status"
                    else BridgePromptDecision(prompt=message.text.strip())
                ),
                submit_task=lambda message, _session, prompt, _passthrough: submitted.append((message.sender_id, prompt)) or BridgeSubmittedTask("task-001", {"id": "task-001"}),
                remember_pending_task=lambda _message, _session, task: remembered.append(task.task_id),
                send_reply=lambda _target, text: replies.append(text),
            )

            runtime.handle_message(
                IncomingBridgeMessage(
                    sender_id="sender-a",
                    text="/status",
                    reply_target={"id": "sender-a"},
                    source="test",
                    session_name="session-a",
                    attachments=[],
                    attachment_errors=[],
                )
            )

        self.assertEqual(["ok"], replies)
        self.assertEqual([], submitted)

    def test_ignore_and_duplicate_short_circuit_before_submit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            media_store = PendingMediaContextStore(Path(temp_dir) / "media.json", ttl_seconds=60)
            submitted: list[tuple[str, str]] = []
            replies: list[str] = []
            remembered: list[str] = []
            ignored: list[str] = []
            runtime = BridgeMessageRuntime(
                pending_media=media_store,
                resolve_session=lambda message: message.session_name,
                prepare_prompt=lambda message, _session: BridgePromptDecision(prompt=message.text.strip()),
                submit_task=lambda message, _session, prompt, _passthrough: submitted.append((message.sender_id, prompt)) or BridgeSubmittedTask("task-001", {"id": "task-001"}),
                remember_pending_task=lambda _message, _session, task: remembered.append(task.task_id),
                send_reply=lambda _target, text: replies.append(text),
                should_ignore=lambda message: message.text.startswith("#"),
                is_duplicate=lambda message: message.metadata.get("duplicate") is True,
                on_ignored=lambda _message, reason: ignored.append(reason),
            )

            for text, duplicate in (("#ignored", False), ("same", True)):
                runtime.handle_message(
                    IncomingBridgeMessage(
                        sender_id="sender-a",
                        text=text,
                        reply_target={"id": "sender-a"},
                        source="test",
                        session_name="session-a",
                        attachments=[],
                        attachment_errors=[],
                        metadata={"duplicate": duplicate},
                    )
                )

        self.assertEqual(["ignore_prefix", "duplicate"], ignored)
        self.assertEqual([], submitted)
        self.assertEqual([], replies)

if __name__ == "__main__":
    unittest.main()
