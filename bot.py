"""Connection-only starter for the RP activity bot."""

import os
from pathlib import Path

import discord
from dotenv import load_dotenv


def main():
    # Load the local .env beside this file, regardless of the working directory.
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Missing DISCORD_BOT_TOKEN. Copy .env.example to .env, "
            "then put your bot token in the local .env file."
        )

    # This connection test only needs server information.
    intents = discord.Intents.none()
    intents.guilds = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"Connected as {client.user}.")
        if client.guilds:
            for guild in client.guilds:
                print(f"Server: {guild.name} (ID: {guild.id})")
        else:
            print("No servers found. Invite this bot to your test server first.")
        print("Connection test ready. Press Ctrl+C to stop.")

    try:
        client.run(token)
    except discord.LoginFailure:
        raise SystemExit(
            "Discord rejected the bot token. Check your local .env file. "
            "If the token was reset, replace its local value and run again."
        ) from None


if __name__ == "__main__":
    main()
