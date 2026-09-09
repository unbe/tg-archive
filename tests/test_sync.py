import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
import pytz

import telethon.tl.types
from tgarchive.db import DB, User, Message
from tgarchive.sync import Sync


class DummyEntity:
    def __init__(self, id, title="Test Group"):
        self.id = id
        self.title = title


class DummyTelethonUser:
    def __init__(self, id, username="user1", first_name="First", last_name="Last"):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.bot = False
        self.scam = False
        self.fake = False


class DummyTelethonMessage:
    def __init__(self, id, text="Test message", date=None):
        self.id = id
        self.raw_text = text
        self.date = date or pytz.utc.localize(datetime(2025, 1, 15, 12, 0, 0))
        self.edit_date = None
        self.reply_to = None
        self.sender = DummyTelethonUser(1)
        self.chat = DummyEntity(100)
        self.media = None
        self.action = None


class TestSync(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test.sqlite")
        self.media_dir = os.path.join(self.tmp_dir.name, "media")
        self.config = {
            "api_id": "123",
            "api_hash": "abc",
            "group": "testgroup",
            "media_dir": self.media_dir,
            "download_media": False,
            "download_avatars": False,
            "fetch_batch_size": 200,
            "fetch_wait": 0,
            "fetch_limit": 0,
            "use_takeout": False,
        }
        self.db = DB(self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    @patch("tgarchive.sync.Sync.new_client")
    def test_sync_ids_with_deletions(self, mock_new_client):
        mock_client = MagicMock()
        mock_new_client.return_value = mock_client

        # Seed the DB with message 11 (which will be deleted)
        date = pytz.utc.localize(datetime(2025, 1, 15, 12, 0, 0))
        self.db.insert_user(User(id=1, username="user1", first_name="First", last_name="Last", tags=[], avatar=None))
        self.db.insert_message(Message(id=11, type="message", date=date, edit_date=None, content="Msg 11", reply_to=None, user=User(id=1, username="user1", first_name="First", last_name="Last", tags=[], avatar=None), media=None, deleted=False))
        self.db.commit()

        # Mock entity and get_messages
        mock_client.get_dialogs.return_value = []
        mock_client.get_entity.return_value = DummyEntity(100)
        
        # When querying ids=[10, 11, 12], 10 and 12 exist, 11 is None (deleted)
        msg10 = DummyTelethonMessage(10, "Hello 10")
        msg12 = DummyTelethonMessage(12, "Hello 12")
        mock_client.get_messages.return_value = [msg10, None, msg12]

        s = Sync(self.config, "session.session", self.db)
        s.sync(ids=[10, 11, 12])

        msgs = {m.id: m for m in self.db.get_messages(2025, 1)}
        self.assertIn(10, msgs)
        self.assertFalse(msgs[10].deleted)
        self.assertIn(11, msgs)
        self.assertTrue(msgs[11].deleted)
        self.assertIn(12, msgs)
        self.assertFalse(msgs[12].deleted)

    @patch("tgarchive.sync.Sync.new_client")
    def test_check_updates(self, mock_new_client):
        mock_client = MagicMock()
        mock_new_client.return_value = mock_client

        # Seed DB with messages 101, 102, 103
        date = pytz.utc.localize(datetime(2025, 1, 15, 12, 0, 0))
        u = User(id=1, username="user1", first_name="First", last_name="Last", tags=[], avatar=None)
        self.db.insert_user(u)
        for mid in [101, 102, 103]:
            self.db.insert_message(Message(id=mid, type="message", date=date, edit_date=None, content=f"Msg {mid}", reply_to=None, user=u, media=None, deleted=False))
        self.db.commit()

        # Mock get_messages:
        # 101 is edited with new text and edit_date
        # 102 is None (deleted)
        # 103 is MessageEmpty (deleted)
        mock_client.get_dialogs.return_value = []
        mock_client.get_entity.return_value = DummyEntity(100)

        edit_date = pytz.utc.localize(datetime(2025, 1, 15, 13, 0, 0))
        msg101 = DummyTelethonMessage(101, "Updated text 101")
        msg101.edit_date = edit_date
        msg_empty = telethon.tl.types.MessageEmpty(id=103, peer_id=telethon.tl.types.PeerChannel(channel_id=100))
        mock_client.get_messages.return_value = [msg101, None, msg_empty]

        s = Sync(self.config, "session.session", self.db)
        s.check_updates()

        msgs = {m.id: m for m in self.db.get_messages(2025, 1)}
        self.assertFalse(msgs[101].deleted)
        self.assertEqual(msgs[101].content, "Updated text 101")
        self.assertTrue(msgs[102].deleted)
        self.assertTrue(msgs[103].deleted)

        # Check that previous version of 101 was archived in message_edits
        edits = self.db.get_message_edits(101)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].content, "Msg 101")

    @patch("tgarchive.sync.Sync.new_client")
    def test_listen_message_deleted_event(self, mock_new_client):
        mock_client = MagicMock()
        mock_new_client.return_value = mock_client
        mock_client.get_dialogs.return_value = []
        mock_client.get_entity.return_value = DummyEntity(100)

        handlers = {}

        def fake_on(event_builder):
            def decorator(func):
                handlers[type(event_builder)] = func
                return func
            return decorator

        mock_client.on.side_effect = fake_on

        # Seed DB with messages 50, 51, 52
        date = pytz.utc.localize(datetime(2025, 1, 15, 12, 0, 0))
        u = User(id=1, username="user1", first_name="First", last_name="Last", tags=[], avatar=None)
        self.db.insert_user(u)
        for mid in [50, 51, 52]:
            self.db.insert_message(Message(id=mid, type="message", date=date, edit_date=None, content=f"Msg {mid}", reply_to=None, user=u, media=None, deleted=False))
        self.db.commit()

        s = Sync(self.config, "session.session", self.db)
        # Call listen() but have run_until_disconnected return immediately
        mock_client.run_until_disconnected.side_effect = lambda: None
        s.listen()

        # Verify handlers were registered
        import telethon.events
        self.assertIn(telethon.events.MessageDeleted, handlers)

        # Trigger on_message_deleted with deleted_ids = [50, 52]
        import asyncio
        del_handler = handlers[telethon.events.MessageDeleted]
        event = MagicMock()
        event.deleted_ids = [50, 52]
        if asyncio.iscoroutinefunction(del_handler):
            asyncio.run(del_handler(event))
        else:
            del_handler(event)

        msgs = {m.id: m for m in self.db.get_messages(2025, 1)}
        self.assertTrue(msgs[50].deleted)
        self.assertFalse(msgs[51].deleted)
        self.assertTrue(msgs[52].deleted)

        # Trigger on_new_message
        new_handler = handlers[telethon.events.NewMessage]
        event_new = MagicMock()
        event_new.message = DummyTelethonMessage(60, "Live new message")
        asyncio.run(new_handler(event_new))

        msgs = {m.id: m for m in self.db.get_messages(2025, 1)}
        self.assertIn(60, msgs)
        self.assertEqual(msgs[60].content, "Live new message")

        # Trigger on_message_edited
        edit_handler = handlers[telethon.events.MessageEdited]
        event_edit = MagicMock()
        event_edit.message = DummyTelethonMessage(60, "Live edited message")
        event_edit.message.edit_date = pytz.utc.localize(datetime(2025, 1, 15, 14, 0, 0))
        asyncio.run(edit_handler(event_edit))

        msgs = {m.id: m for m in self.db.get_messages(2025, 1)}
        self.assertEqual(msgs[60].content, "Live edited message")
        edits = self.db.get_message_edits(60)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].content, "Live new message")

    @patch("tgarchive.sync.Sync.new_client")
    def test_check_updates_skips_avatar_download(self, mock_new_client):
        mock_client = MagicMock()
        mock_new_client.return_value = mock_client
        mock_client.get_dialogs.return_value = []
        mock_client.get_entity.return_value = DummyEntity(100)

        config = dict(self.config)
        config["download_avatars"] = True

        date = pytz.utc.localize(datetime(2025, 1, 15, 12, 0, 0))
        u = User(id=696367351, username="user_no_avatar", first_name="No", last_name="Avatar", tags=[], avatar=None)
        self.db.insert_user(u)
        self.db.insert_message(Message(id=201, type="message", date=date, edit_date=None, content="Initial text", reply_to=None, user=u, media=None, deleted=False))
        self.db.commit()

        # Telethon returns message 201 as edited
        msg201 = DummyTelethonMessage(201, "Edited text")
        msg201.sender = DummyTelethonUser(696367351)
        msg201.edit_date = pytz.utc.localize(datetime(2025, 1, 15, 13, 0, 0))
        mock_client.get_messages.return_value = [msg201]

        s = Sync(config, "session.session", self.db)
        s.check_updates()

        # Verify download_profile_photo was NOT called during check_updates
        mock_client.download_profile_photo.assert_not_called()

        msgs = {m.id: m for m in self.db.get_messages(2025, 1)}
        self.assertEqual(msgs[201].content, "Edited text")


if __name__ == "__main__":
    unittest.main()

