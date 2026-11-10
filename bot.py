"""REAPER Discord activity bot."""

import os
import sqlite3
from datetime import datetime, timezone

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
            joined_at TEXT,
            last_seen_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    """)

    try:
        cursor.execute("""
                ALTER TABLE members
                ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1
            """)
    except sqlite3.OperationalError:
        pass

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
            joined_at,
            last_seen_at,
            is_active
        )
        VALUES (?, ?, ?, ?, 1)

        ON CONFLICT(discord_id) DO UPDATE SET
            username = excluded.username,
            joined_at = excluded.joined_at,
            last_seen_at = excluded.last_seen_at,
            is_active = 1
    """, (
        member.id,
        member.name,
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
        SET is_active = 0
    """)
    connection.commit()
    connection.close()

# --------------------------------------------------
# SHOW MEMBERS THAT HAVE LEFT
# --------------------------------------------------
def show_inactive_members():
    connection = sqlite3.connect("reaper.db")
    cursor = connection.cursor()
    cursor.execute("""
        SELECT discord_id, username
        FROM members
        WHERE is_active = 0
    """)

    inactive_members = cursor.fetchall()

    for discord_id, username in inactive_members:
        print(
            f"Not currently in server: "
            f"{discord_id} | {username}"
        )
    connection.close()

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
                    f"Joined server: {member.joined_at}"
                )

                save_member(member)
        show_inactive_members()
        #print(f"IC categories: {IC_CATEGORY_IDS}")
        #print(f"OOC categories: {OOC_CATEGORY_IDS}")
        #print(f"Character Sheet channel: {CS_CHANNEL_ID}")
        #print(f"Admin channel: {ADMIN_CHANNEL_ID}")

    else:
        print("No servers found.")

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
        print(
            f"IC | "
            f"{message.author.name} | "
            f"Channel: {message.channel.name}"
        )
        return

    # OOC category
    if message.channel.category_id in OOC_CATEGORY_IDS:
        print(
            f"OOC | "
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
