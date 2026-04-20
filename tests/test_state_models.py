from __future__ import annotations

import unittest
from types import SimpleNamespace

from bridge_config import normalize_backend
from core.state_models import AgentRuntimeState, CheckSnapshot, HubStateSnapshot, HubTask, IpcRequestEnvelope, IpcResponseEnvelope, RuntimeSnapshot, WeixinBridgeRuntimeState, WeixinConversationBinding


class StateModelTests(unittest.TestCase):
    def test_runtime_snapshot_to_dict_preserves_fields(self) -> None:
        snapshot = RuntimeSnapshot(
            hub_running=True,
            bridge_running=False,
            hub_pid=101,
            bridge_pid=None,
            codex_processes=["PID 1 :: codex"],
            log_dir=".runtime/logs",
        )

        self.assertEqual(
            {
                "hub_running": True,
                "bridge_running": False,
                "hub_pid": 101,
                "bridge_pid": None,
                "codex_processes": ["PID 1 :: codex"],
                "log_dir": ".runtime/logs",
            },
            snapshot.to_dict(),
        )

    def test_check_snapshot_from_result_normalizes_attributes(self) -> None:
        check = CheckSnapshot.from_result(
            SimpleNamespace(
                key="python",
                label="Python",
                ok=True,
                detail="3.11.9",
            )
        )

        self.assertIsNotNone(check)
        assert check is not None
        self.assertEqual("python", check.key)
        self.assertEqual("Python", check.label)
        self.assertTrue(check.ok)
        self.assertEqual("3.11.9", check.detail)

    def test_hub_task_from_dict_applies_defaults(self) -> None:
        task = HubTask.from_dict(
            {
                "id": "task-1",
                "agent_id": "main",
                "created_at": "2026-01-01T00:00:00",
                "prompt": "hello",
            },
            default_backend="codex",
        )

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual("main", task.agent_name)
        self.assertEqual("codex", task.backend)
        self.assertEqual("queued", task.status)
        self.assertEqual("", task.model)

    def test_agent_runtime_from_dict_recovers_invalid_payload(self) -> None:
        runtime = AgentRuntimeState.from_dict("broken", now="2026-01-01T00:00:00")
        self.assertEqual("idle", runtime.status)
        self.assertEqual("2026-01-01T00:00:00", runtime.updated_at)

    def test_weixin_conversation_binding_normalizes_legacy_payload(self) -> None:
        binding = WeixinConversationBinding.from_dict(
            {
                "current_session": "focus",
                "current_agent_id": "legacy-main",
                "sessions": {
                    "focus": {"backend": "CLAUDE"},
                    "": {"backend": "broken"},
                },
            },
            default_backend="codex",
            now="2026-01-01T00:00:00",
            normalize_backend=normalize_backend,
        )

        self.assertEqual("focus", binding.current_session)
        self.assertEqual({"focus"}, set(binding.sessions.keys()))
        self.assertEqual("claude", binding.sessions["focus"].backend)
        self.assertEqual("", binding.sessions["focus"].model)

    def test_weixin_conversation_binding_ensures_default_session(self) -> None:
        binding = WeixinConversationBinding.from_dict(
            {},
            default_backend="codex",
            now="2026-01-01T00:00:00",
            normalize_backend=normalize_backend,
        )

        self.assertEqual("default", binding.current_session)
        self.assertEqual("codex", binding.sessions["default"].backend)

    def test_weixin_bridge_runtime_state_tracks_mutations(self) -> None:
        state = WeixinBridgeRuntimeState.create(
            now="2026-01-01T00:00:00",
            managed_conversations=1,
            account_file="a.json",
            sync_file="a.sync.json",
        )

        state.mark_poll(now="2026-01-01T00:01:00")
        state.mark_message(now="2026-01-01T00:02:00", sender_id="sender-a")
        state.record_handled()
        state.record_failed()
        state.set_error("broken")
        state.sync_files(managed_conversations=2, account_file="b.json", sync_file="b.sync.json")

        self.assertEqual("2026-01-01T00:01:00", state.last_poll_at)
        self.assertEqual("2026-01-01T00:02:00", state.last_message_at)
        self.assertEqual("sender-a", state.last_sender_id)
        self.assertEqual(1, state.handled_messages)
        self.assertEqual(1, state.failed_messages)
        self.assertEqual("broken", state.last_error)
        self.assertEqual(2, state.managed_conversations)
        self.assertEqual("b.json", state.account_file)

    def test_weixin_bridge_runtime_state_from_dict_recovers_defaults(self) -> None:
        state = WeixinBridgeRuntimeState.from_dict(
            {
                "started_at": "2026-01-01T00:00:00",
                "handled_messages": 3,
            }
        )

        self.assertEqual("2026-01-01T00:00:00", state.started_at)
        self.assertEqual(3, state.handled_messages)
        self.assertEqual("", state.last_error)

    def test_hub_state_snapshot_from_dict_normalizes_nested_items(self) -> None:
        state = HubStateSnapshot.from_dict(
            {
                "generated_at": "2026-01-01T00:00:00",
                "agents": [
                    {
                        "id": "main",
                        "name": "Main",
                        "backend": "codex",
                        "runtime": {"status": "running", "queue_size": 2},
                    }
                ],
                "tasks": [
                    {
                        "id": "task-1",
                        "agent_id": "main",
                        "created_at": "2026-01-01T00:01:00",
                        "status": "queued",
                    }
                ],
                "external_agent_processes": [
                    {
                        "pid": 1234,
                        "name": "codex",
                        "backend": "codex",
                    }
                ],
            },
            default_backend="codex",
            now="2026-01-01T00:00:00",
        )

        self.assertEqual(1, len(state.agents))
        self.assertEqual("running", state.agents[0].runtime.status)
        self.assertEqual(1, len(state.tasks))
        self.assertEqual("task-1", state.tasks[0].id)
        self.assertEqual(1, len(state.external_agent_processes))
        self.assertEqual(1234, state.external_agent_processes[0].pid)

    def test_ipc_request_envelope_round_trip(self) -> None:
        request = IpcRequestEnvelope.from_dict(
            {
                "id": "req-1",
                "action": "state",
                "payload": {"scope": "all"},
            }
        )

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("state", request.action)
        self.assertEqual({"id": "req-1", "action": "state", "payload": {"scope": "all"}}, request.to_dict())

    def test_ipc_response_envelope_round_trip(self) -> None:
        response = IpcResponseEnvelope.from_dict(
            {
                "ok": True,
                "task": {"id": "task-1"},
                "generated_at": "2026-01-01T00:00:00",
            }
        )

        self.assertIsNotNone(response)
        assert response is not None
        self.assertTrue(response.ok)
        self.assertEqual({"id": "task-1"}, response.payload["task"])
        self.assertEqual(
            {
                "ok": True,
                "task": {"id": "task-1"},
                "generated_at": "2026-01-01T00:00:00",
            },
            response.to_dict(),
        )


if __name__ == "__main__":
    unittest.main()
