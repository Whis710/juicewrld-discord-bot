"""Global mutable state for the Juice WRLD Discord bot.

All in-memory dicts that track guild playback, user playlists, listening
stats, and SOTD config live here.  Persistence helpers (load/save JSON)
are co-located so any module can call them without importing bot.py.
"""

import json
import time
from typing import Any, Dict, List, Optional

from constants import (
    NOTHING_PLAYING,
    PLAYLISTS_FILE,
    STATS_FILE,
    SOTD_CONFIG_FILE,
)

# ── Per-guild state ──────────────────────────────────────────────────

# Whether radio mode is enabled for a guild.
guild_radio_enabled: Dict[int, bool] = {}

# On-demand playback queue.  Each entry has at least: title, path, stream_url.
guild_queue: Dict[int, List[Dict[str, Any]]] = {}

# Currently-playing metadata (title, path, metadata, message_id, …).
guild_now_playing: Dict[int, Dict[str, Any]] = {}

# Previously-played song per guild.
guild_previous_song: Dict[int, Dict[str, Any]] = {}

# Pre-fetched next radio song per guild.
guild_radio_next: Dict[int, Dict[str, Any]] = {}

# Timestamp of last voice activity per guild (for idle auto-leave).
guild_last_activity: Dict[int, float] = {}

# ── Per-user state ───────────────────────────────────────────────────

# { user_id: { playlist_name: [ track_dict, … ] } }
user_playlists: Dict[int, Dict[str, List[Dict[str, Any]]]] = {}

# { user_id: { "total_plays": int, "total_seconds": int,
#              "songs": { song_name: count }, "eras": { era_name: count } } }
user_listening_stats: Dict[int, Dict[str, Any]] = {}

# ── SOTD config ──────────────────────────────────────────────────────

# { guild_id_str: channel_id }
sotd_config: Dict[str, int] = {}


# ── Playlist helpers ─────────────────────────────────────────────────

def get_or_create_user_playlists(user_id: int) -> Dict[str, List[Dict[str, Any]]]:
    """Return the playlist mapping for a user, creating it if needed."""
    playlists = user_playlists.get(user_id)
    if playlists is None:
        playlists = {}
        user_playlists[user_id] = playlists
    return playlists


def _serialize_user_playlists_for_json() -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for uid, playlists in user_playlists.items():
        data[str(uid)] = playlists
    return data


def load_user_playlists_from_disk() -> None:
    """Load user playlists from disk into memory (best-effort)."""
    global user_playlists
    try:
        with open(PLAYLISTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, Exception):
        return
    if not isinstance(raw, dict):
        return
    loaded: Dict[int, Dict[str, List[Dict[str, Any]]]] = {}
    for uid_str, pls in raw.items():
        try:
            uid = int(uid_str)
        except (TypeError, ValueError):
            continue
        if isinstance(pls, dict):
            loaded[uid] = pls
    if loaded:
        user_playlists = loaded


def save_user_playlists_to_disk() -> None:
    """Persist user playlists to disk (best-effort)."""
    try:
        with open(PLAYLISTS_FILE, "w", encoding="utf-8") as f:
            json.dump(_serialize_user_playlists_for_json(), f, ensure_ascii=False)
    except Exception:
        return


# ── Listening stats helpers ──────────────────────────────────────────

def load_listening_stats_from_disk() -> None:
    """Load listening stats from disk into memory (best-effort)."""
    global user_listening_stats
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, Exception):
        return
    if not isinstance(raw, dict):
        return
    loaded: Dict[int, Dict[str, Any]] = {}
    for uid_str, data in raw.items():
        try:
            loaded[int(uid_str)] = data
        except (TypeError, ValueError):
            continue
    if loaded:
        user_listening_stats = loaded


def save_listening_stats_to_disk() -> None:
    """Persist listening stats to disk (best-effort)."""
    try:
        serialized = {str(uid): data for uid, data in user_listening_stats.items()}
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(serialized, f, ensure_ascii=False)
    except Exception:
        return


def record_listen(
    user_id: int,
    title: str,
    era_name: Optional[str],
    duration_seconds: Optional[int],
) -> None:
    """Record a song play for a user's listening stats."""
    stats = user_listening_stats.setdefault(user_id, {
        "total_plays": 0,
        "total_seconds": 0,
        "songs": {},
        "eras": {},
    })
    stats["total_plays"] = stats.get("total_plays", 0) + 1
    if duration_seconds and duration_seconds > 0:
        stats["total_seconds"] = stats.get("total_seconds", 0) + duration_seconds

    songs = stats.setdefault("songs", {})
    songs[title] = songs.get(title, 0) + 1

    if era_name and era_name.strip():
        eras = stats.setdefault("eras", {})
        eras[era_name] = eras.get(era_name, 0) + 1

    save_listening_stats_to_disk()


# ── SOTD config helpers ──────────────────────────────────────────────

def load_sotd_config() -> None:
    """Load SOTD channel config from disk."""
    global sotd_config
    try:
        with open(SOTD_CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, Exception):
        return
    if isinstance(raw, dict):
        sotd_config = raw


def save_sotd_config() -> None:
    """Persist SOTD channel config to disk."""
    try:
        with open(SOTD_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(sotd_config, f, ensure_ascii=False)
    except Exception:
        return


# ── Queue / activity helpers ─────────────────────────────────────────

def ensure_queue(guild_id: int) -> List[Dict[str, Any]]:
    """Get or create the playback queue for a guild."""
    queue = guild_queue.get(guild_id)
    if queue is None:
        queue = []
        guild_queue[guild_id] = queue
    return queue


def touch_activity(guild_id: int) -> None:
    """Update the last-activity timestamp for a guild."""
    guild_last_activity[guild_id] = time.time()


# ── Load persisted data on import ────────────────────────────────────

load_user_playlists_from_disk()
load_listening_stats_from_disk()
load_sotd_config()
