"""
This module implements an extension and cog which extend a bot with
functionality related to the townsquare spy.

It monitors specific channels for mentions of townsquare URLs, then
begins monitoring them and logging what happens to an sqlite database.
While this database could be examined manually, commands to query it
are also provided.

See __main__.py for a much simpler usage of the spy functionality.
"""

import asyncio
import dateparser
import functools
import json
import lzma
import nextcord
import os
import re
import shutil
import sqlite3
import tempfile

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO, TextIOWrapper
from nextcord.ext import commands
from typing import Optional

from .spy import random_player_id, interpret_url, connect_to_session, receive, Player, Session


# Database access

class DatabaseThread(object):
    """
    To ensure the main thread (and thus the bot) remain responsive even if the
    disk is busy/slow, we set aside a thread and do all database access there.
    One thread suffices, and this avoids the need to do additional synchronization
    if there were multiple.
    """
    conn: Optional[sqlite3.Connection]
    executor: ThreadPoolExecutor

    def __init__(self, path: str):
        self.conn = None
        self.executor = ThreadPoolExecutor(1, "Townsquare Spy Database", self.connect, (path,))

    def connect(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""
            BEGIN;
            CREATE TABLE IF NOT EXISTS session_log(
                url VARCHAR(255),
                session_start TIMESTAMP,
                timestamp TIMESTAMP,
                message TEXT,
                state TEXT
            );
            CREATE INDEX IF NOT EXISTS session_log_index
            ON session_log(url, session_start);
            COMMIT;
        """)

    def log(self, messages: list[dict]) -> asyncio.Future:
        def write_on_thread():
            self.conn.executemany(
                """
                    INSERT INTO session_log
                    (url, session_start, timestamp, message, state)
                    VALUES (:url, :session_start, :timestamp, :message, :state);
                """,
                messages)
            self.conn.commit()
        return asyncio.get_running_loop().run_in_executor(
            self.executor, write_on_thread)
    
    def latest(self, url: str, as_of: Optional[datetime] = None) -> asyncio.Future:
        def read_on_thread():
            session_start_condition = "TRUE" if as_of is None else "session_start <= :as_of"
            cur = self.conn.execute(
                f"""
                    SELECT timestamp, message
                    FROM session_log
                    WHERE
                        url = :url AND
                        session_start = (SELECT MAX(session_start) FROM session_log WHERE url = :url AND {session_start_condition})
                """,
                dict(url=url, as_of=as_of))
            return cur.fetchall()
        return asyncio.get_running_loop().run_in_executor(
            self.executor, read_on_thread)

    def dump(self) -> asyncio.Future:
        def dump_on_thread():
            named_temp = tempfile.NamedTemporaryFile()
            try:
                named_temp.close()
                self.conn.execute("VACUUM INTO ?", (named_temp.name,))
                compressed_file = tempfile.TemporaryFile()
                with open(named_temp.name, "rb") as uncompressed, \
                    lzma.LZMAFile(compressed_file, "wb") as lzma_file:
                    shutil.copyfileobj(uncompressed, lzma_file)
                compressed_file.seek(0)
                return compressed_file
            finally:
                os.unlink(named_temp.name)
        return asyncio.get_running_loop().run_in_executor(
            self.executor, dump_on_thread)


# Utilities for formatting data

def summarize_player(p: Player):
    """
    Summarize the state of an individual player for the log.
    This is less complete than the full state, but makes
    it easier to query the interesting state changes.
    """
    summary = dict(name=p.name, is_dead=p.is_dead, known_role=p.known_role)
    if not summary["known_role"]:
        del summary["known_role"]
    return summary

def summarize_state(session: Session):
    """
    Summarize the state of the game for the log.
    This is less complete than the full state, but makes
    it easier to query the interesting state changes.
    """
    return json.dumps(dict(
        players=[summarize_player(p) for p in session.players],
        fabled=session.fabled))

def strip_ansi(s):
    return re.sub(r'\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]', '', s)


# Observing an individual session and timing out when it becomes inactive

@dataclass
class MonitoredSessionState:
    session: Optional[Session] = None
    task: Optional[asyncio.Task] = None

async def monitor_session(monitored: MonitoredSessionState, url: str, db_thread: DatabaseThread):
    """
    Monitors a session. Expected to be run as a task.
    Dispatches database access to a thread pool, but attempts to
    cancel it if the task is itself cancelled.
    """
    monitored.session = Session()
    monitored.session_start = datetime.now(timezone.utc)
    player_id = random_player_id()
    socket_url, app_origin = interpret_url(url, player_id)

    messages = []
    current_state = None
    def log_message(message: str):
        nonlocal current_state
        new_state = summarize_state(monitored.session)
        messages.append(dict(
            url=url,
            session_start=monitored.session_start,
            timestamp=datetime.now(timezone.utc),
            message=message,
            state=new_state if new_state != current_state else None))
        current_state = new_state

    monitored.session.log = log_message

    # We wait 10 minutes to receive initial game state, and 30 minutes
    # without anything happening to stop monitoring.
    initial_timeout = timedelta(minutes=10)
    abandon_timeout = timedelta(minutes=30)

    socket = connect_to_session(socket_url, origin=app_origin, player_id=player_id)
    async with asyncio.timeout(initial_timeout.total_seconds()) as timeout:
        async for m in socket:
            receive(monitored.session, m)
            if not messages: continue
            timeout.reschedule(asyncio.get_running_loop().time() + abandon_timeout.total_seconds())

            # If there are now messages to log, do so. If this task is cancelled
            # while waiting for that to finish, attempt to cancel it.
            write_future = db_thread.log(messages)
            try:
                await write_future
            except asyncio.CancelledError:
                write_future.cancel()
                raise
            messages.clear()


# Observing events and accepting commands from Discord

class TownsquareSpyCog(commands.Cog):
    bot: commands.Bot
    db_thread: DatabaseThread
    monitored_sessions: dict[str, MonitoredSessionState]
    watched_channels: set[int]
    watch_re: re.Pattern

    def __init__(self, bot: commands.Bot, db_path: str):
        self.bot = bot
        self.db_thread = DatabaseThread(db_path)
        self.monitored_sessions = dict()
        self.watched_channels = set(int(c) for c in os.environ["TOWNSQUARE_SPY_CHANNELS"].split(","))
        self.watch_re = re.compile(r'\bhttps?://clocktower\.(?:online|live)/#[A-Za-z0-9-_]+\b')

    def cog_unload(self):
        """
        Called if this cog is being unloaded.
        This cancels all ongoing monitoring.
        """
        for monitored in self.monitored_sessions:
            monitored.task.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: nextcord.Message):
        """
        Observes messages in text channels.
        If a message mentioning a townsquare link is found, it is monitored.
        """
        if message.channel.id not in self.watched_channels: return
        for url in self.watch_re.findall(message.content):
            if url in self.monitored_sessions:
                continue

            monitored = MonitoredSessionState()
            monitored.task = asyncio.create_task(
                monitor_session(monitored, url, db_thread=self.db_thread))
            self.monitored_sessions[url] = monitored
            def discard(url, task):
                monitored = self.monitored_sessions.get(url)
                if monitored and monitored.task == task:
                    del self.monitored_sessions[url]
            monitored.task.add_done_callback(functools.partial(discard, url))

    @nextcord.slash_command(description="Show the log of a particular game")
    async def spyshowlog(self, interaction: nextcord.Interaction, session_url: str, as_of: Optional[str]):
        if as_of is not None:
            as_of = dateparser.parse(as_of)
        if as_of is not None:
            as_of = as_of.astimezone(timezone.utc)
        latest = await self.db_thread.latest(session_url, as_of=as_of)
        if not latest:
            await interaction.send("No session log found.")
        else:
            data = BytesIO()
            text = TextIOWrapper(data, encoding="utf-8", newline="\n")
            for timestamp, message in latest:
                print(f"[{timestamp}] {strip_ansi(message)}", file=text)
            text.flush()
            data.seek(0)
            with nextcord.File(data, filename="session.txt", description="Session Log") as f:
                await interaction.send(file=f)

    @nextcord.slash_command(description="Dump the townsquare spy database")
    async def spydumpdb(self, interaction: nextcord.Interaction):
        dump = await self.db_thread.dump()
        with nextcord.File(dump, filename="townsquare.db.xz", description="Database Dump") as f:
            dm = await interaction.user.create_dm()
            await dm.send(file=f)
        dump.close()
        await interaction.send("Database dump sent via DM.")

    @nextcord.slash_command(description="Explain what's going on right now")
    async def spystatus(self, interaction: nextcord.Interaction):
        markdown_translate = str.maketrans({ c: "\\"+c for c in "\\`*_{}[]()<>#+-.!|~"})
        response = StringIO()
        print("Monitoring the following games:", file=response)
        for url, monitored in self.monitored_sessions.items():
            escaped_edition = monitored.session.edition_name.translate(markdown_translate)
            living_players = sum(1 for p in monitored.session.players if not p.is_dead)
            total_players = len(monitored.session.players)
            print(f"* {url} ({escaped_edition}, {living_players}/{total_players} alive)", file=response)
        await interaction.send(response.getvalue(), suppress_embeds=True)

def setup(bot, db_path=":memory:"):
    """
    Invoked as part of extension loading:
    https://docs.nextcord.dev/en/stable/ext/commands/extensions.html
    """
    bot.add_cog(TownsquareSpyCog(bot, db_path))