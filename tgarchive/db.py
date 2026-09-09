import json
import math
import os
import sqlite3
from collections import namedtuple
from datetime import datetime, timedelta, timezone
import pytz
from typing import Iterator

schema = """
CREATE table messages (
    id INTEGER NOT NULL PRIMARY KEY,
    type TEXT NOT NULL,
    date TIMESTAMP NOT NULL,
    edit_date TIMESTAMP,
    content TEXT,
    reply_to INTEGER,
    user_id INTEGER,
    media_id INTEGER,
    deleted BOOLEAN NOT NULL DEFAULT 0,
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
##
CREATE table message_edits (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    date TIMESTAMP,
    content TEXT,
    FOREIGN KEY(message_id) REFERENCES messages(id)
);
"""

User = namedtuple(
    "User", ["id", "username", "first_name", "last_name", "tags", "avatar"])

Message = namedtuple(
    "Message", ["id", "type", "date", "edit_date", "content", "reply_to", "user", "media", "deleted", "edits"], defaults=[False, None])

MessageEdit = namedtuple(
    "MessageEdit", ["id", "message_id", "date", "content"])

Media = namedtuple(
    "Media", ["id", "type", "url", "title", "description", "thumb"])

Month = namedtuple("Month", ["date", "slug", "label", "count"])

Day = namedtuple("Day", ["date", "slug", "label", "count", "page"])


def _page(n, multiple):
    return math.ceil(n / multiple)


class DB:
    conn = None
    tz = None

    def __init__(self, dbfile, tz=None):
        # Initialize the SQLite DB. If it's new, create the table schema.
        is_new = not os.path.isfile(dbfile)

        self.conn = sqlite3.Connection(
            dbfile, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)

        # Add the custom PAGE() function to get the page number of a row
        # by its row number and a limit multiple.
        self.conn.create_function("PAGE", 2, _page)

        if tz:
            self.tz = pytz.timezone(tz)

        if is_new:
            for s in schema.split("##"):
                self.conn.cursor().execute(s)
                self.conn.commit()
        else:
            self._migrate()

    def _migrate(self):
        """Safely apply migrations to an existing database."""
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(messages)")
        columns = [row[1] for row in cur.fetchall()]
        if "deleted" not in columns:
            cur.execute("ALTER TABLE messages ADD COLUMN deleted BOOLEAN NOT NULL DEFAULT 0")
            self.conn.commit()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='message_edits'")
        if not cur.fetchone():
            cur.execute("""
            CREATE table message_edits (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                date TIMESTAMP,
                content TEXT,
                FOREIGN KEY(message_id) REFERENCES messages(id)
            )
            """)
            self.conn.commit()

    def _parse_date(self, d) -> str:
        return datetime.strptime(d, "%Y-%m-%dT%H:%M:%S%z")

    def get_last_message_id(self) -> [int, datetime]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, strftime('%Y-%m-%d 00:00:00', date) as "[timestamp]" FROM messages
            ORDER BY id DESC LIMIT 1
        """)
        res = cur.fetchone()
        if not res:
            return 0, None

        id, date = res
        return id, date

    def get_timeline(self) -> Iterator[Month]:
        """
        Get the list of all unique yyyy-mm month groups and
        the corresponding message counts per period in chronological order.
        """
        cur = self.conn.cursor()
        cur.execute("""
            SELECT strftime('%Y-%m-%d 00:00:00', date) as "[timestamp]",
            COUNT(*) FROM messages AS count
            GROUP BY strftime('%Y-%m', date) ORDER BY date
        """)

        for r in cur.fetchall():
            date = pytz.utc.localize(r[0])
            if self.tz:
                date = date.astimezone(self.tz)

            yield Month(date=date,
                        slug=date.strftime("%Y-%m"),
                        label=date.strftime("%b %Y"),
                        count=r[1])

    def get_dayline(self, year, month, limit=500) -> Iterator[Day]:
        """
        Get the list of all unique yyyy-mm-dd days corresponding
        message counts and the page number of the first occurrence of 
        the date in the pool of messages for the whole month.
        """
        cur = self.conn.cursor()
        cur.execute("""
            SELECT strftime("%Y-%m-%d 00:00:00", date) AS "[timestamp]",
            COUNT(*), PAGE(rank, ?) FROM (
                SELECT ROW_NUMBER() OVER() as rank, date FROM messages
                WHERE strftime('%Y%m', date) = ? ORDER BY id
            )
            GROUP BY "[timestamp]";
        """, (limit, "{}{:02d}".format(year, month)))

        for r in cur.fetchall():
            date = pytz.utc.localize(r[0])
            if self.tz:
                date = date.astimezone(self.tz)

            yield Day(date=date,
                      slug=date.strftime("%Y-%m-%d"),
                      label=date.strftime("%d %b %Y"),
                      count=r[1],
                      page=r[2])

    def get_messages(self, year, month, last_id=0, limit=500) -> Iterator[Message]:
        date = "{}{:02d}".format(year, month)

        cur = self.conn.cursor()
        cur.execute("""
            SELECT messages.id, messages.type, messages.date, messages.edit_date,
            messages.content, messages.reply_to, messages.user_id,
            users.username, users.first_name, users.last_name, users.tags, users.avatar,
            media.id, media.type, media.url, media.title, media.description, media.thumb,
            messages.deleted
            FROM messages
            LEFT JOIN users ON (users.id = messages.user_id)
            LEFT JOIN media ON (media.id = messages.media_id)
            WHERE strftime('%Y%m', date) = ?
            AND messages.id > ? ORDER by messages.id LIMIT ?
            """, (date, last_id, limit))

        for r in cur.fetchall():
            yield self._make_message(r)

    def get_message_count(self, year, month) -> int:
        date = "{}{:02d}".format(year, month)

        cur = self.conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM messages WHERE strftime('%Y%m', date) = ?
            """, (date,))

        total, = cur.fetchone()
        return total

    def insert_user(self, u: User):
        """Insert a user and if they exist, update the fields."""
        cur = self.conn.cursor()
        cur.execute("""INSERT INTO users (id, username, first_name, last_name, tags, avatar)
            VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT (id)
            DO UPDATE SET username=excluded.username, first_name=excluded.first_name,
                last_name=excluded.last_name, tags=excluded.tags, avatar=excluded.avatar
            """, (u.id, u.username, u.first_name, u.last_name, " ".join(u.tags), u.avatar))

    def insert_media(self, m: Media):
        cur = self.conn.cursor()
        cur.execute("""INSERT OR REPLACE INTO media
            (id, type, url, title, description, thumb)
            VALUES(?, ?, ?, ?, ?, ?)""",
                    (m.id,
                     m.type,
                     m.url,
                     m.title,
                     m.description,
                     m.thumb)
                    )

    def insert_message(self, m: Message):
        deleted = getattr(m, "deleted", False)
        cur = self.conn.cursor()

        # If message exists and content has changed, record old version in message_edits
        cur.execute("SELECT content, edit_date, date FROM messages WHERE id = ?", (m.id,))
        row = cur.fetchone()
        if row:
            old_content, old_edit_date, old_date = row
            if old_content is not None and old_content != m.content:
                rev_date = old_edit_date or old_date
                cur.execute("""
                    INSERT INTO message_edits (message_id, date, content)
                    VALUES (?, ?, ?)
                """, (m.id, rev_date.strftime("%Y-%m-%d %H:%M:%S") if hasattr(rev_date, "strftime") else str(rev_date) if rev_date else None, old_content))

        cur.execute("""INSERT INTO messages
            (id, type, date, edit_date, content, reply_to, user_id, media_id, deleted)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                type=excluded.type,
                date=excluded.date,
                edit_date=excluded.edit_date,
                content=excluded.content,
                reply_to=excluded.reply_to,
                user_id=excluded.user_id,
                media_id=excluded.media_id,
                deleted=excluded.deleted
            """,
                    (m.id,
                     m.type,
                     m.date.strftime("%Y-%m-%d %H:%M:%S"),
                     m.edit_date.strftime(
                         "%Y-%m-%d %H:%M:%S") if m.edit_date else None,
                     m.content,
                     m.reply_to,
                     m.user.id,
                     m.media.id if m.media else None,
                     1 if deleted else 0)
                    )

    def get_message_edits(self, message_id: int) -> list:
        """Get edit revisions for a specific message ID in chronological order."""
        cur = self.conn.cursor()
        cur.execute("SELECT id, message_id, date, content FROM message_edits WHERE message_id = ? ORDER BY id ASC", (message_id,))
        edits = []
        for r in cur.fetchall():
            d = pytz.utc.localize(r[2]) if r[2] else None
            if self.tz and d:
                d = d.astimezone(self.tz)
            edits.append(MessageEdit(id=r[0], message_id=r[1], date=d, content=r[3]))
        return edits

    def get_edits_for_messages(self, message_ids: list) -> dict:
        """Batch load edit revisions for a list of message IDs, returning {message_id: [MessageEdit, ...]}."""
        if not message_ids:
            return {}
        cur = self.conn.cursor()
        cur.execute("SELECT id, message_id, date, content FROM message_edits WHERE message_id IN ({}) ORDER BY id ASC".format(
            ",".join("?" * len(message_ids))), message_ids)
        edits_map = {mid: [] for mid in message_ids}
        for r in cur.fetchall():
            d = pytz.utc.localize(r[2]) if r[2] else None
            if self.tz and d:
                d = d.astimezone(self.tz)
            edits_map.setdefault(r[1], []).append(MessageEdit(id=r[0], message_id=r[1], date=d, content=r[3]))
        return edits_map

    def flag_deleted(self, id: int):
        """Flag a single message as deleted."""
        cur = self.conn.cursor()
        cur.execute("UPDATE messages SET deleted = 1 WHERE id = ?", (id,))

    def flag_deleted_batch(self, ids: list):
        """Flag multiple messages as deleted in a single query."""
        if not ids:
            return
        cur = self.conn.cursor()
        cur.execute("UPDATE messages SET deleted = 1 WHERE id IN ({})".format(
            ",".join("?" * len(ids))), ids)

    def get_active_message_ids(self, since_id: int = None, recent_days: int = None) -> list:
        """Get all message IDs that are not marked deleted, optionally filtered by ID or recent days."""
        cur = self.conn.cursor()
        query = "SELECT id FROM messages WHERE (deleted = 0 OR deleted IS NULL)"
        params = []
        if since_id is not None:
            query += " AND id >= ?"
            params.append(since_id)
        if recent_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
            query += " AND date >= ?"
            params.append(cutoff.strftime("%Y-%m-%d %H:%M:%S"))
        query += " ORDER BY id"
        cur.execute(query, params)
        return [r[0] for r in cur.fetchall()]

    def commit(self):
        """Commit pending writes to the DB."""
        self.conn.commit()

    def _make_message(self, m) -> Message:
        """Makes a Message() object from an SQL result tuple."""
        id, typ, date, edit_date, content, reply_to, \
            user_id, username, first_name, last_name, tags, avatar, \
            media_id, media_type, media_url, media_title, media_description, media_thumb, \
            *extra = m

        deleted = bool(extra[0]) if len(extra) > 0 else False

        md = None
        if media_id:
            desc = media_description
            if media_type == "poll":
                desc = json.loads(media_description)

            md = Media(id=media_id,
                       type=media_type,
                       url=media_url,
                       title=media_title,
                       description=desc,
                       thumb=media_thumb)

        date = pytz.utc.localize(date) if date else None
        edit_date = pytz.utc.localize(edit_date) if edit_date else None

        if self.tz:
            date = date.astimezone(self.tz) if date else None
            edit_date = edit_date.astimezone(self.tz) if edit_date else None

        return Message(id=id,
                       type=typ,
                       date=date,
                       edit_date=edit_date,
                       content=content,
                       reply_to=reply_to,
                       user=User(id=user_id,
                                 username=username,
                                 first_name=first_name,
                                 last_name=last_name,
                                 tags=tags,
                                 avatar=avatar),
                       media=md,
                       deleted=deleted)
