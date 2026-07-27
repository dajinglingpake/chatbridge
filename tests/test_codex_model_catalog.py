from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import codex_model_catalog as catalog


class CodexModelCatalogTests(unittest.TestCase):
    def test_catalog_preserves_codex_recommended_order(self) -> None:
        payload = {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "visibility": "list",
                    "priority": 1,
                    "default_reasoning_level": "low",
                    "supported_reasoning_levels": [
                        {"effort": "xhigh", "description": "Extra high reasoning depth"},
                        {"effort": "max", "description": "Maximum reasoning depth"},
                        {"effort": "ultra", "description": "Maximum reasoning with automatic task delegation"},
                    ],
                },
                {
                    "slug": "gpt-5.6-terra",
                    "display_name": "GPT-5.6-Terra",
                    "visibility": "recommended",
                    "priority": 2,
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [{"effort": "medium"}],
                },
                {
                    "slug": "codex-auto-review",
                    "display_name": "Codex Auto Review",
                    "visibility": "hide",
                    "priority": 99,
                    "supported_reasoning_levels": [],
                },
            ]
        }
        with (
            patch.object(catalog.HubConfig, "load", return_value=SimpleNamespace(codex_command="codex")),
            patch.object(
                catalog.subprocess,
                "run",
                return_value=SimpleNamespace(stdout=json.dumps(payload)),
            ) as run,
        ):
            entries = catalog.load_codex_model_catalog()

        self.assertEqual(["gpt-5.6-sol", "gpt-5.6-terra"], [entry["slug"] for entry in entries])
        self.assertEqual(["xhigh", "max", "ultra"], entries[0]["reasoning_levels"])
        run.assert_called_once_with(
            ["codex", "debug", "models"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def test_reasoning_effort_labels_preserve_codex_names(self) -> None:
        self.assertEqual("XHigh", catalog.display_reasoning_effort("xhigh"))
        self.assertEqual("Max", catalog.display_reasoning_effort("max"))
        self.assertEqual("Ultra", catalog.display_reasoning_effort("ultra"))

    def test_cached_catalog_avoids_restarting_codex_for_each_client(self) -> None:
        entries = [
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6-Sol",
                "description": "Latest frontier agentic coding model.",
                "default_reasoning": "low",
                "reasoning_levels": ["low", "medium", "high", "ultra"],
            }
        ]
        with (
            patch.object(catalog, "_CODEX_MODEL_CATALOG_CACHE", None),
            patch.object(catalog, "load_codex_model_catalog", return_value=entries) as load,
            patch.object(catalog.time, "monotonic", side_effect=[100.0, 100.0, 100.5]),
        ):
            first = catalog.load_codex_model_catalog_cached()
            first[0]["reasoning_levels"].append("mutated")
            second = catalog.load_codex_model_catalog_cached()

        load.assert_called_once_with()
        self.assertEqual(["low", "medium", "high", "ultra"], second[0]["reasoning_levels"])


if __name__ == "__main__":
    unittest.main()
