from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
import config
import db
import jobs


class JobStoreRemovalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_file = config.DB_FILE
        if db._CONN is not None:
            db._CONN.close()
        db._CONN = None
        config.DB_FILE = Path(self.temp_dir.name) / "app.db"
        jobs._JOBS.clear()
        jobs._ready = False
        db.init()

    def tearDown(self) -> None:
        if db._CONN is not None:
            db._CONN.close()
        db._CONN = None
        config.DB_FILE = self.old_db_file
        jobs._JOBS.clear()
        jobs._ready = False
        self.temp_dir.cleanup()

    def test_remove_deletes_terminal_job_from_memory_and_database(self) -> None:
        job = jobs.create(enqueue=False, user_id=1, status="done")

        removed = jobs.remove(job.id)

        self.assertIs(removed, job)
        self.assertIsNone(jobs.get(job.id))
        self.assertIsNone(db.fetchone("SELECT id FROM jobs WHERE id = ?", (job.id,)))

    def test_remove_rejects_active_job(self) -> None:
        job = jobs.create(enqueue=False, user_id=1, status="running")

        with self.assertRaises(ValueError):
            jobs.remove(job.id)

        self.assertIs(jobs.get(job.id), job)
        self.assertIsNotNone(db.fetchone("SELECT id FROM jobs WHERE id = ?", (job.id,)))


class JobArtifactCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.old_paths = (
            config.OUTPUTS_DIR,
            config.LOGS_DIR,
            config.UPLOADS_DIR,
            config.PPT_MASTER_REPO,
        )
        config.OUTPUTS_DIR = self.root / "outputs"
        config.LOGS_DIR = self.root / "logs"
        config.UPLOADS_DIR = self.root / "uploads"
        config.PPT_MASTER_REPO = self.root / "engine"

    def tearDown(self) -> None:
        (
            config.OUTPUTS_DIR,
            config.LOGS_DIR,
            config.UPLOADS_DIR,
            config.PPT_MASTER_REPO,
        ) = self.old_paths
        self.temp_dir.cleanup()

    def test_cleanup_removes_job_files_but_preserves_shared_snapshot(self) -> None:
        job = SimpleNamespace(id="plan-1", upload_id="shared")
        other = SimpleNamespace(id="generate-1", upload_id="shared")
        output = config.OUTPUTS_DIR / job.id
        log = config.LOGS_DIR / f"{job.id}.log"
        snapshot = config.UPLOADS_DIR / "shared"
        project = config.PPT_MASTER_REPO / "projects" / "web_plan-1_ppt169_20260807"
        for directory in (output, snapshot, project):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "file.txt").write_text("test", encoding="utf-8")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("test", encoding="utf-8")

        with patch.object(app.jobs, "all_jobs", return_value=[other]):
            app._cleanup_job_artifacts(job)

        self.assertFalse(output.exists())
        self.assertFalse(log.exists())
        self.assertFalse(project.exists())
        self.assertTrue(snapshot.exists())

        with patch.object(app.jobs, "all_jobs", return_value=[]):
            app._cleanup_job_artifacts(other)
        self.assertFalse(snapshot.exists())

    def test_delete_endpoint_rejects_running_job(self) -> None:
        job = SimpleNamespace(id="job-1", user_id=7, status="running")
        with patch.object(app.jobs, "get", return_value=job):
            response = app.delete_job("job-1", user={"id": 7})

        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
