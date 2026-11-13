"""Bounded, incremental OOC history collection for one configured REAPER server.

Uses discord.py and Python's built-in sqlite3 only. No messages are saved live.
Call after the bot's existing member synchronization has finished. OOC content
qualification is deliberately disabled, matching the current testing policy.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord


def _read_state(db_path: Path, guild_id: int, keep: int):
    """Read a previous successful scan; a changed cap triggers a new baseline."""
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS ooc_scan_status (
                    guild_id INTEGER PRIMARY KEY,
                    keep_posts INTEGER NOT NULL,
                    checked_through TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS ooc_scan_checkpoints (
                    channel_id INTEGER PRIMARY KEY,
                    checked_through TEXT NOT NULL
                )
            """)
        status = connection.execute(
            "SELECT guild_id, keep_posts FROM ooc_scan_status"
        ).fetchall()
        if any(row[0] != guild_id for row in status):
            raise RuntimeError("Use a separate database for each server.")
        member_ids = {
            row[0] for row in connection.execute(
                "SELECT discord_id FROM members WHERE in_server = 1"
            )
        }
        if not status or status[0][1] != keep:
            return member_ids, {}, []
        checkpoints = dict(connection.execute(
            "SELECT channel_id, checked_through FROM ooc_scan_checkpoints"
        ).fetchall())
        records = connection.execute("""
            SELECT message_id, discord_id, channel_id, created_at
            FROM ooc_activity
        """).fetchall()
        return member_ids, checkpoints, records
    finally:
        connection.close()


async def _discover_sources(guild, category_ids, excluded_ids):
    """Discover histories, including active and archived threads of any age."""
    if not category_ids:
        raise RuntimeError("No OOC categories are configured.")
    for category_id in category_ids:
        if not isinstance(guild.get_channel(category_id), discord.CategoryChannel):
            raise RuntimeError(f"OOC category {category_id} was not found.")
    if guild.me is None:
        raise RuntimeError("REAPER's server membership is unavailable.")
    parents = {
        channel.id: channel for channel in guild.channels
        if isinstance(channel, (discord.TextChannel, discord.ForumChannel))
        and channel.category_id in category_ids
        and channel.id not in excluded_ids
    }
    if not parents:
        raise RuntimeError("No OOC text/forum channels were found.")
    sources = {}
    for parent in parents.values():
        permissions = parent.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.read_message_history):
            raise RuntimeError(f"Cannot read OOC channel {parent.id}.")
        if isinstance(parent, discord.TextChannel):
            sources[parent.id] = parent

    for thread in await guild.active_threads():
        if thread.parent_id in parents:
            sources[thread.id] = thread
    for parent in parents.values():
        async for thread in parent.archived_threads(limit=None):
            sources[thread.id] = thread
        if isinstance(parent, discord.TextChannel) and not parent.is_news():
            async for thread in parent.archived_threads(
                limit=None, private=True, joined=True
            ):
                sources[thread.id] = thread
    # Recheck for a thread unarchived during discovery.
    for thread in await guild.active_threads():
        if thread.parent_id in parents:
            sources[thread.id] = thread
    return sources


async def scan_ooc(
    guild: discord.Guild,
    db_path: str | Path,
    category_ids: set[int],
    excluded_channel_ids: set[int],
    keep_posts: int = 5,
) -> dict:
    """Collect new human OOC messages; commit at most keep_posts per member.

    On failure, raise to the caller. Do not advance checkpoints or publish a
    partial scan. Source permissions must be sufficient; invisible private
    threads cannot be discovered. Existing message edits/deletions remain a
    separate review/reconciliation responsibility.
    """
    if keep_posts < 1:
        raise ValueError("keep_posts must be at least 1.")
    db_path = Path(db_path)
    if not db_path.is_file():
        raise RuntimeError("Run setup_database() before the OOC scan.")

    # A stable upper bound: messages during this scan belong to the next scan.
    started = datetime.now(timezone.utc)
    started = started.replace(microsecond=(started.microsecond // 1000) * 1000)
    cutoff = started - timedelta(days=14)
    sources = await _discover_sources(guild, category_ids, excluded_channel_ids)
    member_ids, checkpoints, old_records = _read_state(db_path, guild.id, keep_posts)
    newest = {}

    def retain(record):
        message_id, member_id, channel_id, created_at = record
        bucket = newest.setdefault(member_id, {})
        bucket[message_id] = record
        if len(bucket) > keep_posts:
            del bucket[min(bucket)]

    for row in old_records:
        if (
            row[1] in member_ids
            and row[2] in sources
            and datetime.fromisoformat(row[3]) < started
        ):
            retain(row)

    processed = 0
    for source in sources.values():
        permissions = source.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.read_message_history):
            raise RuntimeError(f"Cannot read OOC history {source.id}.")
        after = cutoff
        if source.id in checkpoints:
            last_check = datetime.fromisoformat(checkpoints[source.id])
            if last_check > started:
                raise RuntimeError("OOC checkpoint is in the future; check the system clock.")
            # A small overlap avoids timestamp-boundary gaps; IDs deduplicate.
            after = max(cutoff, last_check - timedelta(seconds=1))
        print(f"OOC checking: {source.name} ({source.id})")
        async for message in source.history(
            limit=None,
            after=after - timedelta(milliseconds=1),
            before=started,
            oldest_first=False,
        ):
            if (
                message.author.bot
                or message.webhook_id is not None
                or message.is_system()
                or message.author.id not in member_ids
                or not (cutoff <= message.created_at < started)
            ):
                continue
            # Intentionally no OOC content-length check at this stage.
            retain((
                message.id,
                message.author.id,
                source.id,
                message.created_at.isoformat(),
            ))
            processed += 1

    # No database writes per message. Publish the small result in one transaction.
    records = [row for bucket in newest.values() for row in bucket.values()]
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            connection.execute("DELETE FROM ooc_activity")
            connection.executemany("""
                INSERT INTO ooc_activity
                    (message_id, discord_id, channel_id, created_at)
                VALUES (?, ?, ?, ?)
            """, records)
            connection.execute("DELETE FROM ooc_scan_checkpoints")
            connection.executemany("""
                INSERT INTO ooc_scan_checkpoints (channel_id, checked_through)
                VALUES (?, ?)
            """, [(source_id, started.isoformat()) for source_id in sources])
            connection.execute("""
                INSERT INTO ooc_scan_status (guild_id, keep_posts, checked_through)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    keep_posts = excluded.keep_posts,
                    checked_through = excluded.checked_through
            """, (guild.id, keep_posts, started.isoformat()))
    finally:
        connection.close()
    return {
        "checked_through": started,
        "histories": len(sources),
        "processed": processed,
        "stored": len(records),
    }
