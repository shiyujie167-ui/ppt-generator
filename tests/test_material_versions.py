from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app
import config
import db
import qa


class _Upload:
    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self._data = data

    async def read(self) -> bytes:
        return self._data


class MaterialVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.old_db_file = config.DB_FILE
        self.old_library_dir = config.LIBRARY_DIR
        if db._CONN is not None:
            db._CONN.close()
        db._CONN = None
        config.DB_FILE = self.root / "app.db"
        config.LIBRARY_DIR = self.root / "library"
        config.LIBRARY_DIR.mkdir()
        db.init()
        qa._CONVERTING.clear()
        self.user_id = 7
        self.library = config.LIBRARY_DIR / str(self.user_id)
        self.library.mkdir()

    def tearDown(self) -> None:
        qa._CONVERTING.clear()
        if db._CONN is not None:
            db._CONN.close()
        db._CONN = None
        config.DB_FILE = self.old_db_file
        config.LIBRARY_DIR = self.old_library_dir
        self.temp_dir.cleanup()

    def _insert_material(self, name: str, data: bytes, status: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        db.run(
            "INSERT INTO materials (user_id, name, size, status, sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (self.user_id, name, len(data), status, digest),
        )
        return digest

    def test_same_name_upload_preserves_snapshots_and_invalidates_cache(self) -> None:
        name = "report.pdf"
        old_data = b"old source"
        new_data = b"new source"
        source = self.library / name
        source.write_bytes(old_data)
        old_sha = self._insert_material(name, old_data, "done")
        markdown, profile, assets = qa._artifact_paths(self.library, name)
        markdown.write_text("old markdown", encoding="utf-8")
        profile.write_text("old profile", encoding="utf-8")
        assets.mkdir()
        (assets / "figure.png").write_bytes(b"old image")

        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        os.link(source, snapshot / name)
        os.link(markdown, snapshot / markdown.name)
        os.link(profile, snapshot / profile.name)
        snapshot_assets = snapshot / assets.name
        snapshot_assets.mkdir()
        os.link(assets / "figure.png", snapshot_assets / "figure.png")

        with mock.patch.object(qa, "_kick_pending"):
            saved = asyncio.run(app._save_uploads(self.user_id, [_Upload(name, new_data)]))

        row = db.fetchone(
            "SELECT sha256, status FROM materials WHERE user_id = ? AND name = ?",
            (self.user_id, name),
        )
        self.assertEqual(saved, [name])
        self.assertEqual(source.read_bytes(), new_data)
        self.assertEqual((snapshot / name).read_bytes(), old_data)
        self.assertNotEqual(source.stat().st_ino, (snapshot / name).stat().st_ino)
        self.assertEqual((snapshot / markdown.name).read_text(encoding="utf-8"), "old markdown")
        self.assertEqual((snapshot / profile.name).read_text(encoding="utf-8"), "old profile")
        self.assertEqual((snapshot_assets / "figure.png").read_bytes(), b"old image")
        self.assertFalse(markdown.exists())
        self.assertFalse(profile.exists())
        self.assertFalse(assets.exists())
        self.assertNotEqual(row["sha256"], old_sha)
        self.assertEqual(row["sha256"], hashlib.sha256(new_data).hexdigest())
        self.assertEqual(row["status"], "pending")

    def test_stale_conversion_cannot_publish_after_reupload(self) -> None:
        name = "report.pdf"
        old_data = b"old source"
        new_data = b"new source"
        source = self.library / name
        source.write_bytes(old_data)
        old_sha = self._insert_material(name, old_data, "pending")
        qa._CONVERTING[(self.user_id, name)] = old_sha

        def finish_old_conversion(command, **_kwargs):
            output = Path(command[command.index("-o") + 1])
            output.write_text("stale markdown", encoding="utf-8")
            output.with_name(f"{output.stem}.conversion_profile.json").write_text(
                "stale profile", encoding="utf-8"
            )
            asset_dir = output.with_name(f"{output.stem}_files")
            asset_dir.mkdir()
            (asset_dir / "figure.png").write_bytes(b"stale image")

            replacement = self.library / ".replacement"
            replacement.write_bytes(new_data)
            qa.store_uploaded_source(self.user_id, name, replacement)
            return SimpleNamespace(returncode=0)

        with mock.patch.object(qa, "_kick_pending") as kick_pending, mock.patch.object(
            qa.subprocess, "run", side_effect=finish_old_conversion
        ):
            qa._convert_worker(self.user_id, name, old_sha)

        row = db.fetchone(
            "SELECT sha256, status FROM materials WHERE user_id = ? AND name = ?",
            (self.user_id, name),
        )
        markdown, profile, assets = qa._artifact_paths(self.library, name)
        self.assertEqual(source.read_bytes(), new_data)
        self.assertEqual(row["sha256"], hashlib.sha256(new_data).hexdigest())
        self.assertEqual(row["status"], "pending")
        self.assertFalse(markdown.exists())
        self.assertFalse(profile.exists())
        self.assertFalse(assets.exists())
        self.assertNotIn((self.user_id, name), qa._CONVERTING)
        self.assertGreaterEqual(kick_pending.call_count, 1)

    def test_current_conversion_publishes_all_artifacts(self) -> None:
        name = "report.pdf"
        data = b"current source"
        source = self.library / name
        source.write_bytes(data)
        source_sha = self._insert_material(name, data, "pending")
        qa._CONVERTING[(self.user_id, name)] = source_sha

        def finish_conversion(command, **_kwargs):
            output = Path(command[command.index("-o") + 1])
            output.write_text("current markdown", encoding="utf-8")
            output.with_name(f"{output.stem}.conversion_profile.json").write_text(
                "current profile", encoding="utf-8"
            )
            asset_dir = output.with_name(f"{output.stem}_files")
            asset_dir.mkdir()
            (asset_dir / "figure.png").write_bytes(b"current image")
            return SimpleNamespace(returncode=0)

        with mock.patch.object(qa.subprocess, "run", side_effect=finish_conversion):
            qa._convert_worker(self.user_id, name, source_sha)

        row = db.fetchone(
            "SELECT status FROM materials WHERE user_id = ? AND name = ?",
            (self.user_id, name),
        )
        markdown, profile, assets = qa._artifact_paths(self.library, name)
        self.assertEqual(row["status"], "done")
        self.assertEqual(markdown.read_text(encoding="utf-8"), "current markdown")
        self.assertEqual(profile.read_text(encoding="utf-8"), "current profile")
        self.assertEqual((assets / "figure.png").read_bytes(), b"current image")
        self.assertNotIn((self.user_id, name), qa._CONVERTING)


if __name__ == "__main__":
    unittest.main()
