from __future__ import annotations

import json
import os
import re
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import quote

from ui.app import CODEX_THREAD_RUNTIME_STALE_SECONDS, CODEX_THREAD_RUNTIME_TERMINAL_GRACE_SECONDS, _codex_rollout_runtime_hint, _codex_rollout_runtime_snapshot, _codex_rollout_runtime_started_at, _load_persisted_stream_session, _normalize_stream_session_name, _persist_stream_session, _resolve_stream_request_session, _stream_initial_history_limit, _update_codex_thread_runtime_statuses, group_codex_threads_by_workspace
from ui.sections import _prepare_stream_render_context, _stream_activity_render_window, _stream_client_time, _stream_display_time, _stream_image_is_previewable, _stream_markdown, _stream_model_display_name, _stream_reasoning_effort_label, _stream_task_sort_key, _stream_task_uses_utc_naive_time, _stream_text, _stream_time_delta_ms, _stream_timeline_items, render_mobile_stream_composer_section, render_mobile_stream_section


class FakeElement:
    def __init__(self, kind: str, text: str = "", **attrs: object) -> None:
        self.kind = kind
        self.text = text
        self.attrs = attrs
        self.class_text = ""
        self.props_text = ""
        self.value = ""

    def __enter__(self) -> "FakeElement":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def classes(self, value: str) -> "FakeElement":
        self.class_text = value
        return self

    def props(self, value: str) -> "FakeElement":
        self.props_text = value
        return self

    def style(self, value: str) -> "FakeElement":
        self.attrs["style"] = value
        return self

    def set_enabled(self, value: bool) -> "FakeElement":
        self.attrs["enabled"] = value
        return self

    def set_source(self, value: str) -> "FakeElement":
        self.attrs["source"] = value
        return self

    def on(self, *_args, **_kwargs) -> "FakeElement":
        return self

    def on_value_change(self, _handler) -> "FakeElement":
        return self

    def set_value(self, value: object) -> "FakeElement":
        self.value = value
        return self

    def set_options(self, options: list | dict, *, value: object = None) -> None:
        self.attrs["options"] = options
        self.value = value

    def update(self) -> None:
        return None

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def deactivate(self) -> None:
        return None


class FakeUI:
    def __init__(self) -> None:
        self.elements: list[FakeElement] = []

    def add_body_html(self, html: str) -> None:
        self._element("body_html", html)

    def _element(self, kind: str, text: str = "", **attrs: object) -> FakeElement:
        element = FakeElement(kind, text, **attrs)
        self.elements.append(element)
        return element

    def column(self) -> FakeElement:
        return self._element("column")

    def row(self) -> FakeElement:
        return self._element("row")

    def card(self) -> FakeElement:
        return self._element("card")

    def dialog(self) -> FakeElement:
        return self._element("dialog")

    def element(self, tag: str) -> FakeElement:
        return self._element(f"element:{tag}")

    def label(self, text: str = "") -> FakeElement:
        return self._element("label", text)

    def markdown(self, content: str) -> FakeElement:
        return self._element("markdown", content)

    def button(self, text: str, on_click=None, **kwargs) -> FakeElement:
        return self._element("button", text, on_click=on_click, **kwargs)

    def textarea(self, *, label: str = "", placeholder: str = "") -> FakeElement:
        return self._element("textarea", label, placeholder=placeholder)

    def input(self, *, label: str = "", placeholder: str = "") -> FakeElement:
        return self._element("input", label, placeholder=placeholder)

    def select(self, options: list | dict, *, value=None, label: str = "", **kwargs) -> FakeElement:
        return self._element("select", label, options=options, value=value, **kwargs).set_value(value)

    def upload(self, **kwargs) -> FakeElement:
        return self._element("upload", **kwargs)

    def image(self, source: str) -> FakeElement:
        return self._element("image", source)


def _translator(key: str, **_kwargs: object) -> str:
    return key


def _noop(*_args, **_kwargs):
    return None


class StreamComposerTests(unittest.TestCase):
    def test_stream_selection_persistence_survives_ui_restart(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ui_stream_selection.json"
            session_name = "codex:019f6b52-f3a5-7d40-9660-0bd9dcbc37db"

            self.assertEqual("", _load_persisted_stream_session(path))

            _persist_stream_session(session_name, path)

            self.assertEqual(session_name, _load_persisted_stream_session(path))

            _persist_stream_session("", path)

            self.assertEqual("", _load_persisted_stream_session(path))

    def test_invalid_codex_stream_selection_is_rejected_and_self_healed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ui_stream_selection.json"
            path.write_text('{"session_name": "codex:transition"}', encoding="utf-8")

            self.assertEqual("", _normalize_stream_session_name("codex:transition"))
            self.assertEqual("", _load_persisted_stream_session(path))
            self.assertIn('"session_name": ""', path.read_text(encoding="utf-8"))

    def test_stream_request_selection_prefers_url_then_browser_cookie(self) -> None:
        url_session = "codex:019f6b52-f3a5-7d40-9660-0bd9dcbc37db"
        cookie_session = "codex:019f6b53-1ecb-78d6-bb5a-da21d249f208"

        self.assertEqual(
            url_session,
            _resolve_stream_request_session(
                has_session_query=True,
                query_session=url_session,
                cookie_session=quote(cookie_session, safe=""),
                client_session="client-session",
            ),
        )
        self.assertEqual(
            cookie_session,
            _resolve_stream_request_session(
                has_session_query=False,
                cookie_session=quote(cookie_session, safe=""),
                client_session="client-session",
            ),
        )
        self.assertEqual(
            "",
            _resolve_stream_request_session(
                has_session_query=True,
                query_session="",
                cookie_session=quote(cookie_session, safe=""),
                client_session="client-session",
            ),
        )

    def test_stream_request_selection_keeps_browser_clients_isolated(self) -> None:
        browser_a = "codex:019f6b52-f3a5-7d40-9660-0bd9dcbc37db"
        browser_b = "codex:019f6b53-1ecb-78d6-bb5a-da21d249f208"

        resolved_a = _resolve_stream_request_session(
            has_session_query=False,
            cookie_session=quote(browser_a, safe=""),
        )
        resolved_b = _resolve_stream_request_session(
            has_session_query=False,
            cookie_session=quote(browser_b, safe=""),
        )

        self.assertEqual(browser_a, resolved_a)
        self.assertEqual(browser_b, resolved_b)

    def test_stream_request_selection_does_not_inherit_global_legacy_session(self) -> None:
        self.assertEqual(
            "",
            _resolve_stream_request_session(
                has_session_query=False,
                cookie_session="",
                client_session="",
            ),
        )

    def test_stream_text_preserves_leading_indented_code_block(self) -> None:
        self.assertEqual("    first\n    second", _stream_text("    first\n    second"))
        self.assertEqual("plain", _stream_text("\nplain\r\n"))

    def test_stream_task_sort_key_orders_non_padded_times_chronologically(self) -> None:
        tasks = [
            {"id": "task-10am", "created_at": "2026/7/5 10:54:52", "stream_order": 2},
            {"id": "task-6am", "created_at": "2026/7/5 6:59:54", "stream_order": 1},
        ]

        self.assertEqual(["task-6am", "task-10am"], [task["id"] for task in sorted(tasks, key=_stream_task_sort_key)])

    def test_codex_utc_naive_times_display_as_local_time(self) -> None:
        self.assertEqual("2026-07-05T10:54:52", _stream_display_time("2026-07-05T10:54:52"))
        self.assertEqual(
            _stream_display_time("2026-07-05T10:54:52Z"),
            _stream_display_time("2026-07-05T10:54:52", assume_utc_naive=True),
        )
        self.assertEqual(
            _stream_display_time("2026-07-05T10:54:52Z"),
            _stream_display_time("2026/7/5 10:54:52", assume_utc_naive=True),
        )
        self.assertEqual(
            _stream_client_time("2026-07-05T10:54:52Z"),
            _stream_client_time("2026-07-05T10:54:52", assume_utc_naive=True),
        )
        self.assertRegex(_stream_client_time("2026-07-05T10:54:52", assume_utc_naive=True), r"[+-]\d\d:\d\d$")
        self.assertEqual(0, _stream_time_delta_ms("2026-07-05T10:54:52Z", "2026-07-05T10:54:53"))

    def test_stream_render_uses_mobile_state_times_as_already_displayable(self) -> None:
        task = {"source": "stream-web", "backend": "codex", "created_at": "2026-07-05T18:54:52"}

        assume_utc_naive = _stream_task_uses_utc_naive_time(task)

        self.assertFalse(assume_utc_naive)
        self.assertEqual(
            "2026-07-05T18:54:52",
            _stream_display_time(task["created_at"], assume_utc_naive=assume_utc_naive),
        )

    def test_codex_threads_are_grouped_by_workspace_for_sidebar(self) -> None:
        groups = group_codex_threads_by_workspace(
            [
                {
                    "id": "thread-old",
                    "session_name": "codex:thread-old",
                    "cwd": "I:/AI/chatbridge",
                    "project": "chatbridge",
                    "updated_at": "2026-07-04T20:00:00",
                },
                {
                    "id": "thread-new",
                    "session_name": "codex:thread-new",
                    "cwd": "I:/AI/chatbridge",
                    "project": "chatbridge",
                    "updated_at": "2026-07-04T21:00:00",
                },
                {
                    "id": "game-thread",
                    "session_name": "codex:game-thread",
                    "cwd": "I:/AI/chatbridge/workspace/game",
                    "project": "game",
                    "updated_at": "2026-07-04T22:00:00",
                },
                {"id": "missing-session", "cwd": "I:/AI/chatbridge"},
                "not-a-thread",
            ]
        )

        self.assertEqual(["game", "chatbridge"], [group["project"] for group in groups])
        self.assertEqual(
            ["game-thread"],
            [thread["id"] for thread in groups[0]["threads"]],
        )
        self.assertEqual(
            ["thread-new", "thread-old"],
            [thread["id"] for thread in groups[1]["threads"]],
        )

    def test_stream_renders_tasks_from_oldest_to_newest(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-new",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "new prompt",
                    "output": "new answer",
                    "summary": "new answer",
                },
                {
                    "id": "task-old",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:10:00",
                    "prompt": "old prompt",
                    "output": "old answer",
                    "summary": "old answer",
                },
            ],
            "session_task_counts": {"focus": 2},
        }

        def translator(key: str, **_kwargs: object) -> str:
            return {
                "ui.web.mobile.stream_loading": "Loading conversation.",
                "ui.web.mobile.stream_loading_hint": "Messages will appear automatically.",
                "ui.web.mobile.stream_empty": "Empty",
            }.get(key, key)

        render_mobile_stream_section(
            ui,
            translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        body_texts = [
            item.text
            for item in ui.elements
            if "cb-stream-body" in item.class_text.split()
        ]
        send_buttons = [item for item in ui.elements if "cb-composer-send-button" in item.class_text]

        self.assertEqual(["old prompt", "old answer", "new prompt", "new answer"], body_texts)
        self.assertEqual(1, len(send_buttons))
        self.assertEqual("arrow_upward", send_buttons[0].attrs["icon"])

    def test_stream_does_not_render_prompt_as_assistant_output(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-prompt-only",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "only prompt",
                    "summary": "only prompt",
                },
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(ui, _translator, mobile_state, "focus", [], _noop, _noop, _noop, _noop, _noop, _noop, _noop)

        body_texts = [item.text for item in ui.elements if "cb-stream-body" in item.class_text.split()]
        assistant_blocks = [item for item in ui.elements if "cb-stream-assistant" in item.class_text.split()]

        self.assertEqual(["only prompt"], body_texts)
        self.assertEqual([], assistant_blocks)

    def test_stream_hides_manual_load_older_before_history_limit(self) -> None:
        ui = FakeUI()
        tasks = [
            {
                "id": f"task-{index}",
                "agent_id": "qq",
                "agent_name": "QQ",
                "backend": "codex",
                "session_name": "focus",
                "status": "succeeded",
                "created_at": f"2026-07-04T05:{index:02d}:00",
                "prompt": f"prompt {index}",
                "output": f"answer {index}",
            }
            for index in range(20)
        ]
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": tasks,
            "session_task_counts": {"focus": 40},
        }

        render_mobile_stream_section(ui, _translator, mobile_state, "focus", [], _noop, _noop, _noop, _noop, _noop, _noop, _noop)

        auto_buttons = [item for item in ui.elements if "cb-stream-auto-load-older-trigger" in item.class_text]
        manual_buttons = [item for item in ui.elements if "cb-stream-load-older-button" in item.class_text]

        self.assertEqual([], auto_buttons)
        self.assertEqual([], manual_buttons)

    def test_stream_uses_explicit_order_when_timestamps_match(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "turn-z",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "stream_order": 2,
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "second prompt",
                    "output": "second answer",
                    "summary": "second answer",
                },
                {
                    "id": "turn-a",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "stream_order": 1,
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "first prompt",
                    "output": "first answer",
                    "summary": "first answer",
                },
            ],
            "session_task_counts": {"focus": 2},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        body_texts = [
            item.text
            for item in ui.elements
            if "cb-stream-body" in item.class_text.split()
        ]

        self.assertEqual(["first prompt", "first answer", "second prompt", "second answer"], body_texts)

    def test_stream_uses_timestamp_before_explicit_order(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "turn-newer-timestamp",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "stream_order": 1,
                    "created_at": "2026-07-04T05:21:00",
                    "prompt": "first prompt",
                    "output": "first answer",
                    "summary": "first answer",
                },
                {
                    "id": "turn-older-timestamp",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "stream_order": 2,
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "second prompt",
                    "output": "second answer",
                    "summary": "second answer",
                },
            ],
            "session_task_counts": {"focus": 2},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        body_texts = [
            item.text
            for item in ui.elements
            if "cb-stream-body" in item.class_text.split()
        ]

        self.assertEqual(["second prompt", "second answer", "first prompt", "first answer"], body_texts)

    def test_stream_root_carries_active_session_key_for_scroll_state(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-real",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "qq private/session",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "real prompt",
                    "output": "real answer",
                    "summary": "real answer",
                }
            ],
            "session_task_counts": {"qq private/session": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "qq private/session",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        panels = [item for item in ui.elements if "cb-agent-panel" in item.class_text.split()]

        self.assertEqual(1, len(panels))
        self.assertIn("data-stream-key=qq%20private%2Fsession", panels[0].props_text)

    def test_stream_panel_does_not_render_a_main_titlebar(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-real",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "real prompt",
                    "output": "real answer",
                    "summary": "real answer",
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        titlebars = [item for item in ui.elements if "cb-agent-titlebar" in item.class_text.split()]

        self.assertEqual([], titlebars)

    def test_selected_missing_session_stays_empty_for_new_conversation(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-real",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "real-session",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "real prompt",
                    "output": "real answer",
                    "summary": "real answer",
                }
            ],
            "session_task_counts": {"real-session": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "missing-session",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        panels = [item for item in ui.elements if "cb-agent-panel" in item.class_text.split()]
        body_texts = [
            item.text
            for item in ui.elements
            if "cb-stream-body" in item.class_text.split()
        ]

        self.assertEqual(1, len(panels))
        self.assertIn("data-stream-key=missing-session", panels[0].props_text)
        self.assertNotIn("real prompt", body_texts)
        self.assertNotIn("real answer", body_texts)

    def test_selected_missing_session_does_not_fall_back_to_latest_real_session(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "older-session-task",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "older-session",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:10:00",
                    "prompt": "older prompt",
                    "output": "older answer",
                    "summary": "older answer",
                },
                {
                    "id": "latest-session-task",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "latest-session",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "latest prompt",
                    "output": "latest answer",
                    "summary": "latest answer",
                },
            ],
            "session_task_counts": {"older-session": 1, "latest-session": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "missing-session",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        panels = [item for item in ui.elements if "cb-agent-panel" in item.class_text.split()]
        body_texts = [
            item.text
            for item in ui.elements
            if "cb-stream-body" in item.class_text.split()
        ]

        self.assertEqual(1, len(panels))
        self.assertIn("data-stream-key=missing-session", panels[0].props_text)
        self.assertNotIn("latest prompt", body_texts)
        self.assertNotIn("latest answer", body_texts)
        self.assertNotIn("older prompt", body_texts)

    def test_empty_stream_offers_default_composer_for_new_session(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [],
            "session_task_counts": {},
        }

        def translator(key: str, **_kwargs: object) -> str:
            return {
                "ui.web.mobile.stream_loading": "Loading conversation.",
                "ui.web.mobile.stream_loading_hint": "Messages will appear automatically.",
                "ui.web.mobile.stream_empty": "Empty",
            }.get(key, key)

        render_mobile_stream_section(
            ui,
            translator,
            mobile_state,
            "",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        composer_zones = [item for item in ui.elements if "cb-composer-zone" in item.class_text.split()]
        send_buttons = [item for item in ui.elements if "cb-composer-send-button" in item.class_text]

        self.assertEqual(1, len(composer_zones))
        self.assertNotIn("hidden", composer_zones[0].class_text.split())
        self.assertEqual(1, len(send_buttons))
        self.assertNotIn("disable", send_buttons[0].props_text)

    def test_selected_empty_session_uses_its_own_stream_key(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [],
            "session_task_counts": {},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "fresh-session",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        panels = [item for item in ui.elements if "cb-agent-panel" in item.class_text.split()]
        composer_zones = [item for item in ui.elements if "cb-composer-zone" in item.class_text.split()]

        self.assertEqual(1, len(panels))
        self.assertIn("data-stream-key=fresh-session", panels[0].props_text)
        self.assertEqual(1, len(composer_zones))
        self.assertNotIn("hidden", composer_zones[0].class_text.split())

        empty_texts = [
            item.text
            for item in ui.elements
            if "cb-stream-empty-text" in item.class_text.split()
        ]
        self.assertEqual(2, len(empty_texts))

    def test_loading_codex_session_does_not_show_empty_state(self) -> None:
        ui = FakeUI()
        def translator(key: str, **_kwargs: object) -> str:
            return {
                "ui.web.mobile.stream_loading": "Loading conversation.",
                "ui.web.mobile.stream_loading_hint": "Messages will appear automatically.",
                "ui.web.mobile.stream_empty": "Empty",
            }.get(key, key)

        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "selected_codex_thread": {"id": "thread-loading", "loading": True},
            "tasks": [],
            "session_task_counts": {},
        }

        render_mobile_stream_section(
            ui,
            translator,
            mobile_state,
            "codex:thread-loading",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        empty_texts = [
            item.text
            for item in ui.elements
            if "cb-stream-empty-text" in item.class_text.split()
        ]

        self.assertIn("Loading conversation.", empty_texts)
        self.assertIn("Messages will appear automatically.", empty_texts)
        self.assertNotIn("Empty", empty_texts)

    def test_stream_page_does_not_expose_named_new_session_controls(self) -> None:
        ui = FakeUI()
        opened_sessions: list[str] = []
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [],
            "session_task_counts": {},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "",
            [],
            lambda session_name: opened_sessions.append(session_name),
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        new_session_inputs = [
            item for item in ui.elements if "cb-stream-new-session-input" in item.class_text.split()
        ]
        new_session_buttons = [
            item for item in ui.elements if "cb-stream-new-session-button" in item.class_text.split()
        ]

        self.assertEqual([], new_session_inputs)
        self.assertEqual([], new_session_buttons)
        self.assertEqual([], opened_sessions)

    def test_stream_page_does_not_auto_name_new_session(self) -> None:
        ui = FakeUI()
        opened_sessions: list[str] = []
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [],
            "session_task_counts": {},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "",
            [],
            lambda session_name: opened_sessions.append(session_name),
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        new_session_inputs = [
            item for item in ui.elements if "cb-stream-new-session-input" in item.class_text.split()
        ]
        new_session_buttons = [
            item for item in ui.elements if "cb-stream-new-session-button" in item.class_text.split()
        ]

        self.assertEqual([], new_session_inputs)
        self.assertEqual([], new_session_buttons)
        self.assertEqual([], opened_sessions)

    def test_stream_page_does_not_expose_composer_new_session_action(self) -> None:
        ui = FakeUI()
        new_session_calls: list[str] = []
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [],
            "session_task_counts": {},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            lambda: new_session_calls.append("open"),
        )

        buttons = [
            item
            for item in ui.elements
            if "data-stream-new-session-action=1" in item.props_text
        ]

        self.assertEqual(0, len(buttons))
        self.assertEqual([], new_session_calls)

    def test_older_history_hides_manual_button_before_history_limit(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-new",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "new prompt",
                    "output": "new answer",
                    "summary": "new answer",
                }
            ],
            "session_task_counts": {"focus": 2},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        load_older_buttons = [item for item in ui.elements if "cb-stream-load-older-button" in item.class_text]
        load_older_wrappers = [item for item in ui.elements if "cb-stream-load-older-wrap" in item.class_text]
        auto_load_triggers = [
            item for item in ui.elements if "cb-stream-auto-load-older-trigger" in item.class_text
        ]

        self.assertEqual([], load_older_buttons)
        self.assertEqual([], load_older_wrappers)
        self.assertEqual([], auto_load_triggers)

    def test_older_history_uses_manual_button_after_history_limit(self) -> None:
        ui = FakeUI()
        tasks = [
            {
                "id": f"task-{index:02d}",
                "agent_id": "qq",
                "agent_name": "QQ",
                "backend": "codex",
                "session_name": "focus",
                "status": "succeeded",
                "created_at": f"2026-07-04T05:{index:02d}:00",
                "prompt": f"prompt {index}",
                "output": f"answer {index}",
                "summary": f"answer {index}",
            }
            for index in range(60)
        ]
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": tasks,
            "session_task_counts": {"focus": 61},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        load_older_buttons = [item for item in ui.elements if "cb-stream-load-older-button" in item.class_text]
        auto_load_triggers = [
            item for item in ui.elements if "cb-stream-auto-load-older-trigger" in item.class_text
        ]

        self.assertEqual(1, len(load_older_buttons))
        self.assertEqual([], auto_load_triggers)
        self.assertIn("data-load-older-ready=1", load_older_buttons[0].props_text)

    def test_codex_history_uses_manual_button_after_small_initial_window(self) -> None:
        ui = FakeUI()
        tasks = [
            {
                "id": f"task-{index}",
                "agent_id": "codex",
                "agent_name": "Codex",
                "backend": "codex",
                "session_name": "codex:thread-001",
                "status": "succeeded",
                "created_at": f"2026-07-04T05:2{index}:00",
                "prompt": f"prompt {index}",
                "output": f"answer {index}",
            }
            for index in range(4)
        ]
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "tasks": tasks,
            "session_task_counts": {"codex:thread-001": 5},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "codex:thread-001",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        load_older_buttons = [item for item in ui.elements if "cb-stream-load-older-button" in item.class_text]
        self.assertEqual(1, len(load_older_buttons))

    def test_stream_initial_history_window_matches_manual_history_limit(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn("STREAM_HISTORY_PAGE_SIZE = 20", source)
        self.assertIn("STREAM_CODEX_HISTORY_PAGE_SIZE = 4", source)
        self.assertIn("return STREAM_CODEX_INITIAL_HISTORY_LIMIT", source)
        self.assertIn("history_page_size = STREAM_CODEX_HISTORY_PAGE_SIZE", source)
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")
        self.assertIn("STREAM_MANUAL_HISTORY_LIMIT = 60", sections_source)
        self.assertIn("STREAM_CODEX_INITIAL_HISTORY_LIMIT = 4", sections_source)
        self.assertIn("session_total_count > max(displayed_session_count, initial_history_limit)", sections_source)
        self.assertNotIn("data-stream-auto-load-older", sections_source)

    def test_stream_uses_smaller_initial_history_limit_for_codex_sessions(self) -> None:
        self.assertEqual(60, _stream_initial_history_limit("qq-private-10001"))
        self.assertEqual(4, _stream_initial_history_limit("codex:thread-001"))

    def test_latest_task_activity_log_hides_routine_lifecycle_items(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-activity",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:20:00",
                    "finished_at": "2026-07-04T05:21:00",
                    "prompt": "do work",
                    "output": "done",
                    "summary": "done",
                    "activity_items": [
                        {
                            "event": "accepted",
                            "type": "system",
                            "at": "2026-07-04T05:20:00",
                            "detail": "",
                            "metadata": {
                                "task_id": "task-activity",
                                "agent": "qq",
                                "backend": "codex",
                                "session": "focus",
                            },
                        },
                        {
                            "event": "succeeded",
                            "type": "success",
                            "at": "2026-07-04T05:21:00",
                            "detail": "done",
                            "metadata": {
                                "task_id": "task-activity",
                                "status": "succeeded",
                            },
                        },
                        {
                            "event": "codex_tool_call",
                            "type": "info",
                            "at": "2026-07-04T05:20:30",
                            "detail": "shell: pytest",
                            "metadata": {
                                "item_type": "tool_call",
                                "name": "shell",
                            },
                        },
                    ],
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        activity_logs = [item for item in ui.elements if "cb-stream-activity-log" in item.class_text.split()]
        activity_items = [item for item in ui.elements if "cb-stream-activity-item" in item.class_text.split()]
        activity_details = [item for item in ui.elements if "data-activity-details=1" in item.props_text]
        activity_icons = [item for item in ui.elements if "cb-stream-activity-icon" in item.class_text.split()]
        activity_detail_rows = [item for item in ui.elements if "cb-stream-activity-details-row" in item.class_text.split()]
        activity_detail_labels = [item for item in ui.elements if "cb-stream-activity-details-label" in item.class_text.split()]
        activity_chevrons = [item for item in ui.elements if "cb-stream-activity-chevron" in item.class_text.split()]
        activity_dots = [item for item in ui.elements if "cb-stream-activity-dot" in item.class_text.split()]
        activity_inline_details = [item for item in ui.elements if "cb-stream-activity-detail" in item.class_text.split()]
        activity_inline_times = [item for item in ui.elements if "cb-stream-activity-time" in item.class_text.split()]
        metadata_rows = [item for item in ui.elements if "cb-stream-activity-meta-row" in item.class_text.split()]
        metadata_texts = [item for item in ui.elements if "cb-stream-activity-metadata-text" in item.class_text.split()]
        activity_messages = [item.text for item in ui.elements if "cb-stream-activity-message" in item.class_text.split()]

        self.assertEqual([], activity_logs)
        self.assertEqual([], activity_items)
        self.assertEqual([], activity_details)
        self.assertEqual([], activity_icons)
        self.assertEqual([], activity_detail_rows)
        self.assertEqual([], activity_detail_labels)
        self.assertEqual([], activity_chevrons)
        self.assertEqual([], activity_dots)
        self.assertEqual([], activity_inline_details)
        self.assertEqual([], activity_inline_times)
        self.assertEqual([], metadata_rows)
        self.assertEqual([], metadata_texts)
        self.assertNotIn("已接收任务", activity_messages)
        self.assertNotIn("任务完成", activity_messages)
        self.assertNotIn("工具调用", activity_messages)
        metadata_payload = "\n".join(item.text for item in metadata_texts)
        self.assertNotIn('"task_id": "task-activity"', metadata_payload)
        self.assertNotIn('"status": "succeeded"', metadata_payload)
        self.assertNotIn('"name": "shell"', metadata_payload)

    def test_canceled_task_error_text_is_not_rendered_as_failure_red(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-canceled",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "canceled",
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "stop",
                    "error": "user canceled",
                    "summary": "user canceled",
                },
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(ui, _translator, mobile_state, "focus", [], _noop, _noop, _noop, _noop, _noop, _noop, _noop)

        markdown_classes = [item.class_text for item in ui.elements if item.kind == "markdown"]
        self.assertTrue(any("cb-stream-markdown" in item for item in markdown_classes))
        self.assertFalse(any("cb-stream-error" in item for item in markdown_classes))

    def test_codex_activity_log_is_hidden_even_when_turn_is_historical(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:30:00",
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "tasks": [
                {
                    "id": "codex-thread-turn-1",
                    "agent_id": "codex",
                    "agent_name": "Codex",
                    "backend": "codex",
                    "source": "codex-app-server",
                    "session_name": "codex:thread-1",
                    "status": "succeeded",
                    "stream_order": 1,
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "",
                    "output": "",
                    "summary": "shell: pytest",
                    "activity_items": [
                        {
                            "event": "codex_tool_call",
                            "type": "info",
                            "at": "2026-07-04T05:20:01",
                            "detail": "shell: pytest",
                            "metadata": {"item_type": "tool_call", "name": "shell"},
                        }
                    ],
                },
                {
                    "id": "codex-thread-turn-2",
                    "agent_id": "codex",
                    "agent_name": "Codex",
                    "backend": "codex",
                    "source": "codex-app-server",
                    "session_name": "codex:thread-1",
                    "status": "succeeded",
                    "stream_order": 2,
                    "created_at": "2026-07-04T05:21:00",
                    "prompt": "next prompt",
                    "output": "next answer",
                    "summary": "next answer",
                    "activity_items": [],
                },
            ],
            "session_task_counts": {"codex:thread-1": 2},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "codex:thread-1",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        activity_logs = [item for item in ui.elements if "cb-stream-activity-log" in item.class_text.split()]
        activity_messages = [item.text for item in ui.elements if "cb-stream-activity-message" in item.class_text.split()]
        metadata_texts = [item.text for item in ui.elements if "cb-stream-activity-metadata-text" in item.class_text.split()]
        footer_labels = [item.text for item in ui.elements if "cb-stream-footer-label" in item.class_text.split()]
        body_texts = [
            item.text
            for item in ui.elements
            if "cb-stream-body" in item.class_text.split()
        ]

        self.assertEqual([], activity_logs)
        self.assertNotIn("工具调用", activity_messages)
        self.assertNotIn('"at": "2026-07-04T05:20:01"', "\n".join(metadata_texts))
        self.assertIn("2026-07-04T05:21:00", footer_labels)
        self.assertNotIn("shell: pytest", body_texts)
        self.assertEqual(["next prompt", "next answer"], body_texts)

    def test_user_image_attachment_is_marked_for_lightbox_preview(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-image",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "inspect image",
                    "images": ["I:/AI/chatbridge/.runtime/uploads/web/focus/image.png"],
                    "image_previews": [
                        {
                            "source": "/mobile-upload/web/focus/image.png",
                            "label": "image.png",
                        }
                    ],
                    "output": "done",
                    "summary": "done",
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        images = [item for item in ui.elements if item.kind == "image"]

        self.assertEqual(1, len(images))
        self.assertIn("cb-stream-image-lightbox-trigger", images[0].class_text)
        self.assertIn("data-lightbox-src=%2Fmobile-upload%2Fweb%2Ffocus%2Fimage.png", images[0].props_text)
        self.assertIn("data-lightbox-label=image.png", images[0].props_text)
        self.assertIn(".cb-stream-image-attachment {\n            width: min(72vw, 20rem);", source)
        self.assertIn("min-height: 8rem;", source)
        self.assertIn("max-height: 20rem;", source)
        self.assertNotIn(".cb-stream-image-attachment {\n                width: 3rem;", source)
        self.assertIn("border-radius: 6px;", source)
        self.assertIn("border: 1px solid var(--cb-border);", source)
        self.assertIn("background: var(--cb-surface-raised);", source)
        self.assertIn("cb-image-lightbox-stage", source)
        self.assertIn("prepareMarkdownImages", source)
        self.assertIn("prepareLightboxTriggers", source)
        self.assertIn("document.addEventListener('pointerup', openLightbox, true);", source)
        self.assertIn(".cb-stream-markdown img", source)
        self.assertIn('data-lightbox-zoom="in"', source)
        self.assertIn('data-lightbox-nav="next"', source)
        self.assertIn("overlay.__cbLightboxMoveBy = moveBy;", source)
        self.assertIn("overlay.__cbLightboxOpenTrigger = openTrigger;", source)
        self.assertIn("trigger.setAttribute('data-lightbox-key'", source)
        self.assertIn("overlay.addEventListener('touchmove', preventBrowserGesture, { passive: false });", source)
        self.assertIn("event.key === 'ArrowRight'", source)
        self.assertIn("state.swipeLast || state.pointers.get(event.pointerId)", source)
        self.assertIn("stage.addEventListener('pointermove'", source)
        self.assertIn("stage.addEventListener('wheel'", source)
        self.assertTrue(_stream_image_is_previewable("/mobile-local-image/sig/path"))
        self.assertTrue(_stream_image_is_previewable("/mobile-upload/web/image.png"))
        self.assertTrue(_stream_image_is_previewable("/mobile-codex-image/sig/thread/reference"))
        self.assertNotIn("width: 6.5rem;", source)
        self.assertNotIn("width: 5.5rem;", source)

    def test_custom_tool_image_preview_uses_collapsible_lightbox_in_output_order(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-tool-image",
                    "agent_id": "codex",
                    "agent_name": "Codex",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "验图",
                    "output": "检查前\n\n检查后",
                    "output_image_previews": [
                        {
                            "source": "/mobile-codex-image/signature/thread/reference",
                            "label": "check.png",
                            "kind": "custom_tool_image",
                        }
                    ],
                    "output_segments": [
                        {"kind": "text", "text": "检查前"},
                        {
                            "kind": "custom_tool_image",
                            "source": "/mobile-codex-image/signature/thread/reference",
                            "label": "check.png",
                        },
                        {"kind": "text", "text": "检查后"},
                    ],
                    "summary": "检查后",
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        details = [item for item in ui.elements if "cb-stream-tool-image-details" in item.class_text]
        summaries = [item for item in ui.elements if "cb-stream-tool-image-summary" in item.class_text]
        labels = [item for item in ui.elements if item.text == "查看图片"]
        images = [item for item in ui.elements if item.kind == "image"]
        markdowns = [item for item in ui.elements if item.kind == "markdown"]
        inline_wrappers = [item for item in ui.elements if "cb-stream-output-tool-image" in item.class_text]

        self.assertEqual(1, len(details))
        self.assertEqual(1, len(summaries))
        self.assertEqual(1, len(labels))
        self.assertEqual(1, len(images))
        self.assertEqual(["检查前", "检查后"], [item.text for item in markdowns])
        self.assertEqual(1, len(inline_wrappers))
        self.assertLess(ui.elements.index(markdowns[0]), ui.elements.index(details[0]))
        self.assertLess(ui.elements.index(details[0]), ui.elements.index(markdowns[1]))
        self.assertIn("cb-stream-image-lightbox-trigger", images[0].class_text)
        self.assertIn("data-lightbox-src=%2Fmobile-codex-image%2Fsignature%2Fthread%2Freference", images[0].props_text)

    def test_pending_image_attachment_uses_paseo_like_composer_pill(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-current",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "previous",
                    "output": "done",
                    "summary": "done",
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [{"path": "web/focus/image.png", "source": "/mobile-upload/web/focus/image.png", "label": "image.png"}],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        attachment_pills = [item for item in ui.elements if "cb-composer-attachment-pill" in item.class_text.split()]
        attachment_thumbs = [item for item in ui.elements if "cb-composer-attachment-thumb" in item.class_text.split()]
        attachment_names = [item for item in ui.elements if "cb-composer-attachment-name" in item.class_text.split()]
        attachment_removes = [item for item in ui.elements if "cb-composer-attachment-remove" in item.class_text.split()]

        self.assertEqual(1, len(attachment_pills))
        self.assertEqual(1, len(attachment_thumbs))
        self.assertEqual("/mobile-upload/web/focus/image.png", attachment_thumbs[0].text)
        self.assertEqual(["image.png"], [item.text for item in attachment_names])
        self.assertEqual(1, len(attachment_removes))
        self.assertIsNone(attachment_removes[0].attrs["color"])

    def test_assistant_inline_code_file_paths_are_linkified(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-file-link",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "what changed",
                    "output": (
                        "Changed `ui/app.py:12` and left `google.com` alone.\n\n"
                        "Template `sessions/<agent_id>__<session>.txt`, suffix `.txt`, and "
                        "markdown example `[x](ui/app.py)` stay plain.\n\n"
                        "```text\n"
                        "`ui/sections.py:20`\n"
                        "```\n\n"
                        "~~~python\n"
                        "`ui/mobile.py:30`\n"
                        "~~~\n\n"
                        "    `ui/app.py:99`\n"
                        "    [sections](ui/sections.py:33)"
                    ),
                    "summary": "changed files",
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        markdown_blocks = [item.text for item in ui.elements if item.kind == "markdown"]
        self.assertEqual(1, len(markdown_blocks))
        markdown_text = markdown_blocks[0]

        self.assertIn("[`ui/app.py:12`](#chatbridge-file=ui%2Fapp.py%3A12", markdown_text)
        self.assertIn("`google.com`", markdown_text)
        self.assertIn("`sessions/<agent_id>__<session>.txt`", markdown_text)
        self.assertIn("`.txt`", markdown_text)
        self.assertIn("`[x](ui/app.py)`", markdown_text)
        self.assertIn("```text\n`ui/sections.py:20`\n```", markdown_text)
        self.assertIn("~~~python\n`ui/mobile.py:30`\n~~~", markdown_text)
        self.assertIn("    `ui/app.py:99`", markdown_text)
        self.assertIn("    [sections](ui/sections.py:33)", markdown_text)
        self.assertEqual(1, markdown_text.count("#chatbridge-file="))

    def test_assistant_explicit_local_markdown_links_are_rewritten(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-explicit-file-link",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "where",
                    "output": (
                        "See [app.py](I:/AI/chatbridge/ui/app.py:12) and "
                        "[OpenAI](https://openai.com).\n\n"
                        "See [My File](<I:/AI/chatbridge/My Project/My File.py:12>) and "
                        "[Titled](I:/AI/chatbridge/My Project/My File.py \"copy title\") and "
                        "[foo](src/foo.py(12)) and [encoded](I:/AI/My%20File.py) and "
                        "[file url](file:///I:/AI/chatbridge/ui/app.py).\n"
                        "See [see [ui]](ui/app.py:12) and [escaped \\] label](ui/sections.py:34)."
                    ),
                    "summary": "links",
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        markdown_text = next(item.text for item in ui.elements if item.kind == "markdown")

        self.assertIn("[app.py](#chatbridge-file=I%3A%2FAI%2Fchatbridge%2Fui%2Fapp.py%3A12", markdown_text)
        self.assertIn("[My File](#chatbridge-file=I%3A%2FAI%2Fchatbridge%2FMy%20Project%2FMy%20File.py%3A12", markdown_text)
        self.assertIn("[Titled](#chatbridge-file=I%3A%2FAI%2Fchatbridge%2FMy%20Project%2FMy%20File.py", markdown_text)
        self.assertIn("[foo](#chatbridge-file=src%2Ffoo.py%2812%29", markdown_text)
        self.assertIn("[encoded](#chatbridge-file=I%3A%2FAI%2FMy%20File.py", markdown_text)
        self.assertIn("[file url](#chatbridge-file=file%3A%2F%2F%2FI%3A%2FAI%2Fchatbridge%2Fui%2Fapp.py", markdown_text)
        self.assertIn("[see [ui]](#chatbridge-file=ui%2Fapp.py%3A12", markdown_text)
        self.assertIn("[escaped \\] label](#chatbridge-file=ui%2Fsections.py%3A34", markdown_text)
        self.assertIn("[OpenAI](https://openai.com)", markdown_text)
        self.assertEqual(8, markdown_text.count("#chatbridge-file="))

    def test_assistant_local_image_links_open_lightbox_without_duplicate_thumbnail(self) -> None:
        with TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "initial_room.png"
            image_path.write_bytes(b"png-data")
            output = f"验证截图：\n\n- [初始测试房]({image_path.as_posix()})"
            markdown = _stream_markdown(output, _translator)

            self.assertIn("[初始测试房](#chatbridge-image=%2Fmobile-local-image%2F", markdown)
            self.assertIn("&label=%E5%88%9D%E5%A7%8B%E6%B5%8B%E8%AF%95%E6%88%BF", markdown)
            self.assertNotIn("#chatbridge-file=", markdown)

            ui = FakeUI()
            mobile_state = {
                "counts": {"running": 0, "queued": 0},
                "updated_at": "2026-07-04T05:20:00",
                "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
                "tasks": [
                    {
                        "id": "task-image-link",
                        "agent_id": "codex",
                        "agent_name": "Codex",
                        "backend": "codex",
                        "session_name": "focus",
                        "status": "succeeded",
                        "created_at": "2026-07-04T05:20:00",
                        "prompt": "验图",
                        "output": output,
                        "output_image_previews": [
                            {"source": "/mobile-local-image/signed/reference", "label": "initial_room.png", "kind": "image_link"}
                        ],
                        "summary": "验证截图",
                    }
                ],
                "session_task_counts": {"focus": 1},
            }
            render_mobile_stream_section(
                ui,
                _translator,
                mobile_state,
                "focus",
                [],
                _noop,
                _noop,
                _noop,
                _noop,
                _noop,
                _noop,
                _noop,
            )

        self.assertFalse([item for item in ui.elements if item.kind == "image"])
        rendered_markdown = next(item.text for item in ui.elements if item.kind == "markdown")
        self.assertIn("#chatbridge-image=", rendered_markdown)

    def test_stream_file_link_copy_normalizes_hash_file_urls(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn("const normalizeFileHref = (value) => {", source)
        self.assertIn("return normalizeFileHref(decodeURIComponent(encoded));", source)
        self.assertIn("return normalizeFileHref(encoded);", source)
        self.assertIn("href.toLowerCase().startsWith('file://')", source)

    def test_running_task_keeps_send_button_next_to_stop_button(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 1, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-running",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "running",
                    "created_at": "2026-07-04T05:19:00",
                    "started_at": "2026-07-04T05:19:00",
                    "prompt": "keep going",
                    "progress_text": "working",
                    "output": "",
                    "error": "",
                    "summary": "working",
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        stop_buttons = [item for item in ui.elements if "cb-composer-stop-button" in item.class_text]
        send_buttons = [item for item in ui.elements if "cb-composer-send-button" in item.class_text]
        working_loaders = [item for item in ui.elements if "cb-stream-working-loader" in item.class_text.split()]
        working_loader_dots = [item for item in ui.elements if "cb-stream-working-loader-dot" in item.class_text.split()]
        working_labels = [item for item in ui.elements if "cb-stream-working-label" in item.class_text.split()]

        self.assertEqual(1, len(stop_buttons))
        self.assertEqual(1, len(send_buttons))
        self.assertEqual(1, len(working_loaders))
        self.assertEqual(6, len(working_loader_dots))
        self.assertEqual([], working_labels)
        self.assertIn("data-task-id=task-running", stop_buttons[0].props_text)
        self.assertIn("data-composer-mode=send", send_buttons[0].props_text)
        self.assertIn("排队发送", send_buttons[0].props_text)
        self.assertEqual("keyboard_return", send_buttons[0].attrs["icon"])

    def test_external_codex_runtime_exposes_precise_thread_stop_controls(self) -> None:
        ui = FakeUI()
        cancel_calls: list[tuple[str, str]] = []
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "selected_codex_thread": {"id": "thread-live", "runtime_status": "running"},
            "tasks": [
                {
                    "id": "codex-thread-live-turn-1",
                    "agent_id": "codex",
                    "agent_name": "Codex",
                    "backend": "codex",
                    "source": "codex-app-server",
                    "session_name": "codex:thread-live",
                    "status": "succeeded",
                    "created_at": "2026-07-17T05:19:00",
                    "prompt": "上一轮问题",
                    "output": "上一段回答",
                },
                {
                    "id": "codex-thread-live-turn-2",
                    "agent_id": "codex",
                    "agent_name": "Codex",
                    "backend": "codex",
                    "source": "codex-app-server",
                    "session_name": "codex:thread-live",
                    "status": "succeeded",
                    "created_at": "2026-07-17T05:20:00",
                    "prompt": "继续",
                    "output": "",
                },
            ],
            "session_task_counts": {"codex:thread-live": 2},
        }

        def translator(key: str, **_kwargs: object) -> str:
            return {"ui.web.mobile.stream_working": "正在处理"}.get(key, key)

        render_mobile_stream_section(
            ui,
            translator,
            mobile_state,
            "codex:thread-live",
            [],
            _noop,
            _noop,
            _noop,
            lambda cancel_kind, target_id: cancel_calls.append((cancel_kind, target_id)),
            _noop,
            _noop,
            _noop,
        )

        runtime_turns = [item for item in ui.elements if "data-codex-runtime-running=1" in item.props_text]
        live_markdowns = [item for item in ui.elements if "cb-stream-live-text" in item.class_text.split()]
        current_activity = [item for item in ui.elements if "cb-stream-current-activity" in item.class_text.split()]
        working_loaders = [item for item in ui.elements if "cb-stream-working-loader" in item.class_text.split()]
        stop_buttons = [item for item in ui.elements if "cb-stream-stop-button" in item.class_text.split()]
        composer_stop_buttons = [item for item in ui.elements if "cb-composer-stop-button" in item.class_text.split()]
        turns = [item for item in ui.elements if "cb-stream-turn" in item.class_text.split()]
        send_buttons = [item for item in ui.elements if "cb-composer-send-button" in item.class_text.split()]

        self.assertEqual(1, len(runtime_turns))
        self.assertEqual(["正在处理"], [item.text for item in current_activity])
        self.assertEqual(current_activity, live_markdowns)
        self.assertIn("data-stream-text-key=codex-thread-live-turn-2%3Aactivity", current_activity[0].props_text)
        self.assertEqual(1, len(working_loaders))
        self.assertEqual(1, len(stop_buttons))
        self.assertEqual(1, len(composer_stop_buttons))
        self.assertIn("data-cancel-kind=codex_thread", stop_buttons[0].props_text)
        self.assertIn("data-cancel-id=thread-live", stop_buttons[0].props_text)
        stop_buttons[0].attrs["on_click"]()
        composer_stop_buttons[0].attrs["on_click"]()
        self.assertEqual(
            [("codex_thread", "thread-live"), ("codex_thread", "thread-live")],
            cancel_calls,
        )
        self.assertEqual(2, len(turns))
        self.assertEqual(1, len(send_buttons))
        self.assertIn("追加消息", send_buttons[0].props_text)
        self.assertEqual("arrow_upward", send_buttons[0].attrs["icon"])

    def test_external_codex_runtime_does_not_duplicate_a_real_running_task(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 1, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "selected_codex_thread": {"id": "thread-live", "runtime_status": "running"},
            "tasks": [
                {
                    "id": "task-running",
                    "agent_id": "codex",
                    "agent_name": "Codex",
                    "backend": "codex",
                    "session_name": "codex:thread-live",
                    "status": "running",
                    "created_at": "2026-07-17T05:19:00",
                    "prompt": "继续",
                    "progress_text": "正在处理",
                }
            ],
            "session_task_counts": {"codex:thread-live": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "codex:thread-live",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        runtime_turns = [item for item in ui.elements if "data-codex-runtime-running=1" in item.props_text]
        working_loaders = [item for item in ui.elements if "cb-stream-working-loader" in item.class_text.split()]

        self.assertEqual([], runtime_turns)
        self.assertEqual(1, len(working_loaders))

    def test_external_codex_runtime_shows_the_latest_reasoning_instead_of_generic_working_text(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "selected_codex_thread": {
                "id": "thread-live",
                "runtime_status": "running",
                "runtime_activity": {
                    "kind": "reasoning",
                    "text": "**Planning current activity rendering**",
                    "status": "running",
                    "at": "2026-07-17T05:20:00",
                },
            },
            "tasks": [],
            "session_task_counts": {"codex:thread-live": 0},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "codex:thread-live",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        current_activity = [item for item in ui.elements if "cb-stream-current-activity" in item.class_text.split()]

        self.assertEqual(["Planning current activity rendering"], [item.text for item in current_activity])
        self.assertIn("data-stream-placeholder=1", current_activity[0].props_text)
        self.assertNotIn("正在处理", [item.text for item in ui.elements])

    def test_external_codex_runtime_describes_the_current_command(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "selected_codex_thread": {
                "id": "thread-live",
                "runtime_status": "running",
                "runtime_activity": {
                    "kind": "command",
                    "text": "python -m unittest tests.test_stream_composer",
                    "status": "running",
                    "count": 1,
                },
            },
            "tasks": [],
            "session_task_counts": {"codex:thread-live": 0},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "codex:thread-live",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        current_activity = [item.text for item in ui.elements if "cb-stream-current-activity" in item.class_text.split()]

        self.assertEqual(["正在运行命令 · python -m unittest tests.test_stream_composer"], current_activity)

    def test_external_codex_runtime_keeps_completed_reply_time_and_adds_separate_running_time(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "selected_codex_thread": {
                "id": "thread-live",
                "runtime_status": "running",
                "runtime_started_at": "2026-07-17T05:19:45",
                "updated_at": "2026-07-17T05:20:00",
            },
            "tasks": [
                {
                    "id": "codex-thread-live-turn-1",
                    "agent_id": "codex",
                    "agent_name": "Codex",
                    "backend": "codex",
                    "source": "codex-app-server",
                    "session_name": "codex:thread-live",
                    "status": "succeeded",
                    "created_at": "2026-07-17T05:19:00",
                    "finished_at": "2026-07-17T05:19:30",
                    "prompt": "上一轮问题",
                    "output": "上一段回答",
                }
            ],
            "session_task_counts": {"codex:thread-live": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "codex:thread-live",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        turns = [item for item in ui.elements if "cb-stream-turn" in item.class_text.split()]
        runtime_turns = [item for item in ui.elements if "data-codex-runtime-running=1" in item.props_text]
        markdown_texts = [item.text for item in ui.elements if item.kind == "markdown" and "cb-stream-markdown" in item.class_text.split()]
        current_activity = [item.text for item in ui.elements if "cb-stream-current-activity" in item.class_text.split()]
        footer_labels = [item.text for item in ui.elements if "cb-stream-footer-label" in item.class_text.split()]
        running_times = [item.text for item in ui.elements if "cb-stream-running-time" in item.class_text.split()]
        live_elapsed = [item.text for item in ui.elements if "cb-stream-live-elapsed" in item.class_text.split()]
        execution_prefixes = [item.text for item in ui.elements if "cb-stream-running-duration-prefix" in item.class_text.split()]

        self.assertEqual(2, len(turns))
        self.assertEqual(1, len(runtime_turns))
        self.assertEqual(["上一段回答"], markdown_texts)
        self.assertEqual(["正在处理"], current_activity)
        self.assertTrue(any("2026-07-17T05:19:30" in text for text in footer_labels))
        self.assertEqual([], running_times)
        self.assertEqual(1, len(live_elapsed))
        self.assertEqual(["执行时长"], execution_prefixes)

    def test_external_codex_runtime_without_probe_start_uses_updated_time_for_execution_timer(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "selected_codex_thread": {
                "id": "thread-live",
                "runtime_status": "running",
                "updated_at": "2026-07-17T05:20:00",
            },
            "tasks": [],
            "session_task_counts": {"codex:thread-live": 0},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "codex:thread-live",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        running_times = [item.text for item in ui.elements if "cb-stream-running-time" in item.class_text.split()]
        live_elapsed = [item.text for item in ui.elements if "cb-stream-live-elapsed" in item.class_text.split()]
        execution_prefixes = [item.text for item in ui.elements if "cb-stream-running-duration-prefix" in item.class_text.split()]

        self.assertEqual([], running_times)
        self.assertEqual(1, len(live_elapsed))
        self.assertEqual(["执行时长"], execution_prefixes)

    def test_running_assistant_text_gets_live_typewriter_key(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 1, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-running",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "running",
                    "created_at": "2026-07-04T05:19:00",
                    "started_at": "2026-07-04T05:19:00",
                    "prompt": "keep going",
                    "progress_text": "working",
                    "output": "",
                    "error": "",
                    "summary": "working",
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        live_markdowns = [
            item for item in ui.elements if item.kind == "markdown" and "cb-stream-live-text" in item.class_text.split()
        ]

        self.assertEqual(1, len(live_markdowns))
        self.assertEqual("working", live_markdowns[0].text)
        self.assertIn("data-stream-live=1", live_markdowns[0].props_text)
        self.assertIn("data-stream-text-key=task-running", live_markdowns[0].props_text)
        self.assertNotIn("data-stream-placeholder=1", live_markdowns[0].props_text)

    def test_reasoning_uses_compact_disclosure_above_separate_live_output(self) -> None:
        ui = FakeUI()
        reasoning_text = "**开始检查项目结构** " + ("中间分析内容 " * 30) + "最新进度：正在核对流式命令输出。"
        mobile_state = {
            "counts": {"running": 1, "queued": 0},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-running",
                    "agent_id": "codex",
                    "agent_name": "Codex",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "running",
                    "created_at": "2026-07-04T05:19:00",
                    "started_at": "2026-07-04T05:19:00",
                    "prompt": "keep going",
                    "progress_text": "正在输出回答",
                    "reasoning_text": reasoning_text,
                    "output": "",
                    "error": "",
                    "summary": "正在输出回答",
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        reasoning_details = [item for item in ui.elements if "cb-stream-reasoning" in item.class_text.split()]
        reasoning_previews = [item for item in ui.elements if "cb-stream-reasoning-preview" in item.class_text.split()]
        reasoning_bodies = [item for item in ui.elements if "cb-stream-reasoning-body" in item.class_text.split()]
        expand_labels = [item for item in ui.elements if "cb-stream-reasoning-toggle-label-open" in item.class_text.split()]
        collapse_labels = [item for item in ui.elements if "cb-stream-reasoning-toggle-label-close" in item.class_text.split()]
        live_markdowns = [item for item in ui.elements if "cb-stream-live-text" in item.class_text.split()]

        self.assertEqual(1, len(reasoning_details))
        self.assertIn("data-reasoning-details=1", reasoning_details[0].props_text)
        self.assertIn("data-reasoning-key=task-running", reasoning_details[0].props_text)
        self.assertNotIn("open", reasoning_details[0].props_text.split())
        self.assertIn("cb-stream-command", reasoning_details[0].class_text.split())
        self.assertIn("cb-stream-tool-activity", reasoning_details[0].class_text.split())
        self.assertEqual([], reasoning_previews)
        self.assertEqual([], expand_labels)
        self.assertEqual([], collapse_labels)
        self.assertEqual([reasoning_text], [item.text for item in reasoning_bodies])
        self.assertEqual(["正在输出回答"], [item.text for item in live_markdowns])

    def test_reasoning_disclosure_persists_open_state_across_stream_refreshes(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn("window.__cbReasoningOpenState", source)
        self.assertIn("data-reasoning-key", source)
        self.assertIn("summary?.addEventListener('click'", source)
        self.assertIn("localStorage.setItem(reasoningStorageKey", source)
        self.assertIn("setupReasoningDisclosures();", source)

    def test_running_command_keeps_output_inside_the_compact_disclosure(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 1, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-command",
                    "agent_id": "codex",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "running",
                    "created_at": "2026-07-17T05:19:00",
                    "started_at": "2026-07-17T05:19:00",
                    "prompt": "run tests",
                    "activity_items": [
                        {
                            "id": "command-1",
                            "event": "codex_command",
                            "type": "info",
                            "at": "2026-07-17T05:19:01",
                            "detail": "pytest -q",
                            "metadata": {
                                "command": "pytest -q",
                                "cwd": "I:/AI/chatbridge",
                                "status": "inProgress",
                                "output": "INITIAL OUTPUT MARKER " + ("old output " * 30) + "latest output: collecting final tests...",
                            },
                        }
                    ],
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(ui, _translator, mobile_state, "focus", [], _noop, _noop, _noop, _noop, _noop, _noop, _noop)

        command_details = [item for item in ui.elements if "cb-stream-command" in item.class_text.split()]
        command_previews = [item.text for item in ui.elements if "cb-stream-command-preview" in item.class_text.split()]
        command_command_previews = [item.text for item in ui.elements if "cb-stream-command-single-preview" in item.class_text.split()]
        command_outputs = [item.text for item in ui.elements if "cb-stream-command-output" in item.class_text.split()]
        command_labels = [item.text for item in ui.elements if "cb-stream-tool-action" in item.class_text.split()]

        self.assertEqual(1, len(command_details))
        self.assertIn("cb-stream-command-running", command_details[0].class_text.split())
        self.assertIn("data-command-details=1", command_details[0].props_text)
        self.assertIn("data-command-key=task-command%3Acommand-1", command_details[0].props_text)
        self.assertNotIn("open", command_details[0].props_text.split())
        self.assertEqual(["pytest -q"], command_command_previews)
        self.assertEqual([], command_previews)
        self.assertEqual(["INITIAL OUTPUT MARKER " + ("old output " * 30) + "latest output: collecting final tests..."], command_outputs)
        self.assertEqual(["正在运行命令"], command_labels)

    def test_running_task_with_output_segments_keeps_execution_footer(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 1, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-segment-running",
                    "agent_id": "codex",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "running",
                    "created_at": "2026-07-18T01:00:00",
                    "prompt": "继续执行",
                    "output_segments": [{"kind": "text", "text": "阶段性输出"}],
                    "activity_items": [
                        {
                            "id": "reasoning-1",
                            "event": "codex_reasoning",
                            "type": "reasoning",
                            "detail": "正在检查",
                            "metadata": {},
                        }
                    ],
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        working_loaders = [item for item in ui.elements if "cb-stream-working-loader" in item.class_text.split()]
        working_loader_dots = [item for item in ui.elements if "cb-stream-working-loader-dot" in item.class_text.split()]
        turn_footers = [item for item in ui.elements if "cb-stream-turn-footer" in item.class_text.split()]
        running_times = [item.text for item in ui.elements if "cb-stream-running-time" in item.class_text.split()]
        live_elapsed = [item.text for item in ui.elements if "cb-stream-live-elapsed" in item.class_text.split()]
        execution_prefixes = [item.text for item in ui.elements if "cb-stream-running-duration-prefix" in item.class_text.split()]

        self.assertEqual(1, len(turn_footers))
        self.assertEqual(1, len(working_loaders))
        self.assertEqual(6, len(working_loader_dots))
        self.assertEqual([], running_times)
        self.assertEqual(1, len(live_elapsed))
        self.assertEqual(["执行时长"], execution_prefixes)

    def test_stream_activity_render_window_keeps_recent_items(self) -> None:
        items = [{"id": f"activity-{index}"} for index in range(10)]

        visible, omitted = _stream_activity_render_window(items, limit=4)

        self.assertEqual(6, omitted)
        self.assertEqual(["activity-6", "activity-7", "activity-8", "activity-9"], [item["id"] for item in visible])

    def test_reasoning_and_commands_follow_their_original_timeline(self) -> None:
        ui = FakeUI()
        first_reasoning = "第一阶段：检查项目结构。" + ("继续分析结构细节。" * 16)
        second_reasoning = "第二阶段：命令结束后核对结果。" + ("继续分析测试结果。" * 16)
        mobile_state = {
            "counts": {"running": 1, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-timeline",
                    "agent_id": "codex",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "running",
                    "created_at": "2026-07-17T05:19:00",
                    "prompt": "inspect and test",
                    "reasoning_text": f"{first_reasoning}\n\n{second_reasoning}",
                    "activity_items": [
                        {
                            "id": "reasoning-1",
                            "event": "codex_reasoning",
                            "type": "reasoning",
                            "at": "2026-07-17T05:19:01",
                            "detail": first_reasoning,
                            "metadata": {},
                        },
                        {
                            "id": "command-1",
                            "event": "codex_command",
                            "type": "success",
                            "at": "2026-07-17T05:19:02",
                            "detail": "pytest -q",
                            "metadata": {
                                "command": "pytest -q",
                                "status": "completed",
                                "output": "629 passed",
                                "exit_code": 0,
                                "durationMs": "2600",
                            },
                        },
                        {
                            "id": "reasoning-2",
                            "event": "codex_reasoning",
                            "type": "reasoning",
                            "at": "2026-07-17T05:19:03",
                            "detail": second_reasoning,
                            "metadata": {},
                        },
                    ],
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(ui, _translator, mobile_state, "focus", [], _noop, _noop, _noop, _noop, _noop, _noop, _noop)

        timeline_cards = [
            item
            for item in ui.elements
            if "cb-stream-reasoning" in item.class_text.split() or "cb-stream-command" in item.class_text.split()
        ]
        reasoning_bodies = [item.text for item in ui.elements if "cb-stream-reasoning-body" in item.class_text.split()]
        timeline_times = [item.text for item in ui.elements if "cb-stream-timeline-time" in item.class_text.split()]
        inline_times = [item.text for item in ui.elements if "cb-stream-tool-inline-meta" in item.class_text.split()]

        self.assertEqual(
            ["reasoning", "command", "reasoning"],
            ["reasoning" if "cb-stream-reasoning" in item.class_text.split() else "command" for item in timeline_cards],
        )
        self.assertEqual([first_reasoning, second_reasoning], reasoning_bodies)
        self.assertIn("data-reasoning-key=task-timeline%3Areasoning-1", timeline_cards[0].props_text)
        self.assertIn("data-command-key=task-timeline%3Acommand-1", timeline_cards[1].props_text)
        self.assertIn("data-reasoning-key=task-timeline%3Areasoning-2", timeline_cards[2].props_text)
        self.assertEqual([], timeline_times)
        self.assertEqual(
            [
                "2026-07-17T05:19:01",
                "执行时长 3 秒 · 2026-07-17T05:19:02",
                "2026-07-17T05:19:03",
            ],
            inline_times,
        )

    def test_consecutive_reasoning_items_merge_until_the_next_tool_boundary(self) -> None:
        ui = FakeUI()
        first_reasoning = "先规划检查范围。" + ("继续确认项目边界。" * 12)
        second_reasoning = "再确定读取顺序。" + ("继续安排读取步骤。" * 12)
        third_reasoning = "根据结果检查差异。" + ("继续核对实际结果。" * 12)
        fourth_reasoning = "最后准备回答。" + ("继续整理结论内容。" * 12)
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-grouped-reasoning",
                    "agent_id": "codex",
                    "backend": "codex",
                    "source": "codex-app-server",
                    "session_name": "codex:thread-1",
                    "status": "succeeded",
                    "created_at": "2026-07-17T05:19:00",
                    "prompt": "inspect",
                    "output": "done",
                    "activity_items": [
                        {"id": "reasoning-1", "event": "codex_reasoning", "detail": first_reasoning, "metadata": {}},
                        {"id": "reasoning-2", "event": "codex_reasoning", "detail": second_reasoning, "metadata": {}},
                        {
                            "id": "tool-1",
                            "event": "codex_tool_call",
                            "type": "success",
                            "detail": "node_repl.js - 读取项目",
                            "metadata": {"command": "node_repl.js - 读取项目", "status": "completed", "output": "ready"},
                        },
                        {"id": "reasoning-3", "event": "codex_reasoning", "detail": third_reasoning, "metadata": {}},
                        {"id": "reasoning-4", "event": "codex_reasoning", "detail": fourth_reasoning, "metadata": {}},
                    ],
                }
            ],
            "session_task_counts": {"codex:thread-1": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "codex:thread-1",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        timeline_cards = [
            item
            for item in ui.elements
            if "cb-stream-reasoning" in item.class_text.split() or "cb-stream-command" in item.class_text.split()
        ]
        reasoning_bodies = [item.text for item in ui.elements if "cb-stream-reasoning-body" in item.class_text.split()]

        self.assertEqual(
            ["reasoning", "tool", "reasoning"],
            [
                "reasoning"
                if "cb-stream-reasoning" in item.class_text.split()
                else "tool"
                if "cb-stream-command-tool" in item.class_text.split()
                else "command"
                for item in timeline_cards
            ],
        )
        self.assertEqual(
            [f"{first_reasoning}\n\n{second_reasoning}", f"{third_reasoning}\n\n{fourth_reasoning}"],
            reasoning_bodies,
        )
        self.assertIn("data-reasoning-key=task-grouped-reasoning%3Areasoning-1", timeline_cards[0].props_text)
        self.assertIn("data-command-key=task-grouped-reasoning%3Atool-1", timeline_cards[1].props_text)
        self.assertIn("data-reasoning-key=task-grouped-reasoning%3Areasoning-3", timeline_cards[2].props_text)

    def test_subagent_activity_aggregates_by_thread_without_changing_timeline_position(self) -> None:
        activity_items: list[dict[str, object]] = [
            {"id": "reasoning-1", "event": "codex_reasoning", "detail": "先拆分任务", "metadata": {}},
            {
                "id": "subagent-started",
                "event": "codex_subagent",
                "at": "2026-07-17T05:19:00",
                "detail": "/root/repo_audit",
                "metadata": {
                    "agent_path": "/root/repo_audit",
                    "agent_thread_id": "thread-child-1",
                    "kind": "started",
                },
            },
            {
                "id": "command-1",
                "event": "codex_command",
                "detail": "git status --short",
                "metadata": {"command": "git status --short", "status": "completed"},
            },
        ]
        activity_items.extend(
            {
                "id": f"subagent-interacted-{index}",
                "event": "codex_subagent",
                "at": "2026-07-17T05:19:02",
                "detail": "/root/repo_audit",
                "metadata": {
                    "agent_path": "/root/repo_audit",
                    "agent_thread_id": "thread-child-1",
                    "kind": "interacted",
                },
            }
            for index in range(500)
        )

        timeline = _stream_timeline_items(activity_items, reasoning_text="", task_status="running")

        self.assertEqual(["reasoning", "subagent", "command_group"], [item["kind"] for item in timeline])
        subagent = timeline[1]["item"]
        self.assertEqual("subagent:thread-child-1", subagent["id"])
        self.assertEqual("interacted", subagent["kind"])
        self.assertEqual("running", subagent["status"])
        self.assertEqual("2000", subagent["duration_ms"])

    def test_subagent_status_uses_parent_terminal_state_and_explicit_interruption(self) -> None:
        started = {
            "event": "codex_subagent",
            "detail": "/root/docs_audit",
            "metadata": {"agent_path": "/root/docs_audit", "agent_thread_id": "thread-child-1", "kind": "started"},
        }
        interrupted = {
            "event": "codex_subagent",
            "detail": "/root/test_probe",
            "metadata": {"agent_path": "/root/test_probe", "agent_thread_id": "thread-child-2", "kind": "interrupted"},
        }

        running = _stream_timeline_items([started], reasoning_text="", task_status="running")
        historical = _stream_timeline_items([started, interrupted], reasoning_text="", task_status="succeeded")

        self.assertEqual("running", running[0]["item"]["status"])
        self.assertEqual("completed", historical[0]["item"]["status"])
        self.assertEqual("interrupted", historical[1]["item"]["status"])

    def test_subagent_cards_render_one_compact_disclosure_per_thread_with_stable_keys(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-subagents",
                    "agent_id": "codex",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-17T05:19:00",
                    "prompt": "并行检查",
                    "activity_items": [
                        {
                            "id": "subagent-started",
                            "event": "codex_subagent",
                            "at": "2026-07-17T05:19:01",
                            "detail": "/root/repo_audit",
                            "metadata": {
                                "agent_path": "/root/repo_audit",
                                "agent_thread_id": "thread-child-1",
                                "kind": "started",
                            },
                        },
                        {
                            "id": "subagent-interacted",
                            "event": "codex_subagent",
                            "at": "2026-07-17T05:19:02",
                            "detail": "/root/repo_audit",
                            "metadata": {
                                "agent_path": "/root/repo_audit",
                                "agent_thread_id": "thread-child-1",
                                "kind": "interacted",
                            },
                        },
                        {
                            "id": "subagent-interrupted",
                            "event": "codex_subagent",
                            "at": "2026-07-17T05:19:03",
                            "detail": "/root/test_probe",
                            "metadata": {
                                "agent_path": "/root/test_probe",
                                "agent_thread_id": "thread-child-2",
                                "kind": "interrupted",
                            },
                        },
                    ],
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(ui, _translator, mobile_state, "focus", [], _noop, _noop, _noop, _noop, _noop, _noop, _noop)

        cards = [item for item in ui.elements if "cb-stream-subagent" in item.class_text.split()]
        labels = [item.text for item in ui.elements if "cb-stream-subagent-label" in item.class_text.split()]
        names = [item.text for item in ui.elements if "cb-stream-subagent-name" in item.class_text.split()]
        paths = [item.text for item in ui.elements if "cb-stream-subagent-path" in item.class_text.split()]
        thread_ids = [item.text for item in ui.elements if "cb-stream-subagent-thread" in item.class_text.split()]
        event_times = [item.text for item in ui.elements if "cb-stream-timeline-time" in item.class_text.split()]

        self.assertEqual(2, len(cards))
        self.assertIn("cb-stream-command-completed", cards[0].class_text.split())
        self.assertIn("cb-stream-command-interrupted", cards[1].class_text.split())
        self.assertIn("data-command-key=task-subagents%3Asubagent%3Athread-child-1", cards[0].props_text)
        self.assertIn("data-command-key=task-subagents%3Asubagent%3Athread-child-2", cards[1].props_text)
        self.assertEqual(["repo_audit 子代理已更新", "test_probe 子代理已中断"], labels)
        self.assertEqual(["repo_audit", "test_probe"], names)
        self.assertEqual(["/root/repo_audit", "/root/test_probe"], paths)
        self.assertEqual(["thread-child-1", "thread-child-2"], thread_ids)
        self.assertEqual(
            ["2026-07-17T05:19:02", "2026-07-17T05:19:03"],
            event_times,
        )

    def test_historical_tool_call_is_rendered_in_the_original_timeline(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-tool-timeline",
                    "agent_id": "codex",
                    "backend": "codex",
                    "source": "codex-app-server",
                    "session_name": "codex:thread-1",
                    "status": "succeeded",
                    "created_at": "2026-07-17T05:19:00",
                    "prompt": "inspect and test",
                    "output": "done",
                    "activity_items": [
                        {"id": "reasoning-1", "event": "codex_reasoning", "detail": "先读取项目状态", "metadata": {}},
                        {
                            "id": "tool-1",
                            "event": "codex_tool_call",
                            "type": "success",
                            "detail": "node_repl.js - 读取项目入口文档\ninspect_project()",
                            "metadata": {
                                "command": "node_repl.js - 读取项目入口文档\ninspect_project()",
                                "server": "node_repl",
                                "status": "completed",
                                "tool": "js",
                                "output": "project ready",
                                "durationMs": "34601",
                            },
                        },
                        {"id": "reasoning-2", "event": "codex_reasoning", "detail": "再核对工具结果", "metadata": {}},
                    ],
                }
            ],
            "session_task_counts": {"codex:thread-1": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "codex:thread-1",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        timeline_cards = [
            item
            for item in ui.elements
            if "cb-stream-reasoning" in item.class_text.split() or "cb-stream-command" in item.class_text.split()
        ]
        tool_actions = [item.text for item in ui.elements if "cb-stream-tool-action" in item.class_text.split()]
        section_labels = [item.text for item in ui.elements if "cb-stream-command-section-label" in item.class_text.split()]
        command_outputs = [item.text for item in ui.elements if "cb-stream-command-output" in item.class_text.split()]
        mcp_servers = [item.text for item in ui.elements if "cb-stream-command-tool-server" in item.class_text.split()]
        tool_names = [item.text for item in ui.elements if "cb-stream-command-tool-name" in item.class_text.split()]

        self.assertEqual(
            ["reasoning", "tool", "reasoning"],
            [
                "reasoning"
                if "cb-stream-reasoning" in item.class_text.split()
                else "tool"
                if "cb-stream-command-tool" in item.class_text.split()
                else "command"
                for item in timeline_cards
            ],
        )
        self.assertEqual(["读取项目入口文档"], tool_actions)
        self.assertEqual(["MCP 服务", "工具", "命令 / 调用内容", "结果"], section_labels)
        self.assertEqual(["node_repl"], mcp_servers)
        self.assertEqual(["js"], tool_names)
        self.assertEqual(["project ready"], command_outputs)
        self.assertIn("data-command-key=task-tool-timeline%3Atool-1", timeline_cards[1].props_text)
        self.assertIn("data-tool-server=node_repl", timeline_cards[1].props_text)
        self.assertIn("data-tool-name=js", timeline_cards[1].props_text)
        self.assertIn("cb-stream-command-mcp", timeline_cards[1].class_text.split())

    def test_named_tool_call_shows_the_tool_name_without_claiming_it_is_mcp(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-named-tool",
                    "agent_id": "codex",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-17T05:19:00",
                    "prompt": "查看图片",
                    "activity_items": [
                        {
                            "id": "tool-image",
                            "event": "codex_tool_call",
                            "type": "success",
                            "detail": "view_image {path: preview.png}",
                            "metadata": {
                                "command": "view_image {path: preview.png}",
                                "name": "view_image",
                                "status": "completed",
                            },
                        }
                    ],
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(ui, _translator, mobile_state, "focus", [], _noop, _noop, _noop, _noop, _noop, _noop, _noop)

        tool_actions = [item.text for item in ui.elements if "cb-stream-tool-action" in item.class_text.split()]
        tool_names = [item.text for item in ui.elements if "cb-stream-command-tool-name" in item.class_text.split()]
        cards = [item for item in ui.elements if "cb-stream-command-tool" in item.class_text.split()]

        self.assertEqual(["view_image {path: preview.png}"], tool_actions)
        self.assertEqual(["view_image"], tool_names)
        self.assertEqual(1, len(cards))
        self.assertNotIn("cb-stream-command-mcp", cards[0].class_text.split())
        self.assertIn("data-tool-name=view_image", cards[0].props_text)

    def test_timeline_keeps_mcp_calls_and_shell_commands_as_separate_entries(self) -> None:
        timeline = _stream_timeline_items(
            [
                {
                    "id": "mcp-1",
                    "event": "codex_tool_call",
                    "detail": "node_repl.js - 运行检查\nrun_checks()",
                    "metadata": {
                        "command": "node_repl.js - 运行检查\nrun_checks()",
                        "server": "node_repl",
                        "status": "completed",
                        "tool": "js",
                    },
                },
                {
                    "id": "command-1",
                    "event": "codex_command",
                    "detail": "pytest -q",
                    "metadata": {"command": "pytest -q", "status": "completed"},
                },
            ],
            reasoning_text="",
            task_status="succeeded",
        )

        self.assertEqual(["command", "command_group"], [item["kind"] for item in timeline])
        self.assertEqual(["tool", "command_group"], [item["item"]["activity_kind"] for item in timeline])
        self.assertEqual("node_repl", timeline[0]["item"]["tool_server"])
        self.assertEqual("pytest -q", timeline[1]["item"]["entries"][0]["command"])

    def test_consecutive_shell_commands_render_as_one_compact_command_group(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-command-group",
                    "agent_id": "codex",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-17T05:19:00",
                    "prompt": "运行检查",
                    "activity_items": [
                        {
                            "id": "command-1",
                            "event": "codex_command",
                            "at": "2026-07-17T05:19:01",
                            "detail": "git status --short",
                            "metadata": {
                                "command": "git status --short",
                                "durationMs": "1000",
                                "status": "completed",
                            },
                        },
                        {
                            "id": "command-2",
                            "event": "codex_command",
                            "at": "2026-07-17T05:19:03",
                            "detail": "pytest -q",
                            "metadata": {
                                "command": "pytest -q",
                                "durationMs": "2000",
                                "output": "657 passed",
                                "status": "completed",
                            },
                        },
                    ],
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(ui, _translator, mobile_state, "focus", [], _noop, _noop, _noop, _noop, _noop, _noop, _noop)

        groups = [item for item in ui.elements if "cb-stream-command-group" in item.class_text.split()]
        actions = [item.text for item in ui.elements if "cb-stream-tool-action" in item.class_text.split()]
        commands = [item.text for item in ui.elements if "cb-stream-command-code" in item.class_text.split()]
        inline_meta = [item.text for item in ui.elements if "cb-stream-tool-inline-meta" in item.class_text.split()]

        self.assertEqual(1, len(groups))
        self.assertIn("data-command-count=2", groups[0].props_text)
        self.assertEqual(["运行了多个命令"], actions)
        self.assertEqual(["git status --short", "pytest -q"], commands)
        self.assertEqual(["执行时长 3 秒 · 2026-07-17T05:19:03"], inline_meta)

    def test_compact_stream_items_remove_the_old_reasoning_preview(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertNotIn(".cb-stream-reasoning-preview", source)
        self.assertRegex(source, r"\.cb-stream-command-heading\s*\{[^}]*justify-content:\s*flex-end")

    def test_stream_metadata_rows_share_the_same_right_alignment(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertRegex(source, r"\.cb-stream-timeline-time-row\s*\{[^}]*justify-content:\s*flex-end")
        self.assertNotRegex(source, r"\.cb-stream-timeline-time-row\s*\{[^}]*padding:")
        self.assertRegex(source, r"\.cb-stream-command-group-copy\s*>\s*\.cb-stream-tool-action\s*\{[^}]*flex:\s*0\s+0\s+auto")
        self.assertRegex(source, r"\.cb-stream-command-single-preview\s*\{[^}]*flex:\s*1\s+1\s+auto")
        self.assertRegex(source, r"\.cb-stream-turn-footer\s*\{[^}]*justify-content:\s*flex-end")
        self.assertRegex(source, r"\.cb-stream-user-footer\s*\{[^}]*justify-content:\s*flex-end")
        self.assertRegex(source, r"\.cb-stream-turn-footer \.cb-stream-copy-button\s*\{[^}]*margin-right:\s*auto")
        self.assertRegex(source, r"\.cb-stream-user-footer \.cb-stream-copy-button\s*\{[^}]*margin-right:\s*auto")
        self.assertRegex(source, r"\.cb-stream-running-controls\s*\{[^}]*margin-right:\s*auto")
        self.assertNotRegex(source, r"\.cb-stream-running-meta\s*\{[^}]*margin-left:\s*auto")

        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")
        controls_start = sections_source.index('with ui.element("span").classes("cb-stream-running-controls")')
        metadata_start = sections_source.index('with ui.element("span").classes("cb-stream-running-meta")', controls_start)
        stop_button_start = sections_source.index('if cancel_target_id and task.get("cancelable") is not False:', controls_start)
        self.assertLess(controls_start, metadata_start)
        self.assertLess(metadata_start, stop_button_start)

    def test_command_disclosure_persists_open_state_across_stream_refreshes(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn("window.__cbCommandOpenState", source)
        self.assertIn("data-command-key", source)
        self.assertIn("localStorage.setItem(commandStorageKey", source)
        self.assertIn("setupCommandDisclosures();", source)

    def test_short_reasoning_uses_the_same_compact_disclosure(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-finished",
                    "agent_id": "codex",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:19:00",
                    "prompt": "inspect",
                    "reasoning_text": "先检查项目结构",
                    "output": "检查完成",
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(ui, _translator, mobile_state, "focus", [], _noop, _noop, _noop, _noop, _noop, _noop, _noop)

        reasoning_details = [item for item in ui.elements if "data-reasoning-details=1" in item.props_text]
        reasoning_previews = [item.text for item in ui.elements if "cb-stream-reasoning-preview" in item.class_text.split()]
        reasoning_bodies = [item.text for item in ui.elements if "cb-stream-reasoning-body" in item.class_text.split()]

        self.assertEqual(1, len(reasoning_details))
        self.assertEqual([], reasoning_previews)
        self.assertEqual(["先检查项目结构"], reasoning_bodies)

    def test_queued_task_is_rendered_in_composer_queue_track_while_running(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 1, "queued": 1},
            "updated_at": "2026-07-04T05:20:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-running",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "running",
                    "created_at": "2026-07-04T05:19:00",
                    "started_at": "2026-07-04T05:19:00",
                    "prompt": "first",
                    "progress_text": "working",
                    "context_left_percent": 42,
                    "summary": "working",
                },
                {
                    "id": "task-queued",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "queued",
                    "created_at": "2026-07-04T05:20:00",
                    "prompt": "second",
                    "context_left_percent": 99,
                    "summary": "second",
                },
            ],
            "session_task_counts": {"focus": 2},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        queue_items = [item for item in ui.elements if "cb-composer-queue-item" in item.class_text]
        queue_texts = [item for item in ui.elements if "cb-composer-queue-text" in item.class_text]
        composer_stop_buttons = [
            item for item in ui.elements if "cb-composer-stop-button" in item.class_text
        ]
        queue_cancel_buttons = [
            item for item in ui.elements if "cb-composer-queue-cancel" in item.class_text
        ]
        queue_badges = [
            item for item in ui.elements if "cb-composer-queue-badge" in item.class_text
        ]
        stream_turns = [
            item for item in ui.elements if "cb-stream-turn" in item.class_text.split()
        ]
        context_meters = [
            item for item in ui.elements if "cb-context-meter" in item.class_text.split()
        ]
        working_loaders = [item for item in ui.elements if "cb-stream-working-loader" in item.class_text.split()]
        working_loader_dots = [item for item in ui.elements if "cb-stream-working-loader-dot" in item.class_text.split()]
        working_labels = [item for item in ui.elements if "cb-stream-working-label" in item.class_text.split()]
        status_labels = [item.text for item in ui.elements if item.kind == "label"]

        self.assertEqual(1, len(queue_items))
        self.assertIn("data-task-id=task-queued", queue_items[0].props_text)
        self.assertEqual(["second"], [item.text for item in queue_texts])
        self.assertEqual(1, len(stream_turns))
        self.assertEqual(1, len(context_meters))
        self.assertEqual(1, len(working_loaders))
        self.assertEqual(6, len(working_loader_dots))
        self.assertEqual([], working_labels)
        self.assertIn("data-context-left=42", context_meters[0].props_text)
        self.assertNotIn("running", status_labels)
        self.assertNotIn("queued", status_labels)
        self.assertEqual(1, len(composer_stop_buttons))
        self.assertIn("data-task-id=task-running", composer_stop_buttons[0].props_text)
        self.assertEqual(1, len(queue_cancel_buttons))
        self.assertIn("data-task-id=task-queued", queue_cancel_buttons[0].props_text)
        self.assertIsNone(queue_cancel_buttons[0].attrs["color"])
        self.assertEqual([], queue_badges)

    def test_active_codex_goal_is_rendered_above_composer(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "selected_codex_thread": {
                "id": "thread-goal",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "ultra",
                "active_goal": {
                    "objective": "继续完成游戏内容并保持静态验证",
                    "status": "active",
                    "started_at": "2026-07-18T10:00:00Z",
                    "updated_at": "2026-07-18T10:05:00Z",
                },
            },
            "tasks": [],
            "session_task_counts": {"codex:thread-goal": 0},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "codex:thread-goal",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            on_goal_action=_noop,
        )

        goal_details = [item for item in ui.elements if "data-goal-details=1" in item.props_text]
        goal_titles = [item.text for item in ui.elements if "cb-composer-goal-title" in item.class_text]
        goal_objectives = [item.text for item in ui.elements if "cb-composer-goal-objective" in item.class_text]
        goal_elapsed = [item for item in ui.elements if "cb-composer-goal-elapsed" in item.class_text]
        goal_actions = [item for item in ui.elements if "cb-composer-goal-action" in item.class_text.split()]
        model_indicators = [item for item in ui.elements if "cb-composer-model-indicator" in item.class_text]
        model_labels = [item.text for item in ui.elements if "cb-composer-model-name" in item.class_text]
        effort_labels = [item.text for item in ui.elements if "cb-composer-model-effort" in item.class_text]
        composer_boxes = [item for item in ui.elements if "cb-composer-box" in item.class_text.split()]

        self.assertEqual(1, len(goal_details))
        self.assertEqual(["进行中的目标"], goal_titles)
        self.assertEqual(["继续完成游戏内容并保持静态验证"], goal_objectives)
        self.assertEqual(1, len(goal_elapsed))
        self.assertIn('data-started-at="', goal_elapsed[0].props_text)
        self.assertEqual(3, len(goal_actions))
        self.assertTrue(any("data-goal-action=edit" in item.props_text for item in goal_actions))
        self.assertTrue(any("data-goal-action=pause" in item.props_text for item in goal_actions))
        self.assertTrue(any("data-goal-action=delete" in item.props_text for item in goal_actions))
        self.assertEqual(1, len(model_indicators))
        self.assertEqual(["5.6 Sol"], model_labels)
        self.assertEqual(["Ultra"], effort_labels)
        self.assertEqual(1, len(composer_boxes))
        self.assertIn("cb-composer-box-has-goal", composer_boxes[0].class_text)

    def test_paused_codex_goal_keeps_controls_and_static_elapsed_time(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "selected_codex_thread": {
                "id": "thread-goal",
                "active_goal": {
                    "objective": "暂停后仍然保留的目标",
                    "status": "paused",
                    "started_at": "2026-07-18T10:00:00Z",
                    "updated_at": "2026-07-18T10:05:00Z",
                    "time_used_seconds": "125",
                },
            },
            "tasks": [],
            "session_task_counts": {"codex:thread-goal": 0},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "codex:thread-goal",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            on_goal_action=_noop,
        )

        goal_titles = [item.text for item in ui.elements if "cb-composer-goal-title" in item.class_text]
        goal_elapsed = [item for item in ui.elements if "cb-composer-goal-elapsed" in item.class_text]
        goal_actions = [item for item in ui.elements if "cb-composer-goal-action" in item.class_text.split()]

        self.assertEqual(["已暂停的目标"], goal_titles)
        self.assertEqual(["2:05"], [item.text for item in goal_elapsed])
        self.assertNotIn("cb-stream-live-elapsed", goal_elapsed[0].class_text)
        self.assertTrue(any("data-goal-action=resume" in item.props_text for item in goal_actions))

    def test_stream_model_labels_match_codex_composer_style(self) -> None:
        self.assertEqual("5.6 Sol", _stream_model_display_name("gpt-5.6-sol"))
        self.assertEqual("5 Codex", _stream_model_display_name("gpt-5-codex"))
        self.assertEqual("XHigh", _stream_reasoning_effort_label("xhigh"))
        self.assertEqual("Max", _stream_reasoning_effort_label("max"))
        self.assertEqual("Ultra", _stream_reasoning_effort_label("ultra"))

    def test_stream_model_preference_overrides_rollout_model(self) -> None:
        context = _prepare_stream_render_context(
            {
                "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
                "tasks": [],
                "selected_codex_thread": {
                    "id": "thread-model",
                    "model": "gpt-5.2",
                    "reasoning_effort": "medium",
                },
                "composer_model_preference": {
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "ultra",
                },
            },
            "codex:thread-model",
        )

        self.assertEqual("gpt-5.6-sol", context["composer_model"])
        self.assertEqual("ultra", context["composer_reasoning_effort"])
        self.assertTrue(context["composer_model_pending"])

    def test_stream_composer_model_indicator_opens_switch_dialog(self) -> None:
        ui = FakeUI()
        render_mobile_stream_composer_section(
            ui,
            _translator,
            {
                "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
                "tasks": [],
                "selected_codex_thread": {
                    "id": "thread-model",
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "ultra",
                },
            },
            "codex:thread-model",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            model_catalog=[
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "default_reasoning": "low",
                    "reasoning_levels": ["low", "medium", "high", "ultra"],
                },
                {
                    "slug": "gpt-5.4-mini",
                    "display_name": "GPT-5.4-Mini",
                    "default_reasoning": "medium",
                    "reasoning_levels": ["low", "medium", "high"],
                },
            ],
            on_model_change=_noop,
        )

        model_buttons = [item for item in ui.elements if item.kind == "button" and "cb-composer-model-indicator" in item.class_text]
        model_selects = [item for item in ui.elements if item.kind == "select" and "cb-composer-model-select" in item.class_text]
        effort_selects = [item for item in ui.elements if item.kind == "select" and "cb-composer-effort-select" in item.class_text]
        switch_buttons = [item for item in ui.elements if "data-model-switch=1" in item.props_text]

        self.assertEqual(1, len(model_buttons))
        self.assertEqual("gpt-5.6-sol", model_selects[0].value)
        self.assertEqual("ultra", effort_selects[0].value)
        self.assertEqual(1, len(switch_buttons))

    def test_completed_codex_goal_is_not_rendered_in_composer(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "selected_codex_thread": {
                "id": "thread-goal",
                "active_goal": {
                    "objective": "已经完成的目标",
                    "status": "complete",
                    "started_at": "2026-07-18T10:00:00Z",
                    "finished_at": "2026-07-18T10:10:00Z",
                },
            },
            "tasks": [],
            "session_task_counts": {"codex:thread-goal": 0},
        }

        render_mobile_stream_section(ui, _translator, mobile_state, "codex:thread-goal", [], _noop, _noop, _noop, _noop, _noop, _noop, _noop)

        self.assertFalse([item for item in ui.elements if "data-goal-details=1" in item.props_text])
        composer_boxes = [item for item in ui.elements if "cb-composer-box" in item.class_text.split()]
        self.assertNotIn("cb-composer-box-has-goal", composer_boxes[0].class_text)


    def test_stream_scroll_observer_is_scoped_to_stream_content(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn("document.querySelector('.cb-agent-stream-content')", source)
        self.assertIn("observe(streamContent", source)
        self.assertIn("__cbStreamResizeObserver", source)
        self.assertIn("new ResizeObserver", source)
        self.assertIn("document.querySelector('.cb-composer-zone')", source)
        self.assertNotIn("document.querySelector('.cb-stream-session-creator')", source)
        self.assertNotIn("observe(document.body", source)

    def test_stream_scroll_reads_panel_key_and_runs_after_render(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertNotIn("__cbStreamHeadScrollInstalled", source)
        self.assertNotIn("__cbStreamInlineScrollInstalled", sections_source)
        self.assertNotIn("__cbStreamInlineSchedule", sections_source)
        self.assertNotIn("__cbStreamInlineObserver", sections_source)
        self.assertNotIn("cbHeadScrollReady", source)
        self.assertIn("__cbStreamAutoScrollObserver", source)
        self.assertIn("dataset?.streamKey", source)
        self.assertIn("decodeURIComponent(value || '')", source)
        self.assertIn("scroll_stream_to_bottom(active_stream_session, force_bottom=force_bottom, preserve_top=preserve_top)", source)
        self.assertIn("patch_options = {", source)
        self.assertIn('"forceBottom": bool(force_bottom)', source)
        self.assertIn('"preserveTop": bool(preserve_top)', source)
        self.assertIn("const forceBottom = options.forceBottom === true;", source)
        self.assertIn("const preserveTop = options.preserveTop === true;", source)
        self.assertIn("window.__cbStreamAfterPatch(__CB_PATCH_OPTIONS__);", source)
        self.assertIn("window.__cbStreamAfterPatch?.", source)
        self.assertIn("window.history.scrollRestoration = 'manual';", source)
        self.assertIn('"stream_scroll_runtime_clients": set()', source)
        self.assertIn('installed_clients.discard(str(getattr(client, "id", id(client))))', source)
        self.assertIn("build_stream_state_snapshot(", source)
        self.assertIn("build_stream_signature_snapshot(", source)
        self.assertNotIn("def _stream_state_signature", source)
        self.assertIn("def _stream_global_task_limit(session_name: str) -> int:", source)
        self.assertIn('return 0 if str(session_name or "").strip() else 1', source)
        self.assertIn("task_limit=_stream_global_task_limit(session_name),", source)
        self.assertIn("inferred_session = _resolve_stream_active_session(stream_state)", source)
        self.assertIn('state["selected_session_name"] = inferred_session', source)
        self.assertIn('state["stream_force_bottom_session"] = inferred_session', source)
        self.assertIn("task_limit=_stream_global_task_limit(inferred_session),", source)
        self.assertIn('model = None if state["active_page"] in {"mobile", "stream"} else refresh_model()', source)
        self.assertIn("window.__cbStreamForceBottomUntil = Date.now() + 1200;", source)
        self.assertIn("const forceBottomActive = Date.now() < Number(window.__cbStreamForceBottomUntil || 0);", source)
        self.assertIn("const shouldStickToBottom = !userScrollIntentActive && (", source)
        self.assertIn("forceBottomActive\n                        || (!state.userScrolledAway && state.nearBottom === true)", source)
        self.assertNotIn("const shouldStickToBottom = forceBottom\n", source)
        self.assertNotIn("forceBottom || streamChanged || state.userScrolledAway !== true", source)
        self.assertNotIn("ui.timer(0.05, lambda session=active_stream_session: scroll_stream_to_bottom(session), once=True)", source)
        self.assertIn("document.querySelector('.cb-agent-panel')?.dataset?.streamKey", source)
        self.assertIn("data-stream-key={encoded_active_session}", sections_source)
        self.assertIn("data-stream-pending=1", sections_source)
        self.assertIn('props("data-stream-pending=1").classes("cb-agent-stream cb-chat-scroll")', sections_source)
        self.assertIn('.cb-agent-stream[data-stream-pending="1"] .cb-agent-stream-content', source)
        self.assertIn('.cb-agent-stream[data-stream-pending="1"]::before', source)
        self.assertIn("@keyframes cb-stream-pending-spin", source)
        self.assertIn("const revealPositionedStream = () => {", source)
        self.assertIn("removeAttribute('data-stream-pending')", source)

        refresh_start = source.index("def refresh_stream")
        refresh_end = source.index("client.on_connect", refresh_start)
        refresh_body = source[refresh_start:refresh_end]
        self.assertIn("_stream_signature_snapshot()", refresh_body)
        self.assertNotIn("_stream_state_signature(", refresh_body)
        self.assertIn("_stream_state_snapshot()", refresh_body)
        self.assertIn("stream_hub_state_file_signature()", refresh_body)
        self.assertIn('next_hub_file_signature == state.get("stream_hub_state_file_signature")', refresh_body)
        self.assertIn("return", refresh_body)
        self.assertIn("codex_thread_id_from_session_name(selected_stream_session)", refresh_body)
        self.assertIn("_refresh_stream_parts(\n                                stream_state,\n                                active_stream_session,", refresh_body)
        self.assertIn("refresh_composer=False", refresh_body)
        self.assertNotIn("stream_messages_view.refresh()", refresh_body)
        self.assertNotIn("stream_composer_view.refresh()", refresh_body)
        self.assertIn("_stream_composer_signature(stream_state, active_stream_session)", refresh_body)
        self.assertNotIn("stream_panel_view.refresh()", refresh_body)
        self.assertNotIn("content_view.refresh()", refresh_body)

    def test_stream_scroll_runtime_is_installed_once_then_called_lightly(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        scroll_start = source.index("def scroll_stream_to_bottom")
        scroll_end = source.index("def install_stream_refresh_timer", scroll_start)
        scroll_body = source[scroll_start:scroll_end]

        self.assertIn('client_key = str(getattr(current_client, "id", id(current_client)))', scroll_body)
        self.assertIn('installed_clients = state.get("stream_scroll_runtime_clients")', scroll_body)
        self.assertIn('if client_key in installed_clients:', scroll_body)
        self.assertIn('ui.run_javascript(f"window.__cbStreamAfterPatch?.({patch_options_json});")', scroll_body)
        self.assertIn('installed_clients.add(client_key)', scroll_body)
        self.assertIn("window.__cbStreamPatchRuntimeReady", scroll_body)
        self.assertIn("const runtimeVersion = '10';", scroll_body)
        self.assertNotIn("window.location.reload();", scroll_body)
        self.assertIn("window.__cbStreamPatchRuntimeReady !== runtimeVersion", scroll_body)
        self.assertIn("const setupStreamWheelFallback = () => {", scroll_body)
        self.assertIn("document.addEventListener('mousewheel', handleWheel, { capture: true, passive: false });", scroll_body)
        self.assertIn("window.__cbStreamAfterPatch = (options = {}) => {", scroll_body)
        self.assertIn("setupStreamBehavior", scroll_body)
        self.assertIn("updateComposerMetrics();", scroll_body)
        self.assertIn("updateLiveExecutionTime();", scroll_body)
        self.assertNotIn("window.__cbStreamBehaviorTimer = window.setInterval(() => {\n                        setupStreamBehavior();", scroll_body)
        self.assertNotIn("triggerLoadOlderIfNeeded", scroll_body)
        self.assertNotIn("document.querySelectorAll('.cb-stream-turn')", scroll_body)
        self.assertNotIn("document.querySelectorAll('.cb-stream-copy-button')", scroll_body)
        self.assertNotIn("document.querySelectorAll('.cb-stream-markdown a[href]')", scroll_body)
        self.assertNotIn("document.querySelectorAll('.cb-stream-footer-label-wrap')", scroll_body)
        self.assertIn("prepareLightboxTriggers", scroll_body)
        self.assertNotIn("document.querySelectorAll('.cb-stream-markdown pre')", scroll_body)
        self.assertIn("window.__cbStreamCopyFeedbackDelegateReady", scroll_body)
        self.assertIn("window.__cbStreamFooterRevealDelegateReady", scroll_body)
        self.assertIn("window.__cbStreamCodeCopyDelegateReady", scroll_body)
        self.assertIn('.cb-stream-markdown a[href^="#chatbridge-file="]', scroll_body)

    def test_stream_session_switch_does_not_refresh_heavy_sidebar(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        start = source.index("def _open_stream_session(session_name")
        end = source.index("def shell_view", start)
        body = source[start:end]

        was_stream_branch = body[body.index("if was_stream_page:"):body.index("else:", body.index("if was_stream_page:"))]
        self.assertNotIn("sidebar_navigation_view.refresh()", was_stream_branch)
        self.assertIn("sidebar_navigation_view.refresh()", body)
        self.assertIn("ui.timer(0.01, refresh_selected_session, once=True)", was_stream_branch)
        self.assertIn("_refresh_stream_parts()", was_stream_branch)
        self.assertNotIn("_stream_state_snapshot()", was_stream_branch)
        self.assertNotIn("_resolve_stream_active_session(stream_state)", was_stream_branch)
        self.assertNotIn("stream_messages_view.refresh()", body)
        self.assertNotIn("stream_composer_view.refresh()", body)
        self.assertIn('panel.dataset.streamKey = {encoded_session_name!r}', body)
        self.assertIn("window.__cbSelectSidebarStreamSession?.({encoded_session_name!r})", body)
        self.assertIn("delete window.__cbStreamScrollStateByKey[{cleaned_session_name!r}]", body)
        self.assertNotIn("__cbStreamInlineDesiredKey", body)
        self.assertNotIn("stream_panel_view.refresh()", body)
        self.assertNotIn("sidebar_sessions_view.refresh()", body)

    def test_sidebar_session_selection_uses_patchable_attribute(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        sidebar_start = source.index("def sidebar_sessions_view() -> None:")
        sidebar_end = source.index("def right_sidebar_view() -> None:", sidebar_start)
        sidebar_body = source[sidebar_start:sidebar_end]
        props_start = source.index("def _stream_session_button_props(session_name: str, selected: bool) -> str:")
        props_end = source.index("def _open_stream_session_from_input(input_box) -> None:", props_start)
        props_body = source[props_start:props_end]

        self.assertIn("const selectSidebarStreamSession = (encodedSessionName) => {", source)
        self.assertIn("window.__cbSelectSidebarStreamSession = selectSidebarStreamSession", source)
        self.assertIn("link.classList.toggle('q-btn--unelevated', selected)", source)
        self.assertIn("link.classList.toggle('bg-primary', selected)", source)
        self.assertIn("data-stream-session-selected", props_body)
        self.assertIn('variant = "unelevated" if selected else "outline"', props_body)
        self.assertIn("_stream_session_button_props(session_name, selected)", sidebar_body)
        self.assertIn("_stream_session_button_props(thread_session_name, selected)", sidebar_body)
        self.assertNotIn('props = "unelevated" if selected else "outline"', sidebar_body)
        self.assertIn('.cb-stream-task-button[data-stream-session-selected="1"]', source)
        self.assertIn("cb-stream-selected-indicator", sidebar_body)
        self.assertIn("window.__cbSelectSidebarStreamSession?.", sidebar_body)

    def test_weixin_binding_navigation_refreshes_once(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        binding_start = source.index("def _open_weixin_binding(session_name")
        binding_end = source.index("def _open_weixin_binding_task", binding_start)
        binding_body = source[binding_start:binding_end]
        task_start = source.index("def _open_weixin_binding_task(task_id")
        task_end = source.index("def _switch_weixin_binding_backend", task_start)
        task_body = source[task_start:task_end]

        self.assertIn('jump_to("sessions")', binding_body)
        self.assertIn('jump_to("sessions")', task_body)
        self.assertNotIn("content_view.refresh()", binding_body)
        self.assertNotIn("content_view.refresh()", task_body)
        self.assertIn('state["load_session_files"] = False', binding_body)
        self.assertIn('state["load_task_list"] = False', binding_body)
        self.assertIn('state["load_session_files"] = False', task_body)
        self.assertIn('state["load_task_list"] = True', task_body)

    def test_stream_has_dedicated_refresh_boundary(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        start = source.index("@ui.refreshable\n    def stream_panel_view")
        end = source.index("@ui.refreshable\n    def content_view", start)
        body = source[start:end]

        self.assertIn("@ui.refreshable\n    def stream_messages_view", source)
        self.assertIn("@ui.refreshable\n    def stream_composer_view", source)
        self.assertIn("render_mobile_stream_shell(", body)
        self.assertIn("render_mobile_stream_messages_section(", source)
        self.assertIn("render_mobile_stream_composer_section(", source)
        self.assertIn('state["stream_panel_render_snapshot"] = (stream_state, active_stream_session)', body)
        self.assertIn('state["stream_panel_render_snapshot"] = None', body)
        self.assertIn('state["stream_panel_render_signature"] = refresh_signature', body)
        self.assertIn('state["stream_panel_hub_state_file_signature"] = hub_file_signature', body)
        self.assertIn('state["stream_panel_render_signature"] = None', body)
        self.assertIn('state["stream_panel_hub_state_file_signature"] = None', body)
        self.assertIn("def _stream_render_snapshot() -> tuple[dict[str, object], str]:", source)
        self.assertIn('cached_snapshot = state.get("stream_panel_render_snapshot")', source)
        self.assertNotIn("scroll_stream_to_bottom(active_stream_session, force_bottom=force_bottom)", body)
        self.assertIn("_refresh_stream_signatures(stream_state, active_stream_session)", body)
        self.assertIn("def _refresh_stream_parts(", body)
        self.assertIn("stream_composer_view.refresh()", body)
        self.assertIn("stream_messages_view.refresh()", body)
        self.assertIn("refresh_signature: tuple | None = None", body)
        self.assertIn("hub_file_signature: tuple | None = None", body)
        self.assertIn("stream_panel_view()", source)

    def test_stream_live_text_runtime_animates_suffix_diffs(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn("__cbStreamLiveTextByKey", source)
        self.assertIn("const setupLiveTypewriter = () => {", source)
        self.assertIn("document.querySelectorAll('[data-stream-live=\"1\"][data-stream-text-key]')", source)
        self.assertIn("const storedFullText = element.dataset.streamFullText || '';", source)
        self.assertIn("window.queueMicrotask || ((callback) => Promise.resolve().then(callback))", source)
        self.assertIn("fullText.startsWith(currentText)", source)
        self.assertIn("animateLiveText(element, key, fullText", source)
        self.assertIn("new MutationObserver(scheduleLiveTextSync)", source)
        self.assertNotIn("window.requestAnimationFrame(syncLiveText)", source)

    def test_stream_timer_reuses_computed_signature_during_refresh(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        refresh_start = source.index("def refresh_stream() -> None:")
        refresh_end = source.index("client.on_connect(install_initial_stream_behavior)", refresh_start)
        refresh_body = source[refresh_start:refresh_end]
        signatures_start = source.index("def _refresh_stream_signatures")
        signatures_end = source.index("@ui.refreshable\n    def stream_messages_view", signatures_start)
        signatures_body = source[signatures_start:signatures_end]

        self.assertIn("render_signature = state.get(\"stream_panel_render_signature\")", signatures_body)
        self.assertIn("render_hub_file_signature = state.get(\"stream_panel_hub_state_file_signature\")", signatures_body)
        self.assertIn("refresh_signature=next_signature", refresh_body)
        self.assertIn("hub_file_signature=next_hub_file_signature", refresh_body)

    def test_stream_updates_refresh_split_boundaries(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        upload_start = source.index("def _upload_stream_image")
        upload_end = source.index("def _remove_stream_image", upload_start)
        upload_body = source[upload_start:upload_end]
        self.assertIn("stream_composer_view.refresh()", upload_body)
        self.assertNotIn("stream_panel_view.refresh()", upload_body)

        remove_start = source.index("def _remove_stream_image")
        remove_end = source.index("def _cancel_stream_task", remove_start)
        remove_body = source[remove_start:remove_end]
        self.assertIn("stream_composer_view.refresh()", remove_body)
        self.assertNotIn("stream_panel_view.refresh()", remove_body)

        older_start = source.index("def _load_older_stream_messages")
        older_end = source.index("def _submit_stream_message", older_start)
        older_body = source[older_start:older_end]
        self.assertIn('state["stream_force_bottom_session"] = ""', older_body)
        self.assertIn('state["stream_preserve_top_session"] = cleaned_session_name', older_body)
        self.assertIn('state["selected_session_name"] = cleaned_session_name', older_body)
        self.assertIn("window.__cbStreamLoadOlderAnchor = {", older_body)
        self.assertIn("stickToBottom: Math.max(0, scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight) <= 120", older_body)
        self.assertIn("_refresh_stream_parts(refresh_composer=False, refresh_messages=True)", older_body)
        self.assertIn("scroll_stream_to_bottom(cleaned_session_name, preserve_top=True)", older_body)
        self.assertNotIn("stream_panel_view.refresh()", older_body)

    def test_sidebar_session_buttons_do_not_let_inner_labels_steal_clicks(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn(".cb-stream-task-button .q-btn__content * {", source)
        self.assertIn("pointer-events: none;", source)
        self.assertIn("data-stream-session-link", source)
        self.assertIn("__cbSidebarStreamDelegateInstalled", source)
        self.assertIn("link.matches('button, .q-btn')", source)
        self.assertNotIn("data-new-stream-session-action=1", source)
        self.assertIn('data-sidebar-close-action=1', source)
        self.assertIn("z-index: 2200;", source)
        self.assertIn("pointer-events: none;", source)

    def test_stream_page_restores_browser_scoped_session_selection(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn('has_session_query = "session" in request.query_params', source)
        self.assertIn('cookie_session=request.cookies.get(STREAM_UI_SESSION_COOKIE)', source)
        self.assertIn('client_session=state.get("selected_session_name")', source)
        self.assertNotIn('persisted_session=_load_persisted_stream_session()', source)
        self.assertIn('if has_session_query and requested_session:', source)
        self.assertIn('state["selected_session_name"] = requested_session', source)
        self.assertIn('state["stream_force_bottom_session"] = requested_session', source)
        self.assertIn('_persist_stream_session(requested_session)', source)
        self.assertIn('sync_stream_session_browser(str(state.get("selected_session_name") or ""))', source)
        self.assertIn('document.cookie = `${{cookieName}}=${{encodeURIComponent(session)}};', source)
        self.assertIn('session_name=str(state.get("selected_session_name") or "")', source)

    def test_stream_refresh_timer_never_persists_session_selection(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        refresh_start = source.index("def refresh_stream() -> None:")
        refresh_end = source.index("client.on_connect", refresh_start)

        self.assertNotIn("_persist_stream_session", source[refresh_start:refresh_end])

    def test_non_stream_navigation_removes_session_from_browser_url(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn("def jump_to(anchor: str) -> None:", source)
        self.assertIn("url.searchParams.set('page',", source)
        self.assertIn("url.searchParams.delete('session');", source)
        self.assertIn("url.searchParams.delete('page');", source)
        self.assertIn("window.history.replaceState(null, '', url.toString());", source)
        self.assertIn("document.body.classList.remove('cb-sidebar-open');", source)

    def test_page_query_and_hash_restore_preserve_active_page(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn("def apply_request_page(request) -> None:", source)
        self.assertIn('requested_page = str(request.query_params.get("page") or "").strip()', source)
        self.assertIn('state["active_page"] = page.key', source)
        self.assertIn("apply_request_page(request)", source)
        self.assertIn("const restorePageFromHash = () => {", source)
        self.assertIn("url.searchParams.set('page', hashPage);", source)
        self.assertIn("window.location.replace(url.toString());", source)
        self.assertNotIn("if (hashPage === 'stream') return;", source)

    def test_head_bootstrap_does_not_install_stream_scroll_runtime(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        head_start = source.index("window.__cbApplyTheme")
        head_end = source.index("</script>", head_start)
        head_script = source[head_start:head_end]

        self.assertIn("__cbShellBootstrapInstalled", head_script)
        self.assertIn("__cbSidebarStreamDelegateInstalled", head_script)
        self.assertIn("const restorePageFromHash = () => {", head_script)
        self.assertNotIn("__cbStreamInitialScrollInstalled", source)
        self.assertNotIn("scheduleInitialScroll", head_script)
        self.assertNotIn("document.querySelector('.cb-agent-stream')", head_script)
        self.assertNotIn("scroller.scrollTop = scroller.scrollHeight", head_script)
        self.assertNotIn("new ResizeObserver", head_script)
        self.assertIn("__cbCodexWorkspaceObserver", head_script)
        self.assertIn("codexWorkspaceObserver.observe(sidebar, {childList: true, subtree: true})", head_script)

    def test_general_ui_surfaces_handle_long_content_without_clipping(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn(".cb-code {", source)
        self.assertIn("overflow-wrap: anywhere;", source)
        self.assertIn("word-break: break-word;", source)
        self.assertIn(".nicegui-markdown,\n        .nicegui-markdown .codehilite,\n        .nicegui-markdown pre", source)
        self.assertIn(".nicegui-markdown .codehilite,\n        .nicegui-markdown pre {\n            overflow-x: auto;", source)
        self.assertIn(".nicegui-markdown code {\n            overflow-wrap: anywhere;", source)
        self.assertIn(".q-table,\n        .q-table__container,\n        .q-table__middle,\n        .q-table th,\n        .q-table td", source)
        self.assertIn("background: var(--cb-surface);\n            color: var(--cb-ink);", source)
        self.assertIn(".q-table.cb-table,\n        .cb-table .q-table__middle", source)
        self.assertIn("max-width: 100%;\n            overflow: auto;", source)
        self.assertIn(".cb-sidebar .cb-codex-workspace .cb-chip", source)
        self.assertIn("white-space: normal;", source)
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")
        self.assertIn('classes("gap-1 min-w-0 flex-1")', sections_source)
        self.assertIn('classes("font-semibold break-all")', sections_source)
        self.assertIn('classes("text-sm cb-ink break-all")', sections_source)

    def test_stream_markdown_images_are_responsive(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn(".cb-stream-markdown img {", source)
        self.assertIn("max-width: 100%;", source)
        self.assertIn("height: auto;", source)
        self.assertIn("max-height: min(60vh, 28rem);", source)

    def test_stream_markdown_rewrites_local_mobile_upload_images(self) -> None:
        markdown = _stream_markdown(
            "![图](http://127.0.0.1:8765/mobile-upload/qq/a.png)\n"
            "`http://127.0.0.1:8765/mobile-upload/qq/code.png`",
            _translator,
        )

        self.assertIn("![图](/mobile-upload/qq/a.png)", markdown)
        self.assertIn("`http://127.0.0.1:8765/mobile-upload/qq/code.png`", markdown)

    def test_stream_markdown_code_block_images_get_preview(self) -> None:
        markdown = _stream_markdown(
            "```markdown\n![图](http://127.0.0.1:8765/mobile-upload/qq/a.png)\n```",
            _translator,
        )

        self.assertIn("```markdown", markdown)
        self.assertIn("![图](http://127.0.0.1:8765/mobile-upload/qq/a.png)", markdown)
        self.assertIn("![图](/mobile-upload/qq/a.png)", markdown)

    def test_stream_markdown_rewrites_local_image_files_for_mobile(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            image_path = temp_path / "shot.png"
            upload_root = temp_path / "uploads"
            image_path.write_bytes(b"png-data")

            with patch("ui.mobile.MOBILE_UPLOAD_ROOT", upload_root):
                markdown = _stream_markdown(f"![图]({image_path.as_posix()})", _translator)

        self.assertIn("![图](/mobile-local-image/", markdown)

    def test_confirmation_dialogs_use_dark_responsive_cards(self) -> None:
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertIn("def _dialog_card(ui: UIFactoryLike", sections_source)
        self.assertIn('cb-card w-[28rem] max-w-[calc(100vw-1rem)] p-5', sections_source)
        self.assertNotIn('ui.card().classes("min-w-[28rem]")', sections_source)

    def test_qr_login_dialogs_scale_on_narrow_viewports(self) -> None:
        qr_source = Path("ui/qr_login.py").read_text(encoding="utf-8")
        qq_source = Path("ui/qq_login.py").read_text(encoding="utf-8")

        for source in (qr_source, qq_source):
            self.assertIn("max-w-[calc(100vw-1rem)]", source)
            self.assertIn("cb-panel w-full min-w-0 p-4 flex justify-center", source)
            self.assertIn("w-full max-w-72 aspect-square h-auto self-center", source)

    def test_legacy_mobile_rows_and_composer_are_responsive(self) -> None:
        source = Path("ui/mobile.py").read_text(encoding="utf-8")

        self.assertIn("main {{ padding: 12px 12px 148px;", source)
        self.assertIn(".row {{ display: flex; flex-wrap: wrap;", source)
        self.assertIn(".row > .title {{ min-width: 0; flex: 1 1 auto; }}", source)
        self.assertIn(".badge {{ display: inline-flex; align-items: center; flex: 0 0 auto;", source)
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")
        self.assertIn('task.get("output_image_previews")', sections_source)
        self.assertIn("cb-stream-image-lightbox-trigger", sections_source)
        self.assertIn("resize: none;", source)
        self.assertIn("background:#111111;color:#f5f5f0", source)
        self.assertIn("--bg: #111111;", source)
        self.assertIn("--surface: #191919;", source)
        self.assertIn("--accent: #2f6f5e;", source)
        self.assertIn("background: rgba(17,17,17,.94);", source)
        self.assertNotIn("background: #f6f7f9", source)
        self.assertNotIn("background: #fff;", source)
        self.assertNotIn("--accent: #52b788;", source)
        self.assertNotIn("--accent: #0f766e;", source)

    def test_legacy_mobile_sse_updates_only_realtime_sections(self) -> None:
        source = Path("ui/mobile.py").read_text(encoding="utf-8")

        self.assertIn("function mergeStatePayload(payload)", source)
        self.assertIn("function renderStateSections(options = {{}})", source)
        self.assertIn("function renderCurrentTab()", source)
        self.assertIn("renderCurrentTab();", source)
        self.assertNotIn("render();", source)
        self.assertNotIn("function render() {{\n      renderOverview(); renderTasks(); renderSessions(); renderSenders(); renderDiagnostics();", source)
        self.assertIn("if (!payload.sessions_loaded)", source)
        self.assertIn("merged.sessions = state.sessions;", source)
        self.assertIn("if (!payload.senders_loaded)", source)
        self.assertIn("merged.senders = state.senders;", source)
        self.assertIn('events.addEventListener("state", event => {{', source)
        event_start = source.index('events.addEventListener("state", event => {{')
        event_end = source.index("events.onerror", event_start)
        event_body = source[event_start:event_end]
        self.assertIn("renderCurrentTab();", event_body)
        self.assertNotIn("renderOverview();", event_body)
        self.assertNotIn("renderTasks();", event_body)
        self.assertNotIn("renderStateSections();", event_body)
        self.assertNotIn("renderDiagnostics();", event_body)

    def test_legacy_mobile_codex_threads_state_is_pageable(self) -> None:
        source = Path("ui/mobile.py").read_text(encoding="utf-8")

        self.assertIn('codex_threads_cursor=str(request.query_params.get("codex_threads_cursor") or "").strip()', source)
        self.assertIn('codex_threads_archived=_query_bool(request, "codex_threads_archived", False)', source)
        self.assertIn("load_codex_threads_page(\n        cursor=codex_threads_cursor,\n        archived=codex_threads_archived,", source)
        self.assertNotIn("_load_codex_threads_cached() if include_codex_threads", source)

    def test_legacy_mobile_diagnostics_loads_logs_only_on_logs_tab(self) -> None:
        source = Path("ui/mobile.py").read_text(encoding="utf-8")

        self.assertIn("def _build_mobile_diagnostics(*, include_logs: bool = False, include_external: bool = False)", source)
        self.assertIn('include_logs=_query_bool(request, "include_logs", False)', source)
        self.assertIn('include_external=_query_bool(request, "include_external", False)', source)
        self.assertIn("if (includeLogs) params.set(\"include_logs\", \"1\");", source)
        self.assertIn("if (includeExternal) params.set(\"include_external\", \"1\");", source)
        self.assertIn('loadDiagnostics({{ includeLogs: button.dataset.tab === "logs" }}', source)
        self.assertIn('loadDiagnostics({{ includeLogs: currentTab === "logs" }}', source)
        self.assertNotIn("const res = await fetch(`/api/mobile/diagnostics?token=", source)

    def test_stream_initial_scroll_runs_after_socket_connection(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn("def install_initial_stream_behavior() -> None:", source)
        self.assertIn("client.on_connect(install_initial_stream_behavior)", source)
        self.assertIn("ui.timer(0.1, install_initial_stream_behavior, once=True)", source)
        initial_start = source.index("def install_initial_stream_behavior() -> None:")
        initial_end = source.index("def refresh_stream() -> None:", initial_start)
        initial_body = source[initial_start:initial_end]
        self.assertIn('state["stream_force_bottom_next"] = True', initial_body)
        self.assertIn("_refresh_stream_parts(\n                    stream_state,\n                    active_stream_session,", initial_body)

    def test_programmatic_stream_scroll_is_not_marked_as_user_scroll(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertIn("const programmaticScrollers = window.__cbStreamProgrammaticScrollers || new WeakSet();", source)
        self.assertIn("const markProgrammaticScroll = (scroller) => {", source)
        self.assertIn("window.requestAnimationFrame(() => programmaticScrollers.delete(scroller));", source)
        self.assertIn("const isProgrammaticScroll = (scroller) => programmaticScrollers.has(scroller);", source)
        self.assertIn("const scrollWindowToBottom = () => {", source)
        self.assertIn("const scrollToBottom = (scroller) => {", source)
        self.assertIn("scrollWindowToBottom();", source)
        self.assertIn("if (source === 'user' && isProgrammaticScroll(scroller))", source)
        self.assertIn("keepSavedTop = true;", source)
        self.assertIn("const wheelDeltaPixels = (event, scroller) => {", source)
        self.assertIn("const maxDelta = Math.max(80, scroller.clientHeight * 0.9);", source)
        self.assertIn("scroller.addEventListener('wheel', (event) => {", source)
        self.assertIn("const nextTop = clampScrollTop(scroller, scroller.scrollTop + deltaY);", source)
        self.assertIn("scroller.scrollTop = nextTop;\n                            updateScrollState(scroller, 'user');", source)
        self.assertIn("window.__cbStreamForceBottomUntil = 0;", source)
        self.assertIn("window.__cbStreamUserScrollIntentUntil = Date.now() + 800;", source)
        self.assertNotIn("window.__cbStreamUserScrollIntentUntil = 0;", source)
        self.assertIn("if (inScrollbar) acceptUserScroll(scroller);", source)
        self.assertIn("updateScrollState(scroller, 'user');", source)
        self.assertIn("scroller.addEventListener('touchstart', () => {", source)
        self.assertIn("if (event.pointerType === 'touch') {", source)
        self.assertIn("beginUserScrollIntent(scroller);", source)
        self.assertIn("source = 'script';", source)
        self.assertIn("scrollToBottom(scroller);", source)
        self.assertNotIn("__cbStreamProgrammaticScrollUntil", source)
        self.assertIn("window.__cbStreamLoadOlderAnchor = {", source)
        self.assertIn("scroller.scrollHeight - Number(loadOlderAnchor.scrollHeight) + Number(loadOlderAnchor.scrollTop)", source)
        self.assertIn("window.__cbStreamLoadOlderAnchor = {", sections_source)
        self.assertIn("if (loadOlderAnchor.stickToBottom === true)", source)
        self.assertNotIn("maybeLoadOlder", source)
        self.assertNotIn("data-stream-auto-load-older", sections_source)
        self.assertNotIn("button.click();", source[source.index("def scroll_stream_to_bottom"):source.index("def install_stream_refresh_timer")])

    def test_stream_scroll_preserves_small_user_scrolls_across_refreshes(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertIn("const userScrollAwayLimit = 0;", source)
        self.assertIn("const clampScrollTop = (scroller, value)", source)
        self.assertIn("top: 0,", source)
        self.assertIn("state.top = readScrollTop(scroller);", source)
        self.assertIn("restoreTopPending: false,", source)
        self.assertIn("if ((!keepSavedTop && !state.restoreTopPending) || source === 'user') {", source)
        self.assertIn("state.userScrolledAway = delta > userScrollAwayLimit;", source)
        self.assertIn("(!state.userScrolledAway && state.nearBottom === true)", source)
        self.assertIn("const restorePreviousTop = () => {", source)
        self.assertIn("state.restoreTopPending = restoredTop !== previousTop;", source)
        self.assertIn("return state.restoreTopPending;", source)
        self.assertIn("const scrollerChanged = window.__cbStreamAttachedScroller !== scroller;", source)
        self.assertIn("if ((preserveTop || scrollerChanged) && !shouldStickToBottom) {", source)
        self.assertIn("updateScrollState(scroller, 'script');", source)
        self.assertIn("updateScrollState(scroller, 'script', topWasClamped);", source)
        self.assertIn("top: Math.max(0, scroller.scrollHeight - scroller.clientHeight),", sections_source)
        self.assertIn("const programmaticScrollers = window.__cbStreamProgrammaticScrollers;", sections_source)
        self.assertNotIn("__cbStreamProgrammaticScrollUntil", sections_source)

    def test_stream_switch_resets_scroll_state_to_bottom(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn('"stream_switch_refresh_pending": False,', source)
        self.assertIn('"stream_switch_sequence": 0,', source)
        self.assertIn("if was_stream_page and current_session_name == cleaned_session_name:", source)
        self.assertIn('state["stream_switch_refresh_pending"] = True', source)
        self.assertIn('state.get("stream_switch_sequence") != switch_sequence', source)
        self.assertIn('if state.get("stream_switch_refresh_pending"):', source)
        self.assertIn("state[\"stream_force_bottom_session\"] = cleaned_session_name", source)
        self.assertIn("if (streamChanged) {", source)
        self.assertIn("const hasKnownState = Object.prototype.hasOwnProperty.call(", source)
        self.assertIn("if (forceBottom || !hasKnownState) {", source)
        self.assertIn("state.delta = 0;\n                        state.nearBottom = true;\n                        state.userScrolledAway = false;", source)
        self.assertIn("window.__cbStreamForceBottomUntil = Date.now() + 1200;", source)
        self.assertIn("if ((preserveTop || scrollerChanged) && !shouldStickToBottom) {", source)

    def test_sidebar_exposes_explicit_new_session_entry(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn("ui.web.mobile.new_session_placeholder", source)
        self.assertIn("cb-sidebar-new-session-input", source)
        self.assertIn("cb-sidebar-new-session-button", source)
        self.assertIn("def _open_stream_session_from_input(input_box) -> None:", source)
        self.assertIn("ui.web.mobile.new_session_name_required", source)
        self.assertIn("_open_stream_session(session_name)", source)

    def test_sidebar_includes_selected_empty_stream_session(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn('selected_sidebar_session = str(state["selected_session_name"] or "").strip()', source)
        self.assertIn("selected_sidebar_session not in sessions", source)
        self.assertIn("session_order.insert(0, selected_sidebar_session)", source)
        self.assertIn("not codex_thread_id_from_session_name(selected_sidebar_session)", source)

    def test_loaded_sidebar_can_refresh_and_include_current_empty_qq_session(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        start = source.index("def sidebar_sessions_view")
        end = source.index("def right_sidebar_view", start)
        body = source[start:end]

        self.assertIn("stream_qq_current_session_name()", body)
        self.assertIn("qq_current_session not in sessions", body)
        self.assertIn("session_order.insert(0, qq_current_session)", body)
        self.assertIn("ui.web.mobile.refresh_session_list", body)
        self.assertIn("on_click=sidebar_sessions_view.refresh", body)

    def test_sidebar_uses_chunked_state_and_lazy_codex_loading(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        start = source.index("def sidebar_sessions_view")
        end = source.index("def right_sidebar_view", start)
        body = source[start:end]

        self.assertIn("build_stream_sidebar_state_snapshot(", body)
        self.assertIn("task_limit=_stream_sidebar_task_limit()", body)
        self.assertIn("include_codex_threads=False", body)
        self.assertIn("_sidebar_codex_threads()", body)
        self.assertIn("ui.web.mobile.load_more_sessions", body)
        self.assertIn("ui.web.mobile.load_codex_threads", body)
        self.assertIn("ui.web.mobile.retry_codex_threads", body)
        self.assertIn("ui.web.mobile.load_more_codex_threads", body)
        self.assertNotIn('include_codex_threads=bool(state.get("stream_sidebar_codex_loaded"))', body)
        self.assertNotIn("build_mobile_state_snapshot()", body)

    def test_sidebar_codex_threads_load_one_page_at_a_time(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        start = source.index("def _load_sidebar_codex_threads()")
        end = source.index("@ui.refreshable\n    def sidebar_navigation_view", start)
        body = source[start:end]

        self.assertIn("load_codex_threads_page(cursor=cursor, archived=archived)", body)
        self.assertIn('state["stream_sidebar_codex_cursor"] = next_cursor', body)
        self.assertIn('state["stream_sidebar_codex_archived"] = True', body)
        self.assertIn('state["stream_sidebar_codex_done"] = True', body)
        self.assertNotIn("_load_codex_threads_cached", body)
        self.assertNotIn("_load_all_codex_threads", body)

    def test_codex_thread_runtime_status_uses_rollout_tail_without_rescanning_unchanged_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            active_path = root / "active.jsonl"
            completed_path = root / "completed.jsonl"
            stale_path = root / "stale.jsonl"

            active_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-07-17T05:19:00Z",
                                "type": "event_msg",
                                "payload": {"type": "task_started"},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-07-17T05:19:01Z",
                                "type": "response_item",
                                "payload": {
                                    "type": "reasoning",
                                    "summary": [{"text": "Planning runtime activity"}],
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            completed_path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                        json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            stale_path.write_text(
                json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call"}}) + "\n",
                encoding="utf-8",
            )
            now = time.time()
            os.utime(active_path, (now, now))
            os.utime(completed_path, (now, now))
            os.utime(stale_path, (now - 600, now - 600))
            threads = [
                {"id": "active", "path": str(active_path), "status": "notLoaded"},
                {"id": "completed", "path": str(completed_path), "status": "notLoaded"},
                {"id": "stale", "path": str(stale_path), "status": "notLoaded"},
            ]
            probes: dict[str, object] = {}

            changed = _update_codex_thread_runtime_statuses(threads, probes, now=now)

            self.assertTrue(changed)
            self.assertEqual(["running", "idle", "idle"], [thread["runtime_status"] for thread in threads])
            self.assertEqual("2026-07-17T05:19:00Z", threads[0]["runtime_started_at"])
            self.assertEqual("Planning runtime activity", threads[0]["runtime_activity"]["text"])
            self.assertTrue(_codex_rollout_runtime_hint(active_path))
            self.assertEqual("2026-07-17T05:19:00Z", _codex_rollout_runtime_started_at(active_path))
            self.assertFalse(_codex_rollout_runtime_hint(completed_path))

            with patch("ui.app._codex_rollout_runtime_snapshot", wraps=_codex_rollout_runtime_snapshot) as read_tail:
                unchanged = _update_codex_thread_runtime_statuses(threads, probes, now=now + 1)

            self.assertFalse(unchanged)
            read_tail.assert_not_called()

            with active_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
            os.utime(active_path, (now + 2, now + 2))

            completed = _update_codex_thread_runtime_statuses(threads, probes, now=now + 2)

            self.assertTrue(completed)
            self.assertEqual("idle", threads[0]["runtime_status"])
            self.assertEqual("", threads[0]["runtime_started_at"])
            self.assertEqual({}, threads[0]["runtime_activity"])

    def test_codex_terminal_rollout_overrides_stale_running_thread_status(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "completed.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-07-19T12:00:00Z",
                                "type": "event_msg",
                                "payload": {"type": "task_started"},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-07-19T12:01:26Z",
                                "type": "event_msg",
                                "payload": {
                                    "type": "agent_message",
                                    "phase": "final_answer",
                                    "message": "done",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-07-19T12:01:27Z",
                                "type": "event_msg",
                                "payload": {"type": "task_complete"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            now = time.time()
            os.utime(path, (now, now))
            thread = {
                "id": "thread-finished",
                "path": str(path),
                "status": "running",
                "runtime_status": "running",
                "runtime_started_at": "2026-07-19T12:00:00Z",
            }
            probes: dict[str, object] = {}

            changed = _update_codex_thread_runtime_statuses([thread], probes, now=now)

            self.assertTrue(changed)
            self.assertEqual("idle", thread["runtime_status"])
            self.assertEqual("", thread["runtime_started_at"])
            self.assertEqual({}, thread.get("runtime_activity", {}))

            unchanged = _update_codex_thread_runtime_statuses([thread], probes, now=now + 1)

            self.assertFalse(unchanged)
            self.assertEqual("idle", thread["runtime_status"])

    def test_codex_terminal_turn_clears_unterminated_rollout_after_grace_period(self) -> None:
        for latest_turn_status in ("completed", "failed", "canceled", "interrupted"):
            with self.subTest(latest_turn_status=latest_turn_status), TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "unterminated.jsonl"
                path.write_text(
                    "\n".join(
                        [
                            json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                            json.dumps({"type": "event_msg", "payload": {"type": "exec_command_begin", "command": "pytest -q"}}),
                            json.dumps({"type": "event_msg", "payload": {"type": "exec_command_end"}}),
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                now = time.time()
                os.utime(path, (now, now))
                thread = {
                    "id": f"thread-{latest_turn_status}",
                    "path": str(path),
                    "status": "notLoaded",
                    "latest_turn_status": latest_turn_status,
                }
                probes: dict[str, object] = {}

                _update_codex_thread_runtime_statuses([thread], probes, now=now)
                self.assertEqual("running", thread["runtime_status"])
                self.assertEqual("completed", thread["runtime_activity"]["status"])

                changed = _update_codex_thread_runtime_statuses(
                    [thread],
                    probes,
                    now=now + CODEX_THREAD_RUNTIME_TERMINAL_GRACE_SECONDS + 1,
                )

                self.assertTrue(changed)
                self.assertEqual("idle", thread["runtime_status"])
                self.assertEqual({}, thread["runtime_activity"])

    def test_codex_interrupted_turn_keeps_genuinely_running_activity_alive(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "active-command.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                        json.dumps({"type": "event_msg", "payload": {"type": "exec_command_begin", "command": "long-task"}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            now = time.time()
            os.utime(path, (now, now))
            thread = {
                "id": "thread-active-interrupted",
                "path": str(path),
                "status": "notLoaded",
                "latest_turn_status": "interrupted",
            }
            probes: dict[str, object] = {}

            _update_codex_thread_runtime_statuses([thread], probes, now=now)
            changed = _update_codex_thread_runtime_statuses(
                [thread],
                probes,
                now=now + CODEX_THREAD_RUNTIME_TERMINAL_GRACE_SECONDS + 1,
            )

            self.assertFalse(changed)
            self.assertEqual("running", thread["runtime_status"])
            self.assertEqual("running", thread["runtime_activity"]["status"])

            stale = _update_codex_thread_runtime_statuses(
                [thread],
                probes,
                now=now + CODEX_THREAD_RUNTIME_STALE_SECONDS + 1,
            )

            self.assertTrue(stale)
            self.assertEqual("idle", thread["runtime_status"])

    def test_codex_rollout_size_change_resets_terminal_silence_without_mtime_change(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "growing-rollout.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                        json.dumps({"type": "event_msg", "payload": {"type": "exec_command_begin", "command": "step-one"}}),
                        json.dumps({"type": "event_msg", "payload": {"type": "exec_command_end"}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            now = time.time()
            os.utime(path, (now, now))
            thread = {
                "id": "thread-growing-rollout",
                "path": str(path),
                "status": "notLoaded",
                "latest_turn_status": "interrupted",
            }
            probes: dict[str, object] = {}
            _update_codex_thread_runtime_statuses([thread], probes, now=now)

            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")
            os.utime(path, (now, now))
            refreshed_at = now + CODEX_THREAD_RUNTIME_TERMINAL_GRACE_SECONDS + 1

            changed = _update_codex_thread_runtime_statuses([thread], probes, now=refreshed_at)

            self.assertFalse(changed)
            self.assertEqual("running", thread["runtime_status"])
            self.assertEqual(refreshed_at, probes["thread-growing-rollout"]["last_activity_at"])

            finished = _update_codex_thread_runtime_statuses(
                [thread],
                probes,
                now=refreshed_at + CODEX_THREAD_RUNTIME_TERMINAL_GRACE_SECONDS + 1,
            )

            self.assertTrue(finished)
            self.assertEqual("idle", thread["runtime_status"])

    def test_stream_composer_signature_tracks_codex_terminal_state(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        signature_start = source.index("def _stream_composer_signature")
        signature_end = source.index("def _stream_pending_image_paths", signature_start)
        signature_body = source[signature_start:signature_end]

        self.assertIn('bool(task.get("final_answer"))', signature_body)
        self.assertIn('str(task.get("final_answer_at") or "")', signature_body)
        self.assertIn('str(task.get("finished_at") or "")', signature_body)
        self.assertIn('"runtime_status", "runtime_started_at"', signature_body)

    def test_shell_displays_lightweight_system_resource_usage(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        shell_start = source.index("def shell_view() -> None:")
        shell_end = source.index("def install_ui_error_logger() -> None:", shell_start)
        shell_body = source[shell_start:shell_end]
        refresh_start = source.index("def refresh_stream() -> None:")
        refresh_end = source.index("client.on_connect", refresh_start)
        refresh_body = source[refresh_start:refresh_end]

        self.assertIn("data-system-resource-strip=1", shell_body)
        self.assertIn("data-system-resource-value={key}", shell_body)
        self.assertIn("get_system_resource_usage()", shell_body)
        self.assertIn("_patch_system_resource_usage()", refresh_body)
        self.assertIn("resource_now - resource_updated_at >= 2.0", refresh_body)

    def test_codex_terminal_task_suppresses_stale_runtime_controls(self) -> None:
        session_name = "codex:thread-finished"
        mobile_state = {
            "tasks": [
                {
                    "id": "task-finished",
                    "session_name": session_name,
                    "status": "running",
                    "created_at": "2026-07-19T17:59:29",
                    "started_at": "2026-07-19T17:59:29",
                    "finished_at": "",
                    "final_answer": True,
                    "final_answer_at": "2026-07-19T18:18:25",
                    "output": "done",
                }
            ],
            "selected_codex_thread": {
                "id": "thread-finished",
                "runtime_status": "running",
                "runtime_started_at": "2026-07-19T09:59:29Z",
            },
        }

        context = _prepare_stream_render_context(mobile_state, session_name)

        self.assertFalse(context["codex_runtime_running"])
        self.assertEqual("", context["latest_active_task_id"])
        self.assertEqual("", context["latest_cancelable_task_id"])

        mobile_state["selected_codex_thread"]["runtime_started_at"] = "2026-07-19T10:20:00Z"

        active_context = _prepare_stream_render_context(mobile_state, session_name)

        self.assertTrue(active_context["codex_runtime_running"])
        self.assertEqual("codex-runtime-thread-finished", active_context["latest_active_task_id"])
        self.assertEqual("codex_thread", active_context["latest_cancel_target_kind"])

    def test_codex_runtime_context_handles_empty_task_list(self) -> None:
        session_name = "codex:thread-loading"
        mobile_state = {
            "tasks": [],
            "selected_codex_thread": {
                "id": "thread-loading",
                "runtime_status": "running",
                "runtime_started_at": "2026-07-19T10:20:00Z",
            },
        }

        context = _prepare_stream_render_context(mobile_state, session_name)

        self.assertTrue(context["codex_runtime_running"])
        self.assertEqual("codex-runtime-thread-loading", context["latest_active_task_id"])
        self.assertEqual("codex_thread", context["latest_cancel_target_kind"])

    def test_stream_refresh_reads_content_signature_before_runtime_status(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        refresh_start = source.index("def refresh_stream() -> None:")
        refresh_end = source.index("client.on_connect", refresh_start)
        refresh_body = source[refresh_start:refresh_end]

        signature_index = refresh_body.index("next_signature = _stream_signature_snapshot()")
        runtime_index = refresh_body.index("_refresh_codex_runtime_statuses()")

        self.assertLess(signature_index, runtime_index)

    def test_codex_runtime_snapshot_reports_the_latest_mcp_tool_without_a_full_history_read(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "active-mcp.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                        json.dumps(
                            {
                                "timestamp": "2026-07-17T05:19:01Z",
                                "type": "response_item",
                                "payload": {
                                    "type": "custom_tool_call",
                                    "name": "exec",
                                    "call_id": "call-mcp",
                                    "input": 'const result = await tools.mcp__node_repl__js({code: "inspect()"});',
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            snapshot = _codex_rollout_runtime_snapshot(path)

            self.assertTrue(snapshot["running_hint"])
            self.assertEqual(
                {
                    "kind": "tool",
                    "text": "node_repl.js",
                    "status": "running",
                    "at": "2026-07-17T05:19:01Z",
                    "server": "node_repl",
                    "tool": "js",
                },
                snapshot["activity"],
            )

    def test_codex_runtime_status_uses_stable_detection_time_when_start_is_outside_probe(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "active-without-start.jsonl"
            path.write_text(
                json.dumps({"type": "response_item", "payload": {"type": "function_call"}}) + "\n",
                encoding="utf-8",
            )
            now = time.time()
            os.utime(path, (now, now))
            thread = {"id": "active-without-start", "path": str(path), "status": "notLoaded"}
            probes: dict[str, object] = {}

            with patch("ui.app._codex_rollout_runtime_started_at", return_value=""):
                changed = _update_codex_thread_runtime_statuses([thread], probes, now=now)

            detected_at = str(thread.get("runtime_started_at") or "")
            self.assertTrue(changed)
            self.assertEqual("running", thread["runtime_status"])
            self.assertTrue(detected_at)

            unchanged = _update_codex_thread_runtime_statuses([thread], probes, now=now + 1)

            self.assertFalse(unchanged)
            self.assertEqual(detected_at, thread["runtime_started_at"])

    def test_codex_runtime_status_timer_does_not_reload_thread_pages(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        refresh_start = source.index("def refresh_stream() -> None:")
        refresh_end = source.index("client.on_connect", refresh_start)
        refresh_body = source[refresh_start:refresh_end]

        self.assertIn("_refresh_codex_runtime_statuses()", refresh_body)
        self.assertNotIn("load_codex_threads_page", refresh_body)
        self.assertNotIn("read_codex_thread", refresh_body)

    def test_codex_runtime_activity_updates_patch_only_the_live_label(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        signature_start = source.index("def _stream_signature_snapshot() -> tuple:")
        signature_end = source.index("def _stream_task_order_key", signature_start)
        signature_body = source[signature_start:signature_end]
        refresh_start = source.index("def refresh_stream() -> None:")
        refresh_end = source.index("client.on_connect", refresh_start)
        refresh_body = source[refresh_start:refresh_end]
        patch_start = source.index("def _patch_stream_runtime_activity() -> None:")
        patch_end = source.index("def _stream_task_order_key", patch_start)
        patch_body = source[patch_start:patch_end]

        self.assertNotIn("runtime_signature", signature_body)
        self.assertIn("selected_runtime_status_changed", refresh_body)
        self.assertIn("elif selected_runtime_activity_changed:", refresh_body)
        self.assertIn("_patch_stream_runtime_activity()", refresh_body)
        self.assertIn("document.querySelector('.cb-stream-current-activity')", patch_body)
        self.assertNotIn("stream_messages_view.refresh()", patch_body)

    def test_codex_sidebar_runtime_updates_do_not_rebuild_workspace_disclosures(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        refresh_start = source.index("def refresh_stream() -> None:")
        refresh_end = source.index("client.on_connect", refresh_start)
        refresh_body = source[refresh_start:refresh_end]
        patch_start = source.index("def _patch_sidebar_codex_runtime_status() -> None:")
        patch_end = source.index("def _load_sidebar_codex_threads() -> None:", patch_start)
        patch_body = source[patch_start:patch_end]
        sidebar_start = source.index("def sidebar_sessions_view() -> None:")
        sidebar_end = source.index("def right_sidebar_view() -> None:", sidebar_start)
        sidebar_body = source[sidebar_start:sidebar_end]

        self.assertIn("_patch_sidebar_codex_runtime_status()", refresh_body)
        self.assertNotIn("sidebar_sessions_view.refresh()", refresh_body)
        self.assertIn("document.querySelectorAll('[data-codex-workspace-running]')", patch_body)
        self.assertIn("document.querySelectorAll('[data-codex-thread-running]')", patch_body)
        self.assertIn("data-codex-workspace-running", sidebar_body)
        self.assertIn("data-codex-thread-running", sidebar_body)

    def test_sidebar_mounts_fast_local_sessions_without_manual_load(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        open_start = source.index("def open_sidebar")
        open_end = source.index("def close_sidebar", open_start)
        open_body = source[open_start:open_end]
        sidebar_start = source.index("def right_sidebar_view")
        sidebar_end = source.index("def _stream_status_badge_class", sidebar_start)
        sidebar_body = source[sidebar_start:sidebar_end]

        self.assertNotIn('"sidebar_content_loaded": False', source)
        self.assertNotIn("right_sidebar_view.refresh()", open_body)
        self.assertNotIn("ui.web.mobile.load_sidebar_sessions", sidebar_body)
        self.assertNotIn("def _load_sidebar_sessions() -> None:", source)
        self.assertIn("sidebar_sessions_view()", sidebar_body)
        self.assertIn("ui.web.mobile.load_codex_threads", source)
        self.assertIn("js_handler=\"() => document.body.classList.toggle('cb-sidebar-open')\"", source)
        self.assertNotIn('ui.button("", on_click=open_sidebar, icon="menu")', source)
        self.assertNotIn('ui.button("", on_click=close_sidebar, icon="close")', source)

    def test_codex_workspace_disclosures_keep_manual_state_across_sidebar_refreshes(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        sidebar_start = source.index("def sidebar_sessions_view() -> None:")
        sidebar_end = source.index("def right_sidebar_view() -> None:", sidebar_start)
        sidebar_body = source[sidebar_start:sidebar_end]

        self.assertIn("cb_codex_workspace_open_state", source)
        self.assertIn("window.__cbRestoreCodexWorkspaceOpenState", source)
        self.assertIn("document.addEventListener('toggle'", source)
        self.assertIn("data-codex-workspace-key", sidebar_body)
        self.assertIn("stream_sidebar_codex_workspace_open", source)
        self.assertIn("js_handler=\"(event) => emit({open: event.currentTarget.parentElement.open !== true})\"", sidebar_body)
        self.assertIn("window.setTimeout(restore, 120)", sidebar_body)

    def test_stream_panel_does_not_expose_new_session_shortcut(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertNotIn(".cb-stream-session-creator", source)
        self.assertNotIn(".cb-stream-new-session-input", source)
        self.assertNotIn(".cb-stream-new-session-button", source)
        self.assertNotIn("def open_new_session(input_box: UIElementLike) -> None:", sections_source)
        self.assertNotIn("web-{datetime.now().strftime('%Y%m%d-%H%M%S')}", sections_source)
        self.assertNotIn("cb-stream-open-new-session-button", sections_source)
        self.assertNotIn("data-stream-new-session-action=1", sections_source)
        self.assertNotIn("def _open_new_stream_session() -> None:", source)
        self.assertNotIn("cb-new-session-dialog", source)
        self.assertNotIn("document.body.classList.add('cb-sidebar-open')", sections_source)
        self.assertNotIn(".cb-sidebar-new-session-input input, .cb-sidebar-new-session-input textarea", sections_source)

    def test_home_service_controls_expose_hub_only_restart(self) -> None:
        app_source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertIn('"restart-hub"', app_source)
        self.assertIn("ui.web.action.restart_hub", sections_source)
        self.assertIn('on_run_action("restart-hub")', sections_source)

    def test_stream_content_uses_normal_column_order(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        content_start = source.index(".cb-agent-stream-content {")
        content_end = source.index(".cb-agent-titlebar", content_start)
        content_css = source[content_start:content_end]

        self.assertIn("display: flex;", content_css)
        self.assertIn("flex-direction: column;", content_css)
        self.assertIn("min-height: 100%;", content_css)
        self.assertIn("justify-content: flex-end;", content_css)
        self.assertNotIn("column-reverse", content_css)

    def test_desktop_sidebar_opens_as_split_panel_for_stream(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn("@media (min-width: 768px)", source)
        self.assertIn("body.cb-sidebar-open .cb-shell-content-stream", source)
        self.assertIn("width: calc(100vw - 360px) !important;", source)
        self.assertIn("body.cb-sidebar-open .cb-sidebar-backdrop", source)
        self.assertIn("pointer-events: none;", source)
        self.assertIn("body.cb-sidebar-open .cb-sidebar-toggle", source)
        self.assertIn("transform: translateX(-360px);", source)

    def test_sidebar_uses_one_scroll_container_for_expanded_workspaces(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn(".cb-sidebar-shell", source)
        self.assertIn("overflow: auto;", source)
        self.assertIn("body.cb-sidebar-open {\n            overflow: hidden;", source)
        self.assertIn(".cb-sidebar-shell {\n                width: 100vw;\n                max-width: 100vw;", source)
        self.assertIn("body:not(.cb-sidebar-open) .cb-sidebar-shell {\n                display: none;", source)
        self.assertIn(".cb-sidebar-content {\n            min-height: 100vh;", source)
        self.assertIn("overflow: visible;", source)
        self.assertIn(".cb-codex-workspace {\n            border: 1px solid var(--cb-border);", source)
        self.assertIn(".cb-codex-workspace * {\n            min-width: 0;", source)
        self.assertIn(".cb-codex-workspace-body {\n            box-sizing: border-box;", source)
        self.assertIn("max-width: 100%;\n            padding: 0 0.75rem 0.75rem;", source)
        self.assertIn("overflow-x: hidden;", source)
        self.assertIn(".cb-chip {\n            display: inline-flex;", source)
        self.assertIn("flex-shrink: 0;", source)
        self.assertIn("white-space: nowrap;", source)
        self.assertIn("overflow-wrap: anywhere;", source)
        self.assertNotIn('classes("w-full gap-2 overflow-auto pr-1")', source)

    def test_scroll_to_bottom_button_is_centered_like_paseo(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertIn(".cb-scroll-bottom-button", source)
        self.assertIn("left: 50%;", source)
        self.assertIn("bottom: calc(var(--cb-composer-height, 6rem) + 0.75rem);", source)
        self.assertIn("bottom: calc(var(--cb-composer-height, 5.5rem) + 0.5rem);", source)
        self.assertIn("transform: translateX(-50%);", source)
        self.assertIn("border-radius: 24px !important;", source)
        self.assertIn("box-shadow: 0 2px 8px rgba(0, 0, 0, 0.02);", source)
        self.assertNotIn("box-shadow: 0 6px 18px rgba(15, 23, 42, 0.16);", source)
        self.assertIn("html body .q-btn.cb-scroll-bottom-button.bg-primary", source)
        self.assertIn("background-color: var(--cb-surface-muted) !important;", source)
        self.assertIn("body.cb-sidebar-open .cb-scroll-bottom-button", source)
        self.assertIn("left: calc((100vw - 360px) / 2);", source)
        self.assertIn("const updateComposerMetrics = () => {", source)
        self.assertIn("document.documentElement.style.setProperty('--cb-composer-height'", source)
        self.assertIn('ui.button("", icon="keyboard_arrow_down", color=None)', sections_source)
        self.assertIn('props("round unelevated")', sections_source)
        self.assertNotIn('props("round unelevated color=primary")', sections_source)

    def test_shell_theme_tokens_support_dark_light_and_forest(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")

        self.assertIn("--cb-bg: #111111;", source)
        self.assertIn("--cb-surface: #191919;", source)
        self.assertIn("--cb-surface-muted: #242424;", source)
        self.assertIn("--cb-surface-raised: #1f1f1f;", source)
        self.assertIn("--cb-border: #30302d;", source)
        self.assertIn("--cb-ink: #f5f5f0;", source)
        self.assertIn("--cb-muted: #a3a39b;", source)
        self.assertIn("--cb-accent: #2f6f5e;", source)
        self.assertIn("--cb-accent-bright: #3f8f72;", source)
        self.assertIn("--q-primary: #2f6f5e;", source)
        self.assertIn("--cb-info:", source)
        self.assertIn("--cb-info-soft:", source)
        self.assertIn("--cb-ok-soft:", source)
        self.assertIn("--cb-warn-soft:", source)
        self.assertIn("--cb-danger-soft:", source)
        self.assertIn("--cb-code-bg:", source)
        self.assertIn("--cb-code-ink:", source)
        self.assertIn("--cb-bg: #f7f7f4;", source)
        self.assertIn("--cb-accent: #3f8f72;", source)
        self.assertIn('--q-primary: var(--cb-accent) !important;', source)
        self.assertIn(':root[data-cb-theme="light"]', source)
        self.assertIn(':root[data-cb-theme="forest"]', source)
        self.assertIn("window.__cbApplyTheme", source)
        self.assertIn('"theme": "dark"', source)
        self.assertIn("html body .q-btn.cb-sidebar-toggle.bg-primary", source)
        self.assertIn("html body .q-btn.cb-composer-send-button.bg-primary", source)
        self.assertIn("background-color: var(--cb-accent) !important;", source)
        self.assertIn("background-color: var(--cb-accent-bright) !important;", source)
        self.assertNotIn("--cb-accent: #52b788;", source)
        self.assertNotIn("--cb-accent: #0f766e;", source)
        self.assertNotIn("--cb-accent: #0969da;", source)
        self.assertNotIn("--cb-accent: #3b82f6;", source)
        self.assertNotIn("--cb-bg: #101113;", source)
        self.assertNotIn("--cb-bg: #f6f7f4;", source)
        self.assertNotIn("rgba(82, 183, 136", source)
        self.assertNotIn("rgb(88, 152, 212)", source)

    def test_dark_shell_uses_semantic_text_classes(self) -> None:
        app_source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertIn(".cb-ink {\n            color: var(--cb-ink);", app_source)
        self.assertIn("html body .q-btn.bg-primary.text-white", app_source)
        self.assertIn("color: #ffffff !important;", app_source)
        self.assertIn("html body .q-btn.bg-primary .cb-muted", app_source)
        self.assertIn(".cb-language-toggle .q-btn.bg-white.text-primary", app_source)
        self.assertNotRegex(app_source, r"text-slate-[6-9]00")
        self.assertNotRegex(sections_source, r"text-slate-[6-9]00")
        self.assertNotIn("bg-white w-auto", sections_source)
        self.assertIn("ui.image(qr_data_url).classes", sections_source)

    def test_composer_surface_uses_paseo_like_dark_input_chrome(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertIn(".cb-composer-box", source)
        self.assertIn("border: 1px solid var(--cb-border);", source)
        self.assertIn("background: var(--cb-surface);", source)
        self.assertIn("border-radius: 16px;", source)
        self.assertIn("box-shadow: none;", source)
        self.assertIn(".cb-composer-box:focus-within {\n            border-color: var(--cb-border-strong);", source)
        self.assertNotIn(".cb-composer-box:focus-within {\n            border-color: var(--cb-accent);", source)
        self.assertIn("padding: 1rem;", source)
        self.assertIn("min-height: 46px;", source)
        self.assertIn("background: transparent !important;", source)
        self.assertIn("font-size: 16px;", source)
        self.assertIn("line-height: 22.4px;", source)
        self.assertIn(".cb-composer-actions", source)
        self.assertIn("gap: 0;", source)
        self.assertIn("background: var(--cb-surface-muted) !important;", source)
        self.assertIn("width: 1.75rem;", source)
        self.assertIn("height: 1.75rem;", source)
        self.assertIn(".cb-composer-queue-track", source)
        self.assertIn("gap: 0.5rem;", source)
        self.assertIn("padding: 0 0 0.5rem;", source)
        self.assertIn(".cb-composer-queue-item", source)
        self.assertIn("border: 1px solid var(--cb-border);", source)
        self.assertIn("padding: 0.5rem 0.75rem;", source)
        self.assertIn(".cb-composer-queue-text", source)
        self.assertIn("font-size: 1rem;", source)
        self.assertIn("-webkit-line-clamp: 2;", source)
        self.assertIn("white-space: normal;", source)
        self.assertIn("cb-composer-upload-button", sections_source)
        self.assertIn("document.querySelector('.cb-composer-upload-button')", sections_source)
        self.assertIn("document.querySelector('.cb-composer-upload-button')", source)
        self.assertIn("html body .q-btn.cb-composer-queue-cancel", source)
        self.assertIn("min-height: 2rem;", source)
        self.assertIn("height: 2rem;", source)
        self.assertIn("width: 2rem;", source)
        self.assertIn("color: var(--cb-muted) !important;", source)
        self.assertIn('icon="close",', sections_source)
        self.assertIn('color=None,', sections_source)
        self.assertNotIn(".cb-composer-queue-badge", source)
        self.assertIn(".cb-composer-send-button {\n            margin-left: 0.25rem;", source)
        self.assertIn(".cb-composer-zone {\n                padding: 0.75rem 1rem max(1rem, env(safe-area-inset-bottom));", source)
        self.assertIn("document.documentElement.style.setProperty('--cb-composer-height'", sections_source)
        self.assertIn(".cb-composer-box {\n                padding: 0.5rem 0.75rem;", source)
        self.assertIn(".cb-composer-attachment-tray", source)
        self.assertIn("gap: 0.5rem;", source)
        self.assertIn(".cb-composer-attachment-pill", source)
        self.assertIn("position: relative;", source)
        self.assertIn("width: 3rem;", source)
        self.assertIn("height: 3rem;", source)
        self.assertIn(".cb-composer-attachment-thumb", source)
        self.assertIn("border-radius: 6px;", source)
        self.assertIn("border: 1px solid var(--cb-border);", source)
        self.assertIn("background: var(--cb-surface-raised);", source)
        self.assertIn(".cb-composer-attachment-name {\n            display: none;", source)
        self.assertIn("html body .q-btn.cb-composer-attachment-remove", source)
        self.assertIn("top: -0.5rem;", source)
        self.assertIn("left: -0.5rem;", source)
        self.assertIn("height: 1.5rem;", source)
        self.assertIn("border: 1px solid var(--cb-border);", source)
        self.assertIn("color: var(--cb-muted) !important;", source)
        self.assertIn("opacity: 0;", source)
        self.assertIn("html body .q-btn.cb-composer-attachment-remove {\n                opacity: 1;", source)
        self.assertIn('icon="close",', sections_source)
        self.assertNotIn("padding: 0.65rem 0.75rem 0.6rem;", source)
        self.assertNotIn("padding: 0.55rem 0.55rem max(0.75rem, env(safe-area-inset-bottom));", source)

    def test_codex_history_composer_uses_desktop_thread_bridge_without_blocking_ui(self) -> None:
        app_source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        finish_start = app_source.index("async def _finish_stream_message_send")
        submit_start = app_source.index("def _submit_stream_message", finish_start)
        submit_end = app_source.index("def _open_mobile_url", submit_start)
        finish_body = app_source[finish_start:submit_start]
        submit_body = app_source[submit_start:submit_end]
        self.assertIn("send_codex_thread_message", finish_body)
        self.assertIn("submit_hub_task", finish_body)
        self.assertIn("await asyncio.to_thread", finish_body)
        self.assertIn('pending[session_name] = []', finish_body)
        self.assertIn("with client:", finish_body)
        self.assertIn("background_tasks.create(", submit_body)
        self.assertIn("_finish_stream_message_send(", submit_body)
        self.assertIn("client=context.client", submit_body)
        self.assertIn("images=images", submit_body)
        self.assertIn("model=model", finish_body)
        self.assertIn("reasoning_effort=reasoning_effort", finish_body)
        self.assertIn("_prepare_stream_render_context(stream_state, cleaned_session_name)", submit_body)
        self.assertIn("load_codex_model_catalog_cached", app_source)
        self.assertIn('name="load Codex model catalog"', app_source)
        self.assertIn("return True", submit_body)
        self.assertNotIn("await asyncio.to_thread", submit_body)
        self.assertIn("async def submit_composer", sections_source)
        self.assertIn("inspect.isawaitable(submitted)", sections_source)

    def test_stream_message_chrome_tracks_paseo_spacing_contract(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertIn(".cb-agent-stream {\n            flex: 1;", source)
        self.assertIn("padding: 1rem;", source)
        self.assertIn("overscroll-behavior-y: contain;", source)
        self.assertIn(".cb-agent-stream::-webkit-scrollbar", source)
        self.assertIn("@media (any-hover: hover) and (any-pointer: fine)", source)
        self.assertIn("overflow-y: auto !important;", source)
        self.assertIn("scrollbar-gutter: auto;", source)
        self.assertIn("width: 1rem;", source)
        self.assertIn("background: #c7d0d9;", source)
        self.assertIn("const acceptUserScroll = (scroller) =>", source)
        self.assertIn("window.__cbStreamWheelFallbackReady === '4'", source)
        self.assertIn("const handleScrollbarPointerDown = (event) =>", source)
        self.assertIn("document.addEventListener('pointerdown', handleScrollbarPointerDown, true);", source)
        self.assertIn(".cb-agent-stream {\n                padding: 1rem 0.75rem;", source)
        self.assertIn(".cb-agent-stream-content {\n                padding: 0 0.5rem;", source)
        self.assertNotIn("padding: 0.9rem 0.55rem 0.75rem;", source)
        self.assertIn(".cb-agent-panel {\n            height: 100vh;", source)
        self.assertIn("background: var(--cb-bg);", source)
        self.assertIn("max-width: 51.25rem;", source)
        self.assertIn(".cb-stream-user-bubble", source)
        self.assertIn("padding: 1rem;", source)
        self.assertIn("background: var(--cb-surface-muted);", source)
        self.assertIn("border-radius: 16px;", source)
        self.assertIn("border-top-right-radius: 2px;", source)
        self.assertIn("flex-shrink: 1;", source)
        self.assertIn("max-width: 100%;", source)
        self.assertNotIn("max-width: 92%;", source)
        self.assertIn(".cb-stream-user .cb-stream-body", source)
        self.assertIn("font-size: 1rem;", source)
        self.assertIn("line-height: 22px;", source)
        self.assertIn(".cb-stream-markdown a", source)
        self.assertIn("color: var(--cb-accent-bright);", source)
        self.assertIn("text-decoration: none;", source)
        self.assertIn("overflow-wrap: anywhere;", source)
        self.assertIn(".cb-stream-empty-state", source)
        self.assertIn("padding: 3rem 0.5rem;", source)
        self.assertIn(".cb-stream-empty-text", source)
        self.assertIn("font-size: 0.875rem;", source)
        self.assertIn("line-height: 20px;", source)
        self.assertIn('classes("cb-stream-empty-state")', sections_source)
        self.assertIn('classes("cb-stream-empty-text")', sections_source)
        self.assertNotIn("min-h-[48vh]", sections_source)
        self.assertIn(".cb-stream-assistant {\n            display: block;\n            padding: 0.75rem 0;", source)
        self.assertIn(".cb-stream-turn-with-footer {\n            margin-bottom: 0;", source)
        stream_turn_rule = re.search(r"\.cb-stream-turn \{(?P<body>.*?)\n\s*\}", source, re.S)
        self.assertIsNotNone(stream_turn_rule)
        stream_turn_body = stream_turn_rule.group("body") if stream_turn_rule else ""
        self.assertNotIn("content-visibility: auto;", stream_turn_body)
        self.assertNotIn("contain-intrinsic-size: auto 18rem;", stream_turn_body)
        self.assertIn('"cb-stream-turn cb-stream-turn-with-footer" if assistant_has_content or should_show_activity else "cb-stream-turn"', sections_source)
        self.assertIn(".cb-stream-turn-footer", source)
        self.assertIn("gap: 0.5rem;", source)
        self.assertIn("font-size: 13px;", source)
        self.assertIn(".cb-stream-user-footer {\n            margin-top: 0.5rem;", source)
        self.assertIn("html body .q-btn.cb-stream-copy-button", source)
        self.assertIn("padding: 0.25rem !important;", source)
        self.assertIn("color: var(--cb-muted) !important;", source)
        self.assertIn('icon="content_copy",', sections_source)
        self.assertIn('color=None,', sections_source)
        self.assertIn(
            "html body .q-btn.cb-stream-copy-button:hover,\n        html body .q-btn.cb-stream-copy-button:focus-visible",
            source,
        )
        self.assertIn("color: var(--cb-ink) !important;", source)
        self.assertIn(".cb-stream-user-footer .cb-stream-copy-button", source)
        self.assertIn("margin-right: auto;", source)
        self.assertIn(".cb-stream-turn-footer .cb-stream-copy-button", source)
        self.assertIn("margin-left: -0.25rem;", source)
        self.assertIn(".cb-stream-copy-button .q-btn__content", source)
        self.assertIn("width: 1rem;", source)
        self.assertIn("height: 1rem;", source)
        self.assertIn(".cb-stream-working-loader", source)
        self.assertIn("grid-template-columns: repeat(2, 3px);", source)
        self.assertIn("grid-template-rows: repeat(3, 3px);", source)
        self.assertIn("background: #b45309;", source)
        self.assertIn("animation: cb-synced-loader 0.95s linear infinite;", source)
        self.assertIn("margin-left: -2px;", source)
        self.assertNotIn("gap: 0.75rem;", source)
        self.assertNotIn("@keyframes cb-spin", source)
        self.assertNotIn(".cb-stream-working-dot", source)
        self.assertNotIn("cb-stream-working-label", sections_source)
        self.assertIn("padding-bottom: 1.5rem;", source)
        self.assertIn("background: var(--cb-surface-muted) !important;", source)
        self.assertIn(".cb-stream-activity-icon::before", source)
        self.assertIn("background: rgba(39, 39, 42, 0.5);", source)
        self.assertIn("background: var(--cb-info-soft);", source)
        self.assertIn("background: var(--cb-ok-soft);", source)
        self.assertIn("background: var(--cb-danger-soft);", source)
        self.assertIn("color: var(--cb-info);", source)
        self.assertIn("color: var(--cb-ok);", source)
        self.assertIn("color: var(--cb-danger);", source)
        self.assertNotIn("color: #60a5fa;", source)
        self.assertNotIn("color: #4ade80;", source)
        self.assertNotIn("color: #f87171;", source)
        self.assertIn(".cb-stream-activity-log {\n            display: flex;\n            flex-direction: column;\n            gap: 0.25rem;\n            margin-bottom: 0.25rem;", source)
        self.assertIn('content: "check_circle";', source)
        self.assertIn(".cb-stream-activity-details-row", source)
        self.assertIn(".cb-stream-activity-chevron::before", source)
        self.assertIn("margin: 0 0.75rem 0.75rem 2.5rem;", source)
        self.assertIn("border-radius: 4px;", source)
        self.assertIn("background: var(--cb-surface-raised);", source)
        self.assertIn(".cb-stream-activity-metadata-text", source)
        self.assertIn("color: var(--cb-ink);", source)
        self.assertIn("font-size: 0.75rem;", source)
        self.assertIn("white-space: pre-wrap;", source)
        self.assertNotIn(".cb-stream-activity-meta-row", source)
        self.assertNotIn(".cb-stream-activity-meta-key", source)
        self.assertNotIn(".cb-stream-activity-meta-value", source)
        self.assertNotIn("cb-stream-activity-title", sections_source)
        self.assertNotIn("cb-stream-activity-dot", sections_source)

    def test_stream_image_lightbox_script_is_installed(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertIn("const setupImageLightbox", source)
        self.assertIn(".cb-stream-image-lightbox-trigger[data-lightbox-src]", source)
        self.assertIn("cb-image-lightbox-open", source)
        self.assertIn("role=button tabindex=0", sections_source)
        self.assertIn("prepareLightboxTriggers", source)
        self.assertIn("prepareMarkdownImageLinks", source)
        self.assertIn('.cb-stream-markdown a[href^="#chatbridge-image="]', source)
        self.assertIn("cb-stream-inline-image-icon", source)
        self.assertIn("const triggerLabel = decodeAttr(trigger?.getAttribute?.('data-lightbox-label') || '');", source)
        self.assertIn("(document.documentElement || document.body).appendChild(overlay);", source)
        self.assertIn("data-lightbox-nav=\"prev\"", source)
        self.assertIn("gesturestart", source)


    def test_completed_turn_footer_uses_paseo_duration_then_timestamp(self) -> None:
        ui = FakeUI()
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "updated_at": "2026-07-04T05:22:00",
            "agents": [{"id": "qq", "name": "QQ", "backend": "codex"}],
            "tasks": [
                {
                    "id": "task-footer",
                    "agent_id": "qq",
                    "agent_name": "QQ",
                    "backend": "codex",
                    "session_name": "focus",
                    "status": "succeeded",
                    "created_at": "2026-07-04T05:20:00",
                    "started_at": "2026-07-04T05:20:00",
                    "finished_at": "2026-07-04T05:21:00",
                    "prompt": "do work",
                    "output": "done",
                    "summary": "done",
                }
            ],
            "session_task_counts": {"focus": 1},
        }

        render_mobile_stream_section(
            ui,
            _translator,
            mobile_state,
            "focus",
            [],
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
            _noop,
        )

        footer_labels = [item.text for item in ui.elements if "cb-stream-footer-label" in item.class_text.split()]
        main_labels = [item.text for item in ui.elements if "cb-stream-footer-label-main" in item.class_text.split()]
        alt_labels = [item.text for item in ui.elements if "cb-stream-footer-label-alt" in item.class_text.split()]

        self.assertIn("耗时 1 分 0 秒 · 2026-07-04T05:21:00", footer_labels)
        self.assertEqual([], main_labels)
        self.assertEqual([], alt_labels)
        self.assertNotIn("succeeded · 耗时 1 分 0 秒", main_labels)

        source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")
        self.assertIn("setupFooterLabelReveal", source)
        self.assertIn("cb-stream-footer-label-revealed", source)
        self.assertIn('"ui.web.mobile.stream_turn_footer_duration_time"', sections_source)
        self.assertNotIn("wrap.setAttribute('role', 'button')", source)
        self.assertIn("window.setTimeout(() => {", source)

    def test_each_completed_reply_shows_its_own_task_duration(self) -> None:
        ui = FakeUI()
        tasks = [
            {
                "id": "task-footer-1",
                "agent_id": "codex",
                "backend": "codex",
                "session_name": "focus",
                "status": "succeeded",
                "created_at": "2026-07-04T05:20:00",
                "started_at": "2026-07-04T05:20:00",
                "finished_at": "2026-07-04T05:21:00",
                "output": "first reply",
            },
            {
                "id": "task-footer-2",
                "agent_id": "codex",
                "backend": "codex",
                "session_name": "focus",
                "status": "succeeded",
                "created_at": "2026-07-04T05:22:00",
                "started_at": "2026-07-04T05:22:00",
                "finished_at": "2026-07-04T05:24:00",
                "output": "second reply",
            },
            {
                "id": "task-footer-running-output",
                "agent_id": "codex",
                "backend": "codex",
                "session_name": "focus",
                "status": "succeeded",
                "created_at": "2026-07-04T05:25:00",
                "started_at": "2026-07-04T05:25:00",
                "progress_at": "2026-07-04T05:25:00",
                "output": "partial reply",
            },
        ]
        mobile_state = {
            "counts": {"running": 0, "queued": 0},
            "agents": [{"id": "codex", "name": "Codex", "backend": "codex"}],
            "tasks": tasks,
            "session_task_counts": {"focus": 3},
        }

        render_mobile_stream_section(ui, _translator, mobile_state, "focus", [], _noop, _noop, _noop, _noop, _noop, _noop, _noop)

        footer_labels = [item.text for item in ui.elements if "cb-stream-footer-label" in item.class_text.split()]
        self.assertIn("耗时 1 分 0 秒 · 2026-07-04T05:21:00", footer_labels)
        self.assertIn("耗时 2 分 0 秒 · 2026-07-04T05:24:00", footer_labels)
        self.assertNotIn("耗时 0 秒 · 2026-07-04T05:25:00", footer_labels)

    def test_stream_code_blocks_get_paseo_like_copy_control(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        zh_locale = Path("locales/zh-CN.json").read_text(encoding="utf-8")
        en_locale = Path("locales/en-US.json").read_text(encoding="utf-8")
        stream_pre_rule = re.search(r"\.cb-stream-markdown pre \{(?P<body>.*?)\n\s*\}", source, re.S)

        self.assertIsNotNone(stream_pre_rule)
        stream_pre_body = stream_pre_rule.group("body") if stream_pre_rule else ""

        self.assertIn(".cb-stream-code-copy-button", source)
        self.assertIn("setupCodeBlockCopy", source)
        self.assertNotIn("document.querySelectorAll('.cb-stream-markdown pre')", source)
        self.assertIn("document.addEventListener('pointerover', ensureFromEvent, true)", source)
        self.assertIn("document.addEventListener('pointerdown', ensureFromEvent, true)", source)
        self.assertIn("document.addEventListener('click', (event) => {", source)
        self.assertIn("code.textContent", source)
        self.assertIn("'content_copy'", source)
        self.assertIn("'check'", source)
        self.assertIn("background: var(--cb-code-bg);", stream_pre_body)
        self.assertIn("color: var(--cb-code-ink);", stream_pre_body)
        self.assertIn("border: 1px solid var(--cb-border);", stream_pre_body)
        self.assertIn("border-radius: 6px;", stream_pre_body)
        self.assertIn("padding: 0.75rem 2.75rem 0.75rem 0.75rem;", stream_pre_body)
        self.assertIn("font-size: 12px;", stream_pre_body)
        self.assertIn("line-height: 17px;", stream_pre_body)
        self.assertIn(".cb-stream-markdown pre {\n                padding-top: 2.5rem;", source)
        self.assertNotIn("background: #f4f4f5;", stream_pre_body)
        self.assertNotIn("color: #1a1a1e;", stream_pre_body)
        self.assertIn('"copyCodeLabel": t("ui.web.mobile.copy_code", "Copy code")', source)
        self.assertIn('"copiedLabel": t("ui.web.mobile.copied", "Copied")', source)
        self.assertIn('"ui.web.mobile.copy_code": "复制代码"', zh_locale)
        self.assertIn('"ui.web.mobile.copied": "已复制"', zh_locale)
        self.assertIn('"ui.web.mobile.copy_code": "Copy code"', en_locale)
        self.assertIn('"ui.web.mobile.copied": "Copied"', en_locale)

    def test_stream_markdown_paragraph_spacing_uses_paseo_token(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        paragraph_rule = re.search(r"\.cb-stream-markdown p \{(?P<body>.*?)\n\s*\}", source, re.S)

        self.assertIsNotNone(paragraph_rule)
        paragraph_body = paragraph_rule.group("body") if paragraph_rule else ""

        self.assertIn("margin: 0 0 0.75rem;", paragraph_body)
        self.assertNotIn("0.8rem", paragraph_body)

    def test_stream_markdown_inline_formatting_matches_paseo_tokens(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        strong_rule = re.search(r"\.cb-stream-markdown strong \{(?P<body>.*?)\n\s*\}", source, re.S)
        strike_rule = re.search(r"\.cb-stream-markdown s,\n\s*\.cb-stream-markdown del \{(?P<body>.*?)\n\s*\}", source, re.S)

        self.assertIsNotNone(strong_rule)
        self.assertIsNotNone(strike_rule)
        strong_body = strong_rule.group("body") if strong_rule else ""
        strike_body = strike_rule.group("body") if strike_rule else ""

        self.assertIn("font-weight: 500;", strong_body)
        self.assertIn("color: var(--cb-muted);", strike_body)
        self.assertIn("text-decoration-line: line-through;", strike_body)

    def test_stream_markdown_blockquote_uses_paseo_tokens(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        blockquote_rule = re.search(r"\.cb-stream-markdown blockquote \{(?P<body>.*?)\n\s*\}", source, re.S)

        self.assertIsNotNone(blockquote_rule)
        blockquote_body = blockquote_rule.group("body") if blockquote_rule else ""

        self.assertIn("margin: 0.75rem 0;", blockquote_body)
        self.assertIn("border-left: 4px solid var(--cb-accent);", blockquote_body)
        self.assertIn("border-radius: 6px;", blockquote_body)
        self.assertIn("background: var(--cb-surface-raised);", blockquote_body)
        self.assertIn("padding: 0.75rem 1rem;", blockquote_body)
        self.assertIn("color: var(--cb-ink);", blockquote_body)
        self.assertNotIn("color: #71717a;", blockquote_body)

    def test_stream_markdown_heading_and_rule_tokens_match_paseo(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        h1_rule = re.search(r"\.cb-stream-markdown h1 \{(?P<body>.*?)\n\s*\}", source, re.S)
        h2_rule = re.search(r"\.cb-stream-markdown h2 \{(?P<body>.*?)\n\s*\}", source, re.S)
        h3_rule = re.search(r"\.cb-stream-markdown h3 \{(?P<body>.*?)\n\s*\}", source, re.S)
        h6_rule = re.search(r"\.cb-stream-markdown h6 \{(?P<body>.*?)\n\s*\}", source, re.S)
        hr_rule = re.search(r"\.cb-stream-markdown hr \{(?P<body>.*?)\n\s*\}", source, re.S)

        self.assertIsNotNone(h1_rule)
        self.assertIsNotNone(h2_rule)
        self.assertIsNotNone(h3_rule)
        self.assertIsNotNone(h6_rule)
        self.assertIsNotNone(hr_rule)
        h1_body = h1_rule.group("body") if h1_rule else ""
        h2_body = h2_rule.group("body") if h2_rule else ""
        h3_body = h3_rule.group("body") if h3_rule else ""
        h6_body = h6_rule.group("body") if h6_rule else ""
        hr_body = hr_rule.group("body") if hr_rule else ""

        self.assertIn("margin: 1.5rem 0 0.75rem;", h1_body)
        self.assertIn("padding-bottom: 0.5rem;", h1_body)
        self.assertIn("border-bottom: 1px solid var(--cb-border);", h1_body)
        self.assertIn("font-size: 26px;", h1_body)
        self.assertIn("font-weight: bold;", h1_body)
        self.assertIn("line-height: 32px;", h1_body)
        self.assertIn("font-size: 22px;", h2_body)
        self.assertIn("line-height: 28px;", h2_body)
        self.assertIn("margin: 1rem 0 0.5rem;", h3_body)
        self.assertIn("font-size: 20px;", h3_body)
        self.assertIn("font-weight: 600;", h3_body)
        self.assertIn("color: var(--cb-muted);", h6_body)
        self.assertIn("text-transform: uppercase;", h6_body)
        self.assertIn("letter-spacing: 0.5px;", h6_body)
        self.assertIn("height: 1px;", hr_body)
        self.assertIn("margin: 1.5rem 0;", hr_body)
        self.assertIn("background: var(--cb-border);", hr_body)

    def test_stream_markdown_list_spacing_uses_paseo_tokens(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        list_rule = re.search(r"\.cb-stream-markdown ul,\n\s*\.cb-stream-markdown ol \{(?P<body>.*?)\n\s*\}", source, re.S)
        item_rule = re.search(r"\.cb-stream-markdown li \{(?P<body>.*?)\n\s*\}", source, re.S)

        self.assertIsNotNone(list_rule)
        self.assertIsNotNone(item_rule)
        list_body = list_rule.group("body") if list_rule else ""
        item_body = item_rule.group("body") if item_rule else ""

        self.assertIn("margin: 0.25rem 0 1rem;", list_body)
        self.assertIn("padding-left: 1.25rem;", list_body)
        self.assertIn("list-style-position: outside;", list_body)
        self.assertIn("margin: 0 0 0.25rem;", item_body)
        self.assertIn("padding-left: 0.1rem;", item_body)
        self.assertNotIn("1.35rem", list_body)
        self.assertNotIn("0.55rem", list_body)

    def test_stream_tables_use_paseo_like_surface_tokens(self) -> None:
        source = Path("ui/app.py").read_text(encoding="utf-8")
        table_rule = re.search(r"\.cb-stream-markdown table \{(?P<body>.*?)\n\s*\}", source, re.S)
        cell_rule = re.search(r"\.cb-stream-markdown th,\n\s*\.cb-stream-markdown td \{(?P<body>.*?)\n\s*\}", source, re.S)
        th_rule = re.search(r"\.cb-stream-markdown th \{(?P<body>.*?)\n\s*\}", source, re.S)

        self.assertIsNotNone(table_rule)
        self.assertIsNotNone(cell_rule)
        self.assertIsNotNone(th_rule)
        table_body = table_rule.group("body") if table_rule else ""
        cell_body = cell_rule.group("body") if cell_rule else ""
        th_body = th_rule.group("body") if th_rule else ""

        self.assertIn("display: block;", table_body)
        self.assertIn("width: max-content;", table_body)
        self.assertIn("min-width: 100%;", table_body)
        self.assertIn("border-collapse: separate;", table_body)
        self.assertIn("border-spacing: 0;", table_body)
        self.assertIn("max-width: 100%;", table_body)
        self.assertIn("overflow-x: auto;", table_body)
        self.assertIn("overflow-y: hidden;", table_body)
        self.assertIn("border: 1px solid var(--cb-border);", table_body)
        self.assertIn("border-radius: 6px;", table_body)
        self.assertIn("font-size: 0.875rem;", table_body)
        self.assertIn("padding: 0.5rem;", cell_body)
        self.assertIn("min-width: 7.5rem;", cell_body)
        self.assertIn("border-right: 1px solid var(--cb-border);", cell_body)
        self.assertIn("border-bottom: 1px solid var(--cb-border);", cell_body)
        self.assertIn("color: var(--cb-ink);", cell_body)
        self.assertIn("overflow-wrap: anywhere;", cell_body)
        self.assertIn("word-break: normal;", cell_body)
        self.assertIn("background: var(--cb-surface-muted);", th_body)
        self.assertIn("font-weight: 600;", th_body)
        self.assertIn(".cb-stream-markdown th:last-child", source)
        self.assertIn(".cb-stream-markdown tbody tr:last-child td", source)
        self.assertIn(".cb-stream-markdown thead:last-child tr:last-child th", source)
        self.assertNotIn(".cb-stream-markdown tr:last-child th {\n            border-bottom: 0;", source)
        self.assertIn(".cb-stream-markdown table th", source)
        self.assertIn("border-right-color: var(--cb-border);", source)
        self.assertIn("border-bottom-color: var(--cb-border);", source)

if __name__ == "__main__":
    unittest.main()
