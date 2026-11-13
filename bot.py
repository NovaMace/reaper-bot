"""REAPER Discord activity bot."""

import os
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands

from dotenv import load_dotenv

# --------------------------------------------------
# LOAD CONFIG
# --------------------------------------------------

load_dotenv()

token = os.getenv("DISCORD_TOKEN")

if not token:
    raise SystemExit(
        "Missing DISCORD_BOT_TOKEN. "
        "Check your local .env file."
    )

# --------------------------------------------------
# VARIABLES
# --------------------------------------------------
CS_CHANNEL_ID = int(os.getenv("CS_CHANNEL_ID"))
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID"))
IC_CATEGORY_IDS = {
    int(value.strip())
    for value in os.getenv("IC_CATEGORY_IDS", "").split(",")
    if value.strip()
}

OOC_CATEGORY_IDS = {
    int(value.strip())
    for value in os.getenv("OOC_CATEGORY_IDS", "").split(",")
    if value.strip()
}
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID"))

ADMIN_ROLE_NAME = os.getenv("ADMIN_ROLE_NAME", "").strip()

if not ADMIN_ROLE_NAME:
    raise SystemExit(
        "Set ADMIN_ROLE_NAME in your .env file, for example: admin."
    )


# HARD CODED NUMBERS
OOC_REQUIRED_POSTS = 5

ACTIVITY_SCAN_OK = {"IC": False, "OOC": False}
ACTIVITY_SCAN_LOCK = asyncio.Lock()

# --------------------------------------------------
# DISCORD INTENTS
# --------------------------------------------------

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# --------------------------------------------------
# GET ADMIN ROLE SO ONLY ADMIN CAN USE SLASH COMMANDS
# --------------------------------------------------
def is_approved_admin(interaction: discord.Interaction) -> bool:
    # Only accept requests from our configured server.
    if interaction.guild_id != TEST_GUILD_ID:
        return False

    # Ignore requests that do not come from a server member.
    if not isinstance(interaction.user, discord.Member):
        return False

    # Check whether any of this member's roles matches the name.
    return any(
        role.name.casefold() == ADMIN_ROLE_NAME.casefold()
        for role in interaction.user.roles
    )

# --------------------------------------------------
# DATABASE SETUP
# --------------------------------------------------

def setup_database():
    connection = sqlite3.connect("reaper.db")
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS members (
            discord_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            display_name TEXT NOT NULL,
            joined_at TEXT,
            last_seen_at TEXT NOT NULL,
            in_server INTEGER NOT NULL DEFAULT 1,
            sheet_checked INTEGER NOT NULL DEFAULT 0,
            sheet_found INTEGER NOT NULL DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ic_activity (
            discord_id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ooc_activity (
            message_id INTEGER PRIMARY KEY,
            discord_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    connection.commit()
    connection.close()


# --------------------------------------------------
# SAVE MEMBER
# --------------------------------------------------

def save_member(member):
    connection = sqlite3.connect("reaper.db")
    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO members (
            discord_id,
            username,
            display_name,
            joined_at,
            last_seen_at,
            in_server
        )
        VALUES (?, ?, ?, ?, ?, 1)

        ON CONFLICT(discord_id) DO UPDATE SET
            username = excluded.username,
            display_name = excluded.display_name,
            joined_at = excluded.joined_at,
            last_seen_at = excluded.last_seen_at,
            in_server = 1
    """, (
        member.id,
        member.name,
        member.display_name,
        member.joined_at.isoformat() if member.joined_at else None,
        datetime.now(timezone.utc).isoformat()
    ))

    connection.commit()
    connection.close()

# --------------------------------------------------
# RESET MEMBER ACTIVITY
# --------------------------------------------------
def mark_all_members_inactive():
    connection = sqlite3.connect("reaper.db")
    cursor = connection.cursor()

    cursor.execute("""
        UPDATE members
        SET in_server = 0
    """)
    connection.commit()
    connection.close()

# --------------------------------------------------
# CLASSIFY MEMBERS
# --------------------------------------------------

def classify_member(discord_id):
    member_data = get_member_data(discord_id)

    if member_data is None:
        return None, None

    joined_at_text, sheet_found, in_server = member_data

    if not in_server:
        return None, None

    joined_at = datetime.fromisoformat(joined_at_text)

    now = datetime.now(timezone.utc)
    days_in_server = (now - joined_at).total_seconds() / 86400

    # New member grace period
    if days_in_server < 7:
        return None, None

    # Character sheet check
    if not sheet_found:
        return "PURGE", "No character sheet found after 7-day grace period."

    # IC activity
    ic_age = get_ic_age_days(discord_id)

    if ic_age is not None and ic_age < 14:
        return None, None

    # OOC activity
    ooc_count = count_recent_ooc_posts(discord_id)

    if ooc_count < OOC_REQUIRED_POSTS:
        if ic_age is None:
            reason = (
                f"No qualifying IC activity found; "
                f"{ooc_count} OOC posts in the last 14 days."
            )
        else:
            reason = (
                f"Last qualifying IC activity was {ic_age:.1f} days ago; "
                f"{ooc_count} OOC posts in the last 14 days."
            )

        return "SORT", reason

    # Extended IC window
    if ic_age is None:
        return (
            "PURGE",
            f"No qualifying IC activity found; "
            f"OOC requirement met with {ooc_count} posts."
        )

    if ic_age < 42:
        return (
            "WATCH",
            f"Last qualifying IC activity was {ic_age:.1f} days ago; "
            f"OOC requirement met with {ooc_count} posts."
        )

    return (
        "PURGE",
        f"Last qualifying IC activity was {ic_age:.1f} days ago; "
        f"42-day extended allowance exceeded."
    )

# --------------------------------------------------
# SHOW MEMBERS THAT HAVE LEFT
# --------------------------------------------------
def show_inactive_members():
    connection = sqlite3.connect("reaper.db")
    cursor = connection.cursor()
    cursor.execute("""
        SELECT discord_id, username
        FROM members
        WHERE in_server = 0
    """)

    inactive_members = cursor.fetchall()

    for discord_id, username in inactive_members:
        print(
            f"Not currently in server: "
            f"{discord_id} | {username}"
        )
    connection.close()


# --------------------------------------------------
# Scanner Helper Function
# --------------------------------------------------
def get_activity_kind(channel):
    # Threads use their parent text/forum channel's category.
    parent = (
        channel.parent
        if isinstance(channel, discord.Thread)
        else channel
    )

    if not isinstance(
        parent,
        (discord.TextChannel, discord.ForumChannel)
    ):
        return None

    # Sheets and admin reports must not count as IC/OOC activity.
    if parent.id in (CS_CHANNEL_ID, ADMIN_CHANNEL_ID):
        return None

    if parent.category_id in IC_CATEGORY_IDS:
        return "IC"

    if parent.category_id in OOC_CATEGORY_IDS:
        return "OOC"

    return None


# --------------------------------------------------
# IC CHARACTER COUNT
# --------------------------------------------------
def count_ic_characters(content):
    if not content:
        return 0

    # Remove whitespaces
    content = "".join(content.split())

    # Remove common Discord markdown formatting markers
    formatting_markers = ["*", "_", "~", "`", ">"]
    for marker in formatting_markers:
        content = content.replace(marker, "")

    return len(content)

# --------------------------------------------------
# QUALIFYING MESSAGE COUNT
# --------------------------------------------------
def qualifies_ic_message(content):
    return count_ic_characters(content) >= 10

# --------------------------------------------------
# SAVE QUALIFYING IC MESSAGE
# --------------------------------------------------

def save_ic_activity(message):
    connection = sqlite3.connect("reaper.db")

    try:
        connection.execute("""
            INSERT INTO ic_activity (
                discord_id,
                message_id,
                channel_id,
                created_at
            )
            VALUES (?, ?, ?, ?)

            ON CONFLICT(discord_id) DO UPDATE SET
                message_id = excluded.message_id,
                channel_id = excluded.channel_id,
                created_at = excluded.created_at

            WHERE excluded.message_id > ic_activity.message_id
        """, (
            message.author.id,
            message.id,
            message.channel.id,
            message.created_at.isoformat()
        ))

        connection.commit()

    finally:
        connection.close()

# --------------------------------------------------
# GET IC MESSAGE AGE
# --------------------------------------------------
def get_ic_age_days(discord_id):
    connection = sqlite3.connect("reaper.db")
    cursor = connection.cursor()

    cursor.execute("""
        SELECT created_at
        FROM ic_activity
        WHERE discord_id = ?
    """, (discord_id,))

    row = cursor.fetchone()
    connection.close()

    if row is None:
        return None

    created_at = datetime.fromisoformat(row[0])
    now = datetime.now(timezone.utc)

    age = now - created_at

    return age.total_seconds() / 86400

# --------------------------------------------------
# SAVES OOC MESSAGE
# --------------------------------------------------
def save_ooc_activity(message):
    connection = sqlite3.connect("reaper.db")
    cursor = connection.cursor()

    cursor.execute("""
        INSERT OR IGNORE INTO ooc_activity (
            message_id,
            discord_id,
            channel_id,
            created_at
        )
        VALUES (?, ?, ?, ?)
    """, (
        message.id,
        message.author.id,
        message.channel.id,
        message.created_at.isoformat()
    ))

    connection.commit()
    connection.close()

# --------------------------------------------------
# COUNTS OOC MESSAGES IN A GIVEN TIME PERIOD
# --------------------------------------------------

def count_recent_ooc_posts(discord_id):
    connection = sqlite3.connect("reaper.db")
    cursor = connection.cursor()

    cutoff = datetime.now(timezone.utc) - timedelta(days=14)

    cursor.execute("""
        SELECT COUNT(*)
        FROM ooc_activity
        WHERE discord_id = ?
        AND created_at >= ?
    """, (
        discord_id,
        cutoff.isoformat()
    ))

    count = cursor.fetchone()[0]

    connection.close()

    return count

# --------------------------------------------------
# FIND CHARACTER SHEET AND MARK IT FOUND
# --------------------------------------------------

def mark_sheet_found(discord_id):
    connection = sqlite3.connect("reaper.db")
    cursor = connection.cursor()

    cursor.execute("""
        UPDATE members
        SET sheet_checked = 1,
            sheet_found = 1
        WHERE discord_id = ?
    """, (discord_id,))

    connection.commit()
    connection.close()

# --------------------------------------------------
# GETTING MEMBER DATA
# --------------------------------------------------

def get_member_data(discord_id):
    connection = sqlite3.connect("reaper.db")
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            joined_at,
            sheet_found,
            in_server
        FROM members
        WHERE discord_id = ?
    """, (discord_id,))

    row = cursor.fetchone()

    connection.close()

    return row

# --------------------------------------------------
# BUILDS THE ACTIVITY LIST IN MEMORY
# -------------------------------------------------

def build_activity_lists():
    connection = sqlite3.connect("reaper.db")
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            discord_id,
            username,
            display_name,
            joined_at
        FROM members
        WHERE in_server = 1
    """)

    members = cursor.fetchall()
    connection.close()

    purge_list = []
    sort_list = []
    watch_list = []

    for discord_id, username, display_name, joined_at in members:
        classification, reason = classify_member(discord_id)

        if classification is None:
            continue

        entry = {
            "discord_id": discord_id,
            "username": username,
            "display_name": display_name,
            "joined_at": joined_at,
            "reason": reason
        }

        if classification == "PURGE":
            purge_list.append(entry)

        elif classification == "SORT":
            sort_list.append(entry)

        elif classification == "WATCH":
            watch_list.append(entry)

    return purge_list, sort_list, watch_list

# --------------------------------------------------
# BUILDS AND SENDS THE ACTIVITY REPORT TO ADMIN CHANNEL
# --------------------------------------------------
def format_activity_report():
    purge_list, sort_list, watch_list = build_activity_lists()

    lines = []

    lines.append("=== REAPER ROLE CALL ===")
    lines.append("")

    lines.append("PURGE")
    if purge_list:
        for member in purge_list:
            lines.append(
                f"- {member['display_name']} | {member['reason']}"
            )
    else:
        lines.append("- None")

    lines.append("")

    lines.append("SORT")
    if sort_list:
        for member in sort_list:
            lines.append(
                f"- {member['display_name']} | {member['reason']}"
            )
    else:
        lines.append("- None")

    lines.append("")

    lines.append("WATCH")
    if watch_list:
        for member in watch_list:
            lines.append(
                f"- {member['display_name']} | {member['reason']}"
            )
    else:
        lines.append("- None")

    return "\n".join(lines)

# --------------------------------------------------
# INITIAL SCANS
# --------------------------------------------------
async def initial_scan_ic(guild):
    return await scan_activity_history(
        guild,
        kind="IC",
        days=42
    )


async def initial_scan_ooc(guild):
    return await scan_activity_history(
        guild,
        kind="OOC",
        days=14
    )


async def initial_scan_character_sheets(guild):
    channel = guild.get_channel(CS_CHANNEL_ID)

    if channel is None:
        print("Character Sheet channel not found.")
        return

    print("Starting Character Sheet scan...")

    # Normal text channel
    if isinstance(channel, discord.TextChannel):
        async for message in channel.history(limit=None):
            if message.author.bot:
                continue

            mark_sheet_found(message.author.id)

    # Forum channel
    elif isinstance(channel, discord.ForumChannel):

        # Active/open forum posts
        for thread in channel.threads:
            owner_id = thread.owner_id

            if owner_id is not None:
                mark_sheet_found(owner_id)

        # Archived forum posts
        async for thread in channel.archived_threads(limit=None):
            owner_id = thread.owner_id

            if owner_id is not None:
                mark_sheet_found(owner_id)

    else:
        print(
            f"Unsupported Character Sheet channel type: "
            f"{type(channel).__name__}"
        )

    print("Character Sheet scan complete.")


# --------------------------------------------------
# THIS SENDS THE REPORT TO ADMIN CHANNEL
# --------------------------------------------------
async def send_activity_report():
    channel = client.get_channel(ADMIN_CHANNEL_ID)

    if channel is None:
        print("Admin report channel not found.")
        return

    report = format_activity_report()

    await channel.send(report)

    print("Activity report sent.")


# TEMPORARY PRINT CLASSIFICATIONS
def show_member_classifications():
    connection = sqlite3.connect("reaper.db")
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            discord_id,
            display_name
        FROM members
        WHERE in_server = 1
    """)

    members = cursor.fetchall()

    connection.close()

    print("\n--- ACTIVITY CLASSIFICATION ---")

    for discord_id, display_name in members:
        classification, reason = classify_member(discord_id)

        if classification is not None:
            print(
                f"{classification} | "
                f"{display_name} | "
                f"{reason}"
            )

    print("-------------------------------\n")

def show_activity_lists():
    purge_list, sort_list, watch_list = build_activity_lists()

    print("\n========== PURGE ==========")

    for member in purge_list:
        print(
            f"{member['display_name']} | "
            f"{member['reason']}"
        )

    print("\n========== SORT ==========")

    for member in sort_list:
        print(
            f"{member['display_name']} | "
            f"{member['reason']}"
        )

    print("\n========== WATCH ==========")

    for member in watch_list:
        print(
            f"{member['display_name']} | "
            f"{member['reason']}"
        )

    print("===========================\n")


# --------------------------------------------------
# HANDLES BOTH IC AND OOC HISTORIES
# --------------------------------------------------
async def scan_activity_history(guild, kind, days):
    async with ACTIVITY_SCAN_LOCK:
        ACTIVITY_SCAN_OK[kind] = False
        location = "scan setup"

        scan_started = datetime.now(timezone.utc)
        cutoff = scan_started - timedelta(days=days)

        try:
            # Check that we are scanning the configured server.
            if guild.id != TEST_GUILD_ID:
                raise RuntimeError(
                    "Scan requested for the wrong server."
                )

            if guild.me is None:
                raise RuntimeError(
                    "REAPER's server membership is unavailable."
                )

            if IC_CATEGORY_IDS & OOC_CATEGORY_IDS:
                raise RuntimeError(
                    "A category is configured as both IC and OOC."
                )

            category_ids = (
                IC_CATEGORY_IDS
                if kind == "IC"
                else OOC_CATEGORY_IDS
            )

            if not category_ids:
                raise RuntimeError(
                    f"No {kind} categories configured."
                )

            for category_id in category_ids:
                category = guild.get_channel(category_id)

                if not isinstance(category, discord.CategoryChannel):
                    raise RuntimeError(
                        f"Category {category_id} was not found."
                    )

            # Find the relevant text channels and forum containers.
            parents = {
                channel.id: channel
                for channel in guild.channels
                if get_activity_kind(channel) == kind
            }

            if not parents:
                raise RuntimeError(
                    f"No {kind} text/forum channels found."
                )

            # A source is a place with an actual message history.
            sources = {}

            for parent in parents.values():
                location = f"{parent.name} ({parent.id})"
                permissions = parent.permissions_for(guild.me)

                if not (
                    permissions.view_channel
                    and permissions.read_message_history
                ):
                    raise RuntimeError(
                        "Missing View Channel or Read Message History."
                    )

                # Normal text channels have their own history.
                # Forum containers do not.
                if isinstance(parent, discord.TextChannel):
                    sources[parent.id] = parent

            # Find accessible active threads, regardless of their age.
            location = "active thread discovery"

            for thread in await guild.active_threads():
                if thread.parent_id in parents:
                    sources[thread.id] = thread

            # Find archived threads and forum posts.
            # Do NOT filter these by their creation date.
            for parent in parents.values():
                location = (
                    f"archived threads in {parent.name} ({parent.id})"
                )

                print(
                    f"{kind}: discovering threads in {parent.name}"
                )

                async for thread in parent.archived_threads(
                    limit=None
                ):
                    sources[thread.id] = thread

                # Also include archived private threads REAPER joined.
                # Private threads belong to normal text channels.
                if (
                    isinstance(parent, discord.TextChannel)
                    and not parent.is_news()
                ):
                    async for thread in parent.archived_threads(
                        limit=None,
                        private=True,
                        joined=True
                    ):
                        sources[thread.id] = thread

            # Catch threads unarchived while discovery was running.
            location = "active thread recheck"

            for thread in await guild.active_threads():
                if thread.parent_id in parents:
                    sources[thread.id] = thread

            # Read recent messages from each discovered source.
            processed = 0

            for source in sources.values():
                location = f"{source.name} ({source.id})"
                permissions = source.permissions_for(guild.me)

                if not (
                    permissions.view_channel
                    and permissions.read_message_history
                ):
                    raise RuntimeError(
                        "Cannot read this message history."
                    )

                print(f"Scanning {kind}: {location}")

                async for message in source.history(
                    limit=None,
                    after=cutoff,
                    before=scan_started,
                    oldest_first=True
                ):
                    if (
                        message.author.bot
                        or message.webhook_id is not None
                    ):
                        continue

                    # Ignore Discord system notices.
                    if message.is_system():
                        continue

                    if kind == "IC":
                        if not qualifies_ic_message(message.content):
                            continue

                        save_ic_activity(message)

                    else:
                        # OOC remains unqualified logging for now.
                        save_ooc_activity(message)

                    processed += 1

            ACTIVITY_SCAN_OK[kind] = True

            print(
                f"{kind} scan complete: "
                f"{len(sources)} histories checked; "
                f"{processed} messages processed."
            )

            return True

        except (
            discord.HTTPException,
            discord.ClientException,
            sqlite3.Error,
            RuntimeError
        ) as error:
            print(
                f"{kind} SCAN INCOMPLETE at {location}: {error}"
            )

            return False

# --------------------------------------------------
# DISCORD EVENTS
# --------------------------------------------------

# --------------------------------------------------
# CLASSIFY MESSAGES
# --------------------------------------------------

@client.event
@client.event
async def on_message(message):
    # Ignore direct messages and other servers.
    if message.guild is None:
        return

    if message.guild.id != TEST_GUILD_ID:
        return

    if (
        message.author.bot
        or message.webhook_id is not None
        or message.is_system()
    ):
        return

    parent = (
        message.channel.parent
        if isinstance(message.channel, discord.Thread)
        else message.channel
    )

    if parent is None:
        print(
            f"Cannot classify message {message.id}: "
            f"thread parent unavailable."
        )
        return

    # Keep character-sheet discussions separate.
    if parent.id == CS_CHANNEL_ID:
        print(
            f"CHARACTER SHEET | "
            f"{message.author.name} | "
            f"Channel: {message.channel.name}"
        )
        return

    kind = get_activity_kind(message.channel)

    if kind == "IC":
        if qualifies_ic_message(message.content):
            save_ic_activity(message)

            print(
                f"IC LOGGED | "
                f"{message.author.name} | "
                f"Channel/thread: {message.channel.name}"
            )

        else:
            print(
                f"IC TOO SHORT | "
                f"{message.author.name} | "
                f"Channel/thread: {message.channel.name}"
            )

    elif kind == "OOC":
        save_ooc_activity(message)

        print(
            f"OOC LOGGED | "
            f"{message.author.name} | "
            f"Channel/thread: {message.channel.name}"
        )

# --------------------------------------------------
# SPLIT LARGE MESSAGES FOR 2000 CHAR LIMIT
# --------------------------------------------------
def split_report(report: str, limit: int = 1900) -> list[str]:
    if not 2 <= limit <= 1900:
        raise ValueError(
            "The report chunk limit must be between 2 and 1900."
        )

    chunks = []
    current = ""
    current_size = 0

    for line in report.splitlines(keepends=True):
        line_size = len(line.encode("utf-16-le")) // 2

        # Start a new part if adding this line would exceed the limit.
        if current and current_size + line_size > limit:
            if current.strip():
                chunks.append(current)

            current = ""
            current_size = 0

        # This also handles an unusually long individual line.
        for character in line:
            character_size = len(character.encode("utf-16-le")) // 2

            if current_size + character_size > limit:
                if current.strip():
                    chunks.append(current)

                current = ""
                current_size = 0

            current += character
            current_size += character_size

    # Keep the final part.
    if current.strip():
        chunks.append(current)

    return chunks


# --------------------------------------------------
# SPLIT LARGE MESSAGES FOR 2000 CHAR LIMIT
# --------------------------------------------------
async def send_activity_report():
    if not all(ACTIVITY_SCAN_OK.values()):
        raise RuntimeError(
            "The IC/OOC baseline is incomplete or still scanning. "
            "Check the startup scan messages before requesting a report."
        )

    channel = client.get_channel(ADMIN_CHANNEL_ID)

    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError(
            "ADMIN_CHANNEL_ID must point to an available text channel."
        )

    if channel.guild.id != TEST_GUILD_ID:
        raise RuntimeError(
            "The admin channel is not in TEST_GUILD_ID."
        )

    report = format_activity_report()
    parts = split_report(report)

    if not parts:
        raise RuntimeError("The activity report is empty.")

    total_parts = len(parts)
    sent = 0

    try:
        for part_number, part in enumerate(parts, start=1):
            await channel.send(
                f"REAPER report — part {part_number}/{total_parts}\n\n"
                f"{part}",
                allowed_mentions=discord.AllowedMentions.none()
            )

            sent += 1

    except discord.HTTPException as error:
        raise RuntimeError(
            f"Report incomplete: {sent}/{total_parts} parts sent. "
            f"Check the admin channel before retrying. {error}"
        ) from error

    print(f"Activity report sent in {sent} message(s).")

# --------------------------------------------------
# SLASH COMMANDS
# --------------------------------------------------

@tree.command(
    name="activity_report",
    description="Generate the current REAPER activity report",
    guild=discord.Object(id=TEST_GUILD_ID)
)
async def activity_report(interaction: discord.Interaction):
    # Check authorization before building or posting anything.
    if not is_approved_admin(interaction):
        await interaction.response.send_message(
            "You are not authorized to use REAPER commands here.",
            ephemeral=True
        )
        return

    # Acknowledge the request before generating the report.
    await interaction.response.defer(
        ephemeral=True,
        thinking=True
    )

    try:
        await send_activity_report()

    except (discord.HTTPException, sqlite3.Error, RuntimeError) as error:
        print(
            f"Activity report failed: "
            f"{type(error).__name__}: {error}"
        )

        await interaction.edit_original_response(
            content=(
                "REAPER could not send the report. "
                "Check the terminal for the error."
            )
        )
        return

    await interaction.edit_original_response(
        content=(
            f"REAPER activity report posted "
        )
    )


# --------------------------------------------------
# ON READY EVENT
# --------------------------------------------------
@client.event
async def on_ready():
    print(f"Connected as {client.user}.")

    setup_database()
    mark_all_members_inactive()

    if client.guilds:

        for guild in client.guilds:

            print(f"Server: {guild.name} (ID: {guild.id})")
            print(f"Members found: {guild.member_count}")

            for member in guild.members:
                if member.bot:
                    continue

                print(
                    f"{member.name} | "
                    f"ID: {member.id} | "
                )

                save_member(member)
        show_inactive_members()
        #print(f"IC categories: {IC_CATEGORY_IDS}")
        #print(f"OOC categories: {OOC_CATEGORY_IDS}")
        #print(f"Character Sheet channel: {CS_CHANNEL_ID}")
        #print(f"Admin channel: {ADMIN_CHANNEL_ID}")

    else:
        print("No servers found.")

     # This is a temp manual scan comment out once finished
    for guild in client.guilds:
            await initial_scan_ic(guild)
            await initial_scan_ooc(guild)
            await initial_scan_character_sheets(guild)

            #show_member_classifications()
            #show_activity_lists()
            #await send_activity_report()

            await tree.sync(
                guild=discord.Object(id=TEST_GUILD_ID)
            )


    print("REAPER is ready. Press Ctrl+C to stop.")



# --------------------------------------------------
# STARTS THE BOT MUST BE LAST
# --------------------------------------------------

try:
    client.run(token)

except discord.LoginFailure:
    raise SystemExit(
        "Discord rejected the bot token. "
        "Check your local .env file."
    ) from None
