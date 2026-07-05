from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import core.sessions as sessions
import core.view_models as view_models
from core.sessions import build_session_rows_page
from core.view_models import collect_task_filter_options, paginate_filtered_tasks


def _task(session_name: str, status: str = "succeeded"):
    return SimpleNamespace(session_name=session_name, status=status)


class SessionRowsTests(unittest.TestCase):
    def test_session_rows_page_only_builds_requested_page(self) -> None:
        hub_state = SimpleNamespace(
            tasks=[_task(f"s{index:02d}") for index in range(25)],
            agents=[],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            page = build_session_rows_page(hub_state, Path(temp_dir), page=2, page_size=10)

        self.assertEqual(26, page.total_count)
        self.assertEqual(3, page.total_pages)
        self.assertEqual(2, page.page)
        self.assertEqual(10, len(page.rows))
        self.assertEqual([f"s{index:02d}" for index in range(9, 19)], [row.name for row in page.rows])

    def test_session_rows_page_reuses_aggregation_for_same_snapshot(self) -> None:
        sessions._SESSION_ROWS_PAGE_CACHE.clear()
        hub_state = SimpleNamespace(
            generated_at="2026-07-05T02:00:00",
            tasks=[_task(f"s{index:02d}") for index in range(25)],
            agents=[],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("core.sessions._hub_tasks", wraps=sessions._hub_tasks) as hub_tasks:
                first_page = build_session_rows_page(hub_state, Path(temp_dir), page=1, page_size=10)
                second_page = build_session_rows_page(hub_state, Path(temp_dir), page=2, page_size=10)

        self.assertEqual(1, hub_tasks.call_count)
        self.assertEqual([f"s{index:02d}" for index in range(0, 9)], [row.name for row in first_page.rows[1:]])
        self.assertEqual([f"s{index:02d}" for index in range(9, 19)], [row.name for row in second_page.rows])

    def test_session_rows_page_can_skip_historical_session_files(self) -> None:
        hub_state = SimpleNamespace(
            tasks=[_task("recent")],
            agents=[],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir)
            (session_dir / "main__archived.txt").write_text("archived-session-id", encoding="utf-8")
            light_page = build_session_rows_page(
                hub_state,
                session_dir,
                page=1,
                page_size=10,
                include_session_files=False,
            )
            full_page = build_session_rows_page(
                hub_state,
                session_dir,
                page=1,
                page_size=10,
                include_session_files=True,
            )

        self.assertEqual(["default", "recent"], light_page.session_names)
        self.assertEqual(["archived", "default", "recent"], full_page.session_names)

    def test_sessions_view_model_uses_page_builder(self) -> None:
        source = Path("core/view_models.py").read_text(encoding="utf-8")

        self.assertIn("build_session_rows_page(", source)
        self.assertIn("include_session_files=load_session_files", source)
        self.assertNotIn("all_session_rows = build_session_rows(", source)

    def test_task_pagination_collects_only_current_filtered_page(self) -> None:
        tasks = [
            SimpleNamespace(
                id=f"task-{index:02d}",
                session_name="focus" if index % 2 == 0 else "other",
                status="succeeded",
                agent_name="Main",
                agent_id="main",
                backend="codex",
            )
            for index in range(30)
        ]

        page_items, page, total_pages, filtered_count = paginate_filtered_tasks(
            tasks,
            page=2,
            page_size=5,
            session_name="focus",
            status="",
            agent="",
            backend="",
        )

        self.assertEqual(2, page)
        self.assertEqual(3, total_pages)
        self.assertEqual(15, filtered_count)
        self.assertEqual(["task-10", "task-12", "task-14", "task-16", "task-18"], [task.id for task in page_items])

    def test_task_pagination_filters_in_single_pass(self) -> None:
        tasks = [
            SimpleNamespace(
                id=f"task-{index:02d}",
                session_name="focus" if index % 2 == 0 else "other",
                status="succeeded",
                agent_name="Main",
                agent_id="main",
                backend="codex",
            )
            for index in range(30)
        ]

        with patch("core.view_models._task_matches_filters", wraps=view_models._task_matches_filters) as matches:
            page_items, page, total_pages, filtered_count = paginate_filtered_tasks(
                tasks,
                page=99,
                page_size=5,
                session_name="focus",
                status="",
                agent="",
                backend="",
            )

        self.assertEqual(len(tasks), matches.call_count)
        self.assertEqual(3, page)
        self.assertEqual(3, total_pages)
        self.assertEqual(15, filtered_count)
        self.assertEqual(["task-20", "task-22", "task-24", "task-26", "task-28"], [task.id for task in page_items])

    def test_task_filter_options_are_collected_in_single_pass(self) -> None:
        tasks = [
            SimpleNamespace(status="succeeded", agent_name="Main", agent_id="main", backend="codex"),
            SimpleNamespace(status="running", agent_name="", agent_id="worker", backend="claude"),
            SimpleNamespace(status="succeeded", agent_name="Main", agent_id="main", backend="codex"),
        ]

        status_options, agent_options, backend_options = collect_task_filter_options(tasks)

        self.assertEqual(["running", "succeeded"], status_options)
        self.assertEqual(["Main", "worker"], agent_options)
        self.assertEqual(["claude", "codex"], backend_options)

    def test_sessions_view_model_avoids_full_filtered_task_list(self) -> None:
        source = Path("core/view_models.py").read_text(encoding="utf-8")

        self.assertIn("paginate_filtered_tasks(", source)
        self.assertIn("collect_task_filter_options(raw_tasks)", source)
        self.assertNotIn("filtered_raw_tasks = [", source)
        self.assertNotIn("task_status_options = sorted({task.status", source)
        self.assertNotIn("task_agent_options = sorted({(task.agent_name or task.agent_id)", source)
        self.assertNotIn("task_backend_options = sorted({task.backend", source)

    def test_web_view_model_loads_hub_task_text_only_for_details(self) -> None:
        def fail_load(*_args, **_kwargs):
            raise RuntimeError("stop after load args")

        with patch("core.view_models.load_dashboard_state", side_effect=fail_load) as load_dashboard:
            with self.assertRaises(RuntimeError):
                view_models.build_web_console_view_model(Path("."), lambda key, **_kwargs: key, page_key="sessions")

        self.assertFalse(load_dashboard.call_args.kwargs["include_hub_task_text"])

        with patch("core.view_models.load_dashboard_state", side_effect=fail_load) as load_dashboard:
            with self.assertRaises(RuntimeError):
                view_models.build_web_console_view_model(
                    Path("."),
                    lambda key, **_kwargs: key,
                    page_key="sessions",
                    load_session_detail=True,
                )

        self.assertTrue(load_dashboard.call_args.kwargs["include_hub_task_text"])

        with patch("core.view_models.load_dashboard_state", side_effect=fail_load) as load_dashboard:
            with self.assertRaises(RuntimeError):
                view_models.build_web_console_view_model(
                    Path("."),
                    lambda key, **_kwargs: key,
                    page_key="sessions",
                    load_task_list=True,
                    load_task_detail=True,
                )

        self.assertTrue(load_dashboard.call_args.kwargs["include_hub_task_text"])

    def test_web_view_model_loads_hub_tasks_only_for_sessions_page(self) -> None:
        def fail_load(*_args, **_kwargs):
            raise RuntimeError("stop after load args")

        for page_key in ("home", "diagnostics"):
            with patch("core.view_models.load_dashboard_state", side_effect=fail_load) as load_dashboard:
                with self.assertRaises(RuntimeError):
                    view_models.build_web_console_view_model(Path("."), lambda key, **_kwargs: key, page_key=page_key)
            self.assertFalse(load_dashboard.call_args.kwargs["include_hub_tasks"])

        with patch("core.view_models.load_dashboard_state", side_effect=fail_load) as load_dashboard:
            with self.assertRaises(RuntimeError):
                view_models.build_web_console_view_model(Path("."), lambda key, **_kwargs: key, page_key="sessions")

        self.assertFalse(load_dashboard.call_args.kwargs["include_hub_tasks"])

        with patch("core.view_models.load_dashboard_state", side_effect=fail_load) as load_dashboard:
            with self.assertRaises(RuntimeError):
                view_models.build_web_console_view_model(
                    Path("."),
                    lambda key, **_kwargs: key,
                    page_key="sessions",
                    load_session_rows=True,
                )

        self.assertTrue(load_dashboard.call_args.kwargs["include_hub_tasks"])

    def test_sessions_task_list_is_explicitly_lazy_loaded(self) -> None:
        view_source = Path("core/view_models.py").read_text(encoding="utf-8")
        app_source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertIn("load_task_list: bool = False", view_source)
        self.assertIn('raw_tasks = hub_state.tasks if load_task_list else []', view_source)
        self.assertIn('if normalized_page_key == "sessions" and load_task_list:', view_source)
        self.assertIn('"load_task_list": False', app_source)
        self.assertIn('load_task_list=state["load_task_list"]', app_source)
        self.assertIn("ui.web.action.load_task_list", sections_source)
        self.assertIn("model.task_list_loaded", sections_source)

    def test_sessions_historical_files_are_explicitly_lazy_loaded(self) -> None:
        view_source = Path("core/view_models.py").read_text(encoding="utf-8")
        app_source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertIn("load_session_rows: bool = False", view_source)
        self.assertIn('"load_session_rows": False', app_source)
        self.assertIn('load_session_rows=state["load_session_rows"]', app_source)
        self.assertIn("def _load_session_rows() -> None:", app_source)
        self.assertIn("ui.web.action.load_session_rows", sections_source)
        self.assertIn("model.session_rows_loaded", sections_source)
        self.assertIn("load_session_files: bool = False", view_source)
        self.assertIn("include_session_files=load_session_files", view_source)
        self.assertIn('"load_session_files": False', app_source)
        self.assertIn('load_session_files=state["load_session_files"]', app_source)
        self.assertIn("def _load_session_files() -> None:", app_source)
        self.assertIn("ui.web.action.load_session_files", sections_source)
        self.assertIn("model.session_files_loaded", sections_source)

    def test_sessions_weixin_bindings_are_explicitly_lazy_loaded(self) -> None:
        dashboard_source = Path("core/dashboard.py").read_text(encoding="utf-8")
        view_source = Path("core/view_models.py").read_text(encoding="utf-8")
        app_source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertIn("load_bridge_conversations: bool | None = None", dashboard_source)
        self.assertIn("load_bridge_conversations=load_weixin_bindings", view_source)
        self.assertIn("load_weixin_bindings: bool = False", view_source)
        self.assertIn('if normalized_page_key == "sessions" and load_weixin_bindings:', view_source)
        self.assertIn('"load_weixin_bindings": False', app_source)
        self.assertIn('load_weixin_bindings=state["load_weixin_bindings"]', app_source)
        self.assertIn("ui.web.action.load_weixin_bindings", sections_source)
        self.assertIn("model.weixin_bindings_loaded", sections_source)


    def test_diagnostics_logs_are_explicitly_lazy_rendered(self) -> None:
        view_source = Path("core/view_models.py").read_text(encoding="utf-8")
        app_source = Path("ui/app.py").read_text(encoding="utf-8")
        sections_source = Path("ui/sections.py").read_text(encoding="utf-8")

        self.assertIn("load_logs: bool = False", view_source)
        self.assertIn('normalized_page_key == "diagnostics" and load_logs', view_source)
        self.assertIn('"load_logs": False', app_source)
        self.assertIn('load_logs=state["load_logs"]', app_source)
        self.assertIn('state["load_logs"] = True', app_source)
        self.assertIn("ui.web.action.load_logs", sections_source)
        self.assertIn("model.logs_loaded", sections_source)

if __name__ == "__main__":
    unittest.main()
