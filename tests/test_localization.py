from __future__ import annotations

import unittest

from core.bridge_runtime import build_media_context_reply, build_prompt_with_media
from localization import Localizer


class LocalizationTests(unittest.TestCase):
    def test_translate_renders_escaped_newlines(self) -> None:
        localizer = Localizer("en-US")
        rendered = localizer.translate("bridge.notify.current", service="on", config="off", task="off")
        self.assertIn("Current system notices\nService lifecycle: on", rendered)
        self.assertNotIn("\\n", rendered)

    def test_unknown_after_restart_status_is_localized_for_stream_ui(self) -> None:
        self.assertEqual("Unknown after restart", Localizer("en-US").translate("bridge.task.status.unknown_after_restart"))
        self.assertEqual("重启后未知", Localizer("zh-CN").translate("bridge.task.status.unknown_after_restart"))


    def test_media_prompt_uses_localizer(self) -> None:
        localizer = Localizer("en-US")
        rendered = build_prompt_with_media(
            "describe",
            [{"kind": "image", "name": "scene.png", "path": "C:/tmp/scene.png"}],
            [],
            localizer.translate,
        )
        self.assertEqual("describe C:/tmp/scene.png", rendered)
        self.assertNotIn("\n", rendered)
        self.assertNotIn("image: scene.png", rendered)
        self.assertNotIn("Local path", rendered)
        self.assertNotIn("The user sent the following attachments", rendered)
        self.assertNotIn("inspect the image at the local path", rendered)
        self.assertEqual(
            "Received 1 attachment(s). Send a text instruction next and I will handle them together.",
            build_media_context_reply([{"kind": "file", "name": "a.txt", "path": "C:/tmp/a.txt"}], localizer.translate),
        )

if __name__ == "__main__":
    unittest.main()
