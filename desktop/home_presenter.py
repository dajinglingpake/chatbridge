from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtWidgets import QLabel, QPushButton, QPlainTextEdit

from core.view_models import build_home_view_model


@dataclass
class HomeWidgets:
    summary_label: QLabel
    next_step_label: QLabel
    stack_badge: QLabel
    primary_button: QPushButton
    quickstart_status: QLabel
    quickstart_steps: QPlainTextEdit
    overview_text: QPlainTextEdit


def render_home_state(
    widgets: HomeWidgets,
    snapshot: Any,
    checks: dict[str, Any],
    bridge_state: dict[str, Any],
    active_account_id: str,
    accounts_dir: Path,
    t: Callable[..., str],
) -> str:
    model = build_home_view_model(
        snapshot=snapshot,
        checks=checks,
        bridge_state=bridge_state,
        active_account_id=active_account_id,
        accounts_dir=accounts_dir,
        t=t,
    )
    widgets.stack_badge.setText(model.badge_text)
    widgets.stack_badge.setStyleSheet(model.badge_style)
    widgets.overview_text.setPlainText(model.overview_text)
    widgets.summary_label.setText(model.summary_text)
    widgets.primary_button.setText(model.primary_label)
    widgets.next_step_label.setText(model.primary_hint)
    widgets.quickstart_steps.setPlainText(model.quickstart_text)
    widgets.quickstart_status.setText(model.quickstart_status)
    return model.primary_action
