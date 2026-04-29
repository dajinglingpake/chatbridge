from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from core.navigation import PRIMARY_PAGES
from core.navigation import PageDefinition


@dataclass(frozen=True)
class AppShellSchema:
    app_name: str
    pages: Tuple[PageDefinition, ...]


APP_SHELL = AppShellSchema(
    app_name="ChatBridge",
    pages=PRIMARY_PAGES,
)
