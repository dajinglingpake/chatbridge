from __future__ import annotations

import unittest

from localization import Localizer


class LocalizationTests(unittest.TestCase):
    def test_translate_renders_escaped_newlines(self) -> None:
        localizer = Localizer("en-US")
        rendered = localizer.translate("bridge.notify.current", service="on", config="off", task="off")
        self.assertIn("Current system notices\nService lifecycle: on", rendered)
        self.assertNotIn("\\n", rendered)


if __name__ == "__main__":
    unittest.main()
