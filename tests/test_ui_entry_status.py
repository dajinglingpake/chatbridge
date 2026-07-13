from __future__ import annotations

import unittest
from unittest.mock import patch

from ui.app import APP_DIR, _refresh_runtime_status_on_ui_entry


class UIEntryStatusTests(unittest.TestCase):
    def test_ui_entry_forces_one_runtime_status_refresh(self) -> None:
        with patch("ui.app.refresh_dashboard_cache") as refresh_cache:
            _refresh_runtime_status_on_ui_entry()

        refresh_cache.assert_called_once_with(APP_DIR, "runtime")


if __name__ == "__main__":
    unittest.main()
