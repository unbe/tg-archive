from collections import OrderedDict, deque
from importlib.metadata import version
import difflib
import html
import logging
import math
import os
import re
import shutil
import magic

from feedgen.feed import FeedGenerator
from jinja2 import Template

from .db import User, Message


_NL2BR = re.compile(r"\n\n+")
_RE_TG_LINK = re.compile(
    r"(?P<url>(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:c/(?P<c_id>\d+)|s/(?P<s_username>[a-zA-Z0-9_]+)|(?P<username>[a-zA-Z0-9_]+))/(?:(?P<topic_id>\d+)/)?(?P<msg_id>\d+)/?(?:[?#][^\s<>]*)?|tg://(?:privatepost\?channel=(?P<tg_c_id>\d+)&(?:amp;)?post=(?P<tg_msg_id>\d+)|resolve\?domain=(?P<tg_username>[a-zA-Z0-9_]+)&(?:amp;)?post=(?P<tg_u_msg_id>\d+)))",
    re.IGNORECASE
)


class Build:
    config = {}
    template = None
    db = None

    def __init__(self, config, db, symlink):
        self.config = config
        self.db = db
        self.symlink = symlink

        self.rss_template: Template = None

        # Map of all message IDs across all months and the slug of the page
        # in which they occur (paginated), used to link replies to their
        # parent messages that may be on arbitrary pages.
        self.page_ids = {}
        self.timeline = OrderedDict()
        self.chat_identifiers = set()
        self._init_chat_identifiers()

    def build(self):
        # (Re)create the output directory.
        self._create_publish_dir()

        timeline = list(self.db.get_timeline())
        if len(timeline) == 0:
            logging.info("no data found to publish site")
            quit()

        for month in timeline:
            if month.date.year not in self.timeline:
                self.timeline[month.date.year] = []
            self.timeline[month.date.year].append(month)

        # Queue to store the latest N items to publish in the RSS feed.
        rss_entries = deque([], self.config["rss_feed_entries"])
        fname = None
        pages_to_render = []
        for month in timeline:
            # Get the days + message counts for the month.
            dayline = OrderedDict()
            for d in self.db.get_dayline(month.date.year, month.date.month, self.config["per_page"]):
                dayline[d.slug] = d

            # Paginate and fetch messages for the month until the end..
            page = 0
            last_id = 0
            total = self.db.get_message_count(
                month.date.year, month.date.month)
            total_pages = math.ceil(total / self.config["per_page"])

            while True:
                messages = list(self.db.get_messages(month.date.year, month.date.month,
                                                     last_id, self.config["per_page"]))

                if len(messages) == 0:
                    break

                last_id = messages[-1].id

                page += 1
                fname = self.make_filename(month, page)

                # Collect the message ID -> page name for all messages in the set
                # to link to replies in arbitrary positions across months, paginated pages.
                for m in messages:
                    self.page_ids[m.id] = fname

                pages_to_render.append((messages, month, dayline, fname, page, total_pages))

        # Render all pages after page_ids is completely populated across all months
        fname = pages_to_render[-1][3] if pages_to_render else None
        for messages, month, dayline, fname_page, page, total_pages in pages_to_render:
            edits_map = self.db.get_edits_for_messages([m.id for m in messages])
            if edits_map:
                updated_messages = []
                for m in messages:
                    edits = edits_map.get(m.id, [])
                    if edits:
                        computed_edits = []
                        for idx, e in enumerate(edits):
                            next_text = edits[idx + 1].content if idx + 1 < len(edits) else m.content
                            diff_html = self._render_diff(e.content, next_text)
                            computed_edits.append(e._replace(diff=diff_html))
                        m = m._replace(edits=computed_edits)
                    updated_messages.append(m)
                messages = updated_messages

            if self.config["publish_rss_feed"]:
                rss_entries.extend(messages)

            self._render_page(messages, month, dayline,
                              fname_page, page, total_pages)

        # The last page chronologically is the latest page. Make it index.
        if fname:
            if self.symlink:
                os.symlink(fname, os.path.join(self.config["publish_dir"], "index.html"))
            else:
                shutil.copy(os.path.join(self.config["publish_dir"], fname),
                            os.path.join(self.config["publish_dir"], "index.html"))

        # Generate RSS feeds.
        if self.config["publish_rss_feed"]:
            self._build_rss(rss_entries, "index.rss", "index.atom")

    def load_template(self, fname):
        with open(fname, "r") as f:
            self.template = Template(f.read(), autoescape=True)

    def load_rss_template(self, fname):
        with open(fname, "r") as f:
            self.rss_template = Template(f.read(), autoescape=True)

    def make_filename(self, month, page) -> str:
        fname = "{}{}.html".format(
            month.slug, "_" + str(page) if page > 1 else "")
        return fname

    def _render_page(self, messages, month, dayline, fname, page, total_pages):
        html = self.template.render(config=self.config,
                                    timeline=self.timeline,
                                    dayline=dayline,
                                    month=month,
                                    messages=messages,
                                    page_ids=self.page_ids,
                                    pagination={"current": page,
                                                "total": total_pages},
                                    make_filename=self.make_filename,
                                    nl2br=self._nl2br,
                                    get_archive_url=self.get_archive_url)

        with open(os.path.join(self.config["publish_dir"], fname), "w", encoding="utf8") as f:
            f.write(html)

    def _build_rss(self, messages, rss_file, atom_file):
        f = FeedGenerator()
        f.id(self.config["site_url"])
        f.generator(
            "tg-archive {}".format(version("tg-archive")))
        f.link(href=self.config["site_url"], rel="alternate")
        f.title(self.config["site_name"].format(group=self.config["group"]))
        f.subtitle(self.config["site_description"])

        for m in messages:
            url = "{}/{}#{}".format(self.config["site_url"],
                                    self.page_ids[m.id], m.id)
            e = f.add_entry()
            e.id(url)
            e.title("@{} on {} (#{})".format(m.user.username, m.date, m.id))
            e.link({"href": url})
            e.published(m.date)

            media_mime = ""
            if m.media and m.media.url:
                murl = "{}/{}/{}".format(self.config["site_url"],
                                         os.path.basename(self.config["media_dir"]), m.media.url)
                media_path = "{}/{}".format(self.config["media_dir"], m.media.url)
                media_mime = "application/octet-stream"
                media_size = 0

                if "://" in media_path:
                    media_mime = "text/html"
                else:
                    try:
                        media_size = str(os.path.getsize(media_path))
                        try:
                            media_mime = magic.from_file(media_path, mime=True)
                        except:
                            pass
                    except FileNotFoundError:
                        pass

                e.enclosure(murl, media_size, media_mime)
            e.content(self._make_abstract(m, media_mime), type="html")

        f.rss_file(os.path.join(self.config["publish_dir"], "index.xml"), pretty=True)
        f.atom_file(os.path.join(self.config["publish_dir"], "index.atom"), pretty=True)

    def _make_abstract(self, m, media_mime):
        if self.rss_template:
            return self.rss_template.render(config=self.config,
                                            m=m,
                                            media_mime=media_mime,
                                            page_ids=self.page_ids,
                                            nl2br=self._nl2br,
                                            get_archive_url=self.get_archive_url)
        out = m.content
        if not out and m.media:
            out = m.media.title
        return out if out else ""

    def _nl2br(self, s) -> str:
        # There has to be a \n before <br> so as to not break
        # Jinja's automatic hyperlinking of URLs.
        res = _NL2BR.sub("\n\n", s).replace("\n", "\n<br />")
        return self._link_archive_urls(res)

    def _init_chat_identifiers(self):
        self.chat_identifiers = set()

        if "group" in self.config and self.config["group"]:
            g = str(self.config["group"]).strip()
            # If group is a URL like https://t.me/c/1450089406 or https://t.me/mygroup
            m_url = re.search(r"(?:t\.me|telegram\.me)/(?:c/(\d+)|s/([a-zA-Z0-9_]+)|([a-zA-Z0-9_]+))", g)
            if m_url:
                if m_url.group(1):
                    self.chat_identifiers.add(m_url.group(1))
                if m_url.group(2):
                    self.chat_identifiers.add(m_url.group(2).lower())
                if m_url.group(3):
                    self.chat_identifiers.add(m_url.group(3).lower())

            if g.startswith("@"):
                self.chat_identifiers.add(g[1:].lower())
            else:
                self.chat_identifiers.add(g.lower())

            clean_id = g.lstrip("-")
            if clean_id.startswith("100") and len(clean_id) > 3:
                clean_id = clean_id[3:]
            if clean_id.isdigit():
                self.chat_identifiers.add(clean_id)

        if "group_id" in self.config and self.config["group_id"]:
            gid = str(self.config["group_id"]).strip().lstrip("-")
            if gid.startswith("100") and len(gid) > 3:
                gid = gid[3:]
            if gid.isdigit():
                self.chat_identifiers.add(gid)

        if "group_username" in self.config and self.config["group_username"]:
            u = str(self.config["group_username"]).strip().lstrip("@").lower()
            self.chat_identifiers.add(u)

        if self.db:
            try:
                cur = self.db.conn.cursor()
                cur.execute("SELECT id, username FROM users WHERE tags LIKE '%group_self%'")
                for row in cur.fetchall():
                    uid, uname = row[0], row[1]
                    if uid:
                        cid = str(uid).lstrip("-")
                        if cid.startswith("100") and len(cid) > 3:
                            cid = cid[3:]
                        if cid.isdigit():
                            self.chat_identifiers.add(cid)
                    if uname:
                        self.chat_identifiers.add(str(uname).lstrip("@").lower())
            except Exception:
                pass

    def get_archive_url(self, url: str):
        if not url:
            return None
        m = _RE_TG_LINK.search(url)
        if not m:
            return None
        cid = m.group("c_id") or m.group("tg_c_id")
        user = m.group("username") or m.group("s_username") or m.group("tg_username")
        mid_str = m.group("msg_id") or m.group("tg_msg_id") or m.group("tg_u_msg_id")
        if not mid_str:
            return None

        is_match = False
        if cid and cid in self.chat_identifiers:
            is_match = True
        elif user and user.lower() in self.chat_identifiers:
            is_match = True
        elif int(mid_str) in self.page_ids:
            if cid:
                self.chat_identifiers.add(cid)
                is_match = True
            elif user:
                self.chat_identifiers.add(user.lower())
                is_match = True

        if not is_match:
            return None

        mid = int(mid_str)
        if mid in self.page_ids:
            return f"{self.page_ids[mid]}#{mid}"
        return f"#{mid}"

    def _link_archive_urls(self, text: str) -> str:
        if not text:
            return text

        def repl(match):
            full = match.group(0)
            end = match.end()
            after = text[end:end+120]
            if after.strip().startswith('(<a href=') and 'class="archive-link"' in after:
                return full

            trail = ""
            while full and full[-1] in ".,;:!?)":
                trail = full[-1] + trail
                full = full[:-1]

            archive_url = self.get_archive_url(full)
            if not archive_url:
                return match.group(0)

            archive_tag = f' (<a href="{archive_url}" class="archive-link">archive</a>)'
            return f"{full}{archive_tag}{trail}"

        return _RE_TG_LINK.sub(repl, text)

    def _create_publish_dir(self):
        pubdir = self.config["publish_dir"]

        # Clear the output directory.
        if os.path.exists(pubdir):
            shutil.rmtree(pubdir)

        # Re-create the output directory.
        os.mkdir(pubdir)

        # Copy the static directory into the output directory.
        for f in [self.config["static_dir"]]:
            target = os.path.join(pubdir, f)
            if self.symlink:
                self._relative_symlink(os.path.abspath(f), target)
            elif os.path.isfile(f):
                shutil.copyfile(f, target)
            else:
                shutil.copytree(f, target)

        # If media downloading is enabled, copy/symlink the media directory.
        mediadir = self.config["media_dir"]
        if os.path.exists(mediadir):
            if self.symlink:
                self._relative_symlink(os.path.abspath(mediadir), os.path.join(
                    pubdir, os.path.basename(mediadir)))
            else:
                shutil.copytree(mediadir, os.path.join(
                    pubdir, os.path.basename(mediadir)))

    def _relative_symlink(self, src, dst):
        dir_path = os.path.dirname(dst)
        src = os.path.relpath(src, dir_path)
        dst = os.path.join(dir_path, os.path.basename(src))
        return os.symlink(src, dst)

    def _render_diff(self, old_text: str, new_text: str) -> str:
        """Compute an inline word/token diff between old_text and new_text with HTML highlights."""
        if not old_text and not new_text:
            return ""
        if old_text == new_text:
            return self._link_archive_urls(html.escape(old_text or "").replace("\n", "<br />"))

        tokens_old = re.findall(r"\w+|\s+|[^\w\s]", old_text or "", re.UNICODE)
        tokens_new = re.findall(r"\w+|\s+|[^\w\s]", new_text or "", re.UNICODE)
        matcher = difflib.SequenceMatcher(None, tokens_old, tokens_new)
        result = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                result.append(html.escape("".join(tokens_old[i1:i2])))
            elif tag == "delete":
                result.append(f'<del class="diff-del">{html.escape("".join(tokens_old[i1:i2]))}</del>')
            elif tag == "insert":
                result.append(f'<ins class="diff-ins">{html.escape("".join(tokens_new[j1:j2]))}</ins>')
            elif tag == "replace":
                result.append(f'<del class="diff-del">{html.escape("".join(tokens_old[i1:i2]))}</del><ins class="diff-ins">{html.escape("".join(tokens_new[j1:j2]))}</ins>')
        return self._link_archive_urls("".join(result).replace("\n", "<br />"))
