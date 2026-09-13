import os
import shutil
import tempfile
import unittest
from datetime import datetime
import pytz

from tgarchive.build import Build
from tgarchive.db import DB, User, Message, Media


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        # Copy tgarchive/example into temporary site dir
        example_src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tgarchive", "example"))
        self.site_dir = os.path.join(self.tmp_dir.name, "mysite")
        shutil.copytree(example_src, self.site_dir)

        self.db_path = os.path.join(self.site_dir, "test.sqlite")
        self.publish_dir = os.path.join(self.site_dir, "site")
        self.static_dir = os.path.join(self.site_dir, "static")
        self.template_path = os.path.join(self.site_dir, "template.html")
        self.rss_template_path = os.path.join(self.site_dir, "rss_template.html")

        self.config = {
            "group": "testgroup",
            "publish_dir": self.publish_dir,
            "static_dir": "static",
            "media_dir": "media",
            "site_url": "https://example.com",
            "telegram_url": "https://t.me/{id}",
            "per_page": 100,
            "show_sender_fullname": False,
            "timezone": "UTC",
            "site_name": "@{group} archive",
            "site_description": "Archive of @{group}",
            "meta_description": "@{group} archive",
            "page_title": "{group} - {date}",
            "publish_rss_feed": True,
            "rss_feed_entries": 10,
            "show_day_index": True,
            "date": "2025"
        }
        os.makedirs(os.path.join(self.site_dir, "media"), exist_ok=True)
        self.db = DB(self.db_path, tz="UTC")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_build_deleted_message_rendering(self):
        date = pytz.utc.localize(datetime(2025, 1, 15, 12, 0, 0))
        u = User(id=1, username="alice", first_name="Alice", last_name="Smith", tags=[], avatar=None)
        self.db.insert_user(u)

        # Message 1 is normal, message 2 is deleted
        m1 = Message(id=1, type="message", date=date, edit_date=None, content="Normal message", reply_to=None, user=u, media=None, deleted=False)
        m2 = Message(id=2, type="message", date=date, edit_date=None, content="Deleted message", reply_to=None, user=u, media=None, deleted=True)

        self.db.insert_message(m1)
        self.db.insert_message(m2)
        self.db.commit()

        # Edit message 1
        edit_date = pytz.utc.localize(datetime(2025, 1, 15, 13, 0, 0))
        m1_edited = Message(id=1, type="message", date=date, edit_date=edit_date, content="Edited message text", reply_to=None, user=u, media=None, deleted=False)
        self.db.insert_message(m1_edited)
        self.db.commit()

        cur_dir = os.getcwd()
        os.chdir(self.site_dir)
        try:
            builder = Build(self.config, self.db, symlink=False)
            builder.load_template(self.template_path)
            builder.load_rss_template(self.rss_template_path)
            builder.build()
        finally:
            os.chdir(cur_dir)

        # Check index.html exists
        index_file = os.path.join(self.publish_dir, "index.html")
        self.assertTrue(os.path.exists(index_file))

        with open(index_file, "r", encoding="utf-8") as f:
            html = f.read()

        # Verify message 1 has latest text and previous edit in edits-dropdown
        self.assertIn('id="1"', html)
        self.assertNotIn('class="message type-message is-deleted" id="1"', html)
        self.assertIn('Edited message text', html)
        self.assertIn('diff-del">Normal</del>', html)
        self.assertIn('diff-ins">Edited</ins>', html)
        self.assertIn('edits-dropdown', html)

        # Verify message 2 has is-deleted class and badge
        self.assertIn('class="message type-message is-deleted" id="2"', html)
        self.assertIn('[Deleted]', html)
        self.assertIn('badge-deleted', html)

    def test_build_internal_archive_links(self):
        # Configure group with negative supergroup ID: -1001450089406
        config = dict(self.config)
        config["group"] = -1001450089406

        date = pytz.utc.localize(datetime(2025, 1, 15, 12, 0, 0))
        u = User(id=1, username="alice", first_name="Alice", last_name="Smith", tags=[], avatar=None)
        self.db.insert_user(u)

        # Message 360070 is target
        m70 = Message(id=360070, type="message", date=date, edit_date=None,
                      content="Target message to be referenced", reply_to=None, user=u, media=None, deleted=False)
        # Message 360072 links to 360070
        m72 = Message(id=360072, type="message", date=date, edit_date=None,
                      content="As discussed in https://t.me/c/1450089406/360070.", reply_to=None, user=u, media=None, deleted=False)
        # Message 360073 links to 360072 with query param
        m73 = Message(id=360073, type="message", date=date, edit_date=None,
                      content="Check https://t.me/c/1450089406/360072?single for details.", reply_to=None, user=u, media=None, deleted=False)
        # Message 360074 links to an external group
        m74 = Message(id=360074, type="message", date=date, edit_date=None,
                      content="External link https://t.me/c/9999999999/12345 should not get archive link.", reply_to=None, user=u, media=None, deleted=False)
        # Message 360075 has webpage media pointing to 360070
        med = Media(id=360075, type="webpage", url="https://t.me/c/1450089406/360070", title="Telegram Link", description="Webpage preview", thumb=None)
        self.db.insert_media(med)
        m75 = Message(id=360075, type="message", date=date, edit_date=None,
                      content="Here is a webpage preview message", reply_to=None, user=u, media=med, deleted=False)

        for m in [m70, m72, m73, m74, m75]:
            self.db.insert_message(m)
        self.db.commit()

        cur_dir = os.getcwd()
        os.chdir(self.site_dir)
        try:
            builder = Build(config, self.db, symlink=False)
            builder.load_template(self.template_path)
            builder.load_rss_template(self.rss_template_path)
            builder.build()
        finally:
            os.chdir(cur_dir)

        index_file = os.path.join(self.publish_dir, "index.html")
        self.assertTrue(os.path.exists(index_file))

        with open(index_file, "r", encoding="utf-8") as f:
            html = f.read()

        # Message 360072: internal link must be followed by archive link pointing to target
        self.assertIn('href="https://t.me/c/1450089406/360070"', html)
        self.assertIn('(<a href="2025-01.html#360070" class="archive-link">archive</a>)', html)

        # Message 360073: query param link pointing to 360072
        self.assertIn('href="https://t.me/c/1450089406/360072?single"', html)
        self.assertIn('(<a href="2025-01.html#360072" class="archive-link">archive</a>)', html)

        # Message 360074: external link to other channel has no archive link
        self.assertIn('href="https://t.me/c/9999999999/12345"', html)
        self.assertNotIn('12345" class="archive-link"', html)

        # Message 360075: webpage media link pointing to internal message has archive link
        self.assertIn('(<a href="2025-01.html#360070" class="archive-link">archive</a>)', html)


if __name__ == "__main__":
    unittest.main()
