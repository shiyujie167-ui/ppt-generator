from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import config
import db


class AccountTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_file = config.DB_FILE
        if db._CONN is not None:
            db._CONN.close()
        db._CONN = None
        config.DB_FILE = Path(self.temp_dir.name) / "app.db"
        db.init()

    def tearDown(self) -> None:
        if db._CONN is not None:
            db._CONN.close()
        db._CONN = None
        config.DB_FILE = self.old_db_file
        self.temp_dir.cleanup()

    def test_password_change_revokes_existing_sessions(self) -> None:
        user_id = db.create_user("alice", "old-password")
        token = db.create_session(user_id)

        self.assertIsNotNone(db.user_for_token(token))
        self.assertTrue(db.set_password("alice", "new-password"))
        self.assertIsNone(db.user_for_token(token))
        self.assertIsNone(db.verify_login("alice", "old-password"))
        self.assertIsNotNone(db.verify_login("alice", "new-password"))


if __name__ == "__main__":
    unittest.main()
