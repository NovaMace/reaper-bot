"""REAPER's hourly OOC history scanner.

Accepts category IDs and individual text/forum channel IDs in one list.
Retains at most keep_posts OOC records per current member.
OOC content qualification remains disabled for now.

The hourly schedule is controlled by bot.py, not this file.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord


# --------------------------------------------------
# READ PREVIOUS SCAN STATE
# --------------------------------------------------

def _read_state(db_path: Path, guild_id: int, keep: int):
    """Read saved checkpoints and existing OOC records."""

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

        status = connection.execute("""
            SELECT guild_id, keep_posts
            FROM ooc_scan_status
        """).fetchall()

        if any(row[0] != guild_id for row in status):
            raise RuntimeError(
                "This database contains OOC scan state for another server. "
                "Check the configured server and database path."
            )

        member_ids = {
            row[0]
            for row in connection.execute("""
                SELECT discord_id
                FROM members
                WHERE in_server = 1
            """)
        }

        # First scan, or the required number of posts has changed:
        # rebuild the baseline from the preceding 14 days.
        if not status or status[0][1] != keep:
            return member_ids, {}, []

        checkpoints = dict(
            connection.execute("""
                SELECT channel_id, checked_through
                FROM ooc_scan_checkpoints
            """).fetchall()
        )

        records = connection.execute("""
            SELECT
                message_id,
                discord_id,
                channel_id,
                created_at
            FROM ooc_activity
        """).fetchall()

        return member_ids, checkpoints, records

    finally:
        connection.close()


# --------------------------------------------------
# FIND CHANNELS AND THREADS TO SCAN
# --------------------------------------------------

async def _discover_sources(guild, category_ids, excluded_ids):
    """Accept category IDs and individual text/forum channel IDs."""

    if not category_ids:
        raise RuntimeError(
            "No OOC categories or channels are configured."
        )

    # Each configured ID can identify a category, text channel, or forum.
    for source_id in category_ids:
        source = guild.get_channel(source_id)

        if not isinstance(
            source,
            (
                discord.CategoryChannel,
                discord.TextChannel,
                discord.ForumChannel,
            ),
        ):
            raise RuntimeError(
                f"OOC ID {source_id} is not an available "
                "category, text channel, or forum."
            )

    if guild.me is None:
        raise RuntimeError(
            "REAPER's server membership is unavailable."
        )

    # Match either the channel's category OR the channel itself.
    # Using IDs as dictionary keys prevents duplicate scans.
    parents = {
        channel.id: channel
        for channel in guild.channels
        if isinstance(
            channel,
            (discord.TextChannel, discord.ForumChannel),
        )
        and (
            channel.category_id in category_ids
            or channel.id in category_ids
        )
        and channel.id not in excluded_ids
    }

    if not parents:
        raise RuntimeError(
            "No OOC text/forum channels were found."
        )

    sources = {}

    # Text channels have their own histories.
    # Forums are scanned through the threads/posts inside them.
    for parent in parents.values():
        permissions = parent.permissions_for(guild.me)

        if not (
            permissions.view_channel
            and permissions.read_message_history
        ):
            raise RuntimeError(
                f"Cannot read OOC channel "
                f"{parent.name} ({parent.id})."
            )

        if isinstance(parent, discord.TextChannel):
            sources[parent.id] = parent

    # Active threads under selected channels/forums.
    for thread in await guild.active_threads():
        if thread.parent_id in parents:
            sources[thread.id] = thread

    # Archived threads, regardless of their creation dates.
    for parent in parents.values():
        async for thread in parent.archived_threads(limit=None):
            sources[thread.id] = thread

        # Archived private threads that REAPER has joined.
        if (
            isinstance(parent, discord.TextChannel)
            and not parent.is_news()
        ):
            async for thread in parent.archived_threads(
                limit=None,
                private=True,
                joined=True,
            ):
                sources[thread.id] = thread

    # Catch threads unarchived during discovery.
    for thread in await guild.active_threads():
        if thread.parent_id in parents:
            sources[thread.id] = thread

    return sources


# --------------------------------------------------
# COLLECT AND SAVE OOC ACTIVITY
# --------------------------------------------------

async def scan_ooc(
    guild: discord.Guild,
    db_path: str | Path,
    category_ids: set[int],
    excluded_channel_ids: set[int],
    keep_posts: int = 5,
) -> dict:
    """Collect OOC history and retain a bounded number of records.

    On a history-collection failure, do not publish partial activity
    results or advance the saved checkpoints.

    OOC content qualification is intentionally disabled.
    Edit/deletion review remains separate from this scanner.
    """

    if keep_posts < 1:
        raise ValueError(
            "keep_posts must be at least 1."
        )

    db_path = Path(db_path)

    if not db_path.is_file():
        raise RuntimeError(
            "Run setup_database() before the OOC scan."
        )

    # Messages sent during this scan belong to the next scan.
    started = datetime.now(timezone.utc)
    started = started.replace(
        microsecond=(started.microsecond // 1000) * 1000
    )

    cutoff = started - timedelta(days=14)

    sources = await _discover_sources(
        guild,
        category_ids,
        excluded_channel_ids,
    )

    member_ids, checkpoints, old_records = _read_state(
        db_path,
        guild.id,
        keep_posts,
    )

    newest = {}

    def retain(record):
        """Keep only this member's newest records."""
        message_id, member_id, channel_id, created_at = record

        bucket = newest.setdefault(member_id, {})
        bucket[message_id] = record

        if len(bucket) > keep_posts:
            del bucket[min(bucket)]

    # Preserve existing retained records for current members
    # and channels that are still included in monitoring.
    # Report queries separately apply the rolling 14-day cutoff.
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

        if not (
            permissions.view_channel
            and permissions.read_message_history
        ):
            raise RuntimeError(
                f"Cannot read OOC history "
                f"{source.name} ({source.id})."
            )

        # A newly discovered channel/thread gets a 14-day baseline.
        after = cutoff

        if source.id in checkpoints:
            last_check = datetime.fromisoformat(
                checkpoints[source.id]
            )

            if last_check > started:
                raise RuntimeError(
                    "OOC checkpoint is in the future; "
                    "check the system clock."
                )

            # Small overlap avoids timestamp-boundary gaps.
            # Message IDs prevent duplicates.
            after = max(
                cutoff,
                last_check - timedelta(seconds=1),
            )

        print(
            f"OOC checking: {source.name} ({source.id})"
        )

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
                or not (
                    cutoff <= message.created_at < started
                )
            ):
                continue

            # No OOC character/content qualification at this stage.
            retain((
                message.id,
                message.author.id,
                source.id,
                message.created_at.isoformat(),
            ))

            processed += 1

    # Save only the bounded result, not every examined message.
    records = [
        row
        for bucket in newest.values()
        for row in bucket.values()
    ]

    connection = sqlite3.connect(db_path)

    try:
        with connection:
            connection.execute(
                "DELETE FROM ooc_activity"
            )

            # Recheck membership before writing.
            # A member may have left while histories were downloading.
            current_member_ids = {
                row[0]
                for row in connection.execute("""
                    SELECT discord_id
                    FROM members
                    WHERE in_server = 1
                """)
            }

            records = [
                row
                for row in records
                if row[1] in current_member_ids
            ]

            connection.executemany("""
                INSERT INTO ooc_activity (
                    message_id,
                    discord_id,
                    channel_id,
                    created_at
                )
                VALUES (?, ?, ?, ?)
            """, records)

            connection.execute(
                "DELETE FROM ooc_scan_checkpoints"
            )

            connection.executemany("""
                INSERT INTO ooc_scan_checkpoints (
                    channel_id,
                    checked_through
                )
                VALUES (?, ?)
            """, [
                (source_id, started.isoformat())
                for source_id in sources
            ])

            connection.execute("""
                INSERT INTO ooc_scan_status (
                    guild_id,
                    keep_posts,
                    checked_through
                )
                VALUES (?, ?, ?)

                ON CONFLICT(guild_id) DO UPDATE SET
                    keep_posts = excluded.keep_posts,
                    checked_through = excluded.checked_through
            """, (
                guild.id,
                keep_posts,
                started.isoformat(),
            ))

    finally:
        connection.close()

    return {
        "checked_through": started,
        "histories": len(sources),
        "processed": processed,
        "stored": len(records),
    }