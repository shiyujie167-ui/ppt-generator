from __future__ import annotations

import json
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import jobs
import native_company
import prompts
import runner
import runner_agent


class SingleTemplateParsingTest(unittest.TestCase):
    def test_recommendations_accept_only_one_canonical_template(self) -> None:
        result = runner_agent._parse_recommendations(
            '[{"name":"历史 Swiss 风格","description":"材料适配说明"}]'
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], prompts.FINAL_TEMPLATE_LABEL)

        with self.assertRaises(runner_agent.AgentError):
            runner_agent._parse_recommendations(
                '[{"name":"A","description":"a"},{"name":"B","description":"b"}]'
            )

    def test_plan_styles_are_always_the_single_mock_template(self) -> None:
        result = runner_agent._parse_plan(
            '{"styles":[{"name":"旧模板","description":"旧说明"}],'
            '"outline":[{"title":"页面一","points":[]}]}'
        )
        self.assertEqual(result["styles"], [prompts.MOCK_RECOMMENDATIONS[0]])
        self.assertEqual(len(result["styles"]), 1)


class ToolBoxProjectPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "release" / "engine"
        self.projects = self.root / "projects"
        self.uploads = self.root / "data" / "uploads"
        self.repo.mkdir(parents=True)
        self.projects.mkdir()
        self.uploads.mkdir(parents=True)
        (self.repo / "projects").symlink_to(self.projects, target_is_directory=True)
        scripts = self.repo / "skills" / "ppt-master" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "svg_to_pptx.py").write_text("print('ok')\n", encoding="utf-8")

        self.old_paths = config.PPT_MASTER_REPO, config.UPLOADS_DIR
        config.PPT_MASTER_REPO = self.repo
        config.UPLOADS_DIR = self.uploads
        self.job = jobs.Job(
            id="job123",
            created_at=time.time(),
            style="company_free",
        )
        (self.uploads / self.job.id).mkdir()
        self.owned = self.projects / "web_job123_ppt169_20260807"
        self.owned.mkdir()
        self.other = self.projects / "web_other_ppt169_20260807"
        self.other.mkdir()
        self.toolbox = runner_agent.ToolBox(
            self.job,
            lambda _job_id, _message: None,
            time.monotonic() + 60,
        )

    def tearDown(self) -> None:
        config.PPT_MASTER_REPO, config.UPLOADS_DIR = self.old_paths
        self.temp_dir.cleanup()

    def test_reads_own_project_through_external_projects_symlink(self) -> None:
        note = self.owned / "note.md"
        note.write_text("hello", encoding="utf-8")

        result = self.toolbox.tool_read_text_file(str(note))
        listing = self.toolbox.tool_list_files(str(self.owned), "*.md")

        self.assertIn("hello", result["content"])
        self.assertEqual(
            [Path(entry["path"]).resolve() for entry in listing["entries"]],
            [note.resolve()],
        )

    def test_rejects_another_jobs_project(self) -> None:
        note = self.other / "note.md"
        note.write_text("private", encoding="utf-8")

        with self.assertRaises(runner_agent.AgentError):
            self.toolbox.tool_read_text_file(str(note))
        with self.assertRaises(runner_agent.AgentError):
            self.toolbox._validate_script_args("svg_to_pptx.py", [str(self.other)])

    def test_company_free_export_is_forced_to_flat(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(runner_agent.subprocess, "run", return_value=completed) as run:
            result = self.toolbox.tool_run_ppt_script(
                "svg_to_pptx.py",
                [str(self.owned), "--pptx-structure", "structured", "--no-notes"],
            )

        command = run.call_args.args[0]
        structure_index = command.index("--pptx-structure")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(command[structure_index + 1], "flat")

    def test_company_cover_syncs_when_checker_receives_svg_output_dir(self) -> None:
        self.job.ai_images = True
        scripts = self.repo / "skills" / "ppt-master" / "scripts"
        (scripts / "svg_quality_checker.py").write_text("print('ok')\n", encoding="utf-8")
        svg_output = self.owned / "svg_output"
        images = self.owned / "images"
        svg_output.mkdir()
        images.mkdir()
        (svg_output / "01_cover.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<rect id="cover-white-panel" x="338" y="0" width="942" height="277"/>'
            '<g id="company-cover-hero-slot" data-pptx-placeholder="picture" '
            'data-pptx-idx="10"><image id="company-cover-hero-carrier" '
            'data-pptx-carrier="true" href="data:image/png;base64,transparent"/>'
            '</g></svg>',
            encoding="utf-8",
        )
        (images / runner_agent.COMPANY_COVER_IMAGE_NAME).write_bytes(
            b"\x89PNG\r\n\x1a\nplaceholder"
        )
        (images / "image_prompts.json").write_text(
            json.dumps({"items": [{
                "filename": runner_agent.COMPANY_COVER_IMAGE_NAME,
                "page_role": "hero_page",
                "text_policy": "none",
                "aspect_ratio": "3.4:1",
                "status": "Generated",
            }]}),
            encoding="utf-8",
        )

        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(runner_agent.subprocess, "run", return_value=completed) as run:
            result = self.toolbox.tool_run_ppt_script(
                "svg_quality_checker.py",
                [str(svg_output)],
            )

        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(run.called)
        cover_svg = (svg_output / "01_cover.svg").read_text(encoding="utf-8")
        hero_fragment = cover_svg.rsplit("<image", 1)[-1].split("/>", 1)[0]
        self.assertIn(f'../images/{runner_agent.COMPANY_COVER_IMAGE_NAME}', hero_fragment)
        self.assertNotIn("data-pptx-placeholder", cover_svg)
        self.assertNotIn("data-pptx-carrier", hero_fragment)
        self.assertNotIn("data-pptx-idx", cover_svg)


class CompanyFreeRecoveryTest(unittest.TestCase):
    def test_prompt_locks_company_free_to_flat_export(self) -> None:
        brief = prompts.STYLES["company_free"]["brief"]

        self.assertIn("mode: flat", brief)
        self.assertIn("template_reuse_scope: style", brief)
        self.assertIn("禁止写 structured", brief)

    def test_postprocess_exports_flat_base_when_agent_did_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / "template.pptx"
            template.write_bytes(b"template")
            projects = root / "projects"
            project = projects / "web_job123_ppt169_20260807"
            (project / "exports").mkdir(parents=True)
            (project / "svg_output").mkdir()
            svg = project / "svg_output" / "P01.svg"
            svg.write_text(
                '<svg data-pptx-master="m" data-pptx-master-name="Master" '
                'data-pptx-layout="l" data-pptx-layout-name="Layout">'
                '<rect data-pptx-layer="slide" data-custom-master-name="keep"/>'
                '</svg>',
                encoding="utf-8",
            )
            job = SimpleNamespace(
                id="job123",
                style="company_free",
                topic="季度复盘",
            )

            def export_base(_script: str, _args: list[str]) -> dict:
                (project / "exports" / "base.pptx").write_bytes(b"base")
                return {"exit_code": 0, "stdout": "", "stderr": ""}

            toolbox = SimpleNamespace(
                projects=projects,
                project_prefix="web_job123",
                tool_run_ppt_script=export_base,
            )

            def merge(_template: Path, _base: Path, output: Path, _fill: dict) -> dict:
                output.write_bytes(b"merged")
                return {
                    "body_slides": 1,
                    "toc_rows_filled": 1,
                    "slide_numbers_normalized": 1,
                }

            with (
                patch.object(config, "company_template_source", return_value=template),
                patch.object(native_company, "merge", side_effect=merge),
                patch.object(native_company, "verify", return_value=[]),
            ):
                runner_agent._company_postprocess(job, lambda _job_id, _message: None, toolbox)

            self.assertTrue((project / "exports" / "base_company.pptx").is_file())
            sanitized = svg.read_text(encoding="utf-8")
            self.assertNotIn("data-pptx-master", sanitized)
            self.assertNotIn("data-pptx-layout", sanitized)
            self.assertNotIn("data-pptx-layer", sanitized)
            self.assertNotIn(" -name=", sanitized)
            self.assertIn('data-custom-master-name="keep"', sanitized)
            ET.fromstring(sanitized)


class CompanyCoverImageTest(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        project = root / "projects" / "web_job123_ppt169_20260812"
        (project / "exports").mkdir(parents=True)
        (project / "images").mkdir()
        (project / "svg_output").mkdir()
        (project / "svg_final").mkdir()
        (project / "svg_output" / "01_cover.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<rect id="cover-white-panel" x="338" y="0" width="942" height="277"/>'
            '<g id="company-cover-hero-slot" data-pptx-placeholder="picture" '
            'data-pptx-idx="10" data-pptx-bounds="338 0 942 277">'
            '<image id="company-cover-hero-carrier" data-pptx-carrier="true" '
            'href="data:image/png;base64,transparent" x="338" y="0" '
            'width="942" height="277" preserveAspectRatio="xMidYMid slice"/>'
            '</g>'
            '</svg>',
            encoding="utf-8",
        )
        (project / "exports" / "base.pptx").write_bytes(b"base")
        return project

    def test_postprocess_resolves_generated_company_cover_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._project(root)
            cover = project / runner_agent.COMPANY_COVER_IMAGE_RELATIVE
            cover.write_bytes(b"\x89PNG\r\n\x1a\nplaceholder")
            (project / "images" / "image_prompts.json").write_text(
                json.dumps({"items": [{
                    "filename": runner_agent.COMPANY_COVER_IMAGE_NAME,
                    "page_role": "hero_page",
                    "text_policy": "none",
                    "aspect_ratio": "3.4:1",
                    "status": "Generated",
                }]}),
                encoding="utf-8",
            )
            (project / "native_fill.json").write_text(
                json.dumps({
                    "title": "季度复盘",
                    "cover_image": runner_agent.COMPANY_COVER_IMAGE_RELATIVE,
                }),
                encoding="utf-8",
            )
            template = root / "template.pptx"
            template.write_bytes(b"template")
            job = SimpleNamespace(
                id="job123", style="company", topic="季度复盘", ai_images=True,
            )
            def run_script(script: str, _args: list[str]) -> dict:
                self.assertEqual(script, "finalize_svg.py")
                svg_final = project / "svg_final"
                preview = (project / "svg_output" / "01_cover.svg").read_text(encoding="utf-8")
                preview = preview.replace(
                    f'../images/{runner_agent.COMPANY_COVER_IMAGE_NAME}',
                    'data:image/png;base64,embedded-cover',
                )
                (svg_final / "01_cover.svg").write_text(preview, encoding="utf-8")
                return {"exit_code": 0, "stdout": "", "stderr": ""}

            toolbox = SimpleNamespace(
                projects=root / "projects",
                project_prefix="web_job123",
                tool_run_ppt_script=run_script,
            )
            received: dict = {}

            def merge(_template: Path, _base: Path, output: Path, fill: dict) -> dict:
                received.update(fill)
                output.write_bytes(b"merged")
                return {
                    "body_slides": 1,
                    "toc_rows_filled": 1,
                    "slide_numbers_normalized": 1,
                    "cover_image_filled": True,
                }

            with (
                patch.object(config, "company_template_source", return_value=template),
                patch.object(runner_agent, "_sparse_slot_pages", return_value=[]),
                patch.object(native_company, "merge", side_effect=merge),
                patch.object(native_company, "verify", return_value=[]) as verify,
            ):
                runner_agent._company_postprocess(job, lambda _job_id, _message: None, toolbox)

            self.assertEqual(Path(received["cover_image"]), cover.resolve())
            self.assertTrue(verify.call_args.kwargs["expect_cover_image"])
            cover_svg = (project / "svg_output" / "01_cover.svg").read_text(encoding="utf-8")
            self.assertIn(f'href="../images/{runner_agent.COMPANY_COVER_IMAGE_NAME}"', cover_svg)
            self.assertIn('preserveAspectRatio="xMidYMid slice"', cover_svg)
            self.assertEqual(cover_svg.count('id="company-cover-hero-slot"'), 1)

    def test_postprocess_company_free_uses_the_same_cover_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._project(root)
            cover = project / runner_agent.COMPANY_COVER_IMAGE_RELATIVE
            cover.write_bytes(b"\x89PNG\r\n\x1a\nplaceholder")
            (project / "images" / "image_prompts.json").write_text(
                json.dumps({"items": [{
                    "filename": runner_agent.COMPANY_COVER_IMAGE_NAME,
                    "page_role": "hero_page",
                    "text_policy": "none",
                    "aspect_ratio": "3.4:1",
                    "status": "Generated",
                }]}),
                encoding="utf-8",
            )
            (project / "native_fill.json").write_text(
                json.dumps({
                    "title": "自由版季度复盘",
                    "cover_image": runner_agent.COMPANY_COVER_IMAGE_RELATIVE,
                }),
                encoding="utf-8",
            )
            template = root / "template.pptx"
            template.write_bytes(b"template")
            job = SimpleNamespace(
                id="job123", style="company_free", topic="季度复盘", ai_images=True,
            )
            calls: list[tuple[str, list[str]]] = []

            def run_script(script: str, args: list[str]) -> dict:
                calls.append((script, args))
                if script == "svg_to_pptx.py":
                    (project / "exports" / "base.pptx").write_bytes(b"base")
                elif script == "finalize_svg.py":
                    preview = (project / "svg_output" / "01_cover.svg").read_text(
                        encoding="utf-8"
                    ).replace(
                        f'../images/{runner_agent.COMPANY_COVER_IMAGE_NAME}',
                        'data:image/png;base64,embedded-cover',
                    )
                    (project / "svg_final" / "01_cover.svg").write_text(
                        preview,
                        encoding="utf-8",
                    )
                return {"exit_code": 0, "stdout": "", "stderr": ""}

            toolbox = SimpleNamespace(
                projects=root / "projects",
                project_prefix="web_job123",
                tool_run_ppt_script=run_script,
            )

            def merge(_template: Path, _base: Path, output: Path, fill: dict) -> dict:
                self.assertEqual(Path(fill["cover_image"]), cover.resolve())
                output.write_bytes(b"merged")
                return {
                    "body_slides": 1,
                    "toc_rows_filled": 1,
                    "slide_numbers_normalized": 1,
                    "cover_image_filled": True,
                }

            with (
                patch.object(config, "company_template_source", return_value=template),
                patch.object(native_company, "merge", side_effect=merge),
                patch.object(native_company, "verify", return_value=[]),
            ):
                runner_agent._company_postprocess(
                    job,
                    lambda _job_id, _message: None,
                    toolbox,
                )

            self.assertIn(("finalize_svg.py", [f"projects/{project.name}"]), calls)
            flat_cover = (project / "svg_output" / "01_cover.svg").read_text(
                encoding="utf-8"
            )
            self.assertIn('id="company-cover-hero-carrier"', flat_cover)
            hero_fragment = flat_cover.rsplit("<image", 1)[-1].split("/>", 1)[0]
            self.assertNotIn("data-pptx-placeholder", hero_fragment)
            self.assertNotIn("data-pptx-carrier", hero_fragment)
            self.assertNotIn("data-pptx-idx", hero_fragment)

    def test_postprocess_rejects_missing_or_outside_cover_image(self) -> None:
        for declared in ("images/missing.png", "../outside.png"):
            with self.subTest(declared=declared), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = self._project(root)
                (project / "native_fill.json").write_text(
                    json.dumps({"title": "季度复盘", "cover_image": declared}),
                    encoding="utf-8",
                )
                template = root / "template.pptx"
                template.write_bytes(b"template")
                job = SimpleNamespace(
                    id="job123", style="company", topic="季度复盘", ai_images=True,
                )
                toolbox = SimpleNamespace(projects=root / "projects", project_prefix="web_job123")
                with (
                    patch.object(config, "company_template_source", return_value=template),
                    patch.object(runner_agent, "_sparse_slot_pages", return_value=[]),
                    patch.object(native_company, "merge") as merge,
                ):
                    with self.assertRaises(runner_agent.AgentError):
                        runner_agent._company_postprocess(
                            job, lambda _job_id, _message: None, toolbox
                        )
                merge.assert_not_called()

    def test_resolver_rejects_wrong_company_cover_manifest_contract(self) -> None:
        good_item = {
            "filename": runner_agent.COMPANY_COVER_IMAGE_NAME,
            "page_role": "hero_page",
            "text_policy": "none",
            "aspect_ratio": "3.4:1",
            "status": "Generated",
        }
        for field, value in (
            ("page_role", "local"),
            ("text_policy", "embedded"),
            ("aspect_ratio", "1:1"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                project = self._project(Path(tmp))
                cover = project / runner_agent.COMPANY_COVER_IMAGE_RELATIVE
                cover.write_bytes(b"\x89PNG\r\n\x1a\nplaceholder")
                item = dict(good_item)
                item[field] = value
                (project / "images" / "image_prompts.json").write_text(
                    json.dumps({"items": [item]}),
                    encoding="utf-8",
                )
                fill = {"cover_image": runner_agent.COMPANY_COVER_IMAGE_RELATIVE}
                with self.assertRaisesRegex(runner_agent.AgentError, "清单契约不匹配"):
                    runner_agent._resolve_company_cover_image(
                        project,
                        fill,
                        required=True,
                    )


class CompanyPreviewCollectionTest(unittest.TestCase):
    def test_ai_company_prefers_synced_cover_svg_over_old_montage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            project = projects / "web_job123_ppt169_20260812"
            exports = project / "exports"
            validation = project / "validation"
            svg_final = project / "svg_final"
            output_root = root / "outputs"
            exports.mkdir(parents=True)
            validation.mkdir()
            svg_final.mkdir()
            base = exports / "base.pptx"
            merged = exports / "base_company.pptx"
            base.write_bytes(b"base")
            merged.write_bytes(b"merged")
            (validation / "final_montage.png").write_bytes(b"old montage")
            cover_svg = svg_final / "01_cover.svg"
            cover_svg.write_text("<svg><!-- generated hero --></svg>", encoding="utf-8")
            job = SimpleNamespace(
                id="job123",
                style="company",
                ai_images=True,
            )
            with (
                patch.object(config, "PPT_MASTER_REPO", root),
                patch.object(config, "OUTPUTS_DIR", output_root),
            ):
                outputs, preview = runner.collect_outputs(
                    job,
                    since=0,
                    project_prefix="web_job123",
                )

            self.assertEqual(outputs[0], merged.name)
            self.assertEqual(preview, "封面预览.svg")
            self.assertEqual(
                (output_root / job.id / preview).read_text(encoding="utf-8"),
                cover_svg.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
