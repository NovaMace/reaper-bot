"""REAPER Discord activity bot."""

import os
import sqlite3
from datetime import datetime, timezone, timedelta

import discord
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

# HARD CODED NUMBERS
OOC_REQUIRED_POSTS = 5

# --------------------------------------------------
# DISCORD INTENTS
# --------------------------------------------------

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

client = discord.Client(intents=intents)


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
    cursor = connection.cursor()

    cursor.execute("""
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
    """, (
        message.author.id,
        message.id,
        message.channel.id,
        message.created_at.isoformat()
    ))

    connection.commit()
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
    cutoff = datetime.now(timezone.utc) - timedelta(days=42)

    print("Starting initial IC scan...")

    for channel in guild.text_channels:
        if channel.category_id not in IC_CATEGORY_IDS:
            continue

        print(f"Scanning IC channel: {channel.name}")

        async for message in channel.history(
            after=cutoff,
            oldest_first=True
        ):
            if message.author.bot:
                continue

            if qualifies_ic_message(message.content):
                save_ic_activity(message)

    print("Initial IC scan complete.")


async def initial_scan_ooc(guild):
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)

    print("Starting initial OOC scan...")

    for channel in guild.text_channels:
        if channel.category_id not in OOC_CATEGORY_IDS:
            continue

        print(f"Scanning OOC channel: {channel.name}")

        async for message in channel.history(
            after=cutoff,
            oldest_first=True
        ):
            if message.author.bot:
                continue

            save_ooc_activity(message)

    print("Initial OOC scan complete.")


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
# DISCORD EVENTS
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
            await send_activity_report()

    print("REAPER is ready. Press Ctrl+C to stop.")


# --------------------------------------------------
# CLASSIFY MESSAGES
# --------------------------------------------------

@client.event
async def on_message(message):
    # Ignore REAPER's own messages and other bots
    if message.author.bot:
        return

    # Character Sheet channel
    if message.channel.id == CS_CHANNEL_ID:
        print(
            f"CHARACTER SHEET | "
            f"{message.author.name} | "
            f"Channel: {message.channel.name}"
        )
        return

    # IC category
    if message.channel.category_id in IC_CATEGORY_IDS:
        character_count = count_ic_characters(message.content)

        if qualifies_ic_message(message.content):

            save_ic_activity(message)

            print(
                f"IC QUALIFIES | "
                f"{message.author.name} | "
                f"Channel: {message.channel.name}"
            )
        else:
            print(
                f"TOO SHORT | "
                f"{message.author.name} | "
                f"Channel: {message.channel.name}"
            )
        return

    # OOC category
    if message.channel.category_id in OOC_CATEGORY_IDS:

        save_ooc_activity(message)

        print(
                f"OOC QUALIFIES | "
                f"{message.author.name} | "
                f"Channel: {message.channel.name}"
        )

        return

    # Everything else
    print(
        f"OTHER | "
        f"{message.author.name} | "
        f"Channel: {message.channel.name}"
    )




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
