from io import BytesIO
import asyncio
import inspect
from sys import exit
import json
import logging
import os
import re
import tempfile
import shutil
import time

from PIL import Image
from telethon import TelegramClient, errors, events, sync
import telethon.tl.types

from .db import User, Message, Media


def parse_period(s) -> float:
    """
    Parse a human-readable period string (e.g. '30s', '10m', '2h', '1d', '2h30m', '3600')
    into seconds as a float.
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)

    s = str(s).strip()
    if not s:
        return None

    if re.match(r"^[+-]?\d+(?:\.\d+)?$", s):
        return float(s)

    is_negative = False
    if s.startswith("-"):
        is_negative = True
        s = s[1:].strip()
    elif s.startswith("+"):
        s = s[1:].strip()

    units = {
        "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
        "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
        "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
        "d": 86400, "day": 86400, "days": 86400,
        "w": 604800, "week": 604800, "weeks": 604800,
    }

    matches = list(re.finditer(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)", s))
    if not matches:
        raise ValueError(f"Invalid period format: '{s}'")

    reconstructed = "".join(m.group(0) for m in matches)
    if re.sub(r"\s+", "", s) != re.sub(r"\s+", "", reconstructed):
        raise ValueError(f"Invalid period format: '{s}'")

    total = 0.0
    for m in matches:
        val = float(m.group(1))
        unit = m.group(2).lower()
        if unit not in units:
            raise ValueError(f"Unknown time unit: '{unit}' in period '{s}'")
        total += val * units[unit]
    return -total if is_negative else total


def parse_recent_days(s) -> float:
    """
    Parse a recent days/period string (e.g. 7, '7', '7d', '2w', '48h', '30 days')
    into number of days as a float. If None or 'true', returns None.
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)

    s = str(s).strip()
    if not s or s.lower() == "true":
        return None

    if re.match(r"^[+-]?\d+(?:\.\d+)?$", s):
        return float(s)

    seconds = parse_period(s)
    return seconds / 86400.0


class Sync:
    """
    Sync iterates and receives messages from the Telegram group to the
    local SQLite DB.
    """
    config = {}
    db = None

    def __init__(self, config, session_file, db):
        self.config = config
        self.db = db
        self.no_avatar_users = set()

        self.client = self.new_client(session_file, config)

        if not os.path.exists(self.config["media_dir"]):
            os.mkdir(self.config["media_dir"])

    def sync(self, ids=None, from_id=None):
        """
        Sync syncs messages from Telegram from the last synced message
        into the local SQLite DB.
        """

        if ids:
            last_id, last_date = (ids, None)
            logging.info("fetching message id={}".format(ids))
        elif from_id:
            last_id, last_date = (from_id, None)
            logging.info("fetching from last message id={}".format(last_id))
        else:
            last_id, last_date = self.db.get_last_message_id()
            logging.info("fetching from last message id={} ({})".format(
                last_id, last_date))

        group_id = self._get_group_id(self.config["group"])

        n = 0
        while True:
            has = False
            for m in self._get_messages(group_id,
                                        offset_id=last_id if last_id else 0,
                                        ids=ids):
                if not m:
                    continue

                has = True

                # Insert the records into DB.
                self.db.insert_user(m.user)

                if m.media:
                    self.db.insert_media(m.media)

                self.db.insert_message(m)

                last_date = m.date
                n += 1
                if n % 300 == 0:
                    logging.info("fetched {} messages".format(n))
                    self.db.commit()

                if 0 < self.config["fetch_limit"] <= n:
                    has = False
                    break

            self.db.commit()
            if has and not ids:
                last_id = m.id
                logging.info("fetched {} messages. sleeping for {} seconds".format(
                    n, self.config["fetch_wait"]))
                time.sleep(self.config["fetch_wait"])
            else:
                break

        self.db.commit()
        if self.config.get("use_takeout", False):
            self.finish_takeout()
        logging.info(
            "finished. fetched {} messages. last message = {}".format(n, last_date))

    def check_updates(self, from_id=None, recent=None, recent_days=None):
        """
        Check non-deleted messages in the database against Telegram,
        flagging any deleted messages and recording edits for modified messages.
        `recent` (or `recent_days`) can be a duration string (e.g. '7d', '2w', '48h') or number of days (e.g. 7).
        """
        group_id = self._get_group_id(self.config["group"])
        val = recent if recent is not None else recent_days
        days = parse_recent_days(val) if val is not None else None
        all_ids = self.db.get_active_message_ids(since_id=from_id, recent_days=days)
        if not all_ids:
            logging.info("no active messages in DB to check for updates")
            return

        logging.info("checking {} active messages in DB for updates".format(len(all_ids)))

        batch_size = min(self.config.get("fetch_batch_size", 200), 200)
        deleted_count = 0
        edited_count = 0

        total_active = len(all_ids)
        for i in range(0, total_active, batch_size):
            chunk = all_ids[i:i + batch_size]
            remaining = total_active - (i + len(chunk))
            try:
                messages = self.client.get_messages(group_id, ids=chunk)
            except errors.FloodWaitError as e:
                logging.info("flood waited: have to wait {} seconds".format(e.seconds))
                time.sleep(e.seconds)
                messages = self.client.get_messages(group_id, ids=chunk)

            msg_list = messages if isinstance(messages, (list, tuple)) else [messages]
            chunk_deleted = []
            chunk_edited = 0
            for mid, m in zip(chunk, msg_list):
                if not m or isinstance(m, telethon.tl.types.MessageEmpty):
                    chunk_deleted.append(mid)
                elif getattr(m, "edit_date", None):
                    parsed = self._process_telethon_message(m, download_avatars=False)
                    if parsed:
                        if parsed.media:
                            self.db.insert_media(parsed.media)
                        if self.db.insert_message(parsed):
                            chunk_edited += 1

            if chunk_deleted:
                self.db.flag_deleted_batch(chunk_deleted)
                deleted_count += len(chunk_deleted)
                self.db.commit()
            elif chunk_edited > 0:
                self.db.commit()
            edited_count += chunk_edited

            logging.info("batch update: flagged {} deletion(s), recorded {} edit(s) ({} remaining to scan)".format(
                len(chunk_deleted), chunk_edited, remaining))

            time.sleep(self.config["fetch_wait"])

        self.db.commit()
        if self.config.get("use_takeout", False):
            self.finish_takeout()
        logging.info("finished checking updates. Flagged {} deleted messages, recorded {} edited messages".format(
            deleted_count, edited_count))

    check_deleted = check_updates

    def listen(self, timeout=None):
        """
        Listen for live Telegram events (MessageDeleted, NewMessage, MessageEdited)
        and record them in real-time in the SQLite DB.
        If timeout (in seconds or human period string like '10m', '2h') is provided,
        listen mode will exit cleanly after the specified duration.
        """
        group_id = self._get_group_id(self.config["group"])
        period = parse_period(timeout) if timeout is not None else None
        if period is not None and period <= 0:
            logging.info("listen period is 0 or negative; exiting immediately.")
            return

        if period is not None:
            logging.info("listening for live Telegram events on group {} for {}s".format(group_id, period))
        else:
            logging.info("listening for live Telegram events on group {}".format(group_id))

        @self.client.on(events.MessageDeleted(chats=group_id))
        async def on_message_deleted(event):
            deleted_ids = getattr(event, "deleted_ids", None)
            if not deleted_ids:
                single_id = getattr(event, "deleted_id", None)
                deleted_ids = [single_id] if single_id else []
            if deleted_ids:
                self.db.flag_deleted_batch(deleted_ids)
                self.db.commit()
                logging.info("flagged {} deleted message(s) in DB: {}".format(
                    len(deleted_ids), deleted_ids))

        @self.client.on(events.NewMessage(chats=group_id))
        async def on_new_message(event):
            m = await self._process_telethon_message_async(event.message)
            if m:
                self.db.insert_user(m.user)
                if m.media:
                    self.db.insert_media(m.media)
                self.db.insert_message(m)
                self.db.commit()
                logging.info("live: inserted message #{}".format(m.id))

        @self.client.on(events.MessageEdited(chats=group_id))
        async def on_message_edited(event):
            m = await self._process_telethon_message_async(event.message, download_avatars=False)
            if m:
                self.db.insert_user(m.user)
                if m.media:
                    self.db.insert_media(m.media)
                self.db.insert_message(m)
                self.db.commit()
                logging.info("live: updated edited message #{}".format(m.id))

        timer_task = None
        if period is not None and hasattr(self.client, "loop") and self.client.loop:
            async def _stop_after_timeout():
                try:
                    await asyncio.sleep(period)
                    logging.info("listen period ({}s) elapsed; stopping listener".format(period))
                    res = self.client.disconnect()
                    if inspect.isawaitable(res):
                        await res
                except asyncio.CancelledError:
                    pass

            timer_task = self.client.loop.create_task(_stop_after_timeout())

        try:
            self.client.run_until_disconnected()
        finally:
            if timer_task and not timer_task.done():
                timer_task.cancel()

    def new_client(self, session, config):
        if "proxy" in config and config["proxy"].get("enable"):
            proxy = config["proxy"]
            client = TelegramClient(session, config["api_id"], config["api_hash"], proxy=(proxy["protocol"], proxy["addr"], proxy["port"]))
        else:
            client = TelegramClient(session, config["api_id"], config["api_hash"])
        # hide log messages
        # upstream issue https://github.com/LonamiWebs/Telethon/issues/3840
        client_logger = client._log["telethon.client.downloads"]
        client_logger._info = client_logger.info

        def patched_info(*args, **kwargs):
            if (
                args[0] == "File lives in another DC" or
                args[0] == "Starting direct file download in chunks of %d at %d, stride %d"
            ):
                return client_logger.debug(*args, **kwargs)
            client_logger._info(*args, **kwargs)
        client_logger.info = patched_info

        client.start()
        if config.get("use_takeout", False):
            for retry in range(3):
                try:
                    takeout_client = client.takeout(finalize=True).__enter__()
                    # check if the takeout session gets invalidated
                    takeout_client.get_messages("me")
                    return takeout_client
                except errors.TakeoutInitDelayError as e:
                    logging.info(
                        "please allow the data export request received from Telegram on your device. "
                        "you can also wait for {} seconds.".format(e.seconds))
                    logging.info(
                        "press Enter key after allowing the data export request to continue..")
                    input()
                    logging.info("trying again.. ({})".format(retry + 2))
                except errors.TakeoutInvalidError:
                    logging.info("takeout invalidated. delete the session.session file and try again.")
            else:
                logging.info("could not initiate takeout.")
                raise(Exception("could not initiate takeout."))
        else:
            return client

    def finish_takeout(self):
        self.client.__exit__(None, None, None)

    def _get_messages(self, group, offset_id, ids=None) -> Message:
        messages = self._fetch_messages(group, offset_id, ids)
        # https://docs.telethon.dev/en/latest/quick-references/objects-reference.html#message
        if ids:
            id_list = ids if isinstance(ids, (list, tuple)) else [ids]
            msg_list = messages if isinstance(messages, (list, tuple)) else [messages]
            for mid, m in zip(id_list, msg_list):
                if not m or isinstance(m, telethon.tl.types.MessageEmpty):
                    self.db.flag_deleted(mid)
                    logging.info("message #{} was deleted on Telegram, flagged in DB".format(mid))
                    continue
                parsed = self._process_telethon_message(m)
                if parsed:
                    yield parsed
            return

        for m in messages:
            if not m or isinstance(m, telethon.tl.types.MessageEmpty):
                continue
            parsed = self._process_telethon_message(m)
            if parsed:
                yield parsed

    def _fetch_messages(self, group, offset_id, ids=None) -> Message:
        try:
            if self.config.get("use_takeout", False):
                wait_time = 0
            else:
                wait_time = None
            messages = self.client.get_messages(group, offset_id=offset_id,
                                                limit=self.config["fetch_batch_size"],
                                                wait_time=wait_time,
                                                ids=ids,
                                                reverse=True)
            return messages
        except errors.FloodWaitError as e:
            logging.info(
                "flood waited: have to wait {} seconds".format(e.seconds))

    def _extract_sticker(self, m):
        if m.media:
            if isinstance(m.media, telethon.tl.types.MessageMediaDocument) and \
                    hasattr(m.media, "document") and \
                    m.media.document.mime_type == "application/x-tgsticker":
                alt = [a.alt for a in m.media.document.attributes if isinstance(
                    a, telethon.tl.types.DocumentAttributeSticker)]
                if len(alt) > 0:
                    return alt[0]
        return None

    def _build_message(self, m, sticker, user, med):
        typ = "message"
        if m.action:
            if isinstance(m.action, telethon.tl.types.MessageActionChatAddUser):
                typ = "user_joined"
            elif isinstance(m.action, telethon.tl.types.MessageActionChatJoinedByLink):
                typ = "user_joined_by_link"
            elif isinstance(m.action, telethon.tl.types.MessageActionChatDeleteUser):
                typ = "user_left"

        return Message(
            type=typ,
            id=m.id,
            date=m.date,
            edit_date=m.edit_date,
            content=sticker if sticker else m.raw_text,
            reply_to=m.reply_to_msg_id if m.reply_to and m.reply_to.reply_to_msg_id else None,
            user=user,
            media=med,
            deleted=False
        )

    def _process_telethon_message(self, m, download_avatars=True) -> Message:
        if not m or isinstance(m, telethon.tl.types.MessageEmpty):
            return None

        sticker = self._extract_sticker(m)
        med = None
        if m.media and not sticker:
            if isinstance(m.media, telethon.tl.types.MessageMediaPoll):
                med = self._make_poll(m)
            else:
                med = self._get_media(m)

        user = self._get_user(m.sender, m.chat, download_avatar=download_avatars)
        return self._build_message(m, sticker, user, med)

    async def _process_telethon_message_async(self, m, download_avatars=True) -> Message:
        if not m or isinstance(m, telethon.tl.types.MessageEmpty):
            return None

        sticker = self._extract_sticker(m)
        med = None
        if m.media and not sticker:
            if isinstance(m.media, telethon.tl.types.MessageMediaPoll):
                med = self._make_poll(m)
            else:
                med = await self._get_media_async(m)

        user = await self._get_user_async(m.sender, m.chat, download_avatar=download_avatars)
        return self._build_message(m, sticker, user, med)

    def _build_user(self, u, chat, avatar):
        tags = []

        if (
            u is None and
            chat is not None and
            chat.title != ''
            ):
                tags.append("group_self")
                return User(
                    id=chat.id,
                    username=chat.title,
                    first_name=None,
                    last_name=None,
                    tags=tags,
                    avatar=avatar
                )

        is_normal_user = isinstance(u, telethon.tl.types.User)

        if isinstance(u, telethon.tl.types.ChannelForbidden):
            return User(
                id=u.id,
                username=u.title,
                first_name=None,
                last_name=None,
                tags=tags,
                avatar=None
            )

        if is_normal_user:
            if u.bot:
                tags.append("bot")

        if u.scam:
            tags.append("scam")

        if u.fake:
            tags.append("fake")

        return User(
            id=u.id,
            username=u.username if u.username else str(u.id),
            first_name=u.first_name if is_normal_user else None,
            last_name=u.last_name if is_normal_user else None,
            tags=tags,
            avatar=avatar
        )

    def _get_user(self, u, chat, download_avatar=True) -> User:
        if isinstance(u, telethon.tl.types.ChannelForbidden):
            return self._build_user(u, chat, None)
        target = chat if (u is None and chat is not None and chat.title != '') else u
        avatar = self._downloadAvatarForUserOrChat(target) if download_avatar else None
        return self._build_user(u, chat, avatar)

    async def _get_user_async(self, u, chat, download_avatar=True) -> User:
        if isinstance(u, telethon.tl.types.ChannelForbidden):
            return self._build_user(u, chat, None)
        target = chat if (u is None and chat is not None and chat.title != '') else u
        avatar = await self._downloadAvatarForUserOrChat_async(target) if download_avatar else None
        return self._build_user(u, chat, avatar)

    def _make_poll(self, msg):
        if not msg.media.results or not msg.media.results.results:
            return None

        options = [{"label": a.text.text, "count": 0, "correct": False}
                   for a in msg.media.poll.answers]

        total = msg.media.results.total_voters
        if msg.media.results.results:
            for i, r in enumerate(msg.media.results.results):
                options[i]["count"] = r.voters
                options[i]["percent"] = r.voters / \
                    total * 100 if total > 0 else 0
                options[i]["correct"] = r.correct

        return Media(
            id=msg.id,
            type="poll",
            url=None,
            title=msg.media.poll.question.text,
            description=json.dumps(options),
            thumb=None
        )

    def _find_cached_media(self, msg):
        if isinstance(msg.media, telethon.tl.types.MessageMediaWebPage) and \
                not isinstance(msg.media.webpage, telethon.tl.types.WebPageEmpty):
            return True, Media(
                id=msg.id,
                type="webpage",
                url=msg.media.webpage.url,
                title=msg.media.webpage.title,
                description=msg.media.webpage.description if msg.media.webpage.description else None,
                thumb=None
            ), None, None

        if not (isinstance(msg.media, telethon.tl.types.MessageMediaPhoto) or \
                isinstance(msg.media, telethon.tl.types.MessageMediaDocument) or \
                isinstance(msg.media, telethon.tl.types.MessageMediaContact)):
            return True, None, None, None

        if not self.config["download_media"]:
            return True, None, None, None

        media_mime_types = self.config.get("media_mime_types", [])
        if len(media_mime_types) > 0:
            if hasattr(msg, "file") and hasattr(msg.file, "mime_type") and msg.file.mime_type:
                if msg.file.mime_type not in media_mime_types:
                    logging.info(
                        "skipping media #{} / {}".format(msg.file.name, msg.file.mime_type))
                    return True, None, None, None

        telegram_id = None
        if isinstance(msg.media, telethon.tl.types.MessageMediaPhoto) and hasattr(msg.media, "photo"):
            telegram_id = getattr(msg.media.photo, "id", None)
        elif isinstance(msg.media, telethon.tl.types.MessageMediaDocument) and hasattr(msg.media, "document"):
            telegram_id = getattr(msg.media.document, "id", None)

        existing = self.db.get_media(msg.id)
        if existing and existing.url:
            fpath = os.path.join(self.config["media_dir"], existing.url)
            if os.path.exists(fpath):
                is_same = False
                if telegram_id is not None and existing.description == str(telegram_id):
                    is_same = True
                elif existing.description is None:
                    # Legacy record without stored telegram_id: verify by size
                    if isinstance(msg.media, telethon.tl.types.MessageMediaDocument) and hasattr(msg.media, "document"):
                        doc_size = getattr(msg.media.document, "size", None)
                        if doc_size is not None and doc_size == os.path.getsize(fpath):
                            is_same = True
                    elif isinstance(msg.media, telethon.tl.types.MessageMediaPhoto):
                        if os.path.getsize(fpath) > 0:
                            is_same = True

                if is_same:
                    if telegram_id is not None and existing.description != str(telegram_id):
                        updated = Media(
                            id=existing.id,
                            type=existing.type,
                            url=existing.url,
                            title=existing.title,
                            description=str(telegram_id),
                            thumb=existing.thumb
                        )
                        self.db.insert_media(updated)
                        return True, updated, telegram_id, existing
                    return True, existing, telegram_id, existing

        return False, None, telegram_id, existing

    def _finalize_downloaded_media(self, msg, basename, fname, thumb, telegram_id, existing):
        if not fname:
            return None
        if existing and existing.url and existing.url != fname:
            old_fpath = os.path.join(self.config["media_dir"], existing.url)
            if os.path.exists(old_fpath):
                try:
                    os.remove(old_fpath)
                except OSError:
                    pass
        return Media(
            id=msg.id,
            type="photo",
            url=fname,
            title=basename,
            description=str(telegram_id) if telegram_id is not None else None,
            thumb=thumb
        )

    def _get_media(self, msg):
        handled, media, telegram_id, existing = self._find_cached_media(msg)
        if handled:
            return media

        logging.info("downloading media #{}".format(msg.id))
        try:
            basename, fname, thumb = self._download_media(msg)
            return self._finalize_downloaded_media(msg, basename, fname, thumb, telegram_id, existing)
        except Exception as e:
            logging.error(
                "error downloading media: #{}: {}".format(msg.id, e))

    async def _get_media_async(self, msg):
        handled, media, telegram_id, existing = self._find_cached_media(msg)
        if handled:
            return media

        logging.info("downloading media #{}".format(msg.id))
        try:
            basename, fname, thumb = await self._download_media_async(msg)
            return self._finalize_downloaded_media(msg, basename, fname, thumb, telegram_id, existing)
        except Exception as e:
            logging.error(
                "error downloading media: #{}: {}".format(msg.id, e))

    def _handle_downloaded_media_files(self, msg, fpath, tpath=None):
        if not fpath:
            return None, None, None
        basename = os.path.basename(fpath)

        newname = "{}.{}".format(msg.id, self._get_file_ext(basename))
        shutil.move(fpath, os.path.join(self.config["media_dir"], newname))

        # If it's a photo, download the thumbnail.
        tname = None
        if tpath:
            tname = "thumb_{}.{}".format(
                msg.id, self._get_file_ext(os.path.basename(tpath)))
            shutil.move(tpath, os.path.join(self.config["media_dir"], tname))

        return basename, newname, tname

    def _download_media(self, msg) -> [str, str, str]:
        """
        Download a media / file attached to a message and return its original
        filename, sanitized name on disk, and the thumbnail (if any). 
        """
        fpath = self.client.download_media(msg, file=tempfile.gettempdir())
        tpath = None
        if isinstance(msg.media, telethon.tl.types.MessageMediaPhoto):
            tpath = self.client.download_media(
                msg, file=tempfile.gettempdir(), thumb=1)

        return self._handle_downloaded_media_files(msg, fpath, tpath)

    async def _download_media_async(self, msg) -> [str, str, str]:
        """
        Download a media / file attached to a message asynchronously and return its original
        filename, sanitized name on disk, and the thumbnail (if any). 
        """
        fpath = self.client.download_media(msg, file=tempfile.gettempdir())
        if inspect.isawaitable(fpath):
            fpath = await fpath

        tpath = None
        if isinstance(msg.media, telethon.tl.types.MessageMediaPhoto):
            tpath = self.client.download_media(
                msg, file=tempfile.gettempdir(), thumb=1)
            if inspect.isawaitable(tpath):
                tpath = await tpath

        return self._handle_downloaded_media_files(msg, fpath, tpath)

    def _get_file_ext(self, f) -> str:
        if "." in f:
            e = f.split(".")[-1]
            if len(e) < 6:
                return e

        return ".file"

    def _save_avatar_file(self, user, profile_photo, b, fpath, fname):
        if profile_photo is None:
            logging.info("user has no avatar #{}".format(user.id))
            if hasattr(self, "no_avatar_users"):
                self.no_avatar_users.add(user.id)
            return None

        im = Image.open(b)
        im.thumbnail(self.config["avatar_size"], Image.LANCZOS)
        im.save(fpath, "JPEG")

        return fname

    def _download_avatar(self, user):
        fname = "avatar_{}.jpg".format(user.id)
        fpath = os.path.join(self.config["media_dir"], fname)

        if os.path.exists(fpath):
            return fname

        if hasattr(self, "no_avatar_users") and user.id in self.no_avatar_users:
            return None

        logging.info("downloading avatar #{}".format(user.id))

        # Download the file into a container, resize it, and then write to disk.
        b = BytesIO()
        profile_photo = self.client.download_profile_photo(user, file=b)
        return self._save_avatar_file(user, profile_photo, b, fpath, fname)

    async def _download_avatar_async(self, user):
        fname = "avatar_{}.jpg".format(user.id)
        fpath = os.path.join(self.config["media_dir"], fname)

        if os.path.exists(fpath):
            return fname

        if hasattr(self, "no_avatar_users") and user.id in self.no_avatar_users:
            return None

        logging.info("downloading avatar #{}".format(user.id))

        # Download the file into a container, resize it, and then write to disk.
        b = BytesIO()
        profile_photo = self.client.download_profile_photo(user, file=b)
        if inspect.isawaitable(profile_photo):
            profile_photo = await profile_photo
        return self._save_avatar_file(user, profile_photo, b, fpath, fname)

    def _get_group_id(self, group):
        """
        Syncs the Entity cache and returns the Entity ID for the specified group,
        which can be a str/int for group ID, group name, or a group username.

        The authorized user must be a part of the group.
        """
        # Get all dialogs for the authorized user, which also
        # syncs the entity cache to get latest entities
        # ref: https://docs.telethon.dev/en/latest/concepts/entities.html#getting-entities
        _ = self.client.get_dialogs()

        try:
            # If the passed group is a group ID, extract it.
            group = int(group)
        except ValueError:
            # Not a group ID, we have either a group name or
            # a group username: @group-username
            pass

        try:
            entity = self.client.get_entity(group)
        except ValueError:
            logging.critical("the group: {} does not exist,"
                             " or the authorized user is not a participant!".format(group))
            # This is a critical error, so exit with code: 1
            exit(1)

        return entity.id

    def _downloadAvatarForUserOrChat(self, entity):
        avatar = None
        if self.config["download_avatars"]:
            try:
                fname = self._download_avatar(entity)
                avatar = fname
            except Exception as e:
                logging.error(
                    "error downloading avatar: #{}: {}".format(entity.id, e))
        return avatar

    async def _downloadAvatarForUserOrChat_async(self, entity):
        avatar = None
        if self.config["download_avatars"]:
            try:
                fname = await self._download_avatar_async(entity)
                avatar = fname
            except Exception as e:
                logging.error(
                    "error downloading avatar: #{}: {}".format(entity.id, e))
        return avatar
