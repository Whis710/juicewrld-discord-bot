#!/usr/bin/env python3

"""Juice WRLD Discord bot — thin entry point.

All command logic lives in the ``commands/`` package (Cogs).
All UI views live in the ``views/`` package.
Shared state, helpers, and constants are in their respective modules.
"""

import asyncio
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord
from discord.ext import commands

import helpers
import state

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# ── Bot instance ──────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!jw ", intents=intents, help_command=None)

# Extensions to load at startup.
EXTENSIONS = [
    "commands.playback",
    "commands.search",
    "commands.playlists",
    "commands.admin",
    "commands.slash",
]


# ── Events ────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")

    # Sync slash commands.
    try:
        bot.tree.clear_commands(guild=None)
        for guild in bot.guilds:
            bot.tree.clear_commands(guild=guild)
        for guild in bot.guilds:
            await bot.tree.sync(guild=guild)
        await bot.tree.sync()
        print("Cleared and synced application commands.")
    except Exception as e:
        print(f"Failed to sync application commands: {e}", file=sys.stderr)

    # Start linked roles web server (if configured).
    await _start_linked_roles_server()


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    """Handle errors for commands."""

    from discord.ext import commands as _commands_mod

    if isinstance(error, _commands_mod.CommandNotFound):
        try:
            if ctx.message:
                asyncio.create_task(helpers.delete_later(ctx.message, 5))
        except Exception:
            pass
        content = f"Command `{ctx.message.content}` is not found."
        await helpers.send_temporary(ctx, content, delay=5)
        return

    raise error


@bot.before_invoke
async def _delete_user_command(ctx: commands.Context) -> None:
    """Delete the user's command message after a short delay."""
    try:
        msg = ctx.message
        if not msg:
            return
        cmd = getattr(ctx, "command", None)
        delay = 5
        if cmd and getattr(cmd, "name", None) == "stop":
            delay = 1
        asyncio.create_task(helpers.delete_later(msg, delay))
    except Exception:
        return


# ── Linked Roles ──────────────────────────────────────────────────────

async def _start_linked_roles_server() -> None:
    """Start the linked roles FastAPI server if credentials are configured."""
    client_id = os.getenv("CLIENT_ID", "")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("[linked_roles] DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET not set — skipping.")
        return

    try:
        from linked_roles import (
            app as lr_app,
            set_stats_callback,
            register_metadata_schema,
            LINKED_ROLES_PORT,
        )
        import uvicorn
    except ImportError as e:
        print(f"[linked_roles] Missing dependency: {e} — skipping.")
        return

    def _get_user_stats(user_id: int):
        return state.user_listening_stats.get(user_id)

    set_stats_callback(_get_user_stats)

    if DISCORD_TOKEN:
        ok = await register_metadata_schema(DISCORD_TOKEN)
        if ok:
            print("[linked_roles] Metadata schema registered.")

    config = uvicorn.Config(lr_app, host="0.0.0.0", port=LINKED_ROLES_PORT, log_level="warning")
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    print(f"[linked_roles] Web server started on port {LINKED_ROLES_PORT}.")


# ── Main ──────────────────────────────────────────────────────────────

async def _load_extensions() -> None:
    """Load all Cog extensions."""
    for ext in EXTENSIONS:
        try:
            await bot.load_extension(ext)
            print(f"  Loaded extension: {ext}")
        except Exception as e:
            print(f"  FAILED to load {ext}: {e}", file=sys.stderr)


def main() -> None:
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN environment variable is not set.")
        sys.exit(1)

    async def _runner():
        async with bot:
            await _load_extensions()
            await bot.start(DISCORD_TOKEN)

    asyncio.run(_runner())


if __name__ == "__main__":
    main()
