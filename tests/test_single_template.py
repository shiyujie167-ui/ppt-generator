from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app
import prompts


class SingleTemplateProductTest(unittest.TestCase):
    def test_only_final_template_is_public(self) -> None:
        self.assertEqual(list(prompts.STYLES), [prompts.FINAL_STYLE])
        self.assertEqual(len(app.STYLE_CARDS), 1)
        self.assertEqual(app.STYLE_CARDS[0]["example_id"], prompts.FINAL_TEMPLATE_ID)
        self.assertEqual(app.EXAMPLE_CARDS, [])

    def test_legacy_values_collapse_to_final_template(self) -> None:
        for value in ("company", "company_free", "mt_corporate_blue", "swiss", "dark", "custom"):
            self.assertEqual(prompts.normalize_style(value), prompts.FINAL_STYLE)
        self.assertIsNone(prompts.normalize_style("not-a-template", default=None))
        self.assertEqual(
            app._compose_style_brief("__company_free__"),
            (prompts.FINAL_STYLE, ""),
        )
        self.assertEqual(app._compose_style_brief("ex:old-example"), ("", ""))

    def test_public_history_does_not_reexpose_old_choices(self) -> None:
        old = SimpleNamespace(
            id="old",
            style="swiss",
            style_brief="old custom brief",
            kind="plan",
            recommendations=[
                {"name": "Swiss", "description": "old"},
                {"name": "Dark", "description": "old"},
            ],
            plan={"styles": [
                {"name": "Swiss", "description": "old"},
                {"name": "Dark", "description": "old"},
            ]},
            can_resume=False,
        )
        # Job.public() is intentionally not needed here; _public_job only relies
        # on the public payload shape and _can_resume(job), both easy to isolate.
        old.public = lambda: {
            "id": old.id,
            "style": old.style,
            "style_brief": old.style_brief,
            "kind": old.kind,
            "recommendations": old.recommendations,
            "plan": old.plan,
        }
        with patch.object(app, "_can_resume", return_value=False):
            result = app._public_job(old)
        self.assertEqual(result["style"], prompts.FINAL_STYLE)
        self.assertEqual(result["style_brief"], "")
        self.assertEqual(len(result["plan"]["styles"]), 1)
        self.assertEqual(result["plan"]["styles"][0]["name"], prompts.FINAL_TEMPLATE_LABEL)


if __name__ == "__main__":
    unittest.main()
