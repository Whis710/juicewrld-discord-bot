#!/usr/bin/env python3

"""Juice WRLD Discord bot — thin entry point.

All command logic lives in the ``commands/`` package (Cogs).
All UI views live in the ``views/`` package.
Shared state, helpers, and constants are in their respective modules.
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord
from discord.ext import commands

from constants import DISCORD_TOKEN
import helpers
import state

# ── Logging ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("juicewrld-bot")

_start_time: float = time.monotonic()
_connected_at: Optional[datetime] = None


def _uptime() -> str:
    """Return a human-readable uptime string."""
    elapsed = int(time.monotonic() - _start_time)
    parts = []
    days, rem = divmod(elapsed, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


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
    global _connected_at
    _connected_at = datetime.now(timezone.utc)
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("Connected to Discord at %s (uptime: %s)", _connected_at.strftime("%Y-%m-%d %H:%M:%S UTC"), _uptime())
    log.info("Guilds: %d | Latency: %.0fms", len(bot.guilds), bot.latency * 1000)

    # Slash commands are synced manually via `!jw sync` to avoid
    # hitting Discord rate limits on every restart.

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
        log.info("[linked_roles] DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET not set — skipping.")
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
        log.warning("[linked_roles] Missing dependency: %s — skipping.", e)
        return

    def _get_user_stats(user_id: int):
        return state.user_listening_stats.get(user_id)

    set_stats_callback(_get_user_stats)

    if DISCORD_TOKEN:
        ok = await register_metadata_schema(DISCORD_TOKEN)
        if ok:
            log.info("[linked_roles] Metadata schema registered.")

    config = uvicorn.Config(lr_app, host="0.0.0.0", port=LINKED_ROLES_PORT, log_level="warning")
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    log.info("[linked_roles] Web server started on port %s.", LINKED_ROLES_PORT)


# ── Main ──────────────────────────────────────────────────────────────

async def _load_extensions() -> None:
    """Load all Cog extensions."""
    state.load_all()
    for ext in EXTENSIONS:
        try:
            await bot.load_extension(ext)
            log.info("Loaded extension: %s", ext)
        except Exception as e:
            log.error("FAILED to load %s: %s", ext, e)


def main() -> None:
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN environment variable is not set.")
        sys.exit(1)

    log.info("Starting bot (PID %d)...", os.getpid())

    async def _runner():
        async with bot:
            await _load_extensions()
            try:
                await bot.start(DISCORD_TOKEN)
            finally:
                log.info("Shutting down (uptime: %s)...", _uptime())
                await helpers.close_api()

    asyncio.run(_runner())


if __name__ == "__main__":
    main()
