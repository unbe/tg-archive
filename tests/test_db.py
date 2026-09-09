import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
import pytz

from tgarchive.db import DB, User, Message, Media


LEGACY_SCHEMA = """
CREATE table messages (
    id INTEGER NOT NULL PRIMARY KEY,
    type TEXT NOT NULL,
    date TIMESTAMP NOT NULL,
    edit_date TIMESTAMP,
    content TEXT,
    reply_to INTEGER,
    user_id INTEGER,
    media_id INTEGER,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(media_id) REFERENCES media(id)
);
##
CREATE table users (
    id INTEGER NOT NULL PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    tags TEXT,
    avatar TEXT
);
##
CREATE table media (
    id INTEGER NOT NULL PRIMARY KEY,
    type TEXT,
    url TEXT,
    title TEXT,
    description TEXT,
    thumb TEXT
);
"""


class TestDB(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test.sqlite")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _sample_user(self, uid=1):
        return User(id=uid, username=f"user_{uid}", first_name="First", last_name="Last", tags=["tag1"], avatar=None)

    def _sample_message(self, mid=1, uid=1, date=None, deleted=False):
        if not date:
            date = pytz.utc.localize(datetime(2025, 1, 15, 12, 0, 0))
        return Message(
            id=mid,
            type="message",
            date=date,
            edit_date=None,
            content=f"Hello message {mid}",
            reply_to=None,
            user=self._sample_user(uid),
            media=None,
            deleted=deleted
        )

    def test_new_db_schema(self):
        db = DB(self.db_path)
        cur = db.conn.cursor()
        cur.execute("PRAGMA table_info(messages)")
        cols = {row[1]: row for row in cur.fetchall()}
        self.assertIn("deleted", cols)

        u = self._sample_user(1)
        db.insert_user(u)

        m1 = self._sample_message(1, 1, deleted=False)
        m2 = self._sample_message(2, 1, deleted=True)
        db.insert_message(m1)
        db.insert_message(m2)
        db.commit()

        msgs = list(db.get_messages(2025, 1))
        self.assertEqual(len(msgs), 2)
        self.assertFalse(msgs[0].deleted)
        self.assertTrue(msgs[1].deleted)

    def test_legacy_db_migration(self):
        # Create legacy database without 'deleted' column
        conn = sqlite3.connect(self.db_path)
        for s in LEGACY_SCHEMA.split("##"):
            conn.execute(s)
        conn.commit()

        # Insert user and messages using raw SQL
        conn.execute("INSERT INTO users (id, username, first_name, last_name, tags, avatar) VALUES (1, 'alice', 'Alice', 'Smith', '', NULL)")
        conn.execute("INSERT INTO messages (id, type, date, edit_date, content, reply_to, user_id, media_id) VALUES (10, 'message', '2025-01-10 10:00:00', NULL, 'Old msg 1', NULL, 1, NULL)")
        conn.execute("INSERT INTO messages (id, type, date, edit_date, content, reply_to, user_id, media_id) VALUES (20, 'message', '2025-01-11 11:00:00', NULL, 'Old msg 2', NULL, 1, NULL)")
        conn.commit()
        conn.close()

        # Now open with DB which should apply safe migration
        db = DB(self.db_path)
        cur = db.conn.cursor()
        cur.execute("PRAGMA table_info(messages)")
        cols = [row[1] for row in cur.fetchall()]
        self.assertIn("deleted", cols)

        # Verify existing data is preserved intact
        msgs = list(db.get_messages(2025, 1))
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].id, 10)
        self.assertEqual(msgs[0].content, "Old msg 1")
        self.assertFalse(msgs[0].deleted)
        self.assertEqual(msgs[1].id, 20)
        self.assertEqual(msgs[1].content, "Old msg 2")
        self.assertFalse(msgs[1].deleted)

    def test_flag_deleted_and_batch(self):
        db = DB(self.db_path)
        u = self._sample_user(1)
        db.insert_user(u)

        for i in range(1, 6):
            db.insert_message(self._sample_message(i, 1))
        db.commit()

        # Flag single
        db.flag_deleted(2)
        db.commit()

        # Flag batch
        db.flag_deleted_batch([4, 5])
        db.commit()

        msgs = {m.id: m for m in db.get_messages(2025, 1)}
        self.assertFalse(msgs[1].deleted)
        self.assertTrue(msgs[2].deleted)
        self.assertFalse(msgs[3].deleted)
        self.assertTrue(msgs[4].deleted)
        self.assertTrue(msgs[5].deleted)

        active_ids = db.get_active_message_ids()
        self.assertEqual(active_ids, [1, 3])

        active_ids_since = db.get_active_message_ids(since_id=2)
        self.assertEqual(active_ids_since, [3])


if __name__ == "__main__":
    unittest.main()
