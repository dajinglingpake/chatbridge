from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.bridge_pending_tasks import BridgePendingReplyTask, JsonBackedTaskStore


class BridgePendingTaskStoreTests(unittest.TestCase):
    def test_json_backed_task_store_round_trips_pending_reply_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pending.json"
            store = JsonBackedTaskStore(
                path,
                from_dict=lambda raw: BridgePendingReplyTask.from_dict(raw, ttl_seconds=60, now_seconds=100),
                to_dict=lambda task: task.to_dict(),
            )
            task = BridgePendingReplyTask(
                task_id="task-001",
                sender_key="qq:private:10001",
                reply_target={"message_type": "private", "user_id": 10001},
                created_at=90,
                last_progress_seq=2,
                last_progress_text="已发送进度",
            )

            store.save({task.task_id: task})

            loaded = store.load()
            self.assertEqual({"task-001"}, set(loaded))
            self.assertEqual(task, loaded["task-001"])

    def test_bridge_pending_reply_task_filters_expired_items(self) -> None:
        raw = {
            "task_id": "task-old",
            "sender_key": "qq:private:10001",
            "reply_target": {"message_type": "private", "user_id": 10001},
            "created_at": 1,
        }

        self.assertIsNone(BridgePendingReplyTask.from_dict(raw, ttl_seconds=60, now_seconds=100))


if __name__ == "__main__":
    unittest.main()
