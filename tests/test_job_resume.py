from __future__ import annotations

import json
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
import config
import db
import jobs
import runner_agent


class JobResumeStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_file = config.DB_FILE
        self.old_queue = jobs._QUEUE
        if db._CONN is not None:
            db._CONN.close()
        db._CONN = None
        config.DB_FILE = Path(self.temp_dir.name) / "app.db"
        jobs._QUEUE = queue.Queue()
        jobs._JOBS.clear()
        jobs._ready = False
        db.init()

    def tearDown(self) -> None:
        if db._CONN is not None:
            db._CONN.close()
        db._CONN = None
        config.DB_FILE = self.old_db_file
        jobs._QUEUE = self.old_queue
        jobs._JOBS.clear()
        jobs._ready = False
        self.temp_dir.cleanup()

    def test_resume_failed_persists_intent_and_enqueues_once(self) -> None:
        job = jobs.create(
            enqueue=False,
            user_id=1,
            kind="generate",
            status="failed",
            started_at=10,
            finished_at=20,
            outputs=["old.pptx"],
            preview="old.png",
            cost_usd=1.5,
            error="API HTTP 502:upstream unavailable",
        )

        project_name = f"web_{job.id}_ppt169_20260813"
        resumed = jobs.resume_failed(job.id, project_name)

        self.assertIs(resumed, job)
        self.assertEqual(jobs._QUEUE.get_nowait(), job.id)
        self.assertTrue(jobs._QUEUE.empty())
        self.assertEqual(job.status, "queued")
        self.assertTrue(job.resume_existing)
        self.assertEqual(job.resume_project, project_name)
        self.assertIsNone(job.started_at)
        self.assertIsNone(job.finished_at)
        self.assertEqual(job.outputs, [])
        self.assertEqual(job.preview, "")
        self.assertIsNone(job.cost_usd)
        self.assertEqual(job.error, "")
        row = db.fetchone("SELECT * FROM jobs WHERE id = ?", (job.id,))
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["resume_existing"], 1)
        self.assertEqual(row["resume_project"], project_name)
        self.assertEqual(json.loads(row["outputs"]), [])

        self.assertIsNone(jobs.resume_failed(job.id, project_name))
        self.assertTrue(jobs._QUEUE.empty())

    def test_resume_failed_rejects_database_state_race_without_enqueuing(self) -> None:
        job = jobs.create(enqueue=False, user_id=1, kind="generate", status="failed")
        db.run("UPDATE jobs SET status = 'done' WHERE id = ?", (job.id,))

        resumed = jobs.resume_failed(job.id, f"web_{job.id}_ppt169_20260813")

        self.assertIsNone(resumed)
        self.assertEqual(job.status, "failed")
        self.assertTrue(jobs._QUEUE.empty())

    def test_resume_failed_rejects_invalid_project_name(self) -> None:
        job = jobs.create(enqueue=False, user_id=1, kind="generate", status="failed")

        self.assertIsNone(jobs.resume_failed(job.id, "../other-project"))
        self.assertEqual(job.status, "failed")
        self.assertTrue(jobs._QUEUE.empty())

    def test_resume_fields_survive_database_reload(self) -> None:
        job = jobs.create(enqueue=False, user_id=1, kind="generate", status="failed")
        project_name = f"web_{job.id}_ppt169_20260813"
        jobs.resume_failed(job.id, project_name)
        jobs._QUEUE.get_nowait()
        jobs._JOBS.clear()

        jobs.load()

        loaded = jobs.get(job.id)
        self.assertIsNotNone(loaded)
        self.assertTrue(loaded.resume_existing)
        self.assertEqual(loaded.resume_project, project_name)
        self.assertEqual(jobs._QUEUE.get_nowait(), job.id)

    def test_queued_job_is_claimed_once(self) -> None:
        job = jobs.create(enqueue=False, user_id=1, kind="generate", status="queued")

        first = jobs._claim_queued(job.id)
        second = jobs._claim_queued(job.id)

        self.assertIs(first, job)
        self.assertIsNone(second)
        self.assertEqual(job.status, "running")


class JobResumeApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.job = SimpleNamespace(
            id="job-1",
            user_id=7,
            kind="generate",
            status="failed",
            error="API HTTP 502:upstream unavailable",
            public=lambda: {"id": "job-1", "status": "failed"},
        )
        self.project = Path("/tmp/web_job-1_ppt169_20260813")

    def test_public_job_exposes_resume_for_transient_failure_with_checkpoint(self) -> None:
        with (
            patch.object(app.runner_agent, "is_transient_api_failure", return_value=True) as transient,
            patch.object(app.runner_agent, "find_resume_checkpoint", return_value=self.project) as checkpoint,
        ):
            payload = app._public_job(self.job)

        self.assertTrue(payload["can_resume"])
        transient.assert_called_once_with(self.job.error)
        checkpoint.assert_called_once_with(self.job)

    def test_real_checkpoint_without_svg_is_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_repo = config.PPT_MASTER_REPO
            config.PPT_MASTER_REPO = Path(temp_dir)
            try:
                project = config.PPT_MASTER_REPO / "projects" / "web_job-1_ppt169_20260813"
                project.mkdir(parents=True)
                (project / "design_spec.md").write_text("spec", encoding="utf-8")
                (project / "spec_lock.md").write_text("lock", encoding="utf-8")
                self.assertTrue(app._public_job(self.job)["can_resume"])
            finally:
                config.PPT_MASTER_REPO = old_repo

    def test_multiple_valid_checkpoints_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_repo = config.PPT_MASTER_REPO
            config.PPT_MASTER_REPO = Path(temp_dir)
            try:
                for date in ("20260812", "20260813"):
                    project = config.PPT_MASTER_REPO / "projects" / f"web_job-1_ppt169_{date}"
                    project.mkdir(parents=True)
                    (project / "design_spec.md").write_text("spec", encoding="utf-8")
                    (project / "spec_lock.md").write_text("lock", encoding="utf-8")
                self.assertFalse(app._public_job(self.job)["can_resume"])
            finally:
                config.PPT_MASTER_REPO = old_repo

    def test_non_transient_failure_is_not_resumable(self) -> None:
        self.job.error = "公司模板保真度校验未通过"
        with (
            patch.object(app.runner_agent, "is_transient_api_failure", return_value=False),
            patch.object(app.runner_agent, "find_resume_checkpoint") as checkpoint,
        ):
            self.assertFalse(app._public_job(self.job)["can_resume"])
        checkpoint.assert_not_called()

    def test_resume_endpoint_checks_ownership(self) -> None:
        with (
            patch.object(app.jobs, "get", return_value=self.job),
            patch.object(app.jobs, "resume_failed") as resume,
        ):
            response = app.resume_job(self.job.id, user={"id": 8})

        self.assertEqual(response.status_code, 404)
        resume.assert_not_called()

    def test_resume_endpoint_requeues_through_atomic_store_operation(self) -> None:
        with (
            patch.object(app.jobs, "get", return_value=self.job),
            patch.object(app.runner_agent, "is_transient_api_failure", return_value=True),
            patch.object(app.runner_agent, "find_resume_checkpoint", return_value=self.project),
            patch.object(app.jobs, "resume_failed", return_value=self.job) as resume,
            patch.object(app.runner, "_log"),
        ):
            response = app.resume_job(self.job.id, user={"id": 7})

        self.assertEqual(response.status_code, 200)
        resume.assert_called_once_with(self.job.id, self.project.name)

    def test_resume_endpoint_rejects_missing_checkpoint(self) -> None:
        with (
            patch.object(app.jobs, "get", return_value=self.job),
            patch.object(app.runner_agent, "is_transient_api_failure", return_value=True),
            patch.object(app.runner_agent, "find_resume_checkpoint", return_value=None),
            patch.object(app.jobs, "resume_failed") as resume,
        ):
            response = app.resume_job(self.job.id, user={"id": 7})

        self.assertEqual(response.status_code, 409)
        resume.assert_not_called()

    def test_resume_endpoint_rejects_duplicate_transition(self) -> None:
        with (
            patch.object(app.jobs, "get", return_value=self.job),
            patch.object(app.runner_agent, "is_transient_api_failure", return_value=True),
            patch.object(app.runner_agent, "find_resume_checkpoint", return_value=self.project),
            patch.object(app.jobs, "resume_failed", return_value=None),
        ):
            response = app.resume_job(self.job.id, user={"id": 7})

        self.assertEqual(response.status_code, 409)


class RunnerResumeCheckpointTest(unittest.TestCase):
    def test_resume_prompt_lists_existing_pages_and_generated_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "svg_output").mkdir()
            (project / "images").mkdir()
            (project / "svg_output" / "01_cover.svg").write_text("<svg/>", encoding="utf-8")
            (project / "images" / "image_prompts.json").write_text(
                json.dumps({"items": [
                    {"filename": "ready.png", "status": "Generated"},
                    {"filename": "later.png", "status": "Pending"},
                ]}),
                encoding="utf-8",
            )
            (project / "images" / "ready.png").write_bytes(b"image")

            prompt = runner_agent._resume_prompt(project)

            self.assertIn("01_cover.svg", prompt)
            self.assertIn("ready.png", prompt)
            self.assertIn("禁止重新生成", prompt)
            self.assertNotIn("later.png", prompt)

    def test_resume_toolbox_blocks_init_install_and_sibling_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "engine"
            projects = repo / "projects"
            checkpoint = projects / "web_job123_ppt169_20260813"
            sibling = projects / "web_job123_ppt169_20260812"
            checkpoint.mkdir(parents=True)
            sibling.mkdir()
            scripts = repo / "skills" / "ppt-master" / "scripts"
            scripts.mkdir(parents=True)
            old_repo, old_uploads = config.PPT_MASTER_REPO, config.UPLOADS_DIR
            config.PPT_MASTER_REPO = repo
            config.UPLOADS_DIR = root / "uploads"
            (config.UPLOADS_DIR / "job123").mkdir(parents=True)
            try:
                job = jobs.Job(
                    id="job123", created_at=0, resume_existing=True,
                    resume_project=checkpoint.name,
                )
                toolbox = runner_agent.ToolBox(job, lambda *_args: None, 10**12)
                with self.assertRaisesRegex(runner_agent.AgentError, "禁止重新初始化"):
                    toolbox._validate_script_args("project_manager.py", ["init", "web_job123"])
                with self.assertRaisesRegex(runner_agent.AgentError, "检查点"):
                    toolbox._resolve_owned(str(sibling / "page.svg"))
                with self.assertRaisesRegex(runner_agent.AgentError, "其他任务"):
                    toolbox._resolve_read(str(sibling))
                with self.assertRaisesRegex(runner_agent.AgentError, "禁止重新导入"):
                    toolbox._validate_script_args("source_to_md.py", [str(checkpoint)])
                with self.assertRaisesRegex(runner_agent.AgentError, "禁止重新安装"):
                    toolbox.tool_install_template_workspace(str(repo), str(checkpoint))
            finally:
                config.PPT_MASTER_REPO, config.UPLOADS_DIR = old_repo, old_uploads

    def test_resume_preserves_checkpoint_while_fresh_attempt_cleans_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "engine"
            projects = repo / "projects"
            checkpoint = projects / "web_job123_ppt169_20260813"
            checkpoint.mkdir(parents=True)
            marker = checkpoint / "design_spec.md"
            marker.write_text("spec", encoding="utf-8")
            (checkpoint / "spec_lock.md").write_text("lock", encoding="utf-8")
            old_repo, old_uploads = config.PPT_MASTER_REPO, config.UPLOADS_DIR
            config.PPT_MASTER_REPO = repo
            config.UPLOADS_DIR = root / "uploads"
            (config.UPLOADS_DIR / "job123").mkdir(parents=True)
            try:
                resumed = jobs.Job(
                    id="job123", created_at=0, resume_existing=True,
                    resume_project=checkpoint.name,
                )
                resumed_box = runner_agent.ToolBox(resumed, lambda *_args: None, 10**12)
                self.assertEqual(
                    runner_agent._prepare_project_attempt(resumed, lambda *_args: None, resumed_box),
                    checkpoint.resolve(),
                )
                self.assertTrue(marker.exists())

                fresh = jobs.Job(id="job123", created_at=0)
                fresh_box = runner_agent.ToolBox(fresh, lambda *_args: None, 10**12)
                self.assertIsNone(
                    runner_agent._prepare_project_attempt(fresh, lambda *_args: None, fresh_box)
                )
                self.assertFalse(checkpoint.exists())
            finally:
                config.PPT_MASTER_REPO, config.UPLOADS_DIR = old_repo, old_uploads

    def test_resume_setup_failure_is_persisted_by_run(self) -> None:
        job = jobs.Job(
            id="job123", created_at=0, status="running", kind="generate",
            resume_existing=True, resume_project="missing",
        )
        with (
            patch.object(runner_agent, "_new_agent", side_effect=runner_agent.AgentError("bad checkpoint")),
            patch.object(runner_agent.jobs, "update") as update,
        ):
            runner_agent.run(job, lambda *_args: None)

        self.assertEqual(update.call_args.kwargs["status"], "failed")
        self.assertIn("bad checkpoint", update.call_args.kwargs["error"])


if __name__ == "__main__":
    unittest.main()
