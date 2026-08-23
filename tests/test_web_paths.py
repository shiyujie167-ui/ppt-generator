from __future__ import annotations

from pathlib import Path
import unittest

import config


class WebPathTest(unittest.TestCase):
    def test_normalize_base_path(self) -> None:
        self.assertEqual(config._normalize_base_path(""), "")
        self.assertEqual(config._normalize_base_path("/"), "")
        self.assertEqual(config._normalize_base_path("ppt-generator"), "/ppt-generator")
        self.assertEqual(config._normalize_base_path("/ppt-generator/"), "/ppt-generator")

    def test_web_path_uses_configured_prefix(self) -> None:
        original = config.BASE_PATH
        try:
            config.BASE_PATH = "/ppt-generator"
            self.assertEqual(config.web_path("/"), "/ppt-generator/")
            self.assertEqual(config.web_path("login"), "/ppt-generator/login")
        finally:
            config.BASE_PATH = original

    def test_production_service_overrides_legacy_base_path(self) -> None:
        unit = (
            Path(__file__).resolve().parents[1] / "ops" / "ppt-web.service"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "ExecStart=/usr/bin/env PPT_BASE_PATH=/ppt-generator ",
            unit,
        )
        self.assertNotIn("Environment=PPT_BASE_PATH=", unit)


if __name__ == "__main__":
    unittest.main()
