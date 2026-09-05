from __future__ import annotations

import unittest
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import ANY, patch

import ui.mobile as mobile
from ui.mobile import (
    CODEX_THREAD_PAGE_LIMIT,
    _build_mobile_diagnostics,
    _build_mobile_state,
    build_stream_sidebar_state_snapshot,
    build_stream_signature_snapshot,
    build_stream_state_snapshot,
    _event_stream,
    _apply_mobile_no_store_headers,
    _load_all_codex_threads,
    _codex_thread_task_payloads,
    _codex_thread_turn_count,
    _decode_signed_local_image_path,
    _is_mobile_no_store_path,
    _mobile_codex_thread_payload,
    _merge_codex_thread_local_timeline,
    _image_preview_payload,
    _select_mobile_tasks,
    _task_activity_items,
    codex_thread_id_from_session_name,
    codex_thread_session_name,
    load_codex_threads_page,
    stream_hub_state_file_signature,
    stream_qq_current_session_name,
)


def _task(task_id: str, session_name: str):
    return SimpleNamespace(id=task_id, session_name=session_name)

def _signature_task(task_id: str, session_name: str):
    return SimpleNamespace(
        id=task_id,
        session_name=session_name,
        created_at="2026-07-05T00:00:00",
        status="running",
        progress_seq=1,
        progress_at="2026-07-05T00:00:01",
        finished_at="",
        progress_text="working",
        output="",
        error="",
        context_left_percent=99,
    )

def _activity_task(**overrides: object):
    values = {
        "id": "task-real-001",
        "agent_id": "qq",
        "agent_name": "QQ",
        "backend": "codex",
        "source": "qq",
        "sender_id": "qq-private-1",
        "session_name": "focus",
        "session_id": "session-001",
        "workdir": "I:/AI/chatbridge",
        "model": "",
        "prompt": "用户输入",
        "images": [],
        "context_left_percent": 42,
        "created_at": "2026-07-04T05:00:00",
        "started_at": "2026-07-04T05:00:02",
        "progress_at": "2026-07-04T05:00:10",
        "finished_at": "2026-07-04T05:00:20",
        "status": "succeeded",
        "progress_seq": 2,
        "progress_text": "正在处理真实进度",
        "output": "任务完成输出",
        "error": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _raw_activity_task(**overrides: object) -> dict[str, object]:
    return vars(_activity_task(**overrides))


class MobileStateTests(unittest.TestCase):
    def test_mobile_routes_disable_browser_cache(self) -> None:
        response = SimpleNamespace(headers={})

        self.assertTrue(_is_mobile_no_store_path("/mobile-ui"))
        self.assertTrue(_is_mobile_no_store_path("/api/mobile/state"))
        self.assertFalse(_is_mobile_no_store_path("/"))
        self.assertIs(response, _apply_mobile_no_store_headers(response))
        self.assertEqual("no-store", response.headers["Cache-Control"])
        self.assertEqual("no-cache", response.headers["Pragma"])
        self.assertEqual("0", response.headers["Expires"])

    def test_mobile_access_url_carries_selected_browser_session(self) -> None:
        session_name = "codex:01a04cec-a6e5-76f1-ba7a-fefb741f223a"
        with patch("ui.mobile._detect_lan_ip", return_value="192.168.1.2"):
            url = mobile.build_mobile_access_url(
                host="0.0.0.0",
                port=8765,
                session_name=session_name,
            )

        self.assertEqual(
            "http://192.168.1.2:8765/mobile-ui?session=codex%3A01a04cec-a6e5-76f1-ba7a-fefb741f223a",
            url,
        )

    def test_selected_session_tasks_are_kept_beyond_global_limit(self) -> None:
        tasks = [
            _task("recent-1", "default"),
            _task("recent-2", "other"),
            _task("focus-1", "focus"),
            _task("focus-2", "focus"),
            _task("focus-3", "focus"),
            _task("focus-4", "focus"),
        ]

        selected = _select_mobile_tasks(
            tasks,
            selected_session_name="focus",
            task_limit=2,
            session_task_limit=3,
        )

        self.assertEqual(
            ["recent-1", "recent-2", "focus-1", "focus-2", "focus-3"],
            [task.id for task in selected],
        )

    def test_selected_session_tasks_are_not_duplicated(self) -> None:
        tasks = [
            _task("focus-1", "focus"),
            _task("recent-1", "other"),
            _task("focus-2", "focus"),
        ]

        selected = _select_mobile_tasks(
            tasks,
            selected_session_name="focus",
            task_limit=2,
            session_task_limit=2,
        )

        self.assertEqual(["focus-1", "recent-1", "focus-2"], [task.id for task in selected])

    def test_task_activity_items_are_derived_from_real_task_state(self) -> None:
        items = _task_activity_items(_activity_task())

        self.assertEqual(["accepted", "running", "progress", "succeeded"], [item["event"] for item in items])
        self.assertEqual(["system", "info", "info", "success"], [item["type"] for item in items])
        self.assertEqual("正在处理真实进度", items[2]["detail"])
        self.assertEqual("", items[3]["detail"])
        self.assertEqual("task-real-001", items[0]["metadata"]["task_id"])
        self.assertEqual("focus", items[0]["metadata"]["session"])
        self.assertEqual("42", items[0]["metadata"]["context_left_percent"])

    def test_mobile_raw_hub_times_display_utc_as_local(self) -> None:
        payload = mobile._raw_task_payload(
            _raw_activity_task(
                source="desktop",
                created_at="2026/7/5 10:54:52",
                started_at="2026-07-05T10:54:53Z",
                progress_at="2026-07-05T10:54:54",
                finished_at="2026-07-05T10:54:55Z",
            )
        )

        self.assertEqual("2026-07-05T18:54:52", payload["created_at"])
        self.assertEqual("2026-07-05T18:54:53", payload["started_at"])
        self.assertEqual("2026-07-05T18:54:54", payload["progress_at"])
        self.assertEqual("2026-07-05T18:54:55", payload["finished_at"])
        self.assertEqual("2026-07-05T18:54:52", payload["activity_items"][0]["at"])

    def test_mobile_codex_app_server_times_are_not_shifted(self) -> None:
        payload = mobile._raw_task_payload(
            _raw_activity_task(
                source="codex-app-server",
                created_at="2026-07-04T00:45:00",
                started_at="",
                progress_at="",
                finished_at="2026-07-04T00:46:00",
            )
        )

        self.assertEqual("2026-07-04T00:45:00", payload["created_at"])
        self.assertEqual("2026-07-04T00:46:00", payload["finished_at"])
        self.assertEqual("2026-07-04T00:45:00", payload["activity_items"][0]["at"])

    def test_local_task_image_gets_mobile_preview_url(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            upload_root = temp_path / "uploads"
            image_path = temp_path / "outside" / "photo.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"png-data")

            with patch("ui.mobile.MOBILE_UPLOAD_ROOT", upload_root):
                preview = _image_preview_payload(str(image_path))

            self.assertEqual("photo.png", preview["label"])
            self.assertTrue(str(preview["source"]).startswith("/mobile-local-image/"))
            self.assertFalse((upload_root / "previews").exists())
            _empty, route, signature, encoded_path = str(preview["source"]).split("/", 3)
            self.assertEqual("mobile-local-image", route)
            self.assertEqual(image_path.resolve(), _decode_signed_local_image_path(signature, encoded_path))

    def test_output_markdown_images_get_mobile_previews_without_copying(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            upload_root = temp_path / "uploads"
            image_path = temp_path / "contact.png"
            image_path.write_bytes(b"png-data")
            thread = {
                "id": "thread-image",
                "messages": [
                    {"turn_id": "turn-1", "role": "user", "text": "show image"},
                    {"turn_id": "turn-1", "role": "assistant", "text": f"![contact]({image_path.as_posix()})"},
                ],
            }

            with patch("ui.mobile.MOBILE_UPLOAD_ROOT", upload_root):
                tasks = _codex_thread_task_payloads(thread)

            previews = tasks[0]["image_previews"]
            self.assertEqual("contact.png", previews[0]["label"])
            self.assertTrue(str(previews[0]["source"]).startswith("/mobile-local-image/"))
            self.assertEqual(previews, tasks[0]["output_image_previews"])
            self.assertFalse(upload_root.exists())

    def test_localhost_mobile_upload_output_preview_uses_relative_url(self) -> None:
        previews = mobile._output_image_previews("![图](http://127.0.0.1:8765/mobile-upload/qq/a.png)")

        self.assertEqual("/mobile-upload/qq/a.png", previews[0]["source"])
        self.assertEqual("markdown_image", previews[0]["kind"])

    def test_output_file_image_links_get_mobile_previews(self) -> None:
        previews = mobile._output_image_previews("[contact](http://127.0.0.1:8765/mobile-upload/qq/a.png)")

        self.assertEqual("/mobile-upload/qq/a.png", previews[0]["source"])
        self.assertEqual("image_link", previews[0]["kind"])

    def test_codex_user_file_mentions_get_mobile_image_previews(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            upload_root = temp_path / "uploads"
            image_path = temp_path / "qq-image.png"
            image_path.write_bytes(b"png-data")
            thread = {
                "id": "thread-user-image",
                "messages": [
                    {
                        "turn_id": "turn-1",
                        "role": "user",
                        "text": f"# Files mentioned by the user:\n\n## qq-image.png: {image_path.as_posix()}\n\n## My request for Codex:\n看这张图",
                    },
                    {"turn_id": "turn-1", "role": "assistant", "text": "看到了"},
                ],
            }

            with patch("ui.mobile.MOBILE_UPLOAD_ROOT", upload_root):
                tasks = _codex_thread_task_payloads(thread)

            self.assertEqual([image_path.as_posix()], tasks[0]["images"])
            self.assertEqual("qq-image.png", tasks[0]["image_previews"][0]["label"])
            self.assertTrue(str(tasks[0]["image_previews"][0]["source"]).startswith("/mobile-local-image/"))
            self.assertFalse(upload_root.exists())

    def test_codex_raw_view_image_calls_get_mobile_output_previews(self) -> None:
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            upload_root = temp_path / "uploads"
            sessions_root = temp_path / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            image_path = temp_path / "legacy-img2img-optimized-contact-002.png"
            image_path.write_bytes(b"png-data")
            jsonl_path = sessions_root / "rollout-2026-07-09T01-43-06-thread-view-image.jsonl"
            jsonl_path.write_text(
                json.dumps(
                    {
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "验图完成"}],
                            "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                        }
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "payload": {
                            "type": "function_call",
                            "name": "view_image",
                            "arguments": json.dumps({"path": image_path.as_posix()}),
                            "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            thread = {
                "id": "thread-view-image",
                "messages": [
                    {"turn_id": "turn-1", "role": "user", "text": "验图"},
                    {"turn_id": "turn-1", "role": "assistant", "text": "验图完成"},
                ],
            }

            with (
                patch("ui.mobile.MOBILE_UPLOAD_ROOT", upload_root),
                patch("ui.mobile._codex_sessions_root", return_value=sessions_root),
            ):
                mobile._codex_raw_view_image_payload(thread["id"])
                tasks = _codex_thread_task_payloads(thread)

            previews = tasks[0]["output_image_previews"]
            self.assertEqual("legacy-img2img-optimized-contact-002.png", previews[0]["label"])
            self.assertEqual("markdown_image", previews[0]["kind"])
            self.assertTrue(str(previews[0]["source"]).startswith("/mobile-local-image/"))
            self.assertEqual(previews, tasks[0]["image_previews"])
            self.assertTrue(str(tasks[0]["output"]).startswith("验图完成\n\n![legacy-img2img-optimized-contact-002.png]("))
            self.assertFalse(upload_root.exists())
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()

    def test_codex_view_image_results_keep_each_snapshot_without_copying_files(self) -> None:
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            upload_root = temp_path / "uploads"
            sessions_root = temp_path / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            image_path = temp_path / "generated-contact-sheet.png"
            image_path.write_bytes(b"latest-file-content")
            first_snapshot = "data:image/png;base64,c25hcHNob3QtMQ=="
            second_snapshot = "data:image/png;base64,c25hcHNob3QtMg=="
            jsonl_path = sessions_root / "rollout-2026-07-09T01-43-06-thread-view-image-snapshots.jsonl"
            events = [
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "验图完成"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    }
                },
                {
                    "payload": {
                        "type": "function_call",
                        "name": "view_image",
                        "call_id": "call-1",
                        "arguments": json.dumps({"path": image_path.as_posix()}),
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    }
                },
                {
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-1",
                        "output": [{"type": "input_image", "image_url": first_snapshot}],
                    }
                },
                {
                    "payload": {
                        "type": "function_call",
                        "name": "view_image",
                        "call_id": "call-2",
                        "arguments": json.dumps({"path": image_path.as_posix()}),
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    }
                },
                {
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-2",
                        "output": [{"type": "input_image", "image_url": second_snapshot}],
                    }
                },
            ]
            jsonl_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            thread = {
                "id": "thread-view-image-snapshots",
                "messages": [
                    {"turn_id": "turn-1", "role": "user", "text": "验图"},
                    {"turn_id": "turn-1", "role": "assistant", "text": "验图完成"},
                ],
            }

            with (
                patch("ui.mobile.MOBILE_UPLOAD_ROOT", upload_root),
                patch("ui.mobile._codex_sessions_root", return_value=sessions_root),
            ):
                mobile._codex_raw_view_image_payload(thread["id"])
                tasks = _codex_thread_task_payloads(thread)
                previews = tasks[0]["output_image_previews"]
                first_source = str(previews[0]["source"])
                second_source = str(previews[1]["source"])
                _empty, route, first_signature, first_thread, first_reference = first_source.split("/", 4)
                _empty, route_again, second_signature, second_thread, second_reference = second_source.split("/", 4)
                first_decoded = mobile._decode_signed_codex_image_reference(first_signature, first_thread, first_reference)
                second_decoded = mobile._decode_signed_codex_image_reference(second_signature, second_thread, second_reference)

                self.assertEqual("mobile-codex-image", route)
                self.assertEqual(route, route_again)
                self.assertEqual(2, len(previews))
                self.assertNotEqual(first_source, second_source)
                self.assertEqual("thread-view-image-snapshots", first_decoded[0])
                self.assertEqual("thread-view-image-snapshots", second_decoded[0])
                self.assertEqual(("image/png", b"snapshot-1"), mobile._codex_view_image_bytes(*first_decoded))
                self.assertEqual(("image/png", b"snapshot-2"), mobile._codex_view_image_bytes(*second_decoded))
                self.assertEqual(2, tasks[0]["output"].count("![generated-contact-sheet.png]"))

            self.assertFalse(upload_root.exists())
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()

    def test_codex_custom_tool_view_image_result_gets_mobile_preview(self) -> None:
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            sessions_root = temp_path / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            image_path = temp_path / "mobile-check.png"
            image_path.write_bytes(b"current-file-content")
            snapshot = "data:image/png;base64,Y3VzdG9tLXRvb2wtc25hcHNob3Q="
            jsonl_path = sessions_root / "rollout-thread-custom-image.jsonl"
            events = [
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "检查截图"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    }
                },
                {
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "call-custom-image",
                        "input": f'const result = await tools.view_image({{"path":"{image_path.as_posix()}"}}); image(result.image_url);',
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    }
                },
                {
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-custom-image",
                        "output": [{"type": "input_image", "image_url": snapshot}],
                    }
                },
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "检查完成"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    }
                },
            ]
            jsonl_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            thread = {
                "id": "thread-custom-image",
                "messages": [
                    {"turn_id": "turn-1", "role": "user", "text": "验图"},
                    {"turn_id": "turn-1", "role": "assistant", "text": "检查截图"},
                ],
            }

            with patch("ui.mobile._codex_sessions_root", return_value=sessions_root):
                mobile._codex_raw_view_image_payload(thread["id"])
                tasks = _codex_thread_task_payloads(thread)
                preview = tasks[0]["output_image_previews"][0]
                source = str(preview["source"])
                _empty, route, signature, encoded_thread, encoded_reference = source.split("/", 4)
                decoded = mobile._decode_signed_codex_image_reference(signature, encoded_thread, encoded_reference)

                self.assertEqual("mobile-codex-image", route)
                self.assertEqual("mobile-check.png", preview["label"])
                self.assertEqual(("image/png", b"custom-tool-snapshot"), mobile._codex_view_image_bytes(*decoded))
                self.assertEqual(
                    [
                        {"kind": "text", "text": "检查截图"},
                        {"kind": "custom_tool_image", "source": source, "label": "mobile-check.png"},
                        {"kind": "text", "text": "检查完成"},
                    ],
                    tasks[0]["output_segments"],
                )

        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()

    def test_codex_custom_tool_image_hides_preview_matching_user_attachment(self) -> None:
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            sessions_root = temp_path / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            image_path = temp_path / "same-user-attachment.png"
            image_path.write_bytes(b"current-file-content")
            snapshot = "data:image/png;base64,dG9vbC1zbmFwc2hvdA=="
            jsonl_path = sessions_root / "rollout-thread-user-attachment-image.jsonl"
            events = [
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "检查附件"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    }
                },
                {
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "call-user-attachment-image",
                        "input": f'const result = await tools.view_image({{"path":"{image_path.as_posix()}"}}); image(result.image_url);',
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    }
                },
                {
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-user-attachment-image",
                        "output": [{"type": "input_image", "image_url": snapshot}],
                    }
                },
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "检查完成"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    }
                },
            ]
            jsonl_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            thread = {
                "id": "thread-user-attachment-image",
                "messages": [
                    {
                        "turn_id": "turn-1",
                        "role": "user",
                        "text": f"# Files mentioned by the user:\n\n## same-user-attachment.png: {image_path.as_posix()}\n\n检查图片",
                    },
                    {"turn_id": "turn-1", "role": "assistant", "text": "检查完成"},
                ],
            }

            with patch("ui.mobile._codex_sessions_root", return_value=sessions_root):
                mobile._codex_raw_view_image_payload(thread["id"])
                tasks = _codex_thread_task_payloads(thread)

            self.assertEqual([image_path.as_posix()], tasks[0]["images"])
            self.assertEqual(1, len(tasks[0]["image_previews"]))
            self.assertEqual([], tasks[0]["output_image_previews"])
            self.assertEqual(
                [
                    {"kind": "text", "text": "检查附件"},
                    {"kind": "text", "text": "检查完成"},
                ],
                tasks[0]["output_segments"],
            )

        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()

    def test_codex_custom_tool_image_output_deduplicates_repeated_url_fields(self) -> None:
        snapshot = "data:image/png;base64,ZHVwbGljYXRlLXNuYXBzaG90"

        self.assertEqual(
            [snapshot],
            mobile._codex_output_image_urls(
                {
                    "image_url": snapshot,
                    "content": [{"type": "input_image", "image_url": snapshot}],
                }
            ),
        )

    def test_codex_final_markdown_image_hides_matching_custom_tool_preview(self) -> None:
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            sessions_root = temp_path / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            image_path = temp_path / "same-preview.png"
            image_path.write_bytes(b"png-data")
            jsonl_path = sessions_root / "rollout-thread-matching-final-image.jsonl"
            events = [
                {
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "call-matching-image",
                        "input": f'const result = await tools.view_image({{"path":"{image_path.as_posix()}"}}); image(result.image_url);',
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    }
                },
                {
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-matching-image",
                        "output": [{"type": "output_text", "text": "done"}],
                    }
                },
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": f"![同图]({image_path.as_posix()})"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    }
                },
            ]
            jsonl_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            thread = {
                "id": "thread-matching-final-image",
                "messages": [
                    {"turn_id": "turn-1", "role": "user", "text": "验图"},
                    {"turn_id": "turn-1", "role": "assistant", "text": f"![同图]({image_path.as_posix()})"},
                ],
            }

            with patch("ui.mobile._codex_sessions_root", return_value=sessions_root):
                mobile._codex_raw_view_image_payload(thread["id"])
                tasks = _codex_thread_task_payloads(thread)

            self.assertEqual(1, len(tasks[0]["output_image_previews"]))
            self.assertEqual("markdown_image", tasks[0]["output_image_previews"][0]["kind"])
            self.assertFalse(
                [
                    segment
                    for segment in tasks[0]["output_segments"]
                    if segment.get("kind") == "custom_tool_image"
                ]
            )

        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()

    def test_codex_custom_exec_parser_ignores_mcp_javascript_and_extracts_real_commands(self) -> None:
        payload = {
            "name": "exec",
            "input": """
                const mcp = await tools.mcp__node_repl__js({
                    code: "await tools.exec_command({cmd: 'not-a-shell-event'})"
                });
                // tools.exec_command({cmd: "also-not-a-shell-event"});
                const results = await Promise.all([
                    tools.exec_command({cmd: "git status --short", workdir: "I:/repo"}),
                    tools.exec_command({cmd: 'pytest -q', workdir: 'I:/repo'})
                ]);
            """,
        }

        commands = mobile._codex_custom_tool_exec_commands(payload)

        self.assertEqual(
            [
                {"command": "git status --short", "workdir": "I:/repo"},
                {"command": "pytest -q", "workdir": "I:/repo"},
            ],
            commands,
        )
        self.assertEqual(
            ["mcp__node_repl__js", "exec_command", "exec_command"],
            mobile._codex_custom_tool_names(payload),
        )

    def test_codex_rollout_keeps_mcp_and_shell_commands_as_separate_cached_activities(self) -> None:
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            sessions_root = Path(temp_dir) / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            jsonl_path = sessions_root / "rollout-thread-tools-and-commands.jsonl"
            events = [
                {
                    "timestamp": "2026-07-18T10:00:03",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "call-shell-group",
                        "input": """
                            const results = await Promise.all([
                                tools.exec_command({cmd: "git status --short", workdir: "I:/repo"}),
                                tools.exec_command({cmd: "pytest -q", workdir: "I:/repo"})
                            ]);
                        """,
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    },
                },
                {
                    "timestamp": "2026-07-18T10:00:03.250",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-shell-group",
                        "output": [{"type": "input_text", "text": "Script completed\nOutput:\n2 passed"}],
                    },
                },
                {
                    "timestamp": "2026-07-18T10:00:04",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "call-mcp-wrapper",
                        "input": """
                            await tools.mcp__node_repl__js({
                                code: "await tools.exec_command({cmd: 'must-not-appear'})"
                            });
                        """,
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    },
                },
            ]
            jsonl_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            thread = {
                "id": "thread-tools-and-commands",
                "messages": [
                    {"turn_id": "turn-1", "role": "user", "text": "检查工具", "at": "2026-07-18T10:00:00"},
                    {"turn_id": "turn-1", "role": "reasoning", "text": "先运行工具", "at": "2026-07-18T10:00:01"},
                    {
                        "id": "mcp-1",
                        "turn_id": "turn-1",
                        "role": "activity",
                        "activity": {
                            "event": "codex_tool_call",
                            "type": "success",
                            "at": "2026-07-18T10:00:02",
                            "detail": "ronin.project_run - 启动项目",
                            "metadata": {
                                "item_type": "mcpToolCall",
                                "server": "ronin",
                                "tool": "project_run",
                                "command": "ronin.project_run - 启动项目",
                                "status": "completed",
                            },
                        },
                    },
                    {"turn_id": "turn-1", "role": "assistant", "text": "检查完成", "at": "2026-07-18T10:00:05"},
                ],
            }

            with (
                patch("ui.mobile._codex_sessions_root", return_value=sessions_root),
                patch("ui.mobile.json.loads", wraps=json.loads) as loads,
            ):
                first_payload = mobile._codex_raw_view_image_payload(thread["id"])
                first_parse_count = loads.call_count
                second_payload = mobile._codex_raw_view_image_payload(thread["id"])
                tasks = _codex_thread_task_payloads(thread)

            self.assertEqual(first_parse_count, loads.call_count)
            self.assertEqual(first_payload["commands"], second_payload["commands"])
            activities = tasks[0]["activity_items"]
            self.assertEqual(
                ["codex_reasoning", "codex_tool_call", "codex_command", "codex_command"],
                [activity["event"] for activity in activities],
            )
            self.assertEqual("ronin", activities[1]["metadata"]["server"])
            self.assertNotIn("server", activities[2]["metadata"])
            self.assertEqual(
                ["git status --short", "pytest -q"],
                [activity["metadata"]["command"] for activity in activities[2:]],
            )
            self.assertNotIn("must-not-appear", json.dumps(activities, ensure_ascii=False))
            self.assertEqual("250", activities[3]["metadata"]["duration_ms"])

        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()

    def test_codex_rollout_cache_only_parses_newly_appended_lines(self) -> None:
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            sessions_root = Path(temp_dir) / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            jsonl_path = sessions_root / "rollout-thread-incremental.jsonl"
            first_event = {
                "type": "response_item",
                "timestamp": "2026-07-18T10:00:00",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"text": "first"}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                },
            }
            second_event = {
                "type": "response_item",
                "timestamp": "2026-07-18T10:00:01",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"text": "second"}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                },
            }
            jsonl_path.write_text(json.dumps(first_event) + "\n", encoding="utf-8")

            with (
                patch("ui.mobile._codex_sessions_root", return_value=sessions_root),
                patch("ui.mobile.json.loads", wraps=json.loads) as loads,
            ):
                first_payload = mobile._codex_raw_view_image_payload("thread-incremental")
                first_parse_count = loads.call_count
                with jsonl_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(second_event) + "\n")
                second_payload = mobile._codex_raw_view_image_payload("thread-incremental")

            self.assertEqual(first_parse_count + 1, loads.call_count)
            self.assertEqual(
                ["first", "second"],
                [item["text"] for item in second_payload["reasoning_times"]["turn-1"]],
            )
            self.assertEqual(1, len(first_payload["reasoning_times"]["turn-1"]))

        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()

    def test_codex_thread_detail_uses_rollout_history_and_tracks_appends(self) -> None:
        thread_id = "thread-rollout-history"
        mobile._CODEX_THREAD_DETAIL_CACHE.clear()
        mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
        mobile._CODEX_ROLLOUT_REFRESH_INFLIGHT.clear()
        with TemporaryDirectory() as temp_dir:
            sessions_root = Path(temp_dir) / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            jsonl_path = sessions_root / f"rollout-{thread_id}.jsonl"
            initial_events = [
                {
                    "type": "session_meta",
                    "timestamp": "2026-08-30T10:00:00Z",
                    "payload": {
                        "id": thread_id,
                        "session_id": thread_id,
                        "cwd": "I:/AI/chatbridge",
                        "source": "vscode",
                        "model_provider": "openai",
                        "git": {"branch": "main", "commit_hash": "abc123"},
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-30T10:00:01Z",
                    "payload": {"type": "task_started", "turn_id": "turn-1"},
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-08-30T10:00:02Z",
                    "payload": {
                        "type": "message",
                        "id": "internal-context",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "<environment_context>hidden</environment_context>"}],
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": "turn-1",
                            "content_item_kinds": ["environments.environment_context"],
                        },
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-08-30T10:00:03Z",
                    "payload": {
                        "type": "message",
                        "id": "user-1",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "保留这段历史"}],
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": "turn-1",
                            "content_item_kinds": ["user.text"],
                        },
                    },
                },
            ]
            jsonl_path.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in initial_events) + "\n",
                encoding="utf-8",
            )

            try:
                with (
                    patch("ui.mobile._codex_sessions_root", return_value=sessions_root),
                    patch(
                        "ui.mobile.read_codex_thread",
                        return_value={
                            "id": thread_id,
                            "messages": [{"role": "user", "text": "保留这段历史"}],
                            "latest_turn_status": "completed",
                            "path": str(jsonl_path),
                        },
                    ) as read_thread,
                ):
                    initial_thread = mobile._load_codex_thread_now(thread_id)
                    appended_events = [
                        {
                            "type": "response_item",
                            "timestamp": "2026-08-30T10:00:04Z",
                            "payload": {
                                "type": "reasoning",
                                "id": "reasoning-1",
                                "summary": [{"type": "summary_text", "text": "正在处理"}],
                                "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                            },
                        },
                        {
                            "type": "response_item",
                            "timestamp": "2026-08-30T10:00:05Z",
                            "payload": {
                                "type": "message",
                                "id": "assistant-1",
                                "role": "assistant",
                                "phase": "final_answer",
                                "content": [{"type": "output_text", "text": "历史不会再清空"}],
                                "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                            },
                        },
                        {
                            "type": "event_msg",
                            "timestamp": "2026-08-30T10:00:06Z",
                            "payload": {"type": "task_complete", "turn_id": "turn-1"},
                        },
                    ]
                    with jsonl_path.open("a", encoding="utf-8") as handle:
                        handle.write("\n".join(json.dumps(event, ensure_ascii=False) for event in appended_events) + "\n")
                    mobile._refresh_codex_rollout_cache_now(thread_id)
                refreshed_thread = mobile._CODEX_THREAD_DETAIL_CACHE[thread_id]["thread"]
            finally:
                mobile._CODEX_THREAD_DETAIL_CACHE.clear()
                mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()
                mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
                mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
                mobile._CODEX_ROLLOUT_REFRESH_INFLIGHT.clear()

        read_thread.assert_called_once_with(thread_id, timeout_seconds=8)
        self.assertEqual(["保留这段历史"], [message["text"] for message in initial_thread["messages"]])
        self.assertEqual(
            ["user", "reasoning", "assistant"],
            [message["role"] for message in refreshed_thread["messages"]],
        )
        self.assertEqual("历史不会再清空", refreshed_thread["messages"][-1]["text"])
        self.assertEqual("completed", refreshed_thread["latest_turn_status"])
        self.assertEqual("2026-08-30T10:00:06Z", refreshed_thread["latest_turn_completed_at"])
        self.assertEqual(str(jsonl_path), refreshed_thread["path"])

    def test_codex_rollout_tracks_active_goal_across_completion_and_recreation(self) -> None:
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            sessions_root = Path(temp_dir) / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            jsonl_path = sessions_root / "rollout-thread-goal.jsonl"

            def goal_context(timestamp: str, objective: str, *, edited: bool = False) -> dict[str, object]:
                message = "The active thread goal objective was edited by the user.\n" if edited else "Continue working toward the active thread goal.\n"
                return {
                    "type": "response_item",
                    "timestamp": timestamp,
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    '<codex_internal_context source="goal">\n'
                                    f"{message}<objective>\n{objective}\n</objective>\n"
                                    "</codex_internal_context>"
                                ),
                            }
                        ],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-goal"},
                    },
                }

            events = [
                goal_context("2026-07-18T10:00:00Z", "第一个目标"),
                {
                    "type": "response_item",
                    "timestamp": "2026-07-18T10:05:00Z",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "call-complete-goal",
                        "input": 'const result = await tools.update_goal({ status: "complete" }); text(result);',
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-goal"},
                    },
                },
                goal_context("2026-07-18T11:00:00Z", "第二个目标"),
                goal_context("2026-07-18T11:05:00Z", "第二个目标（已编辑）", edited=True),
            ]
            jsonl_path.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n", encoding="utf-8")

            with patch("ui.mobile._codex_sessions_root", return_value=sessions_root):
                payload = mobile._codex_raw_view_image_payload("thread-goal")

            self.assertEqual(
                {
                    "objective": "第二个目标（已编辑）",
                    "status": "active",
                    "started_at": "2026-07-18T11:00:00Z",
                    "updated_at": "2026-07-18T11:05:00Z",
                },
                payload["goal"],
            )

        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()

    def test_codex_rollout_prefers_native_goal_status_and_tracks_model(self) -> None:
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            sessions_root = Path(temp_dir) / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            jsonl_path = sessions_root / "rollout-thread-native-goal.jsonl"
            events = [
                {
                    "type": "turn_context",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "payload": {
                        "model": "gpt-5.6-sol",
                        "effort": "ultra",
                        "service_tier": "default",
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-01-01T00:02:05Z",
                    "payload": {
                        "type": "thread_goal_updated",
                        "threadId": "thread-native-goal",
                        "goal": {
                            "threadId": "thread-native-goal",
                            "objective": "继续原生目标",
                            "status": "paused",
                            "tokensUsed": 4200,
                            "timeUsedSeconds": 125,
                            "createdAt": 1767225600,
                            "updatedAt": 1767225725,
                        },
                    },
                },
            ]
            jsonl_path.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n", encoding="utf-8")

            with patch("ui.mobile._codex_sessions_root", return_value=sessions_root):
                payload = mobile._codex_raw_view_image_payload("thread-native-goal")

            self.assertEqual("paused", payload["goal"]["status"])
            self.assertEqual("继续原生目标", payload["goal"]["objective"])
            self.assertEqual("125", payload["goal"]["time_used_seconds"])
            self.assertEqual("1", payload["goal"]["native"])
            self.assertEqual("gpt-5.6-sol", payload["model"]["model"])
            self.assertEqual("ultra", payload["model"]["reasoning_effort"])

        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()

    def test_codex_rollout_applies_goal_completion_after_native_active_event(self) -> None:
        thread_id = "thread-native-goal-complete"
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            sessions_root = Path(temp_dir) / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            jsonl_path = sessions_root / f"rollout-{thread_id}.jsonl"
            events = [
                {
                    "type": "event_msg",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "payload": {
                        "type": "thread_goal_updated",
                        "threadId": thread_id,
                        "goal": {
                            "threadId": thread_id,
                            "objective": "完成后不再展示",
                            "status": "active",
                            "createdAt": 1767225600,
                            "updatedAt": 1767225600,
                        },
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:05:00Z",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "call-complete-native-goal",
                        "input": 'const result = await tools.update_goal({ status: "complete" }); text(result);',
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-goal"},
                    },
                },
            ]
            jsonl_path.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
                encoding="utf-8",
            )

            with patch("ui.mobile._codex_sessions_root", return_value=sessions_root):
                payload = mobile._codex_raw_view_image_payload(thread_id)
                thread = {"active_goal": {"status": "active"}}
                mobile._apply_codex_rollout_state(thread_id, thread)

        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
        self.assertEqual("complete", payload["goal"]["status"])
        self.assertEqual("2026-01-01T00:05:00Z", payload["goal"]["finished_at"])
        self.assertNotIn("active_goal", thread)

    def test_codex_rollout_keeps_newer_goal_control_cache_during_parse(self) -> None:
        thread_id = "thread-newer-goal-cache"
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            sessions_root = Path(temp_dir) / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            rollout_path = sessions_root / f"rollout-{thread_id}.jsonl"
            rollout_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-19T10:00:00Z",
                        "payload": {
                            "type": "thread_goal_updated",
                            "threadId": thread_id,
                            "goal": {
                                "threadId": thread_id,
                                "objective": "文件中的旧目标",
                                "status": "active",
                                "startedAt": "2026-07-19T10:00:00Z",
                                "updatedAt": "2026-07-19T10:00:00Z",
                            },
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE[thread_id] = {
                **mobile._empty_codex_rollout_payload(),
                "signature": None,
                "path": "",
                "offset": 0,
                "checked_at": 0.0,
                "goal": {
                    "objective": "刚刚暂停的目标",
                    "status": "paused",
                    "started_at": "2026-07-19T10:00:00Z",
                    "updated_at": "2026-07-19T10:05:00Z",
                    "native": "1",
                },
            }

            with patch("ui.mobile._codex_sessions_root", return_value=sessions_root):
                payload = mobile._codex_raw_view_image_payload(thread_id)

        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
        self.assertEqual("paused", payload["goal"]["status"])
        self.assertEqual("刚刚暂停的目标", payload["goal"]["objective"])

    def test_effective_codex_goal_prefers_live_clear_over_stale_rollout(self) -> None:
        thread_id = "thread-live-cleared-goal"
        mobile._CODEX_LIVE_GOAL_CACHE.clear()
        mobile._CODEX_LIVE_GOAL_INFLIGHT.clear()
        mobile._CODEX_LIVE_GOAL_CACHE[thread_id] = {
            "checked_at": 100.0,
            "checked_at_epoch": 1767225726.0,
            "resolved": True,
            "goal": {"status": "cleared", "updated_at": "2026-01-01T00:02:06Z", "native": "1"},
            "error": "",
        }
        rollout_goal = {
            "objective": "日志中的旧目标",
            "status": "active",
            "updated_at": "2026-01-01T00:02:05Z",
            "native": "1",
        }

        with (
            patch("ui.mobile.time.monotonic", return_value=101.0),
            patch("ui.mobile._start_codex_live_goal_refresh") as start_refresh,
        ):
            goal = mobile._effective_codex_goal_state(thread_id, rollout_goal)

        self.assertEqual("cleared", goal["status"])
        start_refresh.assert_not_called()
        mobile._CODEX_LIVE_GOAL_CACHE.clear()

    def test_effective_codex_goal_refreshes_when_rollout_is_newer_than_live_cache(self) -> None:
        thread_id = "thread-newer-rollout-goal"
        mobile._CODEX_LIVE_GOAL_CACHE.clear()
        mobile._CODEX_LIVE_GOAL_INFLIGHT.clear()
        mobile._CODEX_LIVE_GOAL_CACHE[thread_id] = {
            "checked_at": 100.0,
            "checked_at_epoch": 1767225600.0,
            "resolved": True,
            "goal": {"status": "cleared", "updated_at": "2026-01-01T00:00:00Z", "native": "1"},
            "error": "",
        }
        rollout_goal = {
            "objective": "刚创建的新目标",
            "status": "active",
            "updated_at": "2026-01-01T00:05:00Z",
            "native": "1",
        }

        with (
            patch("ui.mobile.time.monotonic", return_value=101.0),
            patch("ui.mobile._start_codex_live_goal_refresh") as start_refresh,
        ):
            goal = mobile._effective_codex_goal_state(thread_id, rollout_goal)

        self.assertEqual("active", goal["status"])
        start_refresh.assert_called_once_with(thread_id)
        mobile._CODEX_LIVE_GOAL_CACHE.clear()

    def test_live_goal_refresh_records_missing_desktop_goal_as_cleared(self) -> None:
        thread_id = "thread-missing-live-goal"
        mobile._CODEX_LIVE_GOAL_CACHE.clear()
        mobile._CODEX_LIVE_GOAL_INFLIGHT.clear()
        with (
            patch("ui.mobile.read_codex_thread_goal", return_value=None),
            patch("ui.mobile._invalidate_codex_thread_rollout_cache") as invalidate,
        ):
            mobile._refresh_codex_live_goal_cache_now(thread_id)

        cached = mobile._CODEX_LIVE_GOAL_CACHE[thread_id]
        self.assertTrue(cached["resolved"])
        self.assertEqual("cleared", cached["goal"]["status"])
        invalidate.assert_called_once_with(thread_id)
        mobile._CODEX_LIVE_GOAL_CACHE.clear()

    def test_codex_function_tool_image_result_gets_mobile_preview(self) -> None:
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            sessions_root = Path(temp_dir) / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            snapshot = "data:image/png;base64,YnJvd3Nlci10b29sLXNuaXBwZXQ="
            jsonl_path = sessions_root / "rollout-thread-browser-image.jsonl"
            events = [
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "浏览器检查完成"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    }
                },
                {
                    "payload": {
                        "type": "function_call",
                        "name": "js",
                        "call_id": "call-browser-image",
                        "arguments": json.dumps({"code": "await page.screenshot()"}),
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                    }
                },
                {
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-browser-image",
                        "output": [{"type": "input_image", "image_url": snapshot}],
                    }
                },
            ]
            jsonl_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            thread = {
                "id": "thread-browser-image",
                "messages": [
                    {"turn_id": "turn-1", "role": "user", "text": "验图"},
                    {"turn_id": "turn-1", "role": "assistant", "text": "浏览器检查完成"},
                ],
            }

            with patch("ui.mobile._codex_sessions_root", return_value=sessions_root):
                mobile._codex_raw_view_image_payload(thread["id"])
                tasks = _codex_thread_task_payloads(thread)
                source = str(tasks[0]["output_image_previews"][0]["source"])
                _empty, _route, signature, encoded_thread, encoded_reference = source.split("/", 4)
                decoded = mobile._decode_signed_codex_image_reference(signature, encoded_thread, encoded_reference)

                self.assertEqual("markdown_image", tasks[0]["output_image_previews"][0]["kind"])
                self.assertIn("![image]", str(tasks[0]["output"]))
                self.assertEqual([], tasks[0]["output_segments"])
                self.assertEqual(("image/png", b"browser-tool-snippet"), mobile._codex_view_image_bytes(*decoded))

        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()

    def test_codex_thread_payloads_use_turn_uuid_time_when_messages_have_no_at(self) -> None:
        turn_id = "019f28de-979b-7f91-aa60-f1ad2247bb4c"
        expected = datetime.fromtimestamp(int(turn_id.replace("-", "")[:12], 16) / 1000).isoformat(timespec="seconds")
        thread = {
            "id": "thread-uuid-time",
            "created_at": "2026-07-04T00:45:00",
            "messages": [
                {"turn_id": turn_id, "turn_order": 1, "role": "user", "text": "prompt"},
                {"turn_id": turn_id, "turn_order": 1, "role": "assistant", "text": "answer"},
            ],
        }

        tasks = _codex_thread_task_payloads(thread)

        self.assertEqual(expected, tasks[0]["created_at"])
        self.assertNotEqual(thread["created_at"], tasks[0]["created_at"])

    def test_latest_codex_turn_without_time_uses_thread_updated_at(self) -> None:
        thread = {
            "id": "thread-missing-latest-time",
            "created_at": "2026-07-04T00:45:00",
            "updated_at": "2026-07-05T21:53:28",
            "messages": [
                {"turn_id": "turn-known", "turn_order": 1, "at": "2026-07-05T21:52:20", "role": "user", "text": "known"},
                {"turn_id": "c36f631d-b0ac-4aad-8d60-6f2a443f7ea7", "turn_order": 2, "role": "assistant", "text": "latest"},
            ],
        }

        tasks = _codex_thread_task_payloads(thread, limit=2)

        self.assertEqual("2026-07-05T21:52:20", tasks[0]["created_at"])
        self.assertEqual("2026-07-05T21:53:28", tasks[1]["created_at"])

    def test_non_failed_terminal_activity_is_not_error_red(self) -> None:
        canceled_items = _task_activity_items(_activity_task(status="canceled", error="用户取消"))
        unknown_items = _task_activity_items(_activity_task(status="unknown_after_restart", error="重启中断"))
        failed_items = _task_activity_items(_activity_task(status="failed", error="失败"))

        self.assertEqual("canceled", canceled_items[-1]["event"])
        self.assertEqual("info", canceled_items[-1]["type"])
        self.assertEqual("unknown_after_restart", unknown_items[-1]["event"])
        self.assertEqual("info", unknown_items[-1]["type"])
        self.assertEqual("failed", failed_items[-1]["event"])
        self.assertEqual("error", failed_items[-1]["type"])

    def test_legacy_mobile_canceled_badge_is_not_danger_red(self) -> None:
        source = Path("ui/mobile.py").read_text(encoding="utf-8")

        self.assertIn(".badge.failed {{ background: var(--danger-soft); color: var(--danger); }}", source)
        self.assertIn(".badge.canceled {{ background: var(--warn-soft); color: var(--warn); }}", source)
        self.assertNotIn(".badge.failed, .badge.canceled", source)

    def test_codex_thread_session_name_round_trips(self) -> None:
        session_name = codex_thread_session_name("thread-001")

        self.assertEqual("codex:thread-001", session_name)
        self.assertEqual("thread-001", codex_thread_id_from_session_name(session_name))
        self.assertEqual("", codex_thread_id_from_session_name("default"))

    def test_codex_thread_history_payload_preserves_reasoning(self) -> None:
        thread = {
            "id": "thread-001",
            "title": "真实会话",
            "preview": "继续任务",
            "cwd": "I:/AI/chatbridge",
            "path": "C:/Users/test/.codex/sessions/thread-001.jsonl",
            "updated_at": "2026-07-04T20:00:00",
            "messages": [
                {"turn_id": "turn-1", "role": "user", "text": "用户问题", "at": "2026-07-04T20:00:00"},
                {"turn_id": "turn-1", "role": "reasoning", "text": "先检查状态", "at": "2026-07-04T20:00:01"},
                {
                    "turn_id": "turn-1",
                    "role": "activity",
                    "text": "shell: pytest",
                    "activity": {
                        "event": "codex_tool_call",
                        "type": "info",
                        "at": "2026-07-04T20:00:02",
                        "detail": "shell: pytest",
                        "metadata": {"item_type": "tool_call", "name": "shell"},
                    },
                },
                {"turn_id": "turn-1", "role": "assistant", "text": "最终回答", "at": "2026-07-04T20:00:03"},
            ],
        }

        sidebar_item = _mobile_codex_thread_payload(thread)
        tasks = _codex_thread_task_payloads(thread)

        self.assertEqual("codex:thread-001", sidebar_item["session_name"])
        self.assertEqual("chatbridge", sidebar_item["project"])
        self.assertEqual("C:/Users/test/.codex/sessions/thread-001.jsonl", sidebar_item["path"])
        self.assertEqual(1, len(tasks))
        self.assertEqual("codex:thread-001", tasks[0]["session_name"])
        self.assertEqual("thread-001", tasks[0]["session_id"])
        self.assertEqual("用户问题", tasks[0]["prompt"])
        self.assertEqual("先检查状态", tasks[0]["progress_text"])
        self.assertEqual("先检查状态", tasks[0]["reasoning_text"])
        self.assertEqual("", tasks[0]["live_output_text"])
        self.assertEqual("最终回答", tasks[0]["output"])
        self.assertEqual("2026-07-04T20:00:00", tasks[0]["started_at"])
        self.assertEqual("2026-07-04T20:00:02", tasks[0]["progress_at"])
        self.assertEqual("2026-07-04T20:00:03", tasks[0]["finished_at"])
        self.assertEqual(
            ["codex_reasoning", "codex_tool_call"],
            [item["event"] for item in tasks[0]["activity_items"]],
        )
        self.assertEqual("先检查状态", tasks[0]["activity_items"][0]["detail"])

    def test_codex_thread_history_uses_rollout_turn_times_for_final_reply_duration(self) -> None:
        thread = {
            "id": "thread-timed",
            "updated_at": "2026-07-17T05:20:30",
            "messages": [
                {"id": "item-1", "turn_id": "turn-timed", "role": "reasoning", "text": "处理中"},
                {"id": "item-2", "turn_id": "turn-timed", "role": "assistant", "text": "最终回答"},
            ],
        }

        with patch(
            "ui.mobile._codex_cached_rollout_payload",
            return_value={
                "turn_times": {
                    "turn-timed": {
                        "started_at": "2026-07-17T05:19:00",
                        "finished_at": "2026-07-17T05:20:00",
                    }
                },
            },
        ):
            tasks = _codex_thread_task_payloads(thread)

        self.assertEqual("2026-07-17T05:19:00", tasks[0]["created_at"])
        self.assertEqual("2026-07-17T05:19:00", tasks[0]["started_at"])
        self.assertEqual("2026-07-17T05:20:00", tasks[0]["finished_at"])

    def test_codex_final_answer_is_terminal_before_task_complete_arrives(self) -> None:
        thread = {
            "id": "thread-final-phase",
            "updated_at": "2026-07-17T05:20:30",
            "messages": [
                {
                    "id": "item-user",
                    "turn_id": "turn-final-phase",
                    "role": "user",
                    "text": "检查状态",
                    "at": "2026-07-17T05:19:00",
                },
                {
                    "id": "item-final",
                    "turn_id": "turn-final-phase",
                    "role": "assistant",
                    "phase": "final_answer",
                    "text": "最终回答",
                    "at": "2026-07-17T05:20:00",
                },
            ],
        }

        with patch(
            "ui.mobile._codex_cached_rollout_payload",
            return_value={
                "turn_times": {
                    "turn-final-phase": {
                        "started_at": "2026-07-17T05:19:00",
                        "finished_at": "",
                    }
                },
            },
        ):
            tasks = _codex_thread_task_payloads(thread)

        self.assertTrue(tasks[0]["final_answer"])
        self.assertEqual("2026-07-17T05:20:00", tasks[0]["final_answer_at"])
        self.assertEqual("2026-07-17T05:20:00", tasks[0]["finished_at"])

    def test_codex_thread_history_matches_merged_reasoning_to_rollout_times(self) -> None:
        thread = {
            "id": "thread-reasoning-times",
            "updated_at": "2026-07-17T05:20:30",
            "messages": [
                {
                    "id": "item-1",
                    "turn_id": "turn-timed",
                    "role": "reasoning",
                    "text": "first\nsecond",
                    "at": "2026-07-17T05:19:00",
                },
                {
                    "id": "item-2",
                    "turn_id": "turn-timed",
                    "role": "reasoning",
                    "text": "third",
                    "at": "2026-07-17T05:19:00",
                },
                {"id": "item-3", "turn_id": "turn-timed", "role": "assistant", "text": "最终回答"},
            ],
        }
        raw_payload = {
            "turn_times": {
                "turn-timed": {
                    "started_at": "2026-07-17T05:19:00",
                    "finished_at": "2026-07-17T05:20:00",
                }
            },
            "reasoning_times": {
                "turn-timed": [
                    {"text": "first", "at": "2026-07-17T05:19:01"},
                    {"text": "second", "at": "2026-07-17T05:19:02"},
                    {"text": "third", "at": "2026-07-17T05:19:04"},
                ]
            },
        }

        with patch("ui.mobile._codex_cached_rollout_payload", return_value=raw_payload):
            tasks = _codex_thread_task_payloads(thread)

        reasoning_items = [item for item in tasks[0]["activity_items"] if item["event"] == "codex_reasoning"]
        self.assertEqual(
            ["2026-07-17T05:19:02", "2026-07-17T05:19:04"],
            [item["at"] for item in reasoning_items],
        )
        self.assertEqual("2026-07-17T05:19:04", tasks[0]["progress_at"])

    def test_codex_thread_history_normalizes_subagent_activity(self) -> None:
        thread = {
            "id": "thread-subagent",
            "updated_at": "2026-07-17T05:20:00",
            "messages": [
                {"turn_id": "turn-1", "role": "user", "text": "并行检查"},
                {
                    "id": "call-subagent-1",
                    "turn_id": "turn-1",
                    "role": "activity",
                    "activity": {
                        "event": "codex_item",
                        "type": "info",
                        "at": "2026-07-17T05:19:01",
                        "detail": json.dumps(
                            {
                                "agentPath": "/root/repo_audit",
                                "agentThreadId": "019f6c07-db5b-7500-8999-d4e0c62ba52a",
                                "kind": "started",
                            }
                        ),
                        "metadata": {"item_type": "subAgentActivity", "item_id": "call-subagent-1"},
                    },
                },
                {"turn_id": "turn-1", "role": "assistant", "text": "检查完成"},
            ],
        }

        tasks = _codex_thread_task_payloads(thread)

        activity = tasks[0]["activity_items"][0]
        self.assertEqual("codex_subagent", activity["event"])
        self.assertEqual("/root/repo_audit", activity["detail"])
        self.assertEqual("/root/repo_audit", activity["metadata"]["agent_path"])
        self.assertEqual("019f6c07-db5b-7500-8999-d4e0c62ba52a", activity["metadata"]["agent_thread_id"])
        self.assertEqual("started", activity["metadata"]["kind"])

    def test_codex_history_merges_local_command_into_the_matching_turn(self) -> None:
        history_tasks = [
            {
                "id": "history-1",
                "prompt": "run status",
                "output": "first result",
                "created_at": "2026-07-17T01:00:00",
                "activity_items": [{"id": "reasoning-old-1", "event": "codex_reasoning", "detail": "first reasoning"}],
            },
            {
                "id": "history-2",
                "prompt": "run status",
                "output": "second result",
                "created_at": "2026-07-17T02:00:00",
                "activity_items": [{"id": "reasoning-old-2", "event": "codex_reasoning", "detail": "second reasoning"}],
            },
        ]
        local_tasks = [
            {
                "id": "local-2",
                "session_id": "thread-1",
                "prompt": "run status",
                "output": "second result",
                "created_at": "2026-07-17T02:00:01",
                "activity_items": [
                    {
                        "id": "command-2",
                        "event": "codex_command",
                        "detail": "git status --short",
                        "metadata": {"command": "git status --short", "output": "clean"},
                    }
                ],
            }
        ]

        merged = _merge_codex_thread_local_timeline(history_tasks, local_tasks, thread_id="thread-1")

        self.assertEqual(["codex_reasoning"], [item["event"] for item in merged[0]["activity_items"]])
        self.assertEqual(
            ["codex_reasoning", "codex_command"],
            [item["event"] for item in merged[1]["activity_items"]],
        )
        self.assertEqual("git status --short", merged[1]["activity_items"][1]["metadata"]["command"])

    def test_codex_history_keeps_history_reasoning_when_adding_local_commands(self) -> None:
        history_tasks = [
            {
                "id": "history-1",
                "prompt": "inspect",
                "output": "done",
                "created_at": "2026-07-17T02:00:00",
                "activity_items": [{"id": "history-reasoning", "event": "codex_reasoning", "detail": "combined reasoning"}],
            }
        ]
        local_timeline = [
            {"id": "reasoning-1", "event": "codex_reasoning", "detail": "before command"},
            {"id": "command-1", "event": "codex_command", "detail": "pytest -q", "metadata": {"command": "pytest -q"}},
            {"id": "reasoning-2", "event": "codex_reasoning", "detail": "after command"},
        ]
        local_tasks = [
            {
                "id": "local-1",
                "session_id": "thread-1",
                "prompt": "inspect",
                "output": "done",
                "created_at": "2026-07-17T02:00:01",
                "activity_items": local_timeline,
            }
        ]

        merged = _merge_codex_thread_local_timeline(history_tasks, local_tasks, thread_id="thread-1")

        self.assertEqual(
            ["codex_reasoning", "codex_command"],
            [item["event"] for item in merged[0]["activity_items"]],
        )
        self.assertEqual(["history-reasoning", "command-1"], [item["id"] for item in merged[0]["activity_items"]])

    def test_codex_history_inserts_local_command_without_reordering_tool_calls(self) -> None:
        history_tasks = [
            {
                "id": "history-1",
                "prompt": "inspect",
                "output": "done",
                "created_at": "2026-07-17T02:00:00",
                "activity_items": [
                    {"id": "reasoning-1", "event": "codex_reasoning", "detail": "before command"},
                    {
                        "id": "tool-1",
                        "event": "codex_tool_call",
                        "detail": "node_repl.js - inspect state",
                        "metadata": {"command": "node_repl.js - inspect state", "status": "completed"},
                    },
                    {"id": "reasoning-2", "event": "codex_reasoning", "detail": "after command"},
                ],
            }
        ]
        local_tasks = [
            {
                "id": "local-1",
                "session_id": "thread-1",
                "prompt": "inspect",
                "output": "done",
                "created_at": "2026-07-17T02:00:01",
                "activity_items": [
                    {"id": "reasoning-1", "event": "codex_reasoning", "detail": "before command"},
                    {
                        "id": "command-1",
                        "event": "codex_command",
                        "detail": "git status --short",
                        "metadata": {"command": "git status --short", "status": "completed"},
                    },
                    {"id": "reasoning-2", "event": "codex_reasoning", "detail": "after command"},
                ],
            }
        ]

        merged = _merge_codex_thread_local_timeline(history_tasks, local_tasks, thread_id="thread-1")

        self.assertEqual(
            ["reasoning-1", "tool-1", "command-1", "reasoning-2"],
            [item["id"] for item in merged[0]["activity_items"]],
        )

    def test_codex_thread_history_keeps_supplemental_user_messages_separate(self) -> None:
        thread = {
            "id": "thread-supplemental-inputs",
            "updated_at": "2026-07-04T20:02:00",
            "messages": [
                {"id": "item-1", "turn_id": "turn-1", "turn_order": 1, "item_order": 1, "at": "2026-07-04T20:00:00", "role": "user", "text": "先生成预览"},
                {"id": "item-2", "turn_id": "turn-1", "turn_order": 1, "item_order": 2, "at": "2026-07-04T20:00:01", "role": "assistant", "text": "正在下载模型"},
                {"id": "item-3", "turn_id": "turn-1", "turn_order": 1, "item_order": 3, "at": "2026-07-04T20:01:00", "role": "user", "text": "缓存不要放 C 盘"},
                {"id": "item-4", "turn_id": "turn-1", "turn_order": 1, "item_order": 4, "at": "2026-07-04T20:01:01", "role": "assistant", "text": "已改为项目缓存目录"},
            ],
        }

        tasks = _codex_thread_task_payloads(thread)

        self.assertEqual(2, _codex_thread_turn_count(thread))
        self.assertEqual(["先生成预览", "缓存不要放 C 盘"], [task["prompt"] for task in tasks])
        self.assertEqual(["正在下载模型", "已改为项目缓存目录"], [task["output"] for task in tasks])
        self.assertEqual(["2026-07-04T20:00:00", "2026-07-04T20:01:00"], [task["created_at"] for task in tasks])
        self.assertEqual([1001, 1002], [task["stream_order"] for task in tasks])
        self.assertNotEqual(tasks[0]["id"], tasks[1]["id"])

    def test_codex_thread_payloads_can_build_only_recent_turns(self) -> None:
        messages: list[dict[str, object]] = []
        for index in range(100):
            turn_id = f"turn-{index:03d}"
            messages.append({"turn_id": turn_id, "role": "user", "text": f"prompt {index}"})
            messages.append({"turn_id": turn_id, "role": "assistant", "text": f"answer {index}"})
        thread = {
            "id": "thread-long",
            "cwd": "I:/AI/chatbridge",
            "updated_at": "2026-07-04T20:00:00",
            "messages": messages,
        }

        tasks = _codex_thread_task_payloads(thread, limit=3)

        self.assertEqual(100, _codex_thread_turn_count(thread))
        self.assertEqual(["prompt 97", "prompt 98", "prompt 99"], [task["prompt"] for task in tasks])
        self.assertEqual([98001, 99001, 100001], [task["stream_order"] for task in tasks])
        self.assertEqual("2026-07-04T20:00:00", tasks[0]["created_at"])

    def test_codex_thread_payloads_sort_only_selected_turn_messages(self) -> None:
        messages: list[dict[str, object]] = []
        for index in range(50):
            turn_id = f"turn-{index:03d}"
            messages.append({"turn_id": turn_id, "role": "user", "text": f"prompt {index}", "turn_order": index + 1})
            messages.append({"turn_id": turn_id, "role": "assistant", "text": f"answer {index}", "turn_order": index + 1})
        original_sort_key = mobile._codex_message_sort_key

        with patch("ui.mobile._codex_message_sort_key", wraps=original_sort_key) as sort_key:
            tasks = _codex_thread_task_payloads({"id": "thread-long", "messages": messages}, limit=2)

        self.assertEqual(["prompt 48", "prompt 49"], [task["prompt"] for task in tasks])
        self.assertEqual(4, sort_key.call_count)

    def test_codex_thread_payloads_limited_path_does_not_sort_all_turns(self) -> None:
        messages: list[dict[str, object]] = []
        for index in range(30):
            turn_id = f"turn-{index:03d}"
            messages.append({"turn_id": turn_id, "role": "user", "text": f"prompt {index}"})
            messages.append({"turn_id": turn_id, "role": "assistant", "text": f"answer {index}"})

        with patch("ui.mobile._codex_thread_turn_order", side_effect=AssertionError("should not sort all turns")):
            tasks = _codex_thread_task_payloads({"id": "thread-long", "messages": messages}, limit=2)

        self.assertEqual(["prompt 28", "prompt 29"], [task["prompt"] for task in tasks])
        self.assertEqual([29001, 30001], [task["stream_order"] for task in tasks])

    def test_codex_thread_turn_count_does_not_build_turn_order(self) -> None:
        thread = {
            "id": "thread-count",
            "messages": [
                {"turn_id": "turn-1", "role": "user", "text": "prompt"},
                {"turn_id": "turn-1", "role": "assistant", "text": "answer"},
                {"turn_id": "turn-2", "role": "user", "text": "next"},
            ],
        }

        with patch("ui.mobile._codex_thread_turn_order", side_effect=AssertionError("should not sort")):
            self.assertEqual(2, _codex_thread_turn_count(thread))

    def test_codex_thread_payloads_sort_newest_first_messages_by_time(self) -> None:
        thread = {
            "id": "thread-newest-first",
            "cwd": "I:/AI/chatbridge",
            "updated_at": "2026-07-04T20:02:00",
            "messages": [
                {"turn_id": "turn-2", "role": "assistant", "text": "answer 2", "at": "2026-07-04T20:02:01", "turn_order": 2, "item_order": 2},
                {"turn_id": "turn-2", "role": "user", "text": "prompt 2", "at": "2026-07-04T20:02:00", "turn_order": 2, "item_order": 1},
                {"turn_id": "turn-1", "role": "assistant", "text": "answer 1", "at": "2026-07-04T20:01:01", "turn_order": 1, "item_order": 2},
                {"turn_id": "turn-1", "role": "user", "text": "prompt 1", "at": "2026-07-04T20:01:00", "turn_order": 1, "item_order": 1},
            ],
        }

        tasks = _codex_thread_task_payloads(thread)

        self.assertEqual(["prompt 1", "prompt 2"], [task["prompt"] for task in tasks])
        self.assertEqual(["answer 1", "answer 2"], [task["output"] for task in tasks])
        self.assertEqual([1001, 2001], [task["stream_order"] for task in tasks])
        self.assertEqual(["2026-07-04T20:01:00", "2026-07-04T20:02:00"], [task["created_at"] for task in tasks])

    def test_codex_thread_top_level_error_does_not_override_history_turns(self) -> None:
        thread = {
            "id": "thread-with-error",
            "updated_at": "2026-07-04T20:02:00",
            "error": "thread read warning",
            "messages": [
                {"turn_id": "turn-1", "role": "user", "text": "prompt"},
                {"turn_id": "turn-1", "role": "assistant", "text": "answer"},
            ],
        }

        tasks = _codex_thread_task_payloads(thread)

        self.assertEqual(1, len(tasks))
        self.assertEqual("answer", tasks[0]["output"])
        self.assertEqual("", tasks[0]["error"])

    def test_activity_only_changes_affect_task_signature(self) -> None:
        before = {
            "id": "task-1",
            "status": "running",
            "activity_items": [{"event": "codex_tool_call", "at": "2026-07-04T20:00:00", "detail": "shell"}],
        }
        after = {
            "id": "task-1",
            "status": "running",
            "activity_items": [{"event": "codex_tool_call", "at": "2026-07-04T20:00:01", "detail": "shell done"}],
        }

        self.assertNotEqual(mobile._payload_task_signature_part(before), mobile._payload_task_signature_part(after))

    def test_load_all_codex_threads_follows_pagination_and_deduplicates(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_list_codex_threads(**kwargs: object) -> dict[str, object]:
            calls.append(dict(kwargs))
            cursor = str(kwargs.get("cursor") or "")
            archived = bool(kwargs.get("archived"))
            if archived:
                return {
                    "threads": [{"id": "thread-4", "archived": True}],
                    "next_cursor": "",
                    "backwards_cursor": "cursor-archived-newest",
                }
            if not cursor:
                return {
                    "threads": [{"id": "thread-1"}, {"id": "thread-2"}],
                    "next_cursor": "cursor-2",
                    "backwards_cursor": "cursor-newest",
                }
            self.assertEqual("cursor-2", cursor)
            return {
                "threads": [{"id": "thread-2"}, {"id": "thread-3"}],
                "next_cursor": "",
                "backwards_cursor": "cursor-newest",
            }

        with patch("ui.mobile.list_codex_threads", side_effect=fake_list_codex_threads):
            payload = _load_all_codex_threads()

        self.assertEqual(["thread-1", "thread-2", "thread-3", "thread-4"], [thread["id"] for thread in payload["threads"]])
        self.assertEqual("", payload["next_cursor"])
        self.assertEqual("cursor-archived-newest", payload["backwards_cursor"])
        self.assertEqual(
            [
                {"limit": CODEX_THREAD_PAGE_LIMIT, "cursor": "", "archived": False, "timeout_seconds": 8},
                {"limit": CODEX_THREAD_PAGE_LIMIT, "cursor": "cursor-2", "archived": False, "timeout_seconds": 8},
                {"limit": CODEX_THREAD_PAGE_LIMIT, "cursor": "", "archived": True, "timeout_seconds": 8},
            ],
            calls,
        )

    def test_load_codex_threads_page_fetches_one_page_only(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_list_codex_threads(**kwargs: object) -> dict[str, object]:
            calls.append(dict(kwargs))
            return {
                "threads": [{"id": "thread-1", "title": "First"}],
                "next_cursor": "cursor-2",
                "backwards_cursor": "cursor-newest",
            }

        with patch("ui.mobile.list_codex_threads", side_effect=fake_list_codex_threads):
            payload = load_codex_threads_page(cursor="", archived=False, limit=25)

        self.assertEqual(["thread-1"], [thread["id"] for thread in payload["threads"]])
        self.assertEqual("cursor-2", payload["next_cursor"])
        self.assertEqual("cursor-newest", payload["backwards_cursor"])
        self.assertFalse(payload["archived"])
        self.assertEqual("", payload["error"])
        self.assertEqual(
            [{"limit": 25, "cursor": "", "archived": False, "timeout_seconds": 8}],
            calls,
        )

    def test_codex_thread_default_page_limit_stays_sidebar_sized(self) -> None:
        self.assertLessEqual(CODEX_THREAD_PAGE_LIMIT, 50)

    def test_codex_thread_detail_reads_app_server_thread(self) -> None:
        import ui.mobile as mobile

        mobile._CODEX_THREAD_DETAIL_CACHE.clear()
        mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
        with (
            patch(
                "ui.mobile.read_codex_thread",
                return_value={
                    "id": "thread-1",
                    "messages": [{"role": "user", "text": "hello"}],
                    "path": "C:/archived/rollout-thread-1.jsonl",
                },
            ) as read_thread,
        ):
            thread = mobile._load_codex_thread_now("thread-1")

        self.assertEqual("thread-1", thread["id"])
        self.assertEqual("hello", thread["messages"][0]["text"])
        self.assertEqual("codex-app-server", thread["source"])
        read_thread.assert_called_once_with("thread-1", timeout_seconds=8)
        mobile._CODEX_THREAD_DETAIL_CACHE.clear()
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()

    def test_mobile_state_defaults_to_lightweight_payload(self) -> None:
        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": [], "agents": []}),
            patch("ui.mobile._mobile_runtime_snapshot", return_value={}),
            patch("ui.mobile.load_dashboard_state") as load_dashboard,
            patch("ui.mobile.HubTask.from_dict") as hub_task_from_dict,
            patch("ui.mobile._hub_task_order_lookup") as task_order_lookup,
            patch("ui.mobile.build_session_rows_page") as build_rows,
            patch("ui.mobile._load_codex_threads_cached") as load_codex_threads,
        ):
            state = _build_mobile_state()

        load_dashboard.assert_not_called()
        hub_task_from_dict.assert_not_called()
        task_order_lookup.assert_not_called()
        build_rows.assert_not_called()
        load_codex_threads.assert_not_called()
        self.assertEqual([], state["codex_threads"])
        self.assertEqual("", state["codex_threads_error"])
        self.assertEqual([], state["sessions"])
        self.assertEqual([], state["senders"])
        self.assertFalse(state["sessions_loaded"])
        self.assertFalse(state["senders_loaded"])

    def test_mobile_runtime_snapshot_skips_expensive_runtime_probes(self) -> None:
        import ui.mobile as mobile

        mobile._MOBILE_RUNTIME_CACHE.clear()
        snapshot = SimpleNamespace(to_dict=lambda: {"hub_running": True})

        with patch("ui.mobile.get_runtime_snapshot", return_value=snapshot) as get_snapshot:
            self.assertEqual({"hub_running": True}, mobile._mobile_runtime_snapshot())

        get_snapshot.assert_called_once_with(
            include_agent_processes=False,
            include_qq_login_status=False,
            discover_missing_processes=False,
        )

    def test_mobile_state_loads_session_page_only_when_requested(self) -> None:
        dashboard = SimpleNamespace(
            hub_state=SimpleNamespace(tasks=[], agents=[]),
            bridge_conversations={},
            snapshot=SimpleNamespace(to_dict=lambda: {}),
        )
        row = SimpleNamespace(name="focus", status="idle", queue_size=0, success_count=1, failure_count=0)
        rows_page = SimpleNamespace(rows=[row], page=2, total_pages=4, total_count=37)

        with (
            patch("ui.mobile.load_dashboard_state", return_value=dashboard) as load_dashboard,
            patch("ui.mobile.build_session_rows_page", return_value=rows_page) as build_rows,
            patch("ui.mobile._load_codex_threads_cached") as load_codex_threads,
        ):
            state = _build_mobile_state(include_sessions=True, session_page=2, session_limit=10)

        self.assertFalse(load_dashboard.call_args.kwargs["include_hub_task_text"])
        build_rows.assert_called_once_with(ANY, ANY, 2, 10, include_session_files=False)
        load_codex_threads.assert_not_called()
        self.assertTrue(state["sessions_loaded"])
        self.assertEqual(2, state["session_page"])
        self.assertEqual(4, state["session_total_pages"])
        self.assertEqual(37, state["session_total_count"])
        self.assertEqual([{"name": "focus", "status": "idle", "queue_size": 0, "success_count": 1, "failure_count": 0}], state["sessions"])


    def test_mobile_state_keeps_sender_summaries_on_full_task_text(self) -> None:
        dashboard = SimpleNamespace(
            hub_state=SimpleNamespace(tasks=[], agents=[]),
            bridge_conversations={},
            snapshot=SimpleNamespace(to_dict=lambda: {}),
        )

        with patch("ui.mobile.load_dashboard_state", return_value=dashboard) as load_dashboard:
            _build_mobile_state(include_senders=True)

        self.assertTrue(load_dashboard.call_args.kwargs["include_hub_task_text"])

    def test_mobile_state_loads_codex_threads_one_page_when_requested(self) -> None:
        codex_payload = {
            "threads": [{"id": "thread-1"}],
            "next_cursor": "cursor-2",
            "backwards_cursor": "cursor-newest",
            "archived": False,
            "error": "",
        }

        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": [], "agents": []}),
            patch("ui.mobile._mobile_runtime_snapshot", return_value={}),
            patch("ui.mobile.load_dashboard_state") as load_dashboard,
            patch("ui.mobile.HubTask.from_dict") as hub_task_from_dict,
            patch("ui.mobile.load_codex_threads_page", return_value=codex_payload) as load_page,
            patch("ui.mobile._load_codex_threads_cached") as load_all_cached,
        ):
            state = _build_mobile_state(include_codex_threads=True, codex_threads_cursor="cursor-1", codex_threads_archived=True)

        load_dashboard.assert_not_called()
        hub_task_from_dict.assert_not_called()
        load_page.assert_called_once_with(cursor="cursor-1", archived=True)
        load_all_cached.assert_not_called()
        self.assertEqual([{"id": "thread-1"}], state["codex_threads"])
        self.assertEqual("cursor-2", state["codex_threads_next_cursor"])
        self.assertEqual("cursor-newest", state["codex_threads_backwards_cursor"])
        self.assertFalse(state["codex_threads_archived"])

    def test_mobile_diagnostics_defaults_to_checks_only(self) -> None:
        check = SimpleNamespace(key="python", label="Python", ok=True, detail="ok")
        external = SimpleNamespace(pid=123, name="agent", backend="codex", session_hint="hint", command_line="cmd")
        dashboard = SimpleNamespace(
            checks={"python": check},
            logs={"hub_out": "heavy log"},
            external_agent_processes=[external],
            hub_state=SimpleNamespace(external_agent_processes=[external]),
            checks_in_progress=False,
            checks_progress_text="",
        )

        with patch("ui.mobile.load_dashboard_state", return_value=dashboard):
            state = _build_mobile_diagnostics()

        self.assertEqual([{"key": "python", "label": "Python", "ok": True, "detail": "ok"}], state["checks"])
        self.assertEqual([], state["logs"])
        self.assertFalse(state["logs_loaded"])
        self.assertEqual([], state["external_processes"])
        self.assertFalse(state["external_loaded"])

    def test_mobile_diagnostics_loads_logs_and_external_only_when_requested(self) -> None:
        check = SimpleNamespace(key="python", label="Python", ok=True, detail="ok")
        external = SimpleNamespace(pid=123, name="agent", backend="codex", session_hint="hint", command_line="cmd")
        dashboard = SimpleNamespace(
            checks={"python": check},
            logs={
                "hub_out": "hub out",
                "hub_err": "hub err",
                "bridge_out": "bridge out",
                "bridge_err": "bridge err",
                "onebot_runtime_out": "onebot out",
                "onebot_runtime_err": "onebot err",
                "qq_bridge_out": "qq out",
                "qq_bridge_err": "qq err",
            },
            external_agent_processes=[external],
            hub_state=SimpleNamespace(external_agent_processes=[]),
            checks_in_progress=False,
            checks_progress_text="",
        )

        with patch("ui.mobile.load_dashboard_state", return_value=dashboard):
            state = _build_mobile_diagnostics(include_logs=True, include_external=True)

        self.assertTrue(state["logs_loaded"])
        self.assertTrue(state["external_loaded"])
        self.assertEqual(8, len(state["logs"]))
        self.assertEqual(
            [{"pid": 123, "name": "agent", "backend": "codex", "session_hint": "hint", "command_line": "cmd"}],
            state["external_processes"],
        )

    def test_stream_state_skips_full_session_and_codex_lists(self) -> None:
        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": [], "agents": []}),
            patch("ui.mobile.load_dashboard_state") as load_dashboard,
            patch("ui.mobile.HubTask.from_dict") as hub_task_from_dict,
            patch("ui.mobile.build_session_rows_page") as build_rows,
            patch("ui.mobile._load_codex_threads_cached") as load_codex_threads,
        ):
            state = build_stream_state_snapshot(selected_session_name="focus", task_limit=1, session_task_limit=30)

        load_dashboard.assert_not_called()
        hub_task_from_dict.assert_not_called()
        build_rows.assert_not_called()
        load_codex_threads.assert_not_called()
        self.assertEqual([], state["sessions"])
        self.assertEqual([], state["senders"])
        self.assertEqual([], state["codex_threads"])
        self.assertNotIn("runtime", state)

    def test_stream_sidebar_state_loads_codex_threads_only_when_requested(self) -> None:
        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": [], "agents": []}),
            patch("ui.mobile.load_dashboard_state") as load_dashboard,
            patch("ui.mobile.build_session_rows_page") as build_rows,
            patch("ui.mobile._load_codex_threads_cached") as load_codex_threads,
        ):
            state = build_stream_sidebar_state_snapshot(task_limit=40, include_codex_threads=False)

        load_dashboard.assert_not_called()
        build_rows.assert_not_called()
        load_codex_threads.assert_not_called()
        self.assertEqual([], state["codex_threads"])

        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": [], "agents": []}),
            patch("ui.mobile.load_dashboard_state") as load_dashboard,
            patch("ui.mobile.build_session_rows_page") as build_rows,
            patch("ui.mobile.load_codex_threads_page", return_value={"threads": [{"id": "thread-1"}], "next_cursor": "cursor-2", "error": ""}) as load_codex_threads,
            patch("ui.mobile._load_codex_threads_cached") as load_all_cached,
        ):
            state = build_stream_sidebar_state_snapshot(task_limit=40, include_codex_threads=True)

        load_dashboard.assert_not_called()
        build_rows.assert_not_called()
        load_codex_threads.assert_called_once()
        load_all_cached.assert_not_called()
        self.assertEqual([{"id": "thread-1"}], state["codex_threads"])
        self.assertEqual("cursor-2", state["codex_threads_next_cursor"])

    def test_stream_sidebar_state_pages_by_session_latest_task(self) -> None:
        raw_tasks = [
            _raw_activity_task(id="focus-old", session_name="focus", created_at="2026-07-04T04:01:00"),
            _raw_activity_task(id="focus-new", session_name="focus", created_at="2026-07-04T04:03:00"),
            _raw_activity_task(id="other-new", session_name="other", created_at="2026-07-04T04:02:00"),
        ]

        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": raw_tasks, "agents": []}),
            patch("ui.mobile._build_stream_raw_task_index") as build_index,
            patch("ui.mobile._raw_hub_tasks") as raw_hub_tasks,
            patch("ui.mobile.load_dashboard_state") as load_dashboard,
        ):
            first_page = build_stream_sidebar_state_snapshot(task_limit=1)
            second_page = build_stream_sidebar_state_snapshot(task_limit=2)

        build_index.assert_not_called()
        raw_hub_tasks.assert_not_called()
        load_dashboard.assert_not_called()
        self.assertEqual(["focus-new"], [task["id"] for task in first_page["tasks"]])
        self.assertEqual(["other-new", "focus-new"], [task["id"] for task in second_page["tasks"]])
        self.assertEqual({"focus": 2, "other": 1}, second_page["session_task_counts"])
        self.assertEqual(2, second_page["session_total_count"])

    def test_stream_sidebar_state_uses_top_k_sessions_without_full_sort(self) -> None:
        raw_tasks = [
            _raw_activity_task(id=f"task-{index}", session_name=f"session-{index}", created_at=f"2026-07-04T04:{index:02d}:00")
            for index in range(6)
        ]

        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": raw_tasks, "agents": []}),
            patch("ui.mobile.heapq.nlargest", wraps=mobile.heapq.nlargest) as nlargest,
        ):
            state = build_stream_sidebar_state_snapshot(task_limit=2)

        nlargest.assert_called_once()
        self.assertEqual(2, nlargest.call_args.args[0])
        self.assertEqual(["task-4", "task-5"], [task["id"] for task in state["tasks"]])
        self.assertEqual({"session-4": 1, "session-5": 1}, state["session_task_counts"])
        self.assertEqual(6, state["session_total_count"])

    def test_stream_sidebar_state_reuses_cached_snapshot_for_same_raw_state(self) -> None:
        mobile._RAW_STREAM_SIDEBAR_CACHE.clear()
        mobile._RAW_STREAM_SIDEBAR_CACHE.update({"key": None, "state": None})
        raw_state = {
            "tasks": [
                _raw_activity_task(id="task-1", session_name="one", created_at="2026-07-04T04:01:00"),
                _raw_activity_task(id="task-2", session_name="two", created_at="2026-07-04T04:02:00"),
            ],
            "agents": [],
        }
        original_sort_key = mobile._stream_time_sort_key

        with (
            patch("ui.mobile._load_raw_hub_state", return_value=raw_state),
            patch("ui.mobile._stream_time_sort_key", wraps=original_sort_key) as sort_key,
        ):
            first = build_stream_sidebar_state_snapshot(task_limit=2)
            first["tasks"][0]["id"] = "mutated"
            calls_after_first = sort_key.call_count
            second = build_stream_sidebar_state_snapshot(task_limit=2)

        self.assertGreater(calls_after_first, 0)
        self.assertEqual(calls_after_first, sort_key.call_count)
        self.assertEqual(["task-1", "task-2"], [task["id"] for task in second["tasks"]])

    def test_event_stream_skips_state_build_when_hub_file_is_unchanged(self) -> None:
        async def run() -> tuple[str, str, str, int]:
            async def fast_sleep(_seconds: float) -> None:
                return None

            first_state = {"counts": {"total": 1}, "tasks": [], "senders": []}
            second_state = {"counts": {"total": 2}, "tasks": [], "senders": []}
            with (
                patch("ui.mobile.MOBILE_HEARTBEAT_SECONDS", -1.0),
                patch("ui.mobile.MOBILE_POLL_SECONDS", 0.0),
                patch("ui.mobile.asyncio.sleep", side_effect=fast_sleep),
                patch("ui.mobile.stream_hub_state_file_signature", side_effect=[(1, 10), (1, 10), (2, 20)]),
                patch("ui.mobile._build_mobile_state", side_effect=[first_state, second_state]) as build_state,
            ):
                events = _event_stream("", selected_session_name="focus")
                first = await events.__anext__()
                second = await events.__anext__()
                third = await events.__anext__()
                await events.aclose()
                return first, second, third, build_state.call_count

        first, second, third, build_count = asyncio.run(run())

        self.assertIn('"total": 1', first)
        self.assertEqual(": heartbeat\n\n", second)
        self.assertIn('"total": 2', third)
        self.assertEqual(2, build_count)

    def test_stream_signature_snapshot_does_not_build_task_payloads(self) -> None:
        raw_task = vars(_signature_task("focus-1", "focus"))

        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": [raw_task], "agents": []}),
            patch("ui.mobile.load_dashboard_state") as load_dashboard,
            patch("ui.mobile._hub_task_order_lookup") as task_order_lookup,
            patch("ui.mobile._task_payload") as task_payload,
            patch("ui.mobile.build_session_rows_page") as build_rows,
            patch("ui.mobile._load_codex_threads_cached") as load_codex_threads,
        ):
            signature = build_stream_signature_snapshot(selected_session_name="focus", task_limit=1, session_task_limit=30)

        load_dashboard.assert_not_called()
        task_order_lookup.assert_not_called()
        task_payload.assert_not_called()
        build_rows.assert_not_called()
        load_codex_threads.assert_not_called()
        self.assertIn(("focus", "1"), signature[1])

    def test_stream_signature_reuses_cached_codex_thread_signature_parts(self) -> None:
        mobile._CODEX_THREAD_DETAIL_CACHE.clear()
        mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()
        thread = {
            "id": "thread-1",
            "updated_at": "2026-07-04T20:00:00",
            "messages": [
                {"turn_id": "turn-1", "role": "user", "text": "prompt"},
                {"turn_id": "turn-1", "role": "assistant", "text": "answer"},
            ],
        }

        try:
            mobile._CODEX_THREAD_DETAIL_CACHE["thread-1"] = {
                "loaded_at": mobile.time.monotonic(),
                "thread": thread,
                "signature_parts_by_limit": {},
            }
            with (
                patch("ui.mobile._load_raw_hub_state", return_value={"tasks": [], "agents": []}),
                patch("ui.mobile.read_codex_thread", return_value=thread) as read_thread,
                patch("ui.mobile._codex_thread_task_payloads", wraps=mobile._codex_thread_task_payloads) as payloads,
            ):
                first = build_stream_signature_snapshot(selected_session_name="codex:thread-1", task_limit=1, session_task_limit=30)
                second = build_stream_signature_snapshot(selected_session_name="codex:thread-1", task_limit=1, session_task_limit=30)
        finally:
            mobile._CODEX_THREAD_DETAIL_CACHE.clear()
            mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()

        self.assertEqual(first, second)
        read_thread.assert_not_called()
        self.assertEqual(1, payloads.call_count)
        self.assertIn(("codex:thread-1", "1"), first[1])

    def test_stream_signature_tracks_latest_codex_turn_status(self) -> None:
        thread = {
            "id": "thread-runtime",
            "messages": [],
            "latest_turn_status": "interrupted",
            "latest_turn_started_at": "2026-08-30T04:00:00",
            "latest_turn_completed_at": "",
        }
        try:
            mobile._CODEX_THREAD_DETAIL_CACHE["thread-runtime"] = {
                "loaded_at": mobile.time.monotonic(),
                "thread": thread,
                "signature_parts_by_limit": {},
                "task_payloads_by_limit": {},
                "turn_count": 0,
            }
            with (
                patch("ui.mobile._load_raw_hub_state", return_value={"tasks": [], "agents": []}),
                patch("ui.mobile._codex_cached_rollout_payload", return_value={}),
            ):
                interrupted = build_stream_signature_snapshot(
                    selected_session_name="codex:thread-runtime",
                    task_limit=1,
                    session_task_limit=30,
                )
                thread["latest_turn_status"] = "completed"
                thread["latest_turn_completed_at"] = "2026-08-30T04:02:00"
                completed = build_stream_signature_snapshot(
                    selected_session_name="codex:thread-runtime",
                    task_limit=1,
                    session_task_limit=30,
                )
        finally:
            mobile._CODEX_THREAD_DETAIL_CACHE.clear()
            mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()

        self.assertEqual(("interrupted", "2026-08-30T04:00:00", ""), interrupted[-3])
        self.assertEqual(("completed", "2026-08-30T04:00:00", "2026-08-30T04:02:00"), completed[-3])
        self.assertNotEqual(interrupted, completed)

    def test_stream_signature_tracks_active_goal_updates(self) -> None:
        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": [], "agents": []}),
            patch("ui.mobile._codex_thread_signature_parts_cached", return_value=((), 0)),
            patch(
                "ui.mobile._codex_cached_rollout_payload",
                side_effect=[
                    {
                        "goal": {
                            "objective": "第一个目标",
                            "status": "active",
                            "started_at": "2026-07-18T10:00:00Z",
                            "updated_at": "2026-07-18T10:00:00Z",
                        }
                    },
                    {
                        "goal": {
                            "objective": "更新后的目标",
                            "status": "active",
                            "started_at": "2026-07-18T10:00:00Z",
                            "updated_at": "2026-07-18T10:05:00Z",
                        }
                    },
                ],
            ),
        ):
            first = build_stream_signature_snapshot(selected_session_name="codex:thread-goal", task_limit=1, session_task_limit=30)
            second = build_stream_signature_snapshot(selected_session_name="codex:thread-goal", task_limit=1, session_task_limit=30)

        self.assertNotEqual(first, second)
        self.assertEqual("第一个目标", first[-2][0])
        self.assertEqual("更新后的目标", second[-2][0])

    def test_stream_state_exposes_paused_codex_goal_and_model(self) -> None:
        paused_goal = {
            "objective": "继续完善游戏内容",
            "status": "paused",
            "started_at": "2026-07-18T10:00:00Z",
            "updated_at": "2026-07-18T10:05:00Z",
            "time_used_seconds": "300",
        }
        model_state = {"model": "gpt-5.6-sol", "reasoning_effort": "ultra", "service_tier": "default"}
        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": [], "agents": []}),
            patch("ui.mobile._load_codex_thread_cached", return_value={"id": "thread-goal", "messages": []}),
            patch("ui.mobile._codex_cached_rollout_payload", return_value={"goal": paused_goal, "model": model_state}),
            patch("ui.mobile._codex_thread_task_payloads_cached", return_value=[]),
            patch("ui.mobile._codex_thread_turn_count_cached", return_value=0),
        ):
            state = build_stream_state_snapshot(selected_session_name="codex:thread-goal", task_limit=1, session_task_limit=30)

        self.assertEqual(paused_goal, state["selected_codex_thread"]["active_goal"])
        self.assertEqual("gpt-5.6-sol", state["selected_codex_thread"]["model"])
        self.assertEqual("ultra", state["selected_codex_thread"]["reasoning_effort"])

    def test_codex_thread_signature_tracks_related_local_command_updates(self) -> None:
        mobile._CODEX_THREAD_DETAIL_CACHE.clear()
        mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()
        thread = {
            "id": "thread-1",
            "updated_at": "2026-07-04T20:00:00",
            "messages": [
                {"turn_id": "turn-1", "role": "user", "text": "prompt"},
                {"turn_id": "turn-1", "role": "assistant", "text": "answer"},
            ],
        }
        base_task = vars(_signature_task("local-command", "qq-private-1"))
        base_task.update(
            {
                "session_id": "thread-1",
                "prompt": "prompt",
                "activity_items": [{"id": "command-1", "event": "codex_command", "detail": "pytest -q"}],
            }
        )
        updated_task = {**base_task, "progress_seq": 2}

        try:
            mobile._CODEX_THREAD_DETAIL_CACHE["thread-1"] = {
                "loaded_at": mobile.time.monotonic(),
                "thread": thread,
                "signature_parts_by_limit": {},
            }
            with patch(
                "ui.mobile._load_raw_hub_state",
                side_effect=[{"tasks": [base_task], "agents": []}, {"tasks": [updated_task], "agents": []}],
            ):
                first = build_stream_signature_snapshot(selected_session_name="codex:thread-1", task_limit=1, session_task_limit=30)
                second = build_stream_signature_snapshot(selected_session_name="codex:thread-1", task_limit=1, session_task_limit=30)
        finally:
            mobile._CODEX_THREAD_DETAIL_CACHE.clear()
            mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()

        self.assertNotEqual(first, second)

    def test_stream_state_reuses_cached_codex_thread_payloads(self) -> None:
        mobile._CODEX_THREAD_DETAIL_CACHE.clear()
        mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()
        thread = {
            "id": "thread-cache",
            "updated_at": "2026-07-04T20:00:00",
            "messages": [
                {"turn_id": "turn-1", "role": "user", "text": "prompt"},
                {"turn_id": "turn-1", "role": "assistant", "text": "answer"},
            ],
        }

        try:
            mobile._CODEX_THREAD_DETAIL_CACHE["thread-cache"] = {
                "loaded_at": mobile.time.monotonic(),
                "thread": thread,
                "signature_parts_by_limit": {},
                "task_payloads_by_limit": {},
                "turn_count": None,
            }
            with (
                patch("ui.mobile._load_raw_hub_state", return_value={"tasks": [], "agents": []}),
                patch("ui.mobile._codex_thread_task_payloads", wraps=mobile._codex_thread_task_payloads) as payloads,
                patch("ui.mobile._codex_thread_turn_count", wraps=mobile._codex_thread_turn_count) as turn_count,
            ):
                first = build_stream_state_snapshot(selected_session_name="codex:thread-cache", task_limit=1, session_task_limit=30)
                first["tasks"][0]["prompt"] = "mutated"
                second = build_stream_state_snapshot(selected_session_name="codex:thread-cache", task_limit=1, session_task_limit=30)
        finally:
            mobile._CODEX_THREAD_DETAIL_CACHE.clear()
            mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()

        self.assertEqual(1, payloads.call_count)
        self.assertEqual(1, turn_count.call_count)
        self.assertEqual("prompt", second["tasks"][0]["prompt"])
        self.assertIn(("codex:thread-cache", 1), second["session_task_counts"].items())

    def test_stream_signature_starts_codex_thread_detail_load_without_blocking(self) -> None:
        mobile._CODEX_THREAD_DETAIL_CACHE.clear()
        mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()
        try:
            with (
                patch("ui.mobile._load_raw_hub_state", return_value={"tasks": [], "agents": []}),
                patch("ui.mobile.read_codex_thread", return_value={}) as read_thread,
                patch("ui.mobile._start_codex_thread_detail_load") as start_load,
                patch("ui.mobile._start_codex_rollout_refresh") as start_rollout,
            ):
                signature = build_stream_signature_snapshot(selected_session_name="codex:thread-lazy", task_limit=1, session_task_limit=30)
        finally:
            mobile._CODEX_THREAD_DETAIL_CACHE.clear()
            mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()

        read_thread.assert_not_called()
        start_load.assert_called_once_with("thread-lazy")
        self.assertGreaterEqual(start_rollout.call_count, 1)
        self.assertTrue(all(item.args == ("thread-lazy",) for item in start_rollout.call_args_list))
        self.assertIn(("codex:thread-lazy", "0"), signature[1])

    def test_stream_signature_reads_rollout_cache_without_synchronous_parse(self) -> None:
        thread_id = "thread-rollout-cache"
        mobile._CODEX_THREAD_DETAIL_CACHE.clear()
        mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_REFRESH_INFLIGHT.clear()
        thread = {"id": thread_id, "messages": []}
        try:
            mobile._CODEX_THREAD_DETAIL_CACHE[thread_id] = {
                "loaded_at": mobile.time.monotonic(),
                "thread": thread,
                "signature_parts_by_limit": {},
                "task_payloads_by_limit": {},
                "turn_count": 0,
                "rollout_revision": 0,
            }
            mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE[thread_id] = {
                **mobile._empty_codex_rollout_payload(),
                "signature": ("", 0, 0),
                "path": "",
                "offset": 0,
                "checked_at": mobile.time.monotonic(),
                "goal": {
                    "objective": "保持流畅",
                    "status": "active",
                    "started_at": "2026-07-19T10:00:00Z",
                },
                "model": {"model": "gpt-5.6-sol", "reasoning_effort": "ultra"},
            }
            with (
                patch("ui.mobile._load_raw_hub_state", return_value={"tasks": [], "agents": []}),
                patch("ui.mobile._codex_raw_view_image_payload") as parse_rollout,
                patch("ui.mobile._start_codex_rollout_refresh") as start_refresh,
            ):
                signature = build_stream_signature_snapshot(
                    selected_session_name=f"codex:{thread_id}",
                    task_limit=1,
                    session_task_limit=30,
                )
        finally:
            mobile._CODEX_THREAD_DETAIL_CACHE.clear()
            mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()
            mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
            mobile._CODEX_ROLLOUT_REFRESH_INFLIGHT.clear()

        parse_rollout.assert_not_called()
        start_refresh.assert_not_called()
        self.assertEqual("保持流畅", signature[-2][0])
        self.assertEqual("gpt-5.6-sol", signature[-1][0])

    def test_cached_rollout_payload_returns_stale_data_while_scheduling_refresh(self) -> None:
        thread_id = "thread-stale-rollout"
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            rollout_path = Path(temp_dir) / f"rollout-{thread_id}.jsonl"
            rollout_path.write_text("{}\n", encoding="utf-8")
            mobile._CODEX_ROLLOUT_PATH_CACHE[thread_id] = rollout_path
            mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE[thread_id] = {
                **mobile._empty_codex_rollout_payload(),
                "signature": (str(rollout_path), rollout_path.stat().st_mtime_ns, 0),
                "path": str(rollout_path),
                "offset": 0,
                "checked_at": mobile.time.monotonic(),
                "goal": {"objective": "旧缓存仍可见", "status": "paused"},
            }
            with patch("ui.mobile._start_codex_rollout_refresh") as start_refresh:
                payload = mobile._codex_cached_rollout_payload(thread_id)

        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
        self.assertEqual("旧缓存仍可见", payload["goal"]["objective"])
        start_refresh.assert_called_once_with(thread_id)

    def test_rollout_path_priming_keeps_existing_valid_path(self) -> None:
        thread_id = "thread-existing-rollout-path"
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            first_path = Path(temp_dir) / "first.jsonl"
            second_path = Path(temp_dir) / "second.jsonl"
            first_path.write_text("{}\n", encoding="utf-8")
            second_path.write_text("{}\n", encoding="utf-8")
            mobile._CODEX_ROLLOUT_PATH_CACHE[thread_id] = first_path

            mobile._prime_codex_rollout_path(thread_id, second_path)

            self.assertEqual(first_path, mobile._CODEX_ROLLOUT_PATH_CACHE[thread_id])
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()

    def test_rollout_path_discovers_new_turn_files_and_switches_cached_path(self) -> None:
        thread_id = "thread-new-rollout-format"
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            sessions_root = Path(temp_dir) / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            old_path = sessions_root / f"rollout-2026-08-30T10-00-00-{thread_id}.jsonl"
            new_path = sessions_root / f"rollout-2026-08-30T10-01-00-{thread_id}_turn-2.jsonl"
            old_path.write_text("{}\n", encoding="utf-8")
            os.utime(old_path, ns=(1_000_000_000, 1_000_000_000))

            with patch("ui.mobile._codex_sessions_root", return_value=sessions_root):
                self.assertEqual(old_path, mobile._find_codex_rollout_jsonl(thread_id))
                mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE[thread_id] = {
                    "path": str(old_path),
                    "signature": (str(old_path), old_path.stat().st_mtime_ns, old_path.stat().st_size),
                    "checked_at": mobile.time.monotonic(),
                }
                new_path.write_text("{}\n", encoding="utf-8")
                os.utime(new_path, ns=(2_000_000_000, 2_000_000_000))
                self.assertEqual(new_path, mobile._find_codex_rollout_jsonl(thread_id))
                self.assertTrue(mobile._codex_rollout_refresh_needed(thread_id))

        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()

    def test_rollout_parses_command_execution_item_completed_event(self) -> None:
        thread_id = "thread-command-execution-item"
        turn_id = "turn-command-execution"
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()
        with TemporaryDirectory() as temp_dir:
            sessions_root = Path(temp_dir) / "codex" / "sessions"
            sessions_root.mkdir(parents=True)
            jsonl_path = sessions_root / f"rollout-2026-08-30T10-00-00-{thread_id}_{turn_id}.jsonl"
            event = {
                "type": "event_msg",
                "timestamp": "2026-08-30T10:00:03Z",
                "payload": {
                    "type": "item_completed",
                    "turn_id": turn_id,
                    "item": {
                        "type": "CommandExecution",
                        "id": "exec-command-execution",
                        "command": ["pwsh.exe", "-Command", "pytest -q"],
                        "parsed_cmd": [{"type": "unknown", "cmd": "pytest -q"}],
                        "cwd": "file:///I:/AI/chatbridge",
                        "status": "completed",
                        "aggregated_output": "31 passed",
                        "exit_code": 0,
                        "started_at_ms": 1000,
                        "completed_at_ms": 1125,
                    },
                },
            }
            jsonl_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            with patch("ui.mobile._codex_sessions_root", return_value=sessions_root):
                payload = mobile._codex_raw_view_image_payload(thread_id)

        command = payload["commands"][turn_id][0]
        self.assertEqual("pytest -q", command["detail"])
        self.assertEqual("completed", command["metadata"]["status"])
        self.assertEqual("I:/AI/chatbridge", command["metadata"]["cwd"])
        self.assertEqual("31 passed", command["metadata"]["output"])
        self.assertEqual("0", command["metadata"]["exit_code"])
        self.assertEqual("125", command["metadata"]["duration_ms"])
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()
        mobile._CODEX_ROLLOUT_PATH_CACHE.clear()

    def test_rollout_refresh_invalidates_cached_thread_payloads(self) -> None:
        thread_id = "thread-rollout-revision"
        mobile._CODEX_THREAD_DETAIL_CACHE.clear()
        mobile._CODEX_ROLLOUT_REFRESH_INFLIGHT.clear()
        mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE[thread_id] = {"signature": ("old", 1, 1)}
        mobile._CODEX_THREAD_DETAIL_CACHE[thread_id] = {
            "loaded_at": mobile.time.monotonic(),
            "thread": {"id": thread_id, "messages": []},
            "signature_parts_by_limit": {30: (("old",), 1)},
            "task_payloads_by_limit": {30: [{"id": "old"}]},
            "rollout_revision": 2,
        }

        def refresh_payload(_thread_id: str) -> dict[str, object]:
            mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE[thread_id] = {"signature": ("new", 2, 2)}
            return mobile._empty_codex_rollout_payload()

        try:
            with patch("ui.mobile._codex_raw_view_image_payload", side_effect=refresh_payload):
                mobile._refresh_codex_rollout_cache_now(thread_id)
            cached = mobile._CODEX_THREAD_DETAIL_CACHE[thread_id]
        finally:
            mobile._CODEX_THREAD_DETAIL_CACHE.clear()
            mobile._CODEX_ROLLOUT_REFRESH_INFLIGHT.clear()
            mobile._CODEX_VIEW_IMAGE_PREVIEW_CACHE.clear()

        self.assertEqual(3, cached["rollout_revision"])
        self.assertEqual({}, cached["signature_parts_by_limit"])
        self.assertEqual({}, cached["task_payloads_by_limit"])

    def test_stream_signature_keeps_stale_codex_thread_without_reloading_app_server(self) -> None:
        mobile._CODEX_THREAD_DETAIL_CACHE.clear()
        mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()
        thread = {
            "id": "thread-stale",
            "updated_at": "2026-07-04T20:00:00",
            "messages": [
                {"turn_id": "turn-1", "role": "user", "text": "old prompt"},
                {"turn_id": "turn-1", "role": "assistant", "text": "old answer"},
            ],
        }
        try:
            mobile._CODEX_THREAD_DETAIL_CACHE["thread-stale"] = {
                "loaded_at": mobile.time.monotonic() - mobile.CODEX_THREAD_CACHE_SECONDS - 1,
                "thread": thread,
                "signature_parts_by_limit": {},
            }
            with (
                patch("ui.mobile._load_raw_hub_state", return_value={"tasks": [], "agents": []}),
                patch("ui.mobile._start_codex_thread_detail_load") as start_load,
            ):
                state = build_stream_state_snapshot(selected_session_name="codex:thread-stale", task_limit=1, session_task_limit=30)
        finally:
            mobile._CODEX_THREAD_DETAIL_CACHE.clear()
            mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()

        start_load.assert_not_called()
        self.assertEqual(["old prompt"], [task["prompt"] for task in state["tasks"]])

    def test_codex_thread_detail_refresh_keeps_previous_messages_when_rollout_is_missing(self) -> None:
        mobile._CODEX_THREAD_DETAIL_CACHE.clear()
        mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()
        previous_messages = [
            {"turn_id": "turn-1", "role": "user", "text": "old prompt"},
            {"turn_id": "turn-1", "role": "assistant", "text": "old answer"},
        ]
        mobile._CODEX_THREAD_DETAIL_CACHE["thread-stale"] = {
            "loaded_at": mobile.time.monotonic() - mobile.CODEX_THREAD_CACHE_SECONDS - 1,
            "thread": {"id": "thread-stale", "messages": previous_messages, "cwd": "I:/AI/chatbridge"},
            "signature_parts_by_limit": {},
            "task_payloads_by_limit": {},
            "rollout_revision": 1,
        }
        try:
            with patch("ui.mobile.read_codex_thread", return_value={} ) as read_thread:
                refreshed = mobile._load_codex_thread_now("thread-stale")
        finally:
            mobile._CODEX_THREAD_DETAIL_CACHE.clear()
            mobile._CODEX_THREAD_DETAIL_INFLIGHT.clear()

        self.assertEqual(previous_messages, refreshed["messages"])
        self.assertEqual("I:/AI/chatbridge", refreshed["cwd"])
        self.assertEqual("Codex app-server thread is unavailable", refreshed["refresh_error"])
        read_thread.assert_called_once_with("thread-stale", timeout_seconds=8)

    def test_stream_hub_state_file_signature_uses_stat_only(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "hub_state.json"
            state_path.write_text('{"tasks":[]}', encoding="utf-8")
            with patch("ui.mobile.HUB_STATE_PATH", state_path):
                first_signature = stream_hub_state_file_signature()
                second_signature = stream_hub_state_file_signature()

        self.assertEqual(first_signature, second_signature)
        self.assertGreater(first_signature[0], 0)
        self.assertGreater(first_signature[1], 0)

    def test_stream_qq_current_session_uses_latest_private_binding(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "qq_conversations.json"
            state_path.write_text(
                json.dumps(
                    {
                        "qq:private:old": {
                            "current_session": "old-session",
                            "sessions": {"old-session": {"updated_at": "2026-07-12T01:00:00"}},
                        },
                        "qq:private:current": {
                            "current_session": "new-session",
                            "sessions": {"new-session": {"updated_at": "2026-07-13T01:00:00"}},
                        },
                        "qq:group:123": {
                            "current_session": "group-session",
                            "sessions": {"group-session": {"updated_at": "2026-07-14T01:00:00"}},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch("ui.mobile.QQ_CONVERSATIONS_PATH", state_path):
                current_session = stream_qq_current_session_name()

        self.assertEqual("new-session", current_session)

    def test_stream_raw_hub_state_reuses_cached_parse_for_same_signature(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "hub_state.json"
            state_path.write_text('{"tasks":[]}', encoding="utf-8")
            mobile._RAW_HUB_STATE_CACHE.clear()
            mobile._RAW_HUB_STATE_CACHE.update({"signature": None, "payload": {}})
            mobile._RAW_STREAM_WINDOW_CACHE.clear()
            mobile._RAW_STREAM_WINDOW_CACHE.update({"key": None, "window": None})
            mobile._RAW_STREAM_INDEX_CACHE.clear()
            mobile._RAW_STREAM_INDEX_CACHE.update({"key": None, "index": None})

            with (
                patch("ui.mobile.HUB_STATE_PATH", state_path),
                patch("ui.mobile.read_json", return_value={"tasks": [{"id": "task-1"}]}) as read_json,
            ):
                first = mobile._load_raw_hub_state()
                second = mobile._load_raw_hub_state()

        self.assertEqual(first, second)
        self.assertIs(first, second)
        self.assertEqual(1, read_json.call_count)

    def test_stream_raw_hub_state_rereads_when_signature_changes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "hub_state.json"
            state_path.write_text('{"tasks":[]}', encoding="utf-8")
            mobile._RAW_HUB_STATE_CACHE.clear()
            mobile._RAW_HUB_STATE_CACHE.update({"signature": None, "payload": {}})
            mobile._RAW_STREAM_WINDOW_CACHE.clear()
            mobile._RAW_STREAM_WINDOW_CACHE.update({"key": None, "window": None})

            with (
                patch("ui.mobile.HUB_STATE_PATH", state_path),
                patch("ui.mobile.read_json", side_effect=[{"tasks": [{"id": "task-1"}]}, {"tasks": [{"id": "task-2"}]}]) as read_json,
            ):
                first = mobile._load_raw_hub_state()
                state_path.write_text('{"tasks":[{"id":"changed"}]}', encoding="utf-8")
                second = mobile._load_raw_hub_state()

        self.assertEqual("task-1", first["tasks"][0]["id"])
        self.assertEqual("task-2", second["tasks"][0]["id"])
        self.assertEqual(2, read_json.call_count)

    def test_stream_raw_window_reuses_cached_window_for_same_state_and_limits(self) -> None:
        raw_state = {"tasks": [_raw_activity_task(id="task-1", session_name="focus")], "agents": []}
        mobile._RAW_STREAM_WINDOW_CACHE.clear()
        mobile._RAW_STREAM_WINDOW_CACHE.update({"key": None, "window": None})
        mobile._RAW_STREAM_INDEX_CACHE.clear()
        mobile._RAW_STREAM_INDEX_CACHE.update({"key": None, "index": None})

        with patch("ui.mobile._build_stream_raw_task_index", wraps=mobile._build_stream_raw_task_index) as build_index:
            first = mobile._stream_raw_window_from_state(raw_state, selected_session_name="focus", task_limit=1, session_task_limit=30)
            first.session_task_counts["mutated"] = 99
            second = mobile._stream_raw_window_from_state(raw_state, selected_session_name="focus", task_limit=1, session_task_limit=30)

        self.assertEqual(1, build_index.call_count)
        self.assertNotIn("mutated", second.session_task_counts)
        self.assertEqual(["task-1"], [_task["id"] for _task in second.tasks])

    def test_stream_raw_index_is_reused_when_switching_sessions(self) -> None:
        raw_state = {
            "tasks": [
                _raw_activity_task(id="focus-1", session_name="focus", created_at="2026-07-04T04:01:00"),
                _raw_activity_task(id="other-1", session_name="other", created_at="2026-07-04T04:02:00"),
                _raw_activity_task(id="latest", session_name="default", created_at="2026-07-04T04:03:00"),
            ],
            "agents": [],
        }
        mobile._RAW_STREAM_WINDOW_CACHE.clear()
        mobile._RAW_STREAM_WINDOW_CACHE.update({"key": None, "window": None})
        mobile._RAW_STREAM_INDEX_CACHE.clear()
        mobile._RAW_STREAM_INDEX_CACHE.update({"key": None, "index": None})

        with patch("ui.mobile._build_stream_raw_task_index", wraps=mobile._build_stream_raw_task_index) as build_index:
            focus = mobile._stream_raw_window_from_state(raw_state, selected_session_name="focus", task_limit=1, session_task_limit=30)
            other = mobile._stream_raw_window_from_state(raw_state, selected_session_name="other", task_limit=1, session_task_limit=30)

        self.assertEqual(1, build_index.call_count)
        self.assertEqual(["focus-1", "latest"], [_task["id"] for _task in focus.tasks])
        self.assertEqual(["other-1", "latest"], [_task["id"] for _task in other.tasks])

    def test_stream_raw_index_reuses_structure_when_task_text_changes(self) -> None:
        before = {
            "tasks": [
                _raw_activity_task(
                    id="task-1",
                    session_name="focus",
                    created_at="2026-07-04T04:01:00",
                    progress_text="first",
                )
            ],
            "agents": [],
        }
        after = {
            "tasks": [
                _raw_activity_task(
                    id="task-1",
                    session_name="focus",
                    created_at="2026-07-04T04:01:00",
                    progress_text="second",
                )
            ],
            "agents": [],
        }
        mobile._RAW_STREAM_WINDOW_CACHE.clear()
        mobile._RAW_STREAM_WINDOW_CACHE.update({"key": None, "window": None})
        mobile._RAW_STREAM_INDEX_CACHE.clear()
        mobile._RAW_STREAM_INDEX_CACHE.update({"key": None, "index": None})

        with patch("ui.mobile._build_stream_raw_task_index", wraps=mobile._build_stream_raw_task_index) as build_index:
            mobile._stream_raw_window_from_state(before, selected_session_name="focus", task_limit=1, session_task_limit=30)
            updated = mobile._stream_raw_window_from_state(after, selected_session_name="focus", task_limit=1, session_task_limit=30)

        self.assertEqual(1, build_index.call_count)
        self.assertEqual("second", updated.tasks[0]["progress_text"])

    def test_stream_state_reuses_window_after_signature_snapshot(self) -> None:
        raw_state = {"tasks": [_raw_activity_task(id="task-1", session_name="focus")], "agents": []}
        mobile._RAW_STREAM_WINDOW_CACHE.clear()
        mobile._RAW_STREAM_WINDOW_CACHE.update({"key": None, "window": None})
        mobile._RAW_STREAM_INDEX_CACHE.clear()
        mobile._RAW_STREAM_INDEX_CACHE.update({"key": None, "index": None})

        with (
            patch("ui.mobile._load_raw_hub_state", return_value=raw_state),
            patch("ui.mobile._build_stream_raw_task_index", wraps=mobile._build_stream_raw_task_index) as build_index,
        ):
            build_stream_signature_snapshot(selected_session_name="focus", task_limit=1, session_task_limit=30)
            state = build_stream_state_snapshot(selected_session_name="focus", task_limit=1, session_task_limit=30)

        self.assertEqual(1, build_index.call_count)
        self.assertEqual(["task-1"], [task["id"] for task in state["tasks"]])

    def test_stream_state_uses_bounded_task_window_without_full_order_lookup(self) -> None:
        raw_tasks = [
            _raw_activity_task(id=f"default-{index}", session_name="default", created_at=f"2026-07-04T05:{index:02d}:00")
            for index in range(20)
        ] + [
            _raw_activity_task(id=f"focus-{index}", session_name="focus", created_at=f"2026-07-04T04:{index:02d}:00")
            for index in range(3)
        ]

        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": raw_tasks, "agents": []}),
            patch("ui.mobile.load_dashboard_state") as load_dashboard,
            patch("ui.mobile._hub_task_order_lookup") as task_order_lookup,
            patch("ui.mobile.build_session_rows_page") as build_rows,
            patch("ui.mobile._load_codex_threads_cached") as load_codex_threads,
        ):
            state = build_stream_state_snapshot(selected_session_name="focus", task_limit=1, session_task_limit=2)

        load_dashboard.assert_not_called()
        task_order_lookup.assert_not_called()
        build_rows.assert_not_called()
        load_codex_threads.assert_not_called()
        self.assertEqual(23, state["counts"]["total"])
        self.assertEqual(3, state["session_task_counts"]["focus"])
        self.assertEqual(["focus-1", "focus-2", "default-19"], [task["id"] for task in state["tasks"]])

    def test_stream_state_can_skip_global_latest_when_session_is_selected(self) -> None:
        raw_tasks = [
            _raw_activity_task(id=f"default-{index}", session_name="default", created_at=f"2026-07-04T05:{index:02d}:00")
            for index in range(20)
        ] + [
            _raw_activity_task(id=f"focus-{index}", session_name="focus", created_at=f"2026-07-04T04:{index:02d}:00")
            for index in range(3)
        ]

        with patch("ui.mobile._load_raw_hub_state", return_value={"tasks": raw_tasks, "agents": []}):
            state = build_stream_state_snapshot(selected_session_name="focus", task_limit=0, session_task_limit=2)
            signature = build_stream_signature_snapshot(selected_session_name="focus", task_limit=0, session_task_limit=2)

        self.assertEqual(0, state["task_limit"])
        self.assertEqual(["focus-1", "focus-2"], [task["id"] for task in state["tasks"]])
        self.assertNotIn("default-19", str(signature))

    def test_stream_signature_ignores_unrelated_session_changes_when_session_is_selected(self) -> None:
        before = {
            "tasks": [
                _raw_activity_task(id="focus-1", session_name="focus", created_at="2026-07-04T04:01:00"),
                _raw_activity_task(id="other-1", session_name="other", created_at="2026-07-04T05:01:00"),
            ],
            "agents": [],
        }
        after = {
            "tasks": [
                *before["tasks"],
                _raw_activity_task(id="other-2", session_name="other", created_at="2026-07-04T05:02:00"),
            ],
            "agents": [],
        }

        with patch("ui.mobile._load_raw_hub_state", side_effect=[before, after]):
            first = build_stream_signature_snapshot(selected_session_name="focus", task_limit=0, session_task_limit=30)
            second = build_stream_signature_snapshot(selected_session_name="focus", task_limit=0, session_task_limit=30)

        self.assertEqual(first, second)
        self.assertEqual((("focus", "1"),), first[1])

    def test_stream_state_adds_stable_order_for_same_timestamp_tasks(self) -> None:
        raw_tasks = [
            _raw_activity_task(id="task-old", created_at="2026-07-04T05:00:00", prompt="old prompt"),
            _raw_activity_task(id="task-new", created_at="2026-07-04T05:00:00", prompt="new prompt"),
        ]

        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": raw_tasks, "agents": []}),
            patch("ui.mobile.load_dashboard_state") as load_dashboard,
            patch("ui.mobile.build_session_rows_page"),
            patch("ui.mobile._load_codex_threads_cached"),
        ):
            state = build_stream_state_snapshot(selected_session_name="focus", task_limit=1, session_task_limit=30)

        load_dashboard.assert_not_called()
        tasks = {task["id"]: task for task in state["tasks"]}
        self.assertLess(tasks["task-old"]["stream_order"], tasks["task-new"]["stream_order"])

    def test_stream_state_numbers_tasks_from_oldest_to_newest(self) -> None:
        raw_tasks = [
            _raw_activity_task(id="task-new", created_at="2026-07-04T05:20:00", prompt="new prompt"),
            _raw_activity_task(id="task-old", created_at="2026-07-04T05:10:00", prompt="old prompt"),
        ]

        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": raw_tasks, "agents": []}),
            patch("ui.mobile.load_dashboard_state") as load_dashboard,
            patch("ui.mobile.build_session_rows_page"),
            patch("ui.mobile._load_codex_threads_cached"),
        ):
            state = build_stream_state_snapshot(selected_session_name="focus", task_limit=1, session_task_limit=30)

        load_dashboard.assert_not_called()
        tasks = {task["id"]: task for task in state["tasks"]}
        self.assertLess(tasks["task-old"]["stream_order"], tasks["task-new"]["stream_order"])

    def test_stream_state_renders_raw_hub_newest_first_input_as_oldest_first(self) -> None:
        raw_tasks = [
            _raw_activity_task(id="task-newest", session_name="focus", created_at="2026-07-04T05:30:00"),
            _raw_activity_task(id="task-middle", session_name="focus", created_at="2026-07-04T05:20:00"),
            _raw_activity_task(id="task-oldest", session_name="focus", created_at="2026-07-04T05:10:00"),
        ]

        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": raw_tasks, "agents": []}),
            patch("ui.mobile.load_dashboard_state") as load_dashboard,
        ):
            state = build_stream_state_snapshot(selected_session_name="focus", task_limit=1, session_task_limit=30)

        load_dashboard.assert_not_called()
        self.assertEqual(["task-oldest", "task-middle", "task-newest"], [task["id"] for task in state["tasks"]])

    def test_stream_state_orders_non_padded_hub_times_chronologically(self) -> None:
        raw_tasks = [
            _raw_activity_task(id="task-10am", session_name="focus", created_at="2026/7/5 10:54:52"),
            _raw_activity_task(id="task-6am", session_name="focus", created_at="2026/7/5 6:59:54"),
        ]

        with (
            patch("ui.mobile._load_raw_hub_state", return_value={"tasks": raw_tasks, "agents": []}),
            patch("ui.mobile.load_dashboard_state") as load_dashboard,
        ):
            state = build_stream_state_snapshot(selected_session_name="focus", task_limit=1, session_task_limit=30)

        load_dashboard.assert_not_called()
        self.assertEqual(["task-6am", "task-10am"], [task["id"] for task in state["tasks"]])

if __name__ == "__main__":
    unittest.main()
