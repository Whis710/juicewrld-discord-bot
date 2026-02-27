"""Shared constants for the Juice WRLD Discord bot."""

import os

# Sentinel title used when nothing is playing.  Every comparison in the
# codebase should use this constant instead of a raw string literal.
NOTHING_PLAYING = "Nothing playing"

# How long (seconds) of no playback before auto-leaving voice.
AUTO_LEAVE_IDLE_SECONDS = 30 * 60  # 30 minutes

# Persistent data file paths (next to this file on disk).
_HERE = os.path.dirname(os.path.abspath(__file__))
PLAYLISTS_FILE = os.path.join(_HERE, "playlists.json")
STATS_FILE = os.path.join(_HERE, "listening_stats.json")
SOTD_CONFIG_FILE = os.path.join(_HERE, "sotd_config.json")
HISTORY_FILE = os.path.join(_HERE, "history.json")

# Bot version info
BOT_VERSION = "3.5.2"
BOT_BUILD_DATE = "2026-02-26"

# Environment
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
JUICEWRLD_API_BASE_URL = os.getenv("JUICEWRLD_API_BASE_URL", "https://juicewrldapi.com")
GENIUS_API_TOKEN = os.getenv("GENIUS_API_TOKEN")  # Optional: for lyrics fallback
