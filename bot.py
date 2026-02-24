#!/usr/bin/env python3

"""Juice WRLD Discord bot.

This script runs a real Discord bot (no "examples" output) that lets users
search for songs using the JuiceWRLD API.
"""

import os
import sys
import asyncio
import random
import math
import time
import json
import io
import base64
from typing import Optional, Dict, Any, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from client import JuiceWRLDAPI
from exceptions import JuiceWRLDAPIError, NotFoundError

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
JUICEWRLD_API_BASE_URL = os.getenv("JUICEWRLD_API_BASE_URL", "https://juicewrldapi.com")


intents = discord.Intents.default()
intents.message_content = True

# Use "!jw " so commands are invoked like `!jw ping`, `!jw search`, etc.
bot = commands.Bot(command_prefix="!jw ", intents=intents, help_command=None)

# Slash command group for /jw ... equivalents of core commands.
jw_group = app_commands.Group(name="jw", description="Juice WRLD bot commands")

# Simple per-guild flag to track whether radio mode is enabled
_guild_radio_enabled: dict[int, bool] = {}

# Per-guild playback queue for on-demand tracks (search/comp/play/etc.).
# Each entry is a dict with at least: title, path, stream_url.
_guild_queue: Dict[int, List[Dict[str, Any]]] = {}

# Per-guild tracking of what is currently playing so the UI controls can
# show "now playing" information and operate on the right voice client.
_guild_now_playing: Dict[int, Dict[str, Any]] = {}

# Per-guild tracking of the previously played song
_guild_previous_song: Dict[int, Dict[str, Any]] = {}

# Per-guild pre-fetched next radio song (for showing "Up Next" in radio mode)
_guild_radio_next: Dict[int, Dict[str, Any]] = {}

# Per-guild timestamp of last voice activity (play, queue, radio, etc.)
# Used by the idle auto-leave task.
_guild_last_activity: Dict[int, float] = {}

# How long (seconds) of no playback before auto-leaving voice.
AUTO_LEAVE_IDLE_SECONDS = 30 * 60  # 30 minutes

# Per-user playlists:
# For now this is kept in memory only; a default "Likes" playlist is used
# by the Now Playing "Like" button.
_user_playlists: Dict[int, Dict[str, List[Dict[str, Any]]]] = {}


def create_api_client() -> JuiceWRLDAPI:
    """Create a new JuiceWRLDAPI client instance."""

    return JuiceWRLDAPI(base_url=JUICEWRLD_API_BASE_URL)


def _get_or_create_user_playlists(user_id: int) -> Dict[str, List[Dict[str, Any]]]:
    """Return the playlist mapping for a user, creating it if needed."""

    playlists = _user_playlists.get(user_id)
    if playlists is None:
        playlists = {}
        _user_playlists[user_id] = playlists
    return playlists


PLAYLISTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playlists.json")
STATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "listening_stats.json")
SOTD_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sotd_config.json")

# Song of the Day config: { guild_id_str: channel_id }
_sotd_config: Dict[str, int] = {}

# Per-user listening stats:
# { user_id: { "total_plays": int, "total_seconds": int,
#              "songs": { song_name: play_count },
#              "eras": { era_name: play_count } } }
_user_listening_stats: Dict[int, Dict[str, Any]] = {}


def _serialize_user_playlists_for_json() -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for user_id, playlists in _user_playlists.items():
        data[str(user_id)] = playlists
    return data


def _load_user_playlists_from_disk() -> None:
    """Load user playlists from disk into memory (best-effort)."""

    global _user_playlists
    try:
        with open(PLAYLISTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return
    except Exception:
        return

    if not isinstance(raw, dict):
        return

    loaded: Dict[int, Dict[str, List[Dict[str, Any]]]] = {}
    for user_id_str, playlists in raw.items():
        try:
            user_id = int(user_id_str)
        except (TypeError, ValueError):
            continue
        if isinstance(playlists, dict):
            loaded[user_id] = playlists

    if loaded:
        _user_playlists = loaded


def _save_user_playlists_to_disk() -> None:
    """Persist user playlists to disk (best-effort)."""

    try:
        with open(PLAYLISTS_FILE, "w", encoding="utf-8") as f:
            json.dump(_serialize_user_playlists_for_json(), f, ensure_ascii=False)
    except Exception:
        return


def _load_listening_stats_from_disk() -> None:
    """Load listening stats from disk into memory (best-effort)."""
    global _user_listening_stats
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
        _user_listening_stats = loaded


def _save_listening_stats_to_disk() -> None:
    """Persist listening stats to disk (best-effort)."""
    try:
        serialized = {str(uid): data for uid, data in _user_listening_stats.items()}
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(serialized, f, ensure_ascii=False)
    except Exception:
        return


def _record_listen(user_id: int, title: str, era_name: Optional[str], duration_seconds: Optional[int]) -> None:
    """Record a song play for a user's listening stats."""
    stats = _user_listening_stats.setdefault(user_id, {
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

    _save_listening_stats_to_disk()


def _load_sotd_config() -> None:
    """Load SOTD channel config from disk."""
    global _sotd_config
    try:
        with open(SOTD_CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, Exception):
        return
    if isinstance(raw, dict):
        _sotd_config = raw


def _save_sotd_config() -> None:
    """Persist SOTD channel config to disk."""
    try:
        with open(SOTD_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(_sotd_config, f, ensure_ascii=False)
    except Exception:
        return


_load_user_playlists_from_disk()
_load_listening_stats_from_disk()
_load_sotd_config()


def _parse_length_to_seconds(length: str) -> Optional[int]:
    """Convert a length string like "3:45" or "01:02:03" to seconds."""

    if not length:
        return None
    parts = length.strip().split(":")
    try:
        if len(parts) == 2:
            minutes, seconds = map(int, parts)
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours, minutes, seconds = map(int, parts)
            return hours * 3600 + minutes * 60 + seconds
    except ValueError:
        return None
    return None


def _build_song_metadata_from_song(
    song_obj: Any,
    *,
    path: Optional[str] = None,
    image_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a metadata dict that mirrors the canonical Song model JSON.

    This attempts to map all known Song/ERA fields plus a few optional
    extras (bitrate, lyrics, snippets) if they exist on the object.
    """

    # Era as nested object
    era_obj = getattr(song_obj, "era", None)
    era_dict: Optional[Dict[str, Any]] = None
    if era_obj is not None:
        era_dict = {
            "id": getattr(era_obj, "id", None),
            "name": getattr(era_obj, "name", None),
            "description": getattr(era_obj, "description", None),
            "time_frame": getattr(era_obj, "time_frame", None),
            # Some APIs include play_count; default to 0 if missing.
            "play_count": getattr(era_obj, "play_count", 0),
        }

    # Prefer an explicitly-normalized image_url, fall back to the raw field
    final_image_url = image_url if image_url is not None else getattr(song_obj, "image_url", None)

    meta: Dict[str, Any] = {
        # Core identity
        "id": getattr(song_obj, "id", None),
        "public_id": getattr(song_obj, "public_id", None),
        "name": getattr(song_obj, "name", None),
        "original_key": getattr(song_obj, "original_key", None),
        "category": getattr(song_obj, "category", None),
        # File path within the comp structure
        "path": path,
        # Era object
        "era": era_dict,
        # Titles / tracking
        "track_titles": getattr(song_obj, "track_titles", None),
        "session_titles": getattr(song_obj, "session_titles", None),
        "session_tracking": getattr(song_obj, "session_tracking", None),
        # Credits
        "credited_artists": getattr(song_obj, "credited_artists", None),
        "producers": getattr(song_obj, "producers", None),
        "engineers": getattr(song_obj, "engineers", None),
        # Recording details
        "recording_locations": getattr(song_obj, "recording_locations", None),
        "record_dates": getattr(song_obj, "record_dates", None),
        "dates": getattr(song_obj, "dates", None),
        # Audio / technical
        "length": getattr(song_obj, "length", None),
        "bitrate": getattr(song_obj, "bitrate", None),
        "instrumentals": getattr(song_obj, "instrumentals", None),
        "instrumental_names": getattr(song_obj, "instrumental_names", None),
        # Files
        "file_names": getattr(song_obj, "file_names", None),
        # Release / leak
        "preview_date": getattr(song_obj, "preview_date", None),
        "release_date": getattr(song_obj, "release_date", None),
        "date_leaked": getattr(song_obj, "date_leaked", None),
        "leak_type": getattr(song_obj, "leak_type", None),
        # Text / extra info
        "additional_information": getattr(song_obj, "additional_information", None),
        "notes": getattr(song_obj, "notes", None),
        "lyrics": getattr(song_obj, "lyrics", None),
        # Media / visuals
        "image_url": final_image_url,
        # Misc lists
        "snippets": getattr(song_obj, "snippets", None),
    }

    return meta


def _format_progress_bar(current: int, total: Optional[int], width: int = 10) -> str:
    """Return a simple text progress bar and time display."""

    if not total or total <= 0 or current <= 0:
        return f"00:00 / {time.strftime('%M:%S', time.gmtime(total)) if total else '?:??'}"

    current = max(0, min(current, total))
    filled = int(width * (current / total))
    bar = "▮" * filled + "▯" * (width - filled)
    return f"{bar} {time.strftime('%M:%S', time.gmtime(current))} / {time.strftime('%M:%S', time.gmtime(total))}"


async def _delete_later(message: discord.Message, delay: int) -> None:
    """Delete a message after a delay, ignoring failures."""

    try:
        await asyncio.sleep(delay)
        await message.delete()
    except Exception:
        return


async def _send_temporary(ctx: commands.Context, content: str = None, delay: int = 10, embed: discord.Embed = None) -> None:
    """Send a status message that auto-deletes after `delay` seconds."""

    msg = await ctx.send(content, embed=embed)
    asyncio.create_task(_delete_later(msg, delay))


async def _send_ephemeral_temporary(
    interaction: discord.Interaction, content: str, delay: int = 5
) -> None:
    """Send an ephemeral followup message that auto-deletes after `delay` seconds."""

    msg = await interaction.followup.send(content, ephemeral=True, wait=True)
    asyncio.create_task(_delete_later(msg, delay))


def _schedule_interaction_deletion(interaction: discord.Interaction, delay: int) -> None:
    """Schedule an interaction's original response to be deleted after a delay."""
    async def _delete_after_delay() -> None:
        await asyncio.sleep(delay)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass
    asyncio.create_task(_delete_after_delay())


def _extract_duration_seconds(
    metadata: Dict[str, Any], track: Optional[Dict[str, Any]] = None
) -> Optional[int]:
    """Extract duration in seconds from metadata or track dict."""
    length_str = metadata.get("length") or (track.get("length") if track else None)
    if length_str:
        return _parse_length_to_seconds(length_str)
    return None


def _normalize_image_url(image_url: Optional[str]) -> Optional[str]:
    """Convert relative image URLs to absolute URLs."""
    if image_url and isinstance(image_url, str) and image_url.startswith("/"):
        return f"{JUICEWRLD_API_BASE_URL}{image_url}"
    return image_url


async def _delete_now_playing_message(guild_id: int) -> None:
    """Best-effort deletion of the tracked Now Playing message for a guild."""

    info = _guild_now_playing.get(guild_id)
    if not info:
        return

    message_id = info.get("message_id")
    channel_id = info.get("channel_id")
    if message_id is None or channel_id is None:
        _guild_now_playing.pop(guild_id, None)
        return

    guild_obj = bot.get_guild(guild_id)
    if not guild_obj:
        _guild_now_playing.pop(guild_id, None)
        return

    chan = guild_obj.get_channel(channel_id) or bot.get_channel(channel_id)
    if not isinstance(chan, discord.TextChannel):
        _guild_now_playing.pop(guild_id, None)
        return

    try:
        msg = await chan.fetch_message(message_id)
        await msg.delete()
    except Exception:
        # If we can't delete it for any reason, just drop the tracking entry.
        pass

    _guild_now_playing.pop(guild_id, None)


async def _delete_now_playing_message_after_delay(guild_id: int, delay: int) -> None:
    """Sleep for `delay` seconds, then delete the guild's Now Playing message."""

    await asyncio.sleep(delay)
    await _delete_now_playing_message(guild_id)


async def _schedule_player_cleanup(guild_id: int, delay: int = 15) -> None:
    """Previously deleted the Now Playing message after `delay` seconds if idle.

    Now kept as a no-op placeholder; the player is treated as static and
    reused, so we no longer auto-delete the player message on idle.
    """

    return


def _ensure_queue(guild_id: int) -> List[Dict[str, Any]]:
    """Get or create the playback queue for a guild."""

    queue = _guild_queue.get(guild_id)
    if queue is None:
        queue = []
        _guild_queue[guild_id] = queue
    return queue


def _disable_radio_if_active(ctx: commands.Context) -> bool:
    """Turn off radio mode for this guild if it is currently enabled.

    Returns True if radio was active and is now disabled.
    """

    if not ctx.guild:
        return False

    guild_id = ctx.guild.id
    if _guild_radio_enabled.get(guild_id):
        _guild_radio_enabled[guild_id] = False
        return True
    return False


async def _play_next_from_queue(ctx: commands.Context) -> None:
    """Play the next queued track for this guild, if any.

    This is invoked from after-callbacks when a non-radio track finishes.
    """

    if not ctx.guild:
        return

    guild_id = ctx.guild.id

    # If radio was re-enabled mid-queue, hand control back to radio.
    if _guild_radio_enabled.get(guild_id):
        await _play_random_song_in_guild(ctx)
        return

    queue = _ensure_queue(guild_id)
    if not queue:
        # Nothing left to play; keep the player message but show it as idle.
        await _send_player_controls(
            ctx,
            title="Nothing playing",
            path=None,
            is_radio=False,
            metadata={},
            duration_seconds=None,
        )
        return

    entry = queue.pop(0)
    stream_url = entry.get("stream_url")
    title = str(entry.get("title") or "Unknown")
    path = entry.get("path")
    metadata = entry.get("metadata") or {}
    duration_seconds = entry.get("duration_seconds")

    voice: Optional[discord.VoiceClient] = ctx.voice_client
    if not voice or not stream_url:
        return

    if voice.is_playing():
        voice.stop()

    ffmpeg_before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    ffmpeg_options = "-vn"

    try:
        source = discord.FFmpegPCMAudio(
            stream_url,
            before_options=ffmpeg_before,
            options=ffmpeg_options,
        )
    except Exception as e:  # pragma: no cover
        print(f"Queue playback error creating source: {e}", file=sys.stderr)
        # Try the next track in the queue, if any.
        await _play_next_from_queue(ctx)
        return

    def _after_playback(error: Optional[Exception]) -> None:
        if error:
            print(f"Queue playback error: {error}", file=sys.stderr)
        fut = _play_next_from_queue(ctx)
        asyncio.run_coroutine_threadsafe(fut, bot.loop)

    voice.play(source, after=_after_playback)
    await _send_player_controls(
        ctx,
        title=title,
        path=path,
        is_radio=False,
        metadata=metadata,
        duration_seconds=duration_seconds,
    )


async def _queue_or_play_now(
    ctx: commands.Context,
    *,
    stream_url: str,
    title: str,
    path: Optional[str],
    metadata: Optional[Dict[str, Any]] = None,
    duration_seconds: Optional[int] = None,
    silent: bool = False,
) -> None:
    """If something is already playing, queue this track; otherwise play now.

    This is used by search/comp/play commands for on-demand playback.
    If silent=True, suppress individual "added to queue" messages (useful for batch adds).
    """

    if not ctx.guild:
        return

    guild_id = ctx.guild.id
    voice: Optional[discord.VoiceClient] = ctx.voice_client

    queue = _ensure_queue(guild_id)

    # If there is active playback (song or radio), enqueue this track.
    if voice and (voice.is_playing() or voice.is_paused()):
        queue.append(
            {
                "title": title,
                "path": path,
                "stream_url": stream_url,
                "metadata": metadata or {},
                "duration_seconds": duration_seconds,
            }
        )
        if not silent:
            await _send_temporary(
                ctx,
                f"Added to queue at position {len(queue)}: `{title}`.",
            )
        return

    # Nothing is playing; start immediately and wire up the queue callback.
    if not voice or not voice.is_connected():
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You need to be in a voice channel to play music.")
            return
        channel = ctx.author.voice.channel
        voice = await channel.connect()

    ffmpeg_before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    ffmpeg_options = "-vn"

    try:
        source = discord.FFmpegPCMAudio(
            stream_url,
            before_options=ffmpeg_before,
            options=ffmpeg_options,
        )
    except Exception as e:  # pragma: no cover
        await ctx.send(f"Failed to create audio source: {e}")
        return

    def _after_playback(error: Optional[Exception]) -> None:
        if error:
            print(f"Playback error: {error}", file=sys.stderr)
        fut = _play_next_from_queue(ctx)
        asyncio.run_coroutine_threadsafe(fut, bot.loop)

    voice.play(source, after=_after_playback)
    await _send_player_controls(
        ctx,
        title=title,
        path=path,
        is_radio=False,
        metadata=metadata,
        duration_seconds=duration_seconds,
    )


def _build_playlists_embed_for_user(user: discord.abc.User, playlists: Dict[str, List[Dict[str, Any]]]) -> discord.Embed:
    """Construct an embed summarizing a user's playlists."""

    embed = discord.Embed(title=f"{getattr(user, 'display_name', str(user))}'s Playlists")
    for name, tracks in playlists.items():
        count = len(tracks)
        if not count:
            value = "(empty)"
        else:
            preview_titles = [str(t.get("name") or t.get("id") or "?") for t in tracks[:3]]
            extra = "" if count <= 3 else f" +{count - 3} more"
            value = ", ".join(preview_titles) + extra

        embed.add_field(name=f"{name} ({count})", value=value[:1024], inline=False)
    return embed


def _build_stats_embed(user: discord.abc.User) -> discord.Embed:
    """Construct an embed showing a user's personal listening stats."""

    stats = _user_listening_stats.get(getattr(user, "id", 0))
    display_name = getattr(user, "display_name", str(user))

    if not stats or stats.get("total_plays", 0) == 0:
        embed = discord.Embed(
            title=f"{display_name}'s Listening Stats",
            description="No listening history yet. Play some songs to start tracking!",
            colour=discord.Colour.greyple(),
        )
        return embed

    total_plays = stats.get("total_plays", 0)
    total_seconds = stats.get("total_seconds", 0)
    songs = stats.get("songs", {})
    eras = stats.get("eras", {})

    # Format total listen time
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        time_str = f"{hours}h {minutes}m"
    elif minutes:
        time_str = f"{minutes}m {secs}s"
    else:
        time_str = f"{secs}s"

    embed = discord.Embed(
        title=f"{display_name}'s Listening Stats",
        colour=discord.Colour.purple(),
    )
    embed.add_field(name="Total Plays", value=str(total_plays), inline=True)
    embed.add_field(name="Listen Time", value=time_str, inline=True)

    # Top 5 songs
    if songs:
        top_songs = sorted(songs.items(), key=lambda x: x[1], reverse=True)[:5]
        lines = [f"`{i}.` **{name}** — {count} play{'s' if count != 1 else ''}" for i, (name, count) in enumerate(top_songs, 1)]
        embed.add_field(name="Top Songs", value="\n".join(lines), inline=False)

    # Top 3 eras
    if eras:
        top_eras = sorted(eras.items(), key=lambda x: x[1], reverse=True)[:3]
        lines = [f"`{i}.` **{name}** — {count} play{'s' if count != 1 else ''}" for i, (name, count) in enumerate(top_eras, 1)]
        embed.add_field(name="Top Eras", value="\n".join(lines), inline=False)

    return embed


def _touch_activity(guild_id: int) -> None:
    """Update the last-activity timestamp for a guild."""
    _guild_last_activity[guild_id] = time.time()


def _set_now_playing(
    ctx: commands.Context,
    *,
    title: str,
    path: Optional[str],
    is_radio: bool,
    metadata: Optional[Dict[str, Any]] = None,
    duration_seconds: Optional[int] = None,
) -> None:
    """Record the currently playing track for a guild.

    We keep any existing player message metadata (message_id/channel_id)
    so that subsequent tracks can reuse and edit the same message.
    """

    if not ctx.guild:
        return

    guild_id = ctx.guild.id
    existing = _guild_now_playing.get(guild_id, {})
    
    # Save current song as previous (if there was one playing)
    if existing.get("title") and existing.get("title") != "Nothing playing":
        _guild_previous_song[guild_id] = {
            "title": existing.get("title"),
            "path": existing.get("path"),
            "metadata": existing.get("metadata", {}),
            "duration_seconds": existing.get("duration_seconds"),
        }
    
    existing.update(
        {
            "title": title,
            "path": path,
            "is_radio": is_radio,
            "requester": getattr(ctx.author, "mention", str(ctx.author)),
            "metadata": metadata or {},
            "duration_seconds": duration_seconds,
            "started_at": time.time(),
            "paused_at": None,  # Track when paused
            "total_paused_time": 0,  # Accumulated pause time
        }
    )
    _guild_now_playing[guild_id] = existing

    # Mark activity so the idle auto-leave timer resets.
    _touch_activity(guild_id)

    # Record the listen for the requester's stats.
    if title and title != "Nothing playing":
        user_id = getattr(ctx.author, "id", None)
        if user_id:
            era_name = None
            meta = metadata or {}
            era_val = meta.get("era")
            if isinstance(era_val, dict):
                era_name = era_val.get("name")
            elif era_val:
                era_name = str(era_val)
            _record_listen(user_id, title, era_name, duration_seconds)

    # Update the bot's Discord Rich Presence to show the current song.
    if title and title != "Nothing playing":
        now = time.time()
        # Build timestamps for a live elapsed/remaining timer.
        timestamps: Dict[str, Any] = {"start": now}
        if duration_seconds and duration_seconds > 0:
            timestamps["end"] = now + duration_seconds

        # Compose the "state" line: era + category.
        meta = metadata or {}
        state_parts: List[str] = []
        era_val = meta.get("era")
        if isinstance(era_val, dict) and era_val.get("name"):
            state_parts.append(era_val["name"])
        elif era_val:
            state_parts.append(str(era_val))
        cat = meta.get("category")
        if cat:
            state_parts.append(str(cat))
        state_text = " · ".join(state_parts) if state_parts else None

        # Duration text for the name field.
        if duration_seconds and duration_seconds > 0:
            dm, ds = divmod(duration_seconds, 60)
            activity_name = f"{title} [{dm}:{ds:02d}]"
        else:
            activity_name = title
        if len(activity_name) > 128:
            activity_name = activity_name[:125] + "..."

        # Album art via external URL (works for OAuth2 apps).
        assets: Dict[str, str] = {}
        image_url = meta.get("image_url")
        if image_url and isinstance(image_url, str) and image_url.startswith("http"):
            assets["large_image"] = image_url
            assets["large_text"] = title

        # Small image: radio icon vs play icon.
        if is_radio:
            assets["small_image"] = "https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/1f4fb.png"
            assets["small_text"] = "Radio Mode"
        else:
            assets["small_image"] = "https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/25b6.png"
            assets["small_text"] = "Now Playing"

        # Party size: show how many people are in the voice channel.
        party: Optional[Dict[str, Any]] = None
        voice_client: Optional[discord.VoiceClient] = ctx.voice_client
        if voice_client and voice_client.channel:
            humans = [m for m in voice_client.channel.members if not m.bot]
            if humans:
                party = {"size": [len(humans), voice_client.channel.user_limit or len(humans)]}

        # Rich Presence buttons (up to 2 URL buttons on the activity card).
        buttons = ["Invite Bot"]

        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name=activity_name,
            details=title,
            state=state_text,
            timestamps=timestamps,
            assets=assets if assets else None,
            party=party,
            buttons=buttons,
        )
    else:
        activity = discord.Activity(type=discord.ActivityType.listening, name="nothing")
    asyncio.create_task(bot.change_presence(activity=activity))


class SingleSongResultView(discord.ui.View):
    """Interactive view for a single song search result.
    
    Modes:
    - "main": Shows song details with Play, Add to Playlist, and Info buttons
    - "info": Shows detailed song information
    - "select_playlist": Shows user's playlists to add the song to
    - "create_playlist": Modal to create a new playlist
    """

    def __init__(
        self,
        *,
        ctx: commands.Context,
        song: Any,
        query: str,
    ) -> None:
        super().__init__(timeout=60)
        self.ctx = ctx
        self.song = song
        self.query = query
        self.mode = "main"  # "main", "info", or "select_playlist"
        self.playlist_items: List[tuple] = []  # For playlist selection mode
        self.per_page = 5
        self.current_page = 0
        self._rebuild_buttons()

    def build_embed(self) -> discord.Embed:
        if self.mode == "select_playlist":
            return self._build_playlist_select_embed()
        if self.mode == "info":
            return self._build_info_embed()
        
        # Main mode: show song details
        sid = getattr(self.song, "id", "?")
        name = getattr(self.song, "name", getattr(self.song, "title", "Unknown"))
        category = getattr(self.song, "category", "?")
        length = getattr(self.song, "length", "?")
        era_name = getattr(getattr(self.song, "era", None), "name", "?")
        
        description = (
            f"**{name}** (ID: `{sid}`)\n"
            f"Category: `{category}`\n"
            f"Length: `{length}`\n"
            f"Era: `{era_name}`"
        )
        
        embed = discord.Embed(
            title="Search Result",
            description=description,
        )
        embed.set_footer(text="Use the buttons below to play or add to playlist.")
        return embed

    def _build_info_embed(self) -> discord.Embed:
        """Build embed for detailed song info mode."""
        sid = getattr(self.song, "id", "?")
        name = getattr(self.song, "name", getattr(self.song, "title", "Unknown"))
        category = getattr(self.song, "category", "?")
        length = getattr(self.song, "length", "?")
        era_name = getattr(getattr(self.song, "era", None), "name", "?")
        
        # Build detailed description
        lines = [
            f"**{name}**",
            f"ID: `{sid}`",
            f"Category: `{category}`",
            f"Length: `{length}`",
            f"Era: `{era_name}`",
        ]
        
        # Add additional details if available
        producers = getattr(self.song, "producers", None)
        if producers:
            lines.append(f"Producers: {producers}")
        
        credited_artists = getattr(self.song, "credited_artists", None)
        if credited_artists:
            lines.append(f"Credited Artists: {credited_artists}")
        
        engineers = getattr(self.song, "engineers", None)
        if engineers:
            lines.append(f"Engineers: {engineers}")
        
        recording_locations = getattr(self.song, "recording_locations", None)
        if recording_locations:
            lines.append(f"Recording Locations: {recording_locations}")
        
        record_dates = getattr(self.song, "record_dates", None)
        if record_dates:
            lines.append(f"Record Dates: {record_dates}")
        
        track_titles = getattr(self.song, "track_titles", None)
        if track_titles:
            lines.append(f"Track Titles: {track_titles}")
        
        session_titles = getattr(self.song, "session_titles", None)
        if session_titles:
            lines.append(f"Session Titles: {session_titles}")
        
        embed = discord.Embed(
            title="Song Info",
            description="\n".join(lines),
        )
        embed.set_footer(text="Press Back to return.")
        return embed

    def _build_playlist_select_embed(self) -> discord.Embed:
        """Build embed for playlist selection mode."""
        song_name = getattr(self.song, "name", "Unknown")
        total = len(self.playlist_items)
        total_pages = max(1, math.ceil(total / self.per_page))
        
        if total == 0:
            description = f"Select where to add **{song_name}**\n\nYou don't have any playlists yet."
        else:
            header = f"Page {self.current_page + 1}/{total_pages} • Select where to add **{song_name}**"
            lines: List[str] = []
            start = self.current_page * self.per_page
            page_playlists = self.playlist_items[start:start + self.per_page]
            
            for idx, (name, tracks) in enumerate(page_playlists, start=1):
                count = len(tracks)
                lines.append(f"**{idx}.** {name} ({count} tracks)")
            
            description = header + "\n\n" + "\n".join(lines)
        
        embed = discord.Embed(title="Add to Playlist", description=description)
        embed.set_footer(text="Select a playlist or create a new one.")
        return embed

    def _rebuild_buttons(self) -> None:
        """Rebuild buttons based on current mode."""
        self.clear_items()
        
        if self.mode == "main":
            # Main mode: Play, Add to Playlist, and Info buttons
            play_btn = discord.ui.Button(label="▶️ Play", style=discord.ButtonStyle.primary, row=0)
            play_btn.callback = self._on_play
            self.add_item(play_btn)
            
            add_btn = discord.ui.Button(label="➕ Add to Playlist", style=discord.ButtonStyle.success, row=0)
            add_btn.callback = self._on_add_to_playlist
            self.add_item(add_btn)
            
            info_btn = discord.ui.Button(label="ℹ️ Info", style=discord.ButtonStyle.secondary, row=0)
            info_btn.callback = self._on_info
            self.add_item(info_btn)
        elif self.mode == "info":
            # Info mode: Back button only
            back_btn = discord.ui.Button(label="⬅ Back", style=discord.ButtonStyle.danger, row=0)
            back_btn.callback = self._on_back
            self.add_item(back_btn)
        else:
            # Playlist selection mode
            total = len(self.playlist_items)
            total_pages = max(1, math.ceil(total / self.per_page))
            
            # Row 0: pagination (only if needed) + back
            if self.current_page > 0:
                prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, row=0)
                prev_btn.callback = lambda i: self._change_page(i, -1)
                self.add_item(prev_btn)
            
            if self.current_page < total_pages - 1:
                next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, row=0)
                next_btn.callback = lambda i: self._change_page(i, +1)
                self.add_item(next_btn)
            
            back_btn = discord.ui.Button(label="⬅ Back", style=discord.ButtonStyle.danger, row=0)
            back_btn.callback = self._on_back
            self.add_item(back_btn)
            
            # Row 1: playlist selection buttons
            for slot in range(5):
                global_index = self.current_page * self.per_page + slot
                if global_index >= total:
                    break
                btn = discord.ui.Button(label=str(slot + 1), style=discord.ButtonStyle.primary, row=1)
                btn.callback = self._make_playlist_select_callback(slot)
                self.add_item(btn)
            
            # Row 2: Add to New Playlist button
            new_playlist_btn = discord.ui.Button(label="➕ Add to New Playlist", style=discord.ButtonStyle.success, row=2)
            new_playlist_btn.callback = self._on_create_new_playlist
            self.add_item(new_playlist_btn)

    async def _on_play(self, interaction: discord.Interaction) -> None:
        """Handle Play button press."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return

        song_id = getattr(self.song, "id", None)
        if song_id is None:
            await interaction.response.send_message(
                "This song does not have a valid ID to play.",
                ephemeral=True,
            )
            return

        name = getattr(self.song, "name", getattr(self.song, "title", "Unknown"))

        # Defer the interaction
        await interaction.response.defer(ephemeral=True)

        # If radio is active, disable it and let current song finish
        if self.ctx.guild and _guild_radio_enabled.get(self.ctx.guild.id):
            _guild_radio_enabled[self.ctx.guild.id] = False

        # Play the song (will queue if something is playing)
        await play_song(self.ctx, str(song_id))

        # Close the search result message
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

        self.stop()

    async def _on_add_to_playlist(self, interaction: discord.Interaction) -> None:
        """Handle Add to Playlist button press."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return
        
        # Load user's playlists
        user_playlists = _get_or_create_user_playlists(interaction.user.id)
        self.playlist_items = list(user_playlists.items())
        self.current_page = 0
        self.mode = "select_playlist"
        
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_info(self, interaction: discord.Interaction) -> None:
        """Handle Info button press."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return
        
        self.mode = "info"
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        """Handle Back button press."""
        self.mode = "main"
        self.current_page = 0
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _change_page(self, interaction: discord.Interaction, delta: int) -> None:
        """Handle pagination."""
        total_pages = max(1, math.ceil(len(self.playlist_items) / self.per_page))
        new_page = self.current_page + delta
        if new_page < 0 or new_page >= total_pages:
            await interaction.response.defer()
            return
        
        self.current_page = new_page
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    def _make_playlist_select_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_playlist_select(interaction, slot_index)
        return callback

    async def _handle_playlist_select(self, interaction: discord.Interaction, slot_index: int) -> None:
        """Handle selecting a playlist to add the song to."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return

        global_index = self.current_page * self.per_page + slot_index
        if global_index < 0 or global_index >= len(self.playlist_items):
            await interaction.response.send_message(
                "No playlist in that position.",
                ephemeral=True,
            )
            return

        playlist_name, playlist_tracks = self.playlist_items[global_index]
        song_id = getattr(self.song, "id", None)
        song_name = getattr(self.song, "name", getattr(self.song, "title", "Unknown"))
        song_path = getattr(self.song, "path", None)
        
        # Check for duplicates
        for track in playlist_tracks:
            if song_id is not None and track.get("id") == song_id:
                await interaction.response.send_message(
                    f"`{song_name}` is already in playlist `{playlist_name}`.",
                    ephemeral=True,
                )
                _schedule_interaction_deletion(interaction, 5)
                return
            if song_path and track.get("path") == song_path:
                await interaction.response.send_message(
                    f"`{song_name}` is already in playlist `{playlist_name}`.",
                    ephemeral=True,
                )
                _schedule_interaction_deletion(interaction, 5)
                return

        # Build metadata
        metadata = _build_song_metadata_from_song(self.song, path=song_path)
        
        # Add to playlist
        playlist_tracks.append({
            "id": song_id,
            "name": song_name,
            "path": song_path,
            "metadata": metadata,
            "added_at": time.time(),
        })
        
        _save_user_playlists_to_disk()
        
        # Show success message and close
        await interaction.response.send_message(
            f"Added `{song_name}` to playlist `{playlist_name}`.",
            ephemeral=True,
        )
        _schedule_interaction_deletion(interaction, 30)
        
        # Delete the search result message
        try:
            await interaction.delete_original_response()
        except Exception:
            pass
        
        self.stop()

    async def _on_create_new_playlist(self, interaction: discord.Interaction) -> None:
        """Handle Add to New Playlist button press."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return
        
        modal = SingleSongPlaylistCreateModal(self)
        await interaction.response.send_modal(modal)

    async def on_timeout(self) -> None:
        """Called when the view times out."""
        # Just disable the view, don't delete ephemeral messages
        pass


class SingleSongPlaylistCreateModal(discord.ui.Modal, title="Create New Playlist"):
    """Modal for creating a new playlist from single song result."""
    
    playlist_name = discord.ui.TextInput(
        label="Playlist Name",
        placeholder="Enter playlist name...",
        max_length=100,
    )

    def __init__(self, view: SingleSongResultView) -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = self.playlist_name.value.strip()
        if not name:
            await interaction.response.send_message("Name cannot be empty.", ephemeral=True)
            return
        
        user = interaction.user
        playlists = _get_or_create_user_playlists(user.id)
        
        if name in playlists:
            await interaction.response.send_message(
                f"You already have a playlist named `{name}`.", ephemeral=True
            )
            return
        
        # Create the playlist and add the song
        playlists[name] = []
        song_id = getattr(self.view.song, "id", None)
        song_name = getattr(self.view.song, "name", getattr(self.view.song, "title", "Unknown"))
        song_path = getattr(self.view.song, "path", None)
        
        metadata = _build_song_metadata_from_song(self.view.song, path=song_path)
        
        playlists[name].append({
            "id": song_id,
            "name": song_name,
            "path": song_path,
            "metadata": metadata,
            "added_at": time.time(),
        })
        
        _save_user_playlists_to_disk()
        
        await interaction.response.send_message(
            f"Added `{song_name}` to `{name}` playlist.",
            ephemeral=True,
        )
        _schedule_interaction_deletion(interaction, 5)
        
        # Delete the search result message
        try:
            # Get the original interaction from the view's context
            # We need to delete via the view's original message
            if hasattr(interaction, 'message') and interaction.message:
                await interaction.message.delete()
        except Exception:
            pass
        
        self.view.stop()


class SearchPaginationView(discord.ui.View):
    """Paginated search results with 5 songs per page.
    
    Modes:
    - "list": Shows list of songs with 1-5 selection buttons
    - "song_selected": Shows selected song with Play/Add to Playlist/Info buttons
    - "info": Shows detailed info for selected song
    - "select_playlist": Shows user's playlists to choose where to add the song
    """

    def __init__(
        self,
        *,
        ctx: commands.Context,
        songs: List[Any],
        query: str,
        total_count: Optional[int] = None,
        is_ephemeral: bool = False,
    ) -> None:
        super().__init__(timeout=60)
        self.ctx = ctx
        self.songs = songs
        self.query = query
        self.per_page = 5
        self.current_page = 0
        self.total_pages = max(1, math.ceil(len(songs) / self.per_page))
        self.total_count = total_count or len(songs)
        self.is_ephemeral = is_ephemeral
        self.message: Optional[discord.Message] = None  # Set after sending
        self.mode = "list"  # "list", "song_selected", "info", or "select_playlist"
        self.selected_song: Optional[Any] = None  # Currently selected song
        self.selected_song_index: Optional[int] = None  # Index of selected song in self.songs
        self.playlist_items: List[tuple] = []  # For playlist selection mode
        self.playlist_page = 0
        # Build initial buttons dynamically
        self._rebuild_buttons()

    def _get_page_songs(self) -> List[Any]:
        start = self.current_page * self.per_page
        end = start + self.per_page
        return self.songs[start:end]

    def build_embed(self) -> discord.Embed:
        if self.mode == "select_playlist":
            return self._build_playlist_select_embed()
        if self.mode == "song_selected":
            return self._build_song_selected_embed()
        if self.mode == "info":
            return self._build_info_embed()
        
        # List mode: show all songs on current page
        page_songs = self._get_page_songs()
        total_results = self.total_count

        header = f"Page {self.current_page + 1}/{self.total_pages} • {total_results} result(s) for **{self.query}**"
        lines: List[str] = []
        for idx, song in enumerate(page_songs, start=1):
            sid = getattr(song, "id", "?")
            name = getattr(song, "name", getattr(song, "title", "Unknown"))
            category = getattr(song, "category", "?")
            length = getattr(song, "length", "?")
            era_name = getattr(getattr(song, "era", None), "name", "?")
            lines.append(
                f"**{idx}.** `{sid}` — {name}  "
                f"[{category} · {length} · Era: {era_name}]"
            )

        description = header
        if lines:
            description += "\n\n" + "\n".join(lines)

        embed = discord.Embed(title="Search Results", description=description)
        embed.set_footer(text="Select a song (1–5) to see options.")
        return embed

    def _build_song_selected_embed(self) -> discord.Embed:
        """Build embed for song selected mode."""
        if not self.selected_song:
            return discord.Embed(title="Error", description="No song selected.")
        
        sid = getattr(self.selected_song, "id", "?")
        name = getattr(self.selected_song, "name", getattr(self.selected_song, "title", "Unknown"))
        category = getattr(self.selected_song, "category", "?")
        length = getattr(self.selected_song, "length", "?")
        era_name = getattr(getattr(self.selected_song, "era", None), "name", "?")
        
        description = (
            f"**{name}** (ID: `{sid}`)\n"
            f"Category: `{category}`\n"
            f"Length: `{length}`\n"
            f"Era: `{era_name}`"
        )
        
        embed = discord.Embed(
            title="Search Result",
            description=description,
        )
        embed.set_footer(text="Use the buttons below to play, add to playlist, or view info.")
        return embed

    def _build_info_embed(self) -> discord.Embed:
        """Build embed for detailed song info mode."""
        if not self.selected_song:
            return discord.Embed(title="Error", description="No song selected.")
        
        song = self.selected_song
        sid = getattr(song, "id", "?")
        name = getattr(song, "name", getattr(song, "title", "Unknown"))
        category = getattr(song, "category", "?")
        length = getattr(song, "length", "?")
        era_name = getattr(getattr(song, "era", None), "name", "?")
        
        # Build detailed description
        lines = [
            f"**{name}**",
            f"ID: `{sid}`",
            f"Category: `{category}`",
            f"Length: `{length}`",
            f"Era: `{era_name}`",
        ]
        
        # Add additional details if available
        producers = getattr(song, "producers", None)
        if producers:
            lines.append(f"Producers: {producers}")
        
        credited_artists = getattr(song, "credited_artists", None)
        if credited_artists:
            lines.append(f"Credited Artists: {credited_artists}")
        
        engineers = getattr(song, "engineers", None)
        if engineers:
            lines.append(f"Engineers: {engineers}")
        
        recording_locations = getattr(song, "recording_locations", None)
        if recording_locations:
            lines.append(f"Recording Locations: {recording_locations}")
        
        record_dates = getattr(song, "record_dates", None)
        if record_dates:
            lines.append(f"Record Dates: {record_dates}")
        
        track_titles = getattr(song, "track_titles", None)
        if track_titles:
            lines.append(f"Track Titles: {track_titles}")
        
        session_titles = getattr(song, "session_titles", None)
        if session_titles:
            lines.append(f"Session Titles: {session_titles}")
        
        embed = discord.Embed(
            title="Song Info",
            description="\n".join(lines),
        )
        embed.set_footer(text="Press Back to return.")
        return embed

    def _build_playlist_select_embed(self) -> discord.Embed:
        """Build embed for playlist selection mode."""
        song_name = getattr(self.selected_song, "name", "Unknown") if self.selected_song else "Unknown"
        total = len(self.playlist_items)
        total_pages = max(1, math.ceil(total / self.per_page))
        
        header = f"Page {self.playlist_page + 1}/{total_pages} • Select playlist for **{song_name}**"
        lines: List[str] = []
        start = self.playlist_page * self.per_page
        page_playlists = self.playlist_items[start:start + self.per_page]
        
        for idx, (name, tracks) in enumerate(page_playlists, start=1):
            count = len(tracks)
            lines.append(f"**{idx}.** {name} ({count} tracks)")
        
        if not lines:
            lines.append("No playlists yet. Use `!jw playlist create <name>` to create one.")
        
        description = header + "\n\n" + "\n".join(lines)
        embed = discord.Embed(title="Add to Playlist", description=description)
        embed.set_footer(text="Select a playlist (1–5) or go back.")
        return embed

    def _update_button_states(self) -> None:
        """Enable/disable nav + slot buttons based on current page and results."""

        if self.mode == "select_playlist":
            total = len(self.playlist_items)
            total_pages = max(1, math.ceil(total / self.per_page))
            for child in self.children:
                if not isinstance(child, discord.ui.Button):
                    continue
                label = child.label or ""
                if label == "◀":
                    child.disabled = self.playlist_page == 0
                elif label == "▶":
                    child.disabled = self.playlist_page >= total_pages - 1
                elif label.isdigit() or (len(label) > 1 and label[1:].isdigit()):
                    # Handle both "1" and "➕1" style labels
                    digit = label[-1] if label[-1].isdigit() else label
                    if digit.isdigit():
                        slot_index = int(digit) - 1
                        global_index = self.playlist_page * self.per_page + slot_index
                        child.disabled = global_index >= total
            return

        total = len(self.songs)
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue

            label = child.label or ""
            if label == "◀":
                child.disabled = self.current_page == 0
            elif label == "▶":
                child.disabled = self.current_page >= self.total_pages - 1
            elif label.isdigit() or (len(label) > 1 and label[1:].isdigit()):
                # Handle both "1" and "➕1" style labels
                digit = label[-1] if label[-1].isdigit() else label
                if digit.isdigit():
                    slot_index = int(digit) - 1
                    global_index = self.current_page * self.per_page + slot_index
                    child.disabled = global_index >= total

    async def _change_page(self, interaction: discord.Interaction, delta: int) -> None:
        if self.mode == "select_playlist":
            total_pages = max(1, math.ceil(len(self.playlist_items) / self.per_page))
            new_page = self.playlist_page + delta
            if new_page < 0 or new_page >= total_pages:
                await interaction.response.defer()
                return
            self.playlist_page = new_page
            self._rebuild_buttons()
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)
            return

        new_page = self.current_page + delta
        if new_page < 0 or new_page >= self.total_pages:
            await interaction.response.defer()
            return

        self.current_page = new_page
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _handle_song_select(self, interaction: discord.Interaction, slot_index: int) -> None:
        """Handle selecting a song from the list."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return

        global_index = self.current_page * self.per_page + slot_index
        if global_index < 0 or global_index >= len(self.songs):
            await interaction.response.send_message(
                "No song in that position on this page.",
                ephemeral=True,
            )
            return

        self.selected_song = self.songs[global_index]
        self.selected_song_index = global_index
        self.mode = "song_selected"
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_play_selected(self, interaction: discord.Interaction) -> None:
        """Handle Play button for selected song."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return

        if not self.selected_song:
            await interaction.response.send_message(
                "No song selected.",
                ephemeral=True,
            )
            return

        song_id = getattr(self.selected_song, "id", None)
        if song_id is None:
            await interaction.response.send_message(
                "This result does not have a valid song ID to play.",
                ephemeral=True,
            )
            return

        name = getattr(self.selected_song, "name", getattr(self.selected_song, "title", "Unknown"))

        # Defer the interaction first to avoid timeout
        await interaction.response.defer(ephemeral=True)

        # Play the song
        await play_song(self.ctx, str(song_id))

        await _send_ephemeral_temporary(
            interaction, f"Requested playback for `{name}` (ID `{song_id}`)."
        )

        # Delete or edit the search results message
        try:
            if self.is_ephemeral:
                embed = discord.Embed(title="Search Results", description="Song selected. Search closed.")
                await interaction.edit_original_response(embed=embed, view=None)
                _schedule_interaction_deletion(interaction, 5)
            else:
                msg = self.message or interaction.message
                if msg:
                    await msg.delete()
        except discord.errors.NotFound:
            pass

        self.stop()

    async def _on_add_to_playlist_selected(self, interaction: discord.Interaction) -> None:
        """Handle Add to Playlist button for selected song."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return

        if not self.selected_song:
            await interaction.response.send_message(
                "No song selected.",
                ephemeral=True,
            )
            return

        # Load user's playlists
        user_playlists = _get_or_create_user_playlists(interaction.user.id)
        self.playlist_items = list(user_playlists.items())
        self.playlist_page = 0
        
        if not self.playlist_items:
            await interaction.response.send_message(
                "You don't have any playlists yet. Use `!jw playlist create <name>` to create one.",
                ephemeral=True,
            )
            return
        
        self.mode = "select_playlist"
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_info_selected(self, interaction: discord.Interaction) -> None:
        """Handle Info button for selected song."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return

        self.mode = "info"
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _handle_playlist_select(self, interaction: discord.Interaction, slot_index: int) -> None:
        """Handle selecting a playlist to add the song to."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return

        global_index = self.playlist_page * self.per_page + slot_index
        if global_index < 0 or global_index >= len(self.playlist_items):
            await interaction.response.send_message(
                "No playlist in that position.",
                ephemeral=True,
            )
            return

        playlist_name, playlist_tracks = self.playlist_items[global_index]
        song = self.selected_song
        
        if song is None:
            await interaction.response.send_message(
                "No song selected.",
                ephemeral=True,
            )
            return

        # Build song data
        song_id = getattr(song, "id", None)
        song_name = getattr(song, "name", getattr(song, "title", "Unknown"))
        song_path = getattr(song, "path", None)
        
        # Check for duplicates
        for track in playlist_tracks:
            if song_id is not None and track.get("id") == song_id:
                await interaction.response.send_message(
                    f"`{song_name}` is already in playlist `{playlist_name}`.",
                    ephemeral=True,
                )
                return
            if song_path and track.get("path") == song_path:
                await interaction.response.send_message(
                    f"`{song_name}` is already in playlist `{playlist_name}`.",
                    ephemeral=True,
                )
                return

        # Build metadata
        metadata = _build_song_metadata_from_song(song, path=song_path)
        
        # Add to playlist
        playlist_tracks.append({
            "id": song_id,
            "name": song_name,
            "path": song_path,
            "metadata": metadata,
            "added_at": time.time(),
        })
        
        _save_user_playlists_to_disk()
        
        # Return to song selected mode and update the search view
        self.mode = "song_selected"
        self._rebuild_buttons()
        embed = self.build_embed()
        
        # Edit the search view message and send confirmation
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"Added `{song_name}` to playlist `{playlist_name}`.",
            ephemeral=True,
        )

    def _rebuild_buttons(self) -> None:
        """Rebuild buttons based on current mode."""
        self.clear_items()
        
        if self.mode == "select_playlist":
            # Playlist selection mode
            total = len(self.playlist_items)
            total_pages = max(1, math.ceil(total / self.per_page))
            
            # Row 0: pagination (only if needed) + back
            if self.playlist_page > 0:
                prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, row=0)
                prev_btn.callback = lambda i: self._change_page(i, -1)
                self.add_item(prev_btn)
            
            if self.playlist_page < total_pages - 1:
                next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, row=0)
                next_btn.callback = lambda i: self._change_page(i, +1)
                self.add_item(next_btn)
            
            back_btn = discord.ui.Button(label="⬅ Back", style=discord.ButtonStyle.danger, row=0)
            back_btn.callback = self._on_back_to_song_selected
            self.add_item(back_btn)
            
            # Row 1: playlist selection buttons (only for items that exist)
            for slot in range(5):
                global_index = self.playlist_page * self.per_page + slot
                if global_index >= total:
                    break
                btn = discord.ui.Button(label=str(slot + 1), style=discord.ButtonStyle.success, row=1)
                btn.callback = self._make_playlist_select_callback(slot)
                self.add_item(btn)
        
        elif self.mode == "song_selected":
            # Song selected mode: Play, Add to Playlist, Info, Back buttons
            play_btn = discord.ui.Button(label="▶️ Play", style=discord.ButtonStyle.primary, row=0)
            play_btn.callback = self._on_play_selected
            self.add_item(play_btn)
            
            add_btn = discord.ui.Button(label="➕ Add to Playlist", style=discord.ButtonStyle.success, row=0)
            add_btn.callback = self._on_add_to_playlist_selected
            self.add_item(add_btn)
            
            info_btn = discord.ui.Button(label="ℹ️ Info", style=discord.ButtonStyle.secondary, row=0)
            info_btn.callback = self._on_info_selected
            self.add_item(info_btn)
            
            back_btn = discord.ui.Button(label="⬅ Back", style=discord.ButtonStyle.danger, row=0)
            back_btn.callback = self._on_back_to_list
            self.add_item(back_btn)
        
        elif self.mode == "info":
            # Info mode: Back button only
            back_btn = discord.ui.Button(label="⬅ Back", style=discord.ButtonStyle.danger, row=0)
            back_btn.callback = self._on_back_to_song_selected
            self.add_item(back_btn)
        
        else:
            # List mode
            # Row 0: nav buttons (only if needed)
            if self.current_page > 0:
                prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, row=0)
                prev_btn.callback = lambda i: self._change_page(i, -1)
                self.add_item(prev_btn)
            
            if self.current_page < self.total_pages - 1:
                next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, row=0)
                next_btn.callback = lambda i: self._change_page(i, +1)
                self.add_item(next_btn)
            
            # Row 1: numbered selection buttons (only for items that exist)
            total = len(self.songs)
            for slot in range(5):
                global_index = self.current_page * self.per_page + slot
                if global_index >= total:
                    break
                
                btn = discord.ui.Button(label=str(slot + 1), style=discord.ButtonStyle.primary, row=1)
                btn.callback = self._make_song_select_callback(slot)
                self.add_item(btn)

    def _make_song_select_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_song_select(interaction, slot_index)
        return callback

    def _make_playlist_select_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_playlist_select(interaction, slot_index)
        return callback

    async def _on_back_to_list(self, interaction: discord.Interaction) -> None:
        """Switch back to list mode from song selected."""
        self.mode = "list"
        self.selected_song = None
        self.selected_song_index = None
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_back_to_song_selected(self, interaction: discord.Interaction) -> None:
        """Switch back to song selected mode from info or playlist selection."""
        self.mode = "song_selected"
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        """Called when the view times out. Disable buttons or delete the message."""
        try:
            if self.is_ephemeral:
                # Can't delete ephemeral messages, just disable the view
                # Note: We can't edit the message here without an interaction
                pass
            else:
                # Delete the search results message for non-ephemeral
                if self.message:
                    await self.message.delete()
        except discord.errors.NotFound:
            pass  # Message already deleted
        except Exception:
            pass  # Ignore other errors during cleanup



class PlaylistPaginationView(discord.ui.View):
    """Paginated playlists with menu-first UI.
    
    Modes:
    - "menu": Shows 4 action buttons (Queue, Add, Edit, Download)
    - "queue": Shows 1-5 buttons to queue a playlist
    - "add": Shows ➕1-5 buttons to add current song to a playlist
    - "edit_menu": Shows edit action buttons (Rename, Delete, Remove Song, Create)
    - "rename": Shows 1-5 buttons to select a playlist to rename
    - "delete": Shows 1-5 buttons to select a playlist to delete
    - "remove_song": Shows 1-5 buttons to select a playlist to remove songs from
    - "download": Shows 💾1-5 buttons to download a playlist as a ZIP
    - "share": Shows 📤1-5 buttons to share a playlist publicly
    """

    def __init__(
        self,
        *,
        ctx: commands.Context,
        playlists: Dict[str, List[Dict[str, Any]]],
        user: discord.abc.User,
        mode: str = "menu",
        interaction: Optional[discord.Interaction] = None,
    ) -> None:
        super().__init__(timeout=60)
        self.ctx = ctx
        self.user = user
        self.mode = mode
        self.interaction = interaction  # Store for cleanup on timeout
        # Convert dict to list of (name, tracks) tuples for pagination
        self.playlist_items: List[tuple] = list(playlists.items())
        self.per_page = 5
        self.current_page = 0
        self.total_pages = max(1, math.ceil(len(self.playlist_items) / self.per_page))
        self._rebuild_buttons()

    async def on_timeout(self) -> None:
        """Called when the view times out. Delete the ephemeral message."""
        if self.interaction:
            try:
                await self.interaction.delete_original_response()
            except discord.errors.NotFound:
                pass  # Message already deleted
            except Exception:
                pass  # Ignore other errors during cleanup

    def _get_page_playlists(self) -> List[tuple]:
        start = self.current_page * self.per_page
        end = start + self.per_page
        return self.playlist_items[start:end]

    def build_embed(self) -> discord.Embed:
        page_playlists = self._get_page_playlists()
        total = len(self.playlist_items)

        if self.mode == "menu":
            description = f"{total} playlist(s)"
            lines: List[str] = []
            for idx, (name, tracks) in enumerate(page_playlists, start=1):
                count = len(tracks)
                if not count:
                    preview = "(empty)"
                else:
                    preview_titles = [str(t.get("name") or t.get("id") or "?") for t in tracks[:2]]
                    extra = f" +{count - 2} more" if count > 2 else ""
                    preview = ", ".join(preview_titles) + extra
                lines.append(f"**{idx}.** {name} ({count} tracks)\n    {preview}")
            if lines:
                description += "\n\n" + "\n".join(lines)
            footer = "Select an action below."
        elif self.mode == "edit_menu":
            description = f"{total} playlist(s)\n\nSelect an edit action:"
            footer = "Choose what you want to do with your playlists."
        else:
            header = f"Page {self.current_page + 1}/{self.total_pages} • {total} playlist(s)"
            lines = []
            for idx, (name, tracks) in enumerate(page_playlists, start=1):
                count = len(tracks)
                if not count:
                    preview = "(empty)"
                else:
                    preview_titles = [str(t.get("name") or t.get("id") or "?") for t in tracks[:2]]
                    extra = f" +{count - 2} more" if count > 2 else ""
                    preview = ", ".join(preview_titles) + extra
                lines.append(f"**{idx}.** {name} ({count} tracks)\n    {preview}")
            description = header
            if lines:
                description += "\n\n" + "\n".join(lines)
            
            if self.mode == "queue":
                footer = "Press 1–5 to queue that playlist."
            elif self.mode == "add":
                footer = "Press ➕1–5 to add the currently playing song to that playlist."
            elif self.mode == "rename":
                footer = "Press 1–5 to select a playlist to rename."
            elif self.mode == "delete":
                footer = "Press 1–5 to select a playlist to delete."
            elif self.mode == "remove_song":
                footer = "Press 1–5 to select a playlist to manage tracks."
            elif self.mode == "download":
                footer = "Press 💾1–5 to download that playlist as a ZIP file."
            elif self.mode == "share":
                footer = "Press 📤1–5 to share that playlist publicly."
            else:
                footer = ""

        embed = discord.Embed(
            title=f"{getattr(self.user, 'display_name', str(self.user))}'s Playlists",
            description=description,
        )
        embed.set_footer(text=footer)
        return embed

    def _rebuild_buttons(self) -> None:
        """Rebuild buttons based on current mode."""
        self.clear_items()
        total = len(self.playlist_items)
        
        if self.mode == "menu":
            # Menu mode: show 4 action buttons
            queue_btn = discord.ui.Button(label="🎵 Queue Playlist", style=discord.ButtonStyle.primary, row=0)
            queue_btn.callback = self._on_queue_mode
            self.add_item(queue_btn)
            
            add_btn = discord.ui.Button(label="➕ Add to Playlist", style=discord.ButtonStyle.success, row=0)
            add_btn.callback = self._on_add_mode
            self.add_item(add_btn)
            
            share_btn = discord.ui.Button(label="📤 Share Playlist", style=discord.ButtonStyle.secondary, row=0)
            share_btn.callback = self._on_share_mode
            self.add_item(share_btn)
            
            download_btn = discord.ui.Button(label="💾 Download Playlist", style=discord.ButtonStyle.primary, row=1)
            download_btn.callback = self._on_download_mode
            self.add_item(download_btn)
            
            edit_btn = discord.ui.Button(label="✏️ Edit Playlist", style=discord.ButtonStyle.secondary, row=1)
            edit_btn.callback = self._on_edit_mode
            self.add_item(edit_btn)
        elif self.mode == "edit_menu":
            # Edit menu mode: show edit action buttons
            rename_btn = discord.ui.Button(label="📝 Rename Playlist", style=discord.ButtonStyle.primary, row=0)
            rename_btn.callback = self._on_rename_mode
            self.add_item(rename_btn)
            
            delete_btn = discord.ui.Button(label="🗑️ Delete Playlist", style=discord.ButtonStyle.danger, row=0)
            delete_btn.callback = self._on_delete_mode
            self.add_item(delete_btn)
            
            remove_song_btn = discord.ui.Button(label="➖ Remove Song", style=discord.ButtonStyle.secondary, row=1)
            remove_song_btn.callback = self._on_remove_song_mode
            self.add_item(remove_song_btn)
            
            create_btn = discord.ui.Button(label="➕ Create Playlist", style=discord.ButtonStyle.success, row=1)
            create_btn.callback = self._on_create_playlist
            self.add_item(create_btn)
            
            back_btn = discord.ui.Button(label="⬅ Back", style=discord.ButtonStyle.secondary, row=1)
            back_btn.callback = self._on_back
            self.add_item(back_btn)
        else:
            # Selection mode: show pagination + numbered buttons + back
            # Row 0: pagination (only show if navigable)
            if self.current_page > 0:
                prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, row=0)
                prev_btn.callback = lambda i: self._change_page(i, -1)
                self.add_item(prev_btn)
            
            if self.current_page < self.total_pages - 1:
                next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, row=0)
                next_btn.callback = lambda i: self._change_page(i, +1)
                self.add_item(next_btn)
            
            back_btn = discord.ui.Button(label="⬅ Back", style=discord.ButtonStyle.danger, row=0)
            back_btn.callback = self._on_back
            self.add_item(back_btn)
            
            # Row 1: numbered buttons based on mode (only show if slot has a playlist)
            for slot in range(5):
                global_index = self.current_page * self.per_page + slot
                if global_index >= total:
                    break  # No more playlists, stop adding buttons
                
                if self.mode == "queue":
                    label = str(slot + 1)
                    style = discord.ButtonStyle.primary
                    callback = self._make_queue_callback(slot)
                elif self.mode == "add":
                    label = f"➕{slot + 1}"
                    style = discord.ButtonStyle.success
                    callback = self._make_add_callback(slot)
                elif self.mode == "rename":
                    label = f"📝{slot + 1}"
                    style = discord.ButtonStyle.primary
                    callback = self._make_rename_callback(slot)
                elif self.mode == "delete":
                    label = f"🗑️{slot + 1}"
                    style = discord.ButtonStyle.danger
                    callback = self._make_delete_callback(slot)
                elif self.mode == "remove_song":
                    label = f"➖0{slot + 1}"
                    style = discord.ButtonStyle.secondary
                    callback = self._make_remove_song_callback(slot)
                elif self.mode == "download":
                    label = f"💾{slot + 1}"
                    style = discord.ButtonStyle.primary
                    callback = self._make_download_callback(slot)
                elif self.mode == "share":
                    label = f"📤{slot + 1}"
                    style = discord.ButtonStyle.secondary
                    callback = self._make_share_callback(slot)
                else:
                    continue
                
                btn = discord.ui.Button(label=label, style=style, row=1)
                btn.callback = callback
                self.add_item(btn)

    def _make_queue_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_play(interaction, slot_index)
        return callback

    def _make_add_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_add_to_playlist(interaction, slot_index)
        return callback

    def _make_rename_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_rename_playlist(interaction, slot_index)
        return callback

    def _make_delete_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_delete_playlist(interaction, slot_index)
        return callback

    def _make_remove_song_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_remove_song_playlist(interaction, slot_index)
        return callback

    def _make_download_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_download_playlist(interaction, slot_index)
        return callback

    def _make_share_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_share_playlist(interaction, slot_index)
        return callback

    async def _on_queue_mode(self, interaction: discord.Interaction) -> None:
        self.mode = "queue"
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_add_mode(self, interaction: discord.Interaction) -> None:
        self.mode = "add"
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_edit_mode(self, interaction: discord.Interaction) -> None:
        self.mode = "edit_menu"
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_rename_mode(self, interaction: discord.Interaction) -> None:
        self.mode = "rename"
        self.current_page = 0
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_delete_mode(self, interaction: discord.Interaction) -> None:
        self.mode = "delete"
        self.current_page = 0
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_remove_song_mode(self, interaction: discord.Interaction) -> None:
        self.mode = "remove_song"
        self.current_page = 0
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_download_mode(self, interaction: discord.Interaction) -> None:
        self.mode = "download"
        self.current_page = 0
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_share_mode(self, interaction: discord.Interaction) -> None:
        self.mode = "share"
        self.current_page = 0
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_create_playlist(self, interaction: discord.Interaction) -> None:
        """Show modal to create a new playlist."""
        modal = PlaylistCreateModal(self)
        await interaction.response.send_modal(modal)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        self.mode = "menu"
        self.current_page = 0
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _change_page(self, interaction: discord.Interaction, delta: int) -> None:
        new_page = self.current_page + delta
        if new_page < 0 or new_page >= self.total_pages:
            await interaction.response.defer()
            return

        self.current_page = new_page
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _handle_play(self, interaction: discord.Interaction, slot_index: int) -> None:
        """Handle pressing a numbered button (1–5) to play a playlist."""
        global_index = self.current_page * self.per_page + slot_index
        if global_index < 0 or global_index >= len(self.playlist_items):
            await interaction.response.send_message(
                "No playlist in that position.", ephemeral=True
            )
            return

        playlist_name, tracks = self.playlist_items[global_index]

        if not tracks:
            await interaction.response.send_message(
                f"Playlist `{playlist_name}` is empty.", ephemeral=True
            )
            return

        # Defer since playing a playlist can take time
        await interaction.response.defer(ephemeral=True)

        # Check if user is in a voice channel
        user = interaction.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await interaction.followup.send(
                "You need to be in a voice channel to play a playlist.", ephemeral=True
            )
            return

        # Disable radio if active
        if self.ctx.guild:
            _guild_radio_enabled[self.ctx.guild.id] = False

        channel = user.voice.channel
        voice: Optional[discord.VoiceClient] = self.ctx.voice_client

        # Connect or move the bot to the caller's channel
        if voice and voice.is_connected():
            if voice.channel != channel:
                await voice.move_to(channel)
        else:
            voice = await channel.connect()

        queued = 0
        errors = 0
        for track in tracks:
            file_path = track.get("path")
            if not file_path:
                continue

            try:
                api = create_api_client()
                try:
                    result = api.stream_audio_file(file_path)
                finally:
                    api.close()

                if result.get("status") != "success":
                    errors += 1
                    continue

                stream_url = result.get("stream_url")
                if not stream_url:
                    errors += 1
                    continue

                title = track.get("name") or f"Playlist {playlist_name} item"
                metadata = track.get("metadata") or {}
                duration_seconds = _extract_duration_seconds(metadata, track)

                await _queue_or_play_now(
                    self.ctx,
                    stream_url=stream_url,
                    title=str(title),
                    path=file_path,
                    metadata=metadata,
                    duration_seconds=duration_seconds,
                    silent=True,
                )
                queued += 1
            except Exception as e:
                print(f"Error queueing track from playlist: {e}", file=sys.stderr)
                errors += 1
                continue

        if not queued:
            await interaction.followup.send(
                f"Could not queue any tracks from playlist `{playlist_name}`.",
                ephemeral=True,
            )
        else:
            # Update the original message to show "now playing" status
            playing_embed = discord.Embed(
                title="🎵 Now Playing",
                description=f"Playing playlist **{playlist_name}** ({queued} track(s) queued).",
                color=discord.Color.green(),
            )
            playing_embed.set_footer(text="This message will disappear in 5 seconds.")
            try:
                await interaction.edit_original_response(embed=playing_embed, view=None)
                _schedule_interaction_deletion(interaction, 5)
            except Exception:
                # Fallback if edit fails - use ephemeral temporary
                await _send_ephemeral_temporary(
                    interaction,
                    f"🎵 Now playing playlist `{playlist_name}` ({queued} track(s) queued).",
                    delay=5,
                )

    async def _handle_add_to_playlist(self, interaction: discord.Interaction, slot_index: int) -> None:
        """Handle pressing an 'Add to Playlist' button (➕1–5) to add currently playing song."""
        global_index = self.current_page * self.per_page + slot_index
        if global_index < 0 or global_index >= len(self.playlist_items):
            await interaction.response.send_message(
                "No playlist in that position.", ephemeral=True
            )
            _schedule_interaction_deletion(interaction, 5)
            return

        target_playlist_name, target_tracks = self.playlist_items[global_index]

        # Get the currently playing song
        guild = self.ctx.guild
        if not guild:
            await interaction.response.send_message(
                "Guild context unavailable.", ephemeral=True
            )
            _schedule_interaction_deletion(interaction, 5)
            return

        info = _guild_now_playing.get(guild.id)
        if not info:
            await interaction.response.send_message(
                "Nothing is currently playing.", ephemeral=True
            )
            _schedule_interaction_deletion(interaction, 5)
            return

        await interaction.response.defer(ephemeral=True)

        meta = info.get("metadata") or {}
        title = str(info.get("title", meta.get("name") or "Unknown"))
        path = meta.get("path") or info.get("path")
        song_id_val = meta.get("id") or meta.get("song_id")

        user = interaction.user
        playlists = _get_or_create_user_playlists(user.id)
        playlist = playlists.get(target_playlist_name)
        
        if playlist is None:
            msg = await interaction.followup.send(
                f"Playlist `{target_playlist_name}` not found.", ephemeral=True, wait=True
            )
            asyncio.create_task(_delete_later(msg, 5))
            return

        # Avoid duplicates: prefer matching by song ID, then by path.
        already = False
        for track in playlist:
            if song_id_val is not None and track.get("id") == song_id_val:
                already = True
                break
            if path and track.get("path") == path:
                already = True
                break

        if already:
            msg = await interaction.followup.send(
                f"`{title}` is already in playlist `{target_playlist_name}`.", ephemeral=True, wait=True
            )
            asyncio.create_task(_delete_later(msg, 5))
            return

        playlist.append(
            {
                "id": song_id_val,
                "name": title,
                "path": path,
                "metadata": meta,
                "added_at": time.time(),
            }
        )

        _save_user_playlists_to_disk()

        msg = await interaction.followup.send(
            f"Added `{title}` to playlist `{target_playlist_name}`.", ephemeral=True, wait=True
        )
        
        # Delete the playlist view message
        try:
            if interaction.message:
                await interaction.message.delete()
        except Exception:
            pass
        
        # Schedule deletion of confirmation message after 5 seconds
        asyncio.create_task(_delete_later(msg, 5))

    async def _handle_rename_playlist(self, interaction: discord.Interaction, slot_index: int) -> None:
        """Handle renaming a playlist."""
        global_index = self.current_page * self.per_page + slot_index
        if global_index < 0 or global_index >= len(self.playlist_items):
            await interaction.response.send_message(
                "No playlist in that position.", ephemeral=True
            )
            return

        playlist_name, _ = self.playlist_items[global_index]
        modal = PlaylistRenameModalNew(self, playlist_name)
        await interaction.response.send_modal(modal)

    async def _handle_delete_playlist(self, interaction: discord.Interaction, slot_index: int) -> None:
        """Handle deleting a playlist."""
        global_index = self.current_page * self.per_page + slot_index
        if global_index < 0 or global_index >= len(self.playlist_items):
            await interaction.response.send_message(
                "No playlist in that position.", ephemeral=True
            )
            return

        playlist_name, _ = self.playlist_items[global_index]
        user = interaction.user
        playlists = _user_playlists.get(user.id) or {}
        
        if playlist_name in playlists:
            del playlists[playlist_name]
            if not playlists:
                _user_playlists.pop(user.id, None)
            _save_user_playlists_to_disk()
        
        # Refresh playlist items
        self.playlist_items = list(playlists.items())
        self.total_pages = max(1, math.ceil(len(self.playlist_items) / self.per_page))
        if self.current_page >= self.total_pages:
            self.current_page = max(0, self.total_pages - 1)
        
        # Go back to menu if no playlists left
        if not playlists:
            embed = discord.Embed(
                title="Playlists",
                description="You don't have any playlists yet.",
            )
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            self.mode = "menu"
            self._rebuild_buttons()
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    async def _handle_remove_song_playlist(self, interaction: discord.Interaction, slot_index: int) -> None:
        """Handle selecting a playlist to manage (remove songs)."""
        global_index = self.current_page * self.per_page + slot_index
        if global_index < 0 or global_index >= len(self.playlist_items):
            await interaction.response.send_message(
                "No playlist in that position.", ephemeral=True
            )
            return

        playlist_name, tracks = self.playlist_items[global_index]
        user = interaction.user
        
        # Show edit options view for this playlist
        edit_view = PlaylistEditOptionsView(
            ctx=self.ctx,
            user=user,
            playlist_name=playlist_name,
            tracks=tracks,
            parent_view=self,
        )
        embed = edit_view.build_embed()
        await interaction.response.edit_message(embed=embed, view=edit_view)

    async def _handle_download_playlist(self, interaction: discord.Interaction, slot_index: int) -> None:
        """Handle downloading all files in a playlist as a ZIP."""
        global_index = self.current_page * self.per_page + slot_index
        if global_index < 0 or global_index >= len(self.playlist_items):
            await interaction.response.send_message(
                "No playlist in that position.", ephemeral=True
            )
            return

        playlist_name, tracks = self.playlist_items[global_index]

        if not tracks:
            await interaction.response.send_message(
                f"Playlist `{playlist_name}` is empty.", ephemeral=True
            )
            return

        # Defer since creating a ZIP can take time
        await interaction.response.defer(ephemeral=True)

        # Collect all file paths from the playlist
        file_paths: List[str] = []
        for track in tracks:
            path = track.get("path")
            if path:
                file_paths.append(path)

        if not file_paths:
            await interaction.followup.send(
                f"No valid file paths found in playlist `{playlist_name}`.",
                ephemeral=True,
            )
            return

        # Send status message
        status_embed = discord.Embed(
            title="💾 Packing Playlist",
            description=f"Packing **{playlist_name}** to ZIP...\n{len(file_paths)} file(s) to pack",
            color=discord.Color.blue(),
        )
        status_msg = await interaction.followup.send(
            embed=status_embed,
            ephemeral=True,
            wait=True,
        )
        
        # Close the original playlist view message
        try:
            if interaction.message:
                await interaction.message.delete()
        except Exception:
            pass

        try:
            # Use the API to create a ZIP file
            api = create_api_client()
            try:
                zip_content = api.create_zip(file_paths)
            finally:
                api.close()

            # Send the ZIP file to the user
            zip_file = discord.File(
                io.BytesIO(zip_content),
                filename=f"{playlist_name}.zip"
            )
            
            await interaction.followup.send(
                f"Here's your playlist **{playlist_name}** ({len(file_paths)} file(s)):",
                file=zip_file,
                ephemeral=True,
            )
            
            # Delete the status message
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as e:
            # Update status message to show error
            error_embed = discord.Embed(
                title="❌ Error",
                description=f"Failed to create ZIP file: {str(e)}",
                color=discord.Color.red(),
            )
            try:
                await status_msg.edit(embed=error_embed)
            except Exception:
                await interaction.followup.send(
                    f"Failed to create ZIP file: {str(e)}",
                    ephemeral=True,
                )

    async def _handle_share_playlist(self, interaction: discord.Interaction, slot_index: int) -> None:
        """Handle sharing a playlist publicly."""
        global_index = self.current_page * self.per_page + slot_index
        if global_index < 0 or global_index >= len(self.playlist_items):
            await interaction.response.send_message(
                "No playlist in that position.", ephemeral=True
            )
            return

        playlist_name, tracks = self.playlist_items[global_index]

        if not tracks:
            await interaction.response.send_message(
                f"Playlist `{playlist_name}` is empty.", ephemeral=True
            )
            return

        # Close the ephemeral playlist view
        try:
            if self.interaction:
                await self.interaction.delete_original_response()
        except Exception:
            pass

        # Create a public shared playlist view
        shared_view = SharedPlaylistView(
            ctx=self.ctx,
            owner=self.user,
            playlist_name=playlist_name,
            tracks=list(tracks),  # Copy to avoid mutation issues
        )
        embed = shared_view.build_embed()

        # Send the public message (not ephemeral)
        await interaction.response.send_message(embed=embed, view=shared_view)
        shared_view.message = await interaction.original_response()


class SharedPlaylistView(discord.ui.View):
    """Public view for a shared playlist.
    
    Anyone can interact with this view to:
    - Navigate through tracks
    - Copy the playlist to their own playlists
    - Download the playlist as a ZIP
    - Queue the playlist for playback
    """

    def __init__(
        self,
        *,
        ctx: commands.Context,
        owner: discord.abc.User,
        playlist_name: str,
        tracks: List[Dict[str, Any]],
    ) -> None:
        super().__init__(timeout=120)  # 2 minutes timeout
        self.ctx = ctx
        self.owner = owner
        self.playlist_name = playlist_name
        self.tracks = tracks
        self.per_page = 10
        self.current_page = 0
        self.total_pages = max(1, math.ceil(len(self.tracks) / self.per_page))
        self.message: Optional[discord.Message] = None
        self._rebuild_buttons()

    def build_embed(self) -> discord.Embed:
        total = len(self.tracks)
        owner_name = getattr(self.owner, 'display_name', str(self.owner))
        
        header = f"**{self.playlist_name}** by {owner_name}\n{total} track(s)"
        
        if total == 0:
            description = header + "\n\n(empty playlist)"
        else:
            start = self.current_page * self.per_page
            end = start + self.per_page
            page_tracks = self.tracks[start:end]
            
            lines: List[str] = []
            for idx, track in enumerate(page_tracks, start=start + 1):
                name = track.get("name") or track.get("id") or "Unknown"
                lines.append(f"`{idx}.` {name}")
            
            description = header
            if self.total_pages > 1:
                description += f"\nPage {self.current_page + 1}/{self.total_pages}"
            description += "\n\n" + "\n".join(lines)

        embed = discord.Embed(
            title="🎵 Shared Playlist",
            description=description,
            color=discord.Color.purple(),
        )
        embed.set_footer(text="Use the buttons below to interact with this playlist.")
        return embed

    def _rebuild_buttons(self) -> None:
        self.clear_items()
        
        # Row 0: Navigation
        prev_btn = discord.ui.Button(
            label="◀", 
            style=discord.ButtonStyle.secondary, 
            row=0, 
            disabled=self.current_page == 0
        )
        prev_btn.callback = lambda i: self._change_page(i, -1)
        self.add_item(prev_btn)
        
        next_btn = discord.ui.Button(
            label="▶", 
            style=discord.ButtonStyle.secondary, 
            row=0, 
            disabled=self.current_page >= self.total_pages - 1
        )
        next_btn.callback = lambda i: self._change_page(i, +1)
        self.add_item(next_btn)
        
        # Row 1: Action buttons
        queue_btn = discord.ui.Button(
            label="🎵 Queue Playlist", 
            style=discord.ButtonStyle.primary, 
            row=1
        )
        queue_btn.callback = self._on_queue
        self.add_item(queue_btn)
        
        copy_btn = discord.ui.Button(
            label="📋 Copy Playlist", 
            style=discord.ButtonStyle.success, 
            row=1
        )
        copy_btn.callback = self._on_copy
        self.add_item(copy_btn)
        
        download_btn = discord.ui.Button(
            label="💾 Download", 
            style=discord.ButtonStyle.secondary, 
            row=1
        )
        download_btn.callback = self._on_download
        self.add_item(download_btn)

    async def _change_page(self, interaction: discord.Interaction, delta: int) -> None:
        new_page = self.current_page + delta
        if new_page < 0 or new_page >= self.total_pages:
            await interaction.response.defer()
            return
        self.current_page = new_page
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_queue(self, interaction: discord.Interaction) -> None:
        """Queue all tracks from this playlist for playback."""
        user = interaction.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await interaction.response.send_message(
                "You need to be in a voice channel to queue this playlist.", 
                ephemeral=True
            )
            return

        if not self.tracks:
            await interaction.response.send_message(
                "This playlist is empty.", 
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Disable radio if active
        if self.ctx.guild:
            _guild_radio_enabled[self.ctx.guild.id] = False

        channel = user.voice.channel
        voice: Optional[discord.VoiceClient] = self.ctx.voice_client

        # Connect or move the bot to the caller's channel
        if voice and voice.is_connected():
            if voice.channel != channel:
                await voice.move_to(channel)
        else:
            voice = await channel.connect()

        queued = 0
        for track in self.tracks:
            file_path = track.get("path")
            if not file_path:
                continue

            api = create_api_client()
            try:
                result = api.stream_audio_file(file_path)
            finally:
                api.close()

            if result.get("status") != "success":
                continue

            stream_url = result.get("stream_url")
            if not stream_url:
                continue

            title = track.get("name") or f"Track from {self.playlist_name}"
            metadata = track.get("metadata") or {}
            duration_seconds = _extract_duration_seconds(metadata, track)

            await _queue_or_play_now(
                self.ctx,
                stream_url=stream_url,
                title=str(title),
                path=file_path,
                metadata=metadata,
                duration_seconds=duration_seconds,
                silent=True,
            )
            queued += 1

        if not queued:
            await interaction.followup.send(
                f"Could not queue any tracks from this playlist.",
                ephemeral=True,
            )
        else:
            queue_msg = await interaction.followup.send(
                f"🎵 Queued **{self.playlist_name}** ({queued} track(s)).",
                ephemeral=True,
                wait=True,
            )
            # Auto-delete the queue confirmation after 5 seconds
            asyncio.create_task(_delete_later(queue_msg, 5))
        # Delete the shared playlist message after 120 seconds (only on success)
            await self._schedule_message_deletion()

    async def _on_copy(self, interaction: discord.Interaction) -> None:
        """Copy this playlist to the user's own playlists."""
        user = interaction.user
        user_playlists = _get_or_create_user_playlists(user.id)
        
        # Generate a unique name if there's a conflict
        base_name = self.playlist_name
        new_name = base_name
        counter = 1
        while new_name in user_playlists:
            new_name = f"{base_name} ({counter})"
            counter += 1
        
        # Deep copy the tracks
        copied_tracks = []
        for track in self.tracks:
            copied_tracks.append({
                "id": track.get("id"),
                "name": track.get("name"),
                "path": track.get("path"),
                "metadata": track.get("metadata", {}),
                "added_at": time.time(),
            })
        
        user_playlists[new_name] = copied_tracks
        _save_user_playlists_to_disk()
        
        await interaction.response.send_message(
            f"📋 Copied **{self.playlist_name}** to your playlists as **{new_name}** ({len(copied_tracks)} track(s)).",
            ephemeral=True,
        )

        # Delete the shared playlist message after 120 seconds
        await self._schedule_message_deletion()

    async def _on_download(self, interaction: discord.Interaction) -> None:
        """Download all files in this playlist as a ZIP."""
        if not self.tracks:
            await interaction.response.send_message(
                "This playlist is empty.", 
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Collect all file paths
        file_paths: List[str] = []
        for track in self.tracks:
            path = track.get("path")
            if path:
                file_paths.append(path)

        if not file_paths:
            await interaction.followup.send(
                "No valid file paths found in this playlist.",
                ephemeral=True,
            )
            return

        # Send status message
        status_embed = discord.Embed(
            title="💾 Packing Playlist",
            description=f"Packing **{self.playlist_name}** to ZIP...\n{len(file_paths)} file(s) to pack",
            color=discord.Color.blue(),
        )
        status_msg = await interaction.followup.send(
            embed=status_embed,
            ephemeral=True,
            wait=True,
        )

        try:
            api = create_api_client()
            try:
                zip_content = api.create_zip(file_paths)
            finally:
                api.close()

            zip_file = discord.File(
                io.BytesIO(zip_content),
                filename=f"{self.playlist_name}.zip"
            )
            
            await interaction.followup.send(
                f"Here's **{self.playlist_name}** ({len(file_paths)} file(s)):",
                file=zip_file,
                ephemeral=True,
            )
            
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as e:
            error_embed = discord.Embed(
                title="❌ Error",
                description=f"Failed to create ZIP file: {str(e)}",
                color=discord.Color.red(),
            )
            try:
                await status_msg.edit(embed=error_embed)
            except Exception:
                await interaction.followup.send(
                    f"Failed to create ZIP file: {str(e)}",
                    ephemeral=True,
                )

        # Delete the shared playlist message after 120 seconds
        await self._schedule_message_deletion()

    async def _schedule_message_deletion(self) -> None:
        """Schedule the shared playlist message to be deleted after 120 seconds."""
        if self.message:
            asyncio.create_task(_delete_later(self.message, 120))
            self.stop()  # Stop the view to prevent further interactions

    async def on_timeout(self) -> None:
        """Called when the view times out. Delete the message."""
        if self.message:
            try:
                await self.message.delete()
            except discord.errors.NotFound:
                pass
            except Exception:
                pass


class PlaylistEditOptionsView(discord.ui.View):
    """View for editing a specific playlist (rename, delete, remove tracks)."""

    def __init__(
        self,
        *,
        ctx: commands.Context,
        user: discord.abc.User,
        playlist_name: str,
        tracks: List[Dict[str, Any]],
        parent_view: "PlaylistPaginationView",
    ) -> None:
        super().__init__(timeout=120)
        self.ctx = ctx
        self.user = user
        self.playlist_name = playlist_name
        self.tracks = tracks
        self.parent_view = parent_view
        self.current_page = 0
        self.per_page = 5
        self.total_pages = max(1, math.ceil(len(self.tracks) / self.per_page))
        self._rebuild_buttons()

    def build_embed(self) -> discord.Embed:
        total = len(self.tracks)
        header = f"Editing: **{self.playlist_name}** ({total} track(s))"
        
        if total == 0:
            description = header + "\n\n(empty playlist)"
        else:
            start = self.current_page * self.per_page
            end = start + self.per_page
            page_tracks = self.tracks[start:end]
            
            lines: List[str] = []
            for idx, track in enumerate(page_tracks, start=start + 1):
                name = track.get("name") or track.get("id") or "Unknown"
                lines.append(f"**{idx}.** {name}")
            
            description = header
            if self.total_pages > 1:
                description += f"\nPage {self.current_page + 1}/{self.total_pages}"
            description += "\n\n" + "\n".join(lines)

        embed = discord.Embed(
            title=f"Edit Playlist",
            description=description,
        )
        embed.set_footer(text="🗑️1-5 removes that track. Use Rename/Delete for playlist actions.")
        return embed

    def _rebuild_buttons(self) -> None:
        self.clear_items()
        total = len(self.tracks)
        
        # Row 0: Back, Rename, Delete playlist
        back_btn = discord.ui.Button(label="⬅ Back", style=discord.ButtonStyle.secondary, row=0)
        back_btn.callback = self._on_back
        self.add_item(back_btn)
        
        rename_btn = discord.ui.Button(label="Rename", style=discord.ButtonStyle.primary, row=0)
        rename_btn.callback = self._on_rename
        self.add_item(rename_btn)
        
        delete_btn = discord.ui.Button(label="Delete Playlist", style=discord.ButtonStyle.danger, row=0)
        delete_btn.callback = self._on_delete_playlist
        self.add_item(delete_btn)
        
        # Row 1: Pagination if needed
        if self.total_pages > 1:
            prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, row=1, disabled=self.current_page == 0)
            prev_btn.callback = lambda i: self._change_page(i, -1)
            self.add_item(prev_btn)
            
            next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, row=1, disabled=self.current_page >= self.total_pages - 1)
            next_btn.callback = lambda i: self._change_page(i, +1)
            self.add_item(next_btn)
        
        # Row 2: Remove track buttons (🗑️1-5)
        for slot in range(5):
            global_index = self.current_page * self.per_page + slot
            disabled = global_index >= total
            
            btn = discord.ui.Button(label=f"🗑️{slot + 1}", style=discord.ButtonStyle.danger, row=2, disabled=disabled)
            btn.callback = self._make_remove_callback(slot)
            self.add_item(btn)

    def _make_remove_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_remove_track(interaction, slot_index)
        return callback

    async def _change_page(self, interaction: discord.Interaction, delta: int) -> None:
        new_page = self.current_page + delta
        if new_page < 0 or new_page >= self.total_pages:
            await interaction.response.defer()
            return
        self.current_page = new_page
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        # Refresh the parent view's playlist data and go back
        user_playlists = _user_playlists.get(self.user.id) or {}
        self.parent_view.playlist_items = list(user_playlists.items())
        self.parent_view.total_pages = max(1, math.ceil(len(self.parent_view.playlist_items) / self.parent_view.per_page))
        self.parent_view.mode = "menu"
        self.parent_view._rebuild_buttons()
        embed = self.parent_view.build_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)

    async def _on_rename(self, interaction: discord.Interaction) -> None:
        # Send a modal to get the new name
        modal = PlaylistRenameModal(self)
        await interaction.response.send_modal(modal)

    async def _on_delete_playlist(self, interaction: discord.Interaction) -> None:
        user = interaction.user
        playlists = _user_playlists.get(user.id) or {}
        
        if self.playlist_name in playlists:
            del playlists[self.playlist_name]
            if not playlists:
                _user_playlists.pop(user.id, None)
            _save_user_playlists_to_disk()
        
        # Go back to parent view with refreshed data
        user_playlists = _user_playlists.get(user.id) or {}
        self.parent_view.playlist_items = list(user_playlists.items())
        self.parent_view.total_pages = max(1, math.ceil(len(self.parent_view.playlist_items) / self.parent_view.per_page))
        self.parent_view.mode = "menu"
        self.parent_view._rebuild_buttons()
        
        if not user_playlists:
            embed = discord.Embed(
                title="Playlists",
                description="You don't have any playlists yet.",
            )
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            embed = self.parent_view.build_embed()
            await interaction.response.edit_message(embed=embed, view=self.parent_view)

    async def _handle_remove_track(self, interaction: discord.Interaction, slot_index: int) -> None:
        global_index = self.current_page * self.per_page + slot_index
        if global_index < 0 or global_index >= len(self.tracks):
            await interaction.response.send_message("No track in that position.", ephemeral=True)
            return
        
        user = interaction.user
        playlists = _user_playlists.get(user.id) or {}
        playlist = playlists.get(self.playlist_name)
        
        if playlist is None or global_index >= len(playlist):
            await interaction.response.send_message("Track not found.", ephemeral=True)
            return
        
        removed_track = playlist.pop(global_index)
        self.tracks = playlist  # Update local reference
        _save_user_playlists_to_disk()
        
        # Recalculate pages
        self.total_pages = max(1, math.ceil(len(self.tracks) / self.per_page))
        if self.current_page >= self.total_pages:
            self.current_page = max(0, self.total_pages - 1)
        
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)


class PlaylistRenameModal(discord.ui.Modal, title="Rename Playlist"):
    """Modal for renaming a playlist."""
    
    new_name = discord.ui.TextInput(
        label="New Playlist Name",
        placeholder="Enter new name...",
        max_length=100,
    )

    def __init__(self, edit_view: PlaylistEditOptionsView) -> None:
        super().__init__()
        self.edit_view = edit_view
        self.new_name.default = edit_view.playlist_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_name = self.new_name.value.strip()
        if not new_name:
            await interaction.response.send_message("Name cannot be empty.", ephemeral=True)
            return
        
        user = interaction.user
        playlists = _user_playlists.get(user.id) or {}
        old_name = self.edit_view.playlist_name
        
        if new_name == old_name:
            await interaction.response.defer()
            return
        
        if new_name in playlists:
            await interaction.response.send_message(f"A playlist named `{new_name}` already exists.", ephemeral=True)
            return
        
        if old_name in playlists:
            playlists[new_name] = playlists.pop(old_name)
            _save_user_playlists_to_disk()
        
        # Update the edit view
        self.edit_view.playlist_name = new_name
        self.edit_view.tracks = playlists.get(new_name, [])
        
        # Also update parent view's playlist items
        self.edit_view.parent_view.playlist_items = list(playlists.items())
        
        embed = self.edit_view.build_embed()
        await interaction.response.edit_message(embed=embed, view=self.edit_view)


class PlaylistCreateModal(discord.ui.Modal, title="Create New Playlist"):
    """Modal for creating a new playlist."""
    
    playlist_name = discord.ui.TextInput(
        label="Playlist Name",
        placeholder="Enter playlist name...",
        max_length=100,
    )

    def __init__(self, pagination_view: PlaylistPaginationView) -> None:
        super().__init__()
        self.pagination_view = pagination_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = self.playlist_name.value.strip()
        if not name:
            await interaction.response.send_message("Name cannot be empty.", ephemeral=True)
            return
        
        user = interaction.user
        playlists = _get_or_create_user_playlists(user.id)
        
        if name in playlists:
            await interaction.response.send_message(
                f"You already have a playlist named `{name}`.", ephemeral=True
            )
            return
        
        playlists[name] = []
        _save_user_playlists_to_disk()
        
        # Refresh the view
        self.pagination_view.playlist_items = list(playlists.items())
        self.pagination_view.total_pages = max(1, math.ceil(len(self.pagination_view.playlist_items) / self.pagination_view.per_page))
        self.pagination_view.mode = "menu"
        self.pagination_view._rebuild_buttons()
        
        embed = self.pagination_view.build_embed()
        await interaction.response.edit_message(embed=embed, view=self.pagination_view)


class PlaylistRenameModalNew(discord.ui.Modal, title="Rename Playlist"):
    """Modal for renaming a playlist from the pagination view."""
    
    new_name = discord.ui.TextInput(
        label="New Playlist Name",
        placeholder="Enter new name...",
        max_length=100,
    )

    def __init__(self, pagination_view: PlaylistPaginationView, old_name: str) -> None:
        super().__init__()
        self.pagination_view = pagination_view
        self.old_name = old_name
        self.new_name.default = old_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_name = self.new_name.value.strip()
        if not new_name:
            await interaction.response.send_message("Name cannot be empty.", ephemeral=True)
            return
        
        user = interaction.user
        playlists = _user_playlists.get(user.id) or {}
        
        if new_name == self.old_name:
            await interaction.response.defer()
            return
        
        if new_name in playlists:
            await interaction.response.send_message(f"A playlist named `{new_name}` already exists.", ephemeral=True)
            return
        
        if self.old_name in playlists:
            playlists[new_name] = playlists.pop(self.old_name)
            _save_user_playlists_to_disk()
        
        # Refresh the view
        self.pagination_view.playlist_items = list(playlists.items())
        self.pagination_view.total_pages = max(1, math.ceil(len(self.pagination_view.playlist_items) / self.pagination_view.per_page))
        self.pagination_view.mode = "menu"
        self.pagination_view._rebuild_buttons()
        
        embed = self.pagination_view.build_embed()
        await interaction.response.edit_message(embed=embed, view=self.pagination_view)


class NowPlayingInfoView(discord.ui.View):
    """Ephemeral view for extra track info (lyrics/snippets) shown from ℹ button."""

    def __init__(self) -> None:
        super().__init__(timeout=60)

    @discord.ui.button(label="Lyrics", style=discord.ButtonStyle.secondary)
    async def lyrics_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:  # pragma: no cover - UI callback
        """Show lyrics for the currently playing track in a separate embed."""

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "Guild context unavailable.", ephemeral=True
            )
            return

        info = _guild_now_playing.get(guild.id)
        if not info:
            await interaction.response.send_message(
                "Nothing is currently tracked as playing.", ephemeral=True
            )
            return

        title = str(info.get("title", "Unknown"))
        meta = info.get("metadata") or {}
        lyrics = meta.get("lyrics")

        if not lyrics:
            await interaction.response.send_message(
                "No lyrics are stored for this song.", ephemeral=True
            )
            return

        text = str(lyrics)
        # Discord embed description max is 4096 characters.
        text = text[:4096]

        embed = discord.Embed(title=f"Lyrics - {title}", description=text)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Snippets", style=discord.ButtonStyle.secondary)
    async def snippets_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:  # pragma: no cover - UI callback
        """Show snippets for the currently playing track in a separate embed."""

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "Guild context unavailable.", ephemeral=True
            )
            return

        info = _guild_now_playing.get(guild.id)
        if not info:
            await interaction.response.send_message(
                "Nothing is currently tracked as playing.", ephemeral=True
            )
            return

        title = str(info.get("title", "Unknown"))
        meta = info.get("metadata") or {}
        snippets = meta.get("snippets") or []

        if not snippets:
            await interaction.response.send_message(
                "No snippets are stored for this song.", ephemeral=True
            )
            return

        lines: List[str] = []
        if isinstance(snippets, (list, tuple)):
            for snip in snippets:
                if isinstance(snip, dict):
                    label = (
                        snip.get("label")
                        or snip.get("name")
                        or snip.get("id")
                        or str(snip)
                    )
                    lines.append(f"- {label}")
                else:
                    lines.append(f"- {snip}")
        else:
            lines.append(str(snippets))

        body = "\n".join(lines)
        body = body[:4096]

        embed = discord.Embed(title=f"Snippets - {title}", description=body)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class PlayerView(discord.ui.View):
    """Discord UI controls for playback (pause/resume, stop, skip, now playing)."""

    def __init__(self, *, ctx: commands.Context, is_radio: bool) -> None:
        super().__init__(timeout=None)
        self.ctx = ctx
        self.is_radio = is_radio

        # Hide radio button if radio is already on
        if is_radio:
            for child in self.children[:]:
                if isinstance(child, discord.ui.Button) and child.label == "📻 Radio":
                    self.remove_item(child)
                    break

    async def _get_voice(self) -> Optional[discord.VoiceClient]:
        return self.ctx.voice_client

    @discord.ui.button(label="⏯ Pause/Resume", style=discord.ButtonStyle.primary)
    async def pause_resume(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:  # pragma: no cover - UI callback
        await interaction.response.defer(ephemeral=True)
        voice = await self._get_voice()
        guild = self.ctx.guild
        
        if not voice:
            await _send_ephemeral_temporary(interaction, "No active playback.")
            return

        if voice.is_playing():
            voice.pause()
            # Track when we paused
            if guild:
                info = _guild_now_playing.get(guild.id, {})
                info["paused_at"] = time.time()
                _guild_now_playing[guild.id] = info
            await _send_ephemeral_temporary(interaction, "Paused playback.")
        elif voice.is_paused():
            voice.resume()
            # Add paused duration to total and clear paused_at
            if guild:
                info = _guild_now_playing.get(guild.id, {})
                paused_at = info.get("paused_at")
                if paused_at:
                    paused_duration = time.time() - paused_at
                    info["total_paused_time"] = info.get("total_paused_time", 0) + paused_duration
                info["paused_at"] = None
                _guild_now_playing[guild.id] = info
            await _send_ephemeral_temporary(interaction, "Resumed playback.")
        else:
            await _send_ephemeral_temporary(interaction, "Nothing is currently playing.")

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger)
    async def stop_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:  # pragma: no cover - UI callback
        await interaction.response.defer(ephemeral=True)
        voice = await self._get_voice()
        guild = self.ctx.guild

        if guild:
            _guild_radio_enabled[guild.id] = False
            # Clear the queue and pre-fetched radio song
            _guild_queue[guild.id] = []
            _guild_radio_next.pop(guild.id, None)

        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()

        if guild:
            # Mark the shared player as idle but keep the message for reuse.
            await _send_player_controls(
                self.ctx,
                title="Nothing playing",
                path=None,
                is_radio=False,
                metadata={},
                duration_seconds=None,
            )

        await _send_ephemeral_temporary(interaction, "Stopped playback.")

    @discord.ui.button(label="⏮ Rewind", style=discord.ButtonStyle.secondary)
    async def rewind_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:  # pragma: no cover - UI callback
        """Play the previously played song again."""
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild

        if not guild:
            await _send_ephemeral_temporary(interaction, "Guild context unavailable.")
            return

        prev_song = _guild_previous_song.get(guild.id)
        if not prev_song:
            await _send_ephemeral_temporary(interaction, "No previous song to replay.")
            return

        path = prev_song.get("path")
        if not path:
            await _send_ephemeral_temporary(interaction, "Previous song has no path to replay.")
            return

        # Get stream URL for the previous song
        api = create_api_client()
        try:
            stream_result = api.stream_audio_file(path)
        finally:
            api.close()

        if stream_result.get("status") != "success":
            await _send_ephemeral_temporary(
                interaction, f"Could not stream previous song: {stream_result.get('error', 'unknown error')}"
            )
            return

        stream_url = stream_result.get("stream_url")
        if not stream_url:
            await _send_ephemeral_temporary(interaction, "No stream URL available for previous song.")
            return

        title = prev_song.get("title", "Unknown")
        metadata = prev_song.get("metadata", {})
        duration_seconds = prev_song.get("duration_seconds")

        # For radio mode, disable radio and play the previous song
        if self.is_radio:
            _guild_radio_enabled[guild.id] = False
            _guild_radio_next.pop(guild.id, None)

        # Stop current playback
        voice = await self._get_voice()
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()

        # Play the previous song using _queue_or_play_now
        await _queue_or_play_now(
            self.ctx,
            stream_url=stream_url,
            title=title,
            path=path,
            metadata=metadata,
            duration_seconds=duration_seconds,
        )

        await _send_ephemeral_temporary(interaction, f"⏮ Replaying: {title}")

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.secondary)
    async def skip_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:  # pragma: no cover - UI callback
        await interaction.response.defer(ephemeral=True)
        voice = await self._get_voice()
        guild = self.ctx.guild

        if not voice or not guild:
            await _send_ephemeral_temporary(interaction, "Nothing to skip.")
            return

        if self.is_radio:
            # In radio mode, stopping the current track will trigger the
            # after-callback to queue the next random song.
            if voice.is_playing() or voice.is_paused():
                voice.stop()
            await _send_ephemeral_temporary(interaction, "Skipping to the next radio track...")
        else:
            if voice.is_playing() or voice.is_paused():
                voice.stop()
            # Do not delete the player; the queue callback will either
            # start the next track or mark the player idle.
            await _send_ephemeral_temporary(interaction, "Skipped current track.")

    @discord.ui.button(label="🔀 Shuffle", style=discord.ButtonStyle.secondary)
    async def shuffle_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:  # pragma: no cover - UI callback
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild
        
        if not guild:
            await _send_ephemeral_temporary(interaction, "Guild context unavailable.")
            return
        
        queue = _guild_queue.get(guild.id, [])
        if len(queue) < 2:
            await _send_ephemeral_temporary(interaction, "Not enough songs in queue to shuffle.")
            return
        
        random.shuffle(queue)
        _guild_queue[guild.id] = queue
        
        # Update the player embed to reflect the new queue order
        info = _guild_now_playing.get(guild.id, {})
        await _send_player_controls(
            self.ctx,
            title=info.get("title", "Unknown"),
            path=info.get("path"),
            is_radio=self.is_radio,
            metadata=info.get("metadata", {}),
            duration_seconds=info.get("duration_seconds"),
        )
        
        await _send_ephemeral_temporary(interaction, f"🔀 Shuffled {len(queue)} tracks in queue.")

    @discord.ui.button(label="ℹ Now Playing", style=discord.ButtonStyle.secondary)
    async def now_playing_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:  # pragma: no cover - UI callback
        guild = self.ctx.guild
        if not guild:
            await interaction.response.send_message(
                "Guild context unavailable.", ephemeral=True
            )
            return

        info = _guild_now_playing.get(guild.id)
        if not info:
            await interaction.response.send_message(
                "Nothing is currently tracked as playing.", ephemeral=True
            )
            return

        title = str(info.get("title", "Unknown"))
        path = info.get("path")
        meta = info.get("metadata") or {}

        embed = discord.Embed(title="Now Playing", description=title)

        # Album art / era image
        image_url = meta.get("image_url")
        if image_url:
            embed.set_thumbnail(url=image_url)

        # --- Identity / IDs ---
        song_id_val = meta.get("id") or meta.get("song_id")
        if song_id_val is not None:
            embed.add_field(name="ID", value=str(song_id_val), inline=True)

        if meta.get("public_id") is not None:
            embed.add_field(name="Public ID", value=str(meta.get("public_id")), inline=True)

        if meta.get("original_key"):
            embed.add_field(name="Original Key", value=str(meta.get("original_key")), inline=True)

        # --- Category / path ---
        if meta.get("category"):
            embed.add_field(name="Category", value=str(meta.get("category")), inline=True)

        # Prefer the path from metadata; fall back to stored path.
        full_path = meta.get("path") or path
        if full_path:
            embed.add_field(name="Path", value=f"`{full_path}`", inline=False)

        # --- Era details ---
        era_data = meta.get("era")
        era_name = None
        era_desc = None
        era_time_frame = None
        era_play_count = None

        if isinstance(era_data, dict):
            era_name = era_data.get("name")
            era_desc = era_data.get("description")
            era_time_frame = era_data.get("time_frame")
            era_play_count = era_data.get("play_count")
        elif era_data:
            # Backwards-compat: older metadata stored era as a simple string.
            era_name = str(era_data)

        if any([era_name, era_desc, era_time_frame, era_play_count]):
            lines: list[str] = []
            if era_name:
                lines.append(f"Name: {era_name}")
            if era_desc:
                lines.append(f"Description: {era_desc}")
            if era_time_frame:
                lines.append(f"Time frame: {era_time_frame}")
            if era_play_count is not None:
                lines.append(f"Play count: {era_play_count}")
            embed.add_field(name="Era", value="\n".join(lines)[:1024], inline=False)

        # --- Titles / tracking ---
        track_titles = meta.get("track_titles") or []
        if isinstance(track_titles, (list, tuple)) and track_titles:
            embed.add_field(
                name="Track titles",
                value=", ".join(map(str, track_titles))[:1024],
                inline=False,
            )

        if meta.get("session_titles") or meta.get("session_tracking"):
            sess_lines = []
            if meta.get("session_titles"):
                sess_lines.append(f"Titles: {meta.get('session_titles')}")
            if meta.get("session_tracking"):
                sess_lines.append(f"Tracking: {meta.get('session_tracking')}")
            embed.add_field(name="Sessions", value="\n".join(sess_lines)[:1024], inline=False)

        # --- Credits ---
        credits_lines = []
        if meta.get("credited_artists"):
            credits_lines.append(f"Artists: {meta.get('credited_artists')}")
        if meta.get("producers"):
            credits_lines.append(f"Producers: {meta.get('producers')}")
        if meta.get("engineers"):
            credits_lines.append(f"Engineers: {meta.get('engineers')}")
        if credits_lines:
            embed.add_field(
                name="Credits",
                value="\n".join(credits_lines)[:1024],
                inline=False,
            )

        # --- Recording details ---
        rec_lines = []
        if meta.get("recording_locations"):
            rec_lines.append(f"Locations: {meta.get('recording_locations')}")
        if meta.get("record_dates"):
            rec_lines.append(f"Record dates: {meta.get('record_dates')}")
        if meta.get("dates"):
            rec_lines.append(f"Additional dates: {meta.get('dates')}")
        if rec_lines:
            embed.add_field(
                name="Recording",
                value="\n".join(rec_lines)[:1024],
                inline=False,
            )

        # --- Audio / technical ---
        audio_lines = []
        if meta.get("length"):
            audio_lines.append(f"Length: {meta.get('length')}")
        if meta.get("bitrate"):
            audio_lines.append(f"Bitrate: {meta.get('bitrate')}")
        if meta.get("instrumentals"):
            audio_lines.append(f"Instrumentals: {meta.get('instrumentals')}")
        if meta.get("instrumental_names"):
            audio_lines.append(f"Instrumental names: {meta.get('instrumental_names')}")
        if audio_lines:
            embed.add_field(
                name="Audio",
                value="\n".join(audio_lines)[:1024],
                inline=False,
            )

        # --- Files ---
        if meta.get("file_names"):
            embed.add_field(
                name="File names",
                value=str(meta.get("file_names"))[:1024],
                inline=False,
            )

        # --- Release / leak info ---
        release_lines = []
        if meta.get("preview_date"):
            release_lines.append(f"Preview date: {meta.get('preview_date')}")
        if meta.get("release_date"):
            release_lines.append(f"Release date: {meta.get('release_date')}")
        if meta.get("date_leaked"):
            release_lines.append(f"Leak date: {meta.get('date_leaked')}")
        if meta.get("leak_type"):
            release_lines.append(f"Leak type: {meta.get('leak_type')}")
        if release_lines:
            embed.add_field(
                name="Release / Leak",
                value="\n".join(release_lines)[:1024],
                inline=False,
            )

        # --- Additional information ---
        if meta.get("additional_information"):
            embed.add_field(
                name="Additional information",
                value=str(meta.get("additional_information"))[:1024],
                inline=False,
            )

        # --- Notes ---
        if meta.get("notes"):
            embed.add_field(
                name="Notes",
                value=str(meta.get("notes"))[:1024],
                inline=False,
            )

        # Radio mode indicator in the footer
        embed.set_footer(
            text="Radio" if bool(info.get("is_radio")) else "On-demand playback"
        )

        # Attach a temporary info view so the user can access lyrics/snippets
        # from this Now Playing snapshot only.
        info_view = NowPlayingInfoView()
        await interaction.response.send_message(embed=embed, view=info_view, ephemeral=True)

    @discord.ui.button(label="❤ Like", style=discord.ButtonStyle.success)
    async def like_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:  # pragma: no cover - UI callback
        """Add the currently playing song to the user's Likes playlist."""

        await interaction.response.defer(ephemeral=True)

        guild = self.ctx.guild
        if not guild:
            await _send_ephemeral_temporary(interaction, "Guild context unavailable.")
            return

        info = _guild_now_playing.get(guild.id)
        if not info:
            await _send_ephemeral_temporary(interaction, "Nothing is currently tracked as playing.")
            return

        meta = info.get("metadata") or {}
        title = str(info.get("title", meta.get("name") or "Unknown"))
        path = meta.get("path") or info.get("path")
        song_id_val = meta.get("id") or meta.get("song_id")

        user = interaction.user
        playlists = _get_or_create_user_playlists(user.id)
        likes = playlists.setdefault("Likes", [])

        # Avoid duplicates: prefer matching by song ID, then by path.
        already = False
        for track in likes:
            if song_id_val is not None and track.get("id") == song_id_val:
                already = True
                break
            if path and track.get("path") == path:
                already = True
                break

        if already:
            await _send_ephemeral_temporary(
                interaction, f"`{title}` is already in your Likes playlist."
            )
            return

        likes.append(
            {
                "id": song_id_val,
                "name": title,
                "path": path,
                "metadata": meta,
                "added_at": time.time(),
            }
        )

        _save_user_playlists_to_disk()

        await _send_ephemeral_temporary(
            interaction, f"Added `{title}` to your Likes playlist."
        )

    @discord.ui.button(label="📂 Playlists", style=discord.ButtonStyle.secondary)
    async def playlists_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:  # pragma: no cover - UI callback
        """Show paginated playlists with play buttons."""

        user = interaction.user
        playlists = _user_playlists.get(user.id) or {}

        if not playlists:
            await interaction.response.send_message(
                "You don't have any playlists yet. Use ❤ Like on the player to "
                "add the current song to your Likes playlist.",
                ephemeral=True,
            )
            return

        view = PlaylistPaginationView(ctx=self.ctx, playlists=playlists, user=user, interaction=interaction)
        embed = view.build_embed()

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="📻 Radio", style=discord.ButtonStyle.secondary)
    async def radio_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:  # pragma: no cover - UI callback
        """Start radio mode: continuously play random songs until stopped."""

        await interaction.response.defer(ephemeral=True)

        guild = self.ctx.guild
        if not guild:
            await _send_ephemeral_temporary(interaction, "Guild context unavailable.")
            return

        user = interaction.user
        if not isinstance(user, (discord.Member,)) or not user.voice or not user.voice.channel:
            await _send_ephemeral_temporary(interaction, "You need to be in a voice channel to use radio.")
            return

        _guild_radio_enabled[guild.id] = True

        # If something is already playing, let it finish naturally.
        # The after-callback will detect radio is enabled and start playing
        # random songs once the current track ends.
        voice: Optional[discord.VoiceClient] = self.ctx.voice_client
        if voice and (voice.is_playing() or voice.is_paused()):
            # Pre-fetch so "Up Next" is ready when the current song ends
            await _prefetch_next_radio_song(guild.id)
            await _send_ephemeral_temporary(interaction, "Radio enabled. Current song will finish, then radio starts.")
        else:
            # Nothing playing; start radio immediately
            await _play_random_song_in_guild(self.ctx)
            await _send_ephemeral_temporary(interaction, "Radio started.")


def _build_player_embed(
    guild_id: int,
    *,
    title: str,
    metadata: Optional[Dict[str, Any]] = None,
    duration_seconds: Optional[int] = None,
    started_at: Optional[float] = None,
    paused_at: Optional[float] = None,
    total_paused_time: float = 0,
    is_radio: bool = False,
) -> discord.Embed:
    """Build the Now Playing embed with all standard fields.
    
    This is the single source of truth for the player embed layout.
    All player displays should use this function.
    """
    meta = metadata or {}
    
    embed = discord.Embed(title="Now Playing", description=title)

    # Album art / cover
    image_url = meta.get("image_url")
    if image_url:
        embed.set_thumbnail(url=image_url)

    # Minimal song details for the main player: category & era only
    if meta.get("category"):
        embed.add_field(name="Category", value=str(meta.get("category")), inline=True)

    era_val = meta.get("era")
    if era_val is not None:
        # If era is a dict, prefer the human-friendly name field.
        if isinstance(era_val, dict):
            era_text = str(era_val.get("name") or "").strip()
        else:
            era_text = str(era_val).strip()

        if era_text:
            embed.add_field(name="Era", value=era_text, inline=True)

    # Duration + progress bar (accounting for pause time)
    if duration_seconds and started_at:
        now = time.time()
        # If currently paused, don't count time since pause started
        if paused_at:
            raw_elapsed = paused_at - started_at
        else:
            raw_elapsed = now - started_at
        # Subtract total time spent paused
        elapsed = int(raw_elapsed - total_paused_time)
        elapsed = max(0, elapsed)  # Ensure non-negative
        progress = _format_progress_bar(elapsed, duration_seconds)
        # Add paused indicator if currently paused
        if paused_at:
            progress = "⏸️ " + progress
        embed.add_field(name="Progress", value=progress, inline=False)

    # Previous song
    prev_song = _guild_previous_song.get(guild_id)
    if prev_song:
        prev_title = prev_song.get("title", "Unknown")
        embed.add_field(name="Previous", value=f"**{prev_title}**", inline=True)

    # Queue count with next song name (Up Next)
    queue = _guild_queue.get(guild_id, [])
    if queue:
        next_title = queue[0].get("title", "Unknown")
        queue_text = f"{len(queue)} track(s)\nUp Next: **{next_title}**"
        embed.add_field(name="Queue", value=queue_text, inline=True)
    elif is_radio:
        # Show pre-fetched radio next song if available
        radio_next = _guild_radio_next.get(guild_id)
        if radio_next:
            next_title = radio_next.get("title", "Unknown")
            embed.add_field(name="Up Next", value=f"**{next_title}**", inline=True)

    # Radio mode indicator only in the footer
    if is_radio:
        embed.set_footer(text="Radio mode is ON")
    else:
        embed.set_footer(text="Radio mode is OFF")

    return embed


async def _send_player_controls(
    ctx: commands.Context,
    *,
    title: str,
    path: Optional[str],
    is_radio: bool,
    metadata: Optional[Dict[str, Any]] = None,
    duration_seconds: Optional[int] = None,
) -> None:
    """Send or update the Now Playing embed with interactive controls.

    This keeps a single player message per guild and edits it when tracks
    change instead of sending a new message every time.
    """

    if not ctx.guild:
        return

    _set_now_playing(
        ctx,
        title=title,
        path=path,
        is_radio=is_radio,
        metadata=metadata,
        duration_seconds=duration_seconds,
    )

    guild_id = ctx.guild.id
    info = _guild_now_playing.get(guild_id, {})
    # Store a lightweight reference to ctx so the background task can
    # reconstruct the view. Avoid storing the full object tree.
    info.setdefault("ctx", ctx)
    _guild_now_playing[guild_id] = info
    message_id = info.get("message_id")
    channel_id = info.get("channel_id")

    # Build embed using the centralized function
    embed = _build_player_embed(
        guild_id,
        title=title,
        metadata=info.get("metadata"),
        duration_seconds=duration_seconds or info.get("duration_seconds"),
        started_at=info.get("started_at"),
        paused_at=info.get("paused_at"),
        total_paused_time=info.get("total_paused_time", 0),
        is_radio=is_radio,
    )

    view = PlayerView(ctx=ctx, is_radio=is_radio)

    # If we have a previously-sent player message, try to edit it.
    target_channel = ctx.channel
    if channel_id is not None and ctx.guild is not None:
        chan = ctx.guild.get_channel(channel_id) or bot.get_channel(channel_id)
        if isinstance(chan, discord.TextChannel):
            target_channel = chan

    if message_id is not None and isinstance(target_channel, discord.abc.Messageable):
        try:
            # type: ignore[attr-defined] - TextChannel has fetch_message
            msg = await target_channel.fetch_message(message_id)  # pragma: no cover
            await msg.edit(embed=embed, view=view)
            return
        except Exception:
            # If the message was deleted or can't be fetched, fall back to sending
            # a fresh one and update the stored metadata.
            pass

    sent = await target_channel.send(embed=embed, view=view)
    # Persist the message metadata so we can edit next time.
    _guild_now_playing[guild_id]["message_id"] = sent.id
    _guild_now_playing[guild_id]["channel_id"] = sent.channel.id


@tasks.loop(seconds=5)
async def _update_player_messages() -> None:
    """Periodically refresh Now Playing embeds with updated progress.

    This keeps the progress bar and elapsed time roughly in sync with
    playback. Runs every 5 seconds for all guilds with a tracked
    now-playing message.
    """

    for guild in list(_guild_now_playing.keys()):
        info = _guild_now_playing.get(guild)
        if not info:
            continue

        message_id = info.get("message_id")
        channel_id = info.get("channel_id")
        title = info.get("title")
        path = info.get("path")
        is_radio = bool(info.get("is_radio"))
        metadata = info.get("metadata") or {}
        duration_seconds = info.get("duration_seconds")

        if message_id is None or channel_id is None or not title:
            continue

        guild_obj = bot.get_guild(guild)
        if not guild_obj:
            continue

        chan = guild_obj.get_channel(channel_id) or bot.get_channel(channel_id)
        if not isinstance(chan, discord.TextChannel):
            continue

        try:
            msg = await chan.fetch_message(message_id)  # pragma: no cover
        except Exception:
            # Message may have been deleted; stop tracking it.
            continue

        # Build embed using the centralized function
        embed = _build_player_embed(
            guild,
            title=title,
            metadata=metadata,
            duration_seconds=duration_seconds,
            started_at=info.get("started_at"),
            paused_at=info.get("paused_at"),
            total_paused_time=info.get("total_paused_time", 0),
            is_radio=is_radio,
        )

        view = PlayerView(ctx=info.get("ctx"), is_radio=is_radio) if info.get("ctx") else None
        try:
            await msg.edit(embed=embed, view=view)
        except Exception:
            continue


class SongOfTheDayView(discord.ui.View):
    """View with a Play button for the Song of the Day embed."""

    def __init__(self, *, song_data: Dict[str, Any]) -> None:
        super().__init__(timeout=None)  # persistent — no timeout
        self.song_data = song_data

    @discord.ui.button(label="▶️ Play", style=discord.ButtonStyle.primary)
    async def play_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        user = interaction.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await interaction.response.send_message(
                "You need to be in a voice channel to play this.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            return

        channel = user.voice.channel
        voice: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if voice and voice.is_connected():
            if voice.channel != channel:
                await voice.move_to(channel)
        else:
            voice = await channel.connect()

        file_path = self.song_data.get("path")
        if not file_path:
            await interaction.followup.send("No playable file for this song.", ephemeral=True)
            return

        stream_url = await _get_fresh_stream_url(file_path)
        if not stream_url:
            await interaction.followup.send("Could not get a stream URL for this song.", ephemeral=True)
            return

        # Build a fake ctx from the interaction so queue_or_play_now works.
        ctx = await bot.get_context(await interaction.channel.fetch_message(interaction.message.id))
        ctx.author = user  # type: ignore[assignment]

        title = self.song_data.get("title", "Unknown")
        metadata = self.song_data.get("metadata", {})
        duration_seconds = self.song_data.get("duration_seconds")

        await _queue_or_play_now(
            ctx,
            stream_url=stream_url,
            title=title,
            path=file_path,
            metadata=metadata,
            duration_seconds=duration_seconds,
        )
        await interaction.followup.send(f"Now playing: **{title}**", ephemeral=True)


@tasks.loop(hours=24)
async def _song_of_the_day_task() -> None:
    """Post a random Song of the Day to configured channels."""

    if not _sotd_config:
        return

    song_data = await _fetch_random_radio_song(include_stream_url=False)
    if not song_data:
        return

    title = song_data.get("title", "Unknown")
    metadata = song_data.get("metadata", {})
    duration_seconds = song_data.get("duration_seconds")

    embed = discord.Embed(
        title="🎵 Song of the Day",
        description=f"**{title}**",
        colour=discord.Colour.gold(),
    )
    image_url = metadata.get("image_url")
    if image_url:
        embed.set_thumbnail(url=image_url)
    if metadata.get("category"):
        embed.add_field(name="Category", value=str(metadata["category"]), inline=True)
    era_val = metadata.get("era")
    if era_val:
        era_text = era_val.get("name") if isinstance(era_val, dict) else str(era_val)
        if era_text:
            embed.add_field(name="Era", value=era_text, inline=True)
    producers = metadata.get("producers")
    if producers:
        embed.add_field(name="Producers", value=str(producers), inline=True)
    if duration_seconds:
        m, s = divmod(duration_seconds, 60)
        embed.add_field(name="Length", value=f"{m}:{s:02d}", inline=True)
    embed.set_footer(text="Press Play to listen!")

    view = SongOfTheDayView(song_data=song_data)

    for guild_id_str, channel_id in list(_sotd_config.items()):
        guild_obj = bot.get_guild(int(guild_id_str))
        if not guild_obj:
            continue
        chan = guild_obj.get_channel(channel_id)
        if not isinstance(chan, discord.TextChannel):
            continue
        try:
            # Try webhook-based posting for a custom "Juice WRLD Radio" identity.
            webhook = await _get_or_create_sotd_webhook(chan)
            if webhook:
                await webhook.send(
                    embed=embed,
                    view=view,
                    username="Juice WRLD Radio",
                    avatar_url=image_url if image_url else None,
                )
            else:
                await chan.send(embed=embed, view=view)
        except Exception:
            # Fall back to normal bot message on any failure.
            try:
                await chan.send(embed=embed, view=view)
            except Exception:
                continue


async def _get_or_create_sotd_webhook(channel: discord.TextChannel) -> Optional[discord.Webhook]:
    """Get or create a webhook in the channel for SOTD posts."""
    try:
        webhooks = await channel.webhooks()
        for wh in webhooks:
            if wh.name == "JuiceWRLD-SOTD" and wh.user == bot.user:
                return wh
        return await channel.create_webhook(name="JuiceWRLD-SOTD")
    except Exception:
        return None


@_song_of_the_day_task.before_loop
async def _before_sotd() -> None:
    await bot.wait_until_ready()


async def _auto_disconnect_guild(guild: discord.Guild, reason: str = "inactivity") -> None:
    """Cleanly disconnect the bot from voice in a guild and reset state."""

    guild_id = guild.id
    _guild_radio_enabled[guild_id] = False
    _guild_radio_next.pop(guild_id, None)
    _guild_last_activity.pop(guild_id, None)

    voice: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
    if voice and voice.is_connected():
        if voice.is_playing() or voice.is_paused():
            voice.stop()
        await voice.disconnect()

    # Clear the bot's Discord activity status.
    asyncio.create_task(
        bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="nothing"))
    )

    # Delete the Now Playing message after a brief moment.
    asyncio.create_task(_delete_now_playing_message_after_delay(guild_id, 1))

    # Try to notify a text channel.
    info = _guild_now_playing.get(guild_id)
    channel_id = info.get("channel_id") if info else None
    if channel_id:
        chan = guild.get_channel(channel_id) or bot.get_channel(channel_id)
        if isinstance(chan, discord.TextChannel):
            try:
                msg = await chan.send(f"Disconnected due to {reason}.")
                asyncio.create_task(_delete_later(msg, 10))
            except Exception:
                pass


@tasks.loop(seconds=60)
async def _idle_auto_leave() -> None:
    """Periodically check for guilds where the bot is idle and auto-leave."""

    now = time.time()
    for guild in list(bot.guilds):
        voice: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if not voice or not voice.is_connected():
            continue

        # If actively playing, refresh the timestamp and skip.
        if voice.is_playing():
            _touch_activity(guild.id)
            continue

        last = _guild_last_activity.get(guild.id)
        if last is None:
            # First time we're checking — initialise and give it a full window.
            _touch_activity(guild.id)
            continue

        if now - last >= AUTO_LEAVE_IDLE_SECONDS:
            await _auto_disconnect_guild(guild, reason="30 minutes of inactivity")


@_idle_auto_leave.before_loop
async def _before_idle_auto_leave() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    """Auto-leave when the bot is the only member left in a voice channel."""

    # We only care about users leaving a channel (not the bot itself).
    if member.bot:
        return

    # A user left or moved away from a channel.
    if before.channel is None:
        return

    guild = member.guild
    voice: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
    if not voice or not voice.is_connected():
        return

    # Check if the channel the user left is the one the bot is in.
    if voice.channel != before.channel:
        return

    # Count non-bot members remaining.
    human_members = [m for m in voice.channel.members if not m.bot]
    if len(human_members) == 0:
        await _auto_disconnect_guild(guild, reason="everyone left the voice channel")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    if not _update_player_messages.is_running():
        _update_player_messages.start()
    if not _idle_auto_leave.is_running():
        _idle_auto_leave.start()
    if not _song_of_the_day_task.is_running():
        _song_of_the_day_task.start()

    # Clear and sync application (slash) commands
    try:
        bot.tree.clear_commands(guild=None)  # Clear global commands
        # Clear guild-specific commands for all connected guilds
        for guild in bot.guilds:
            bot.tree.clear_commands(guild=guild)
        bot.tree.add_command(jw_group)        # Re-add your command group
        # Sync to all guilds first (instant), then global
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
    """Handle errors for commands.

    For unknown commands, tell the user and auto-delete both messages
    after a short delay to keep channels clean.
    """

    from discord.ext import commands as _commands_mod

    if isinstance(error, _commands_mod.CommandNotFound):
        # Schedule deletion of the user's unknown command message
        try:
            if ctx.message:
                asyncio.create_task(_delete_later(ctx.message, 5))
        except Exception:
            pass

        # Send a temporary "command not found" notice
        content = f"Command `{ctx.message.content}` is not found."
        await _send_temporary(ctx, content, delay=5)
        return

    # For all other errors, re-raise so default handler/logging still occurs.
    raise error


@bot.before_invoke
async def _delete_user_command(ctx: commands.Context) -> None:
    """Delete the user's command message after a short delay (best-effort).

    For most commands we wait ~5s so users can see what they typed.
    For `!jw stop` specifically, delete more aggressively after 1s.
    """

    try:
        msg = ctx.message
        if not msg:
            return

        cmd = getattr(ctx, "command", None)
        # Default delay for most commands
        delay = 5
        if cmd and getattr(cmd, "name", None) == "stop":
            delay = 1

        asyncio.create_task(_delete_later(msg, delay))
    except Exception:
        return


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    """Show help for the bot commands in a clean, organized embed."""

    embed = discord.Embed(title="Juice WRLD Bot Help", colour=discord.Colour.purple())

    core_lines = [
        "`!jw ping` — Check if the bot is alive.",
        "`!jw search <query>` — Search for Juice WRLD songs.",
        "`!jw song <song_id>` — Get details for a specific song by ID.",
        "`!jw join` — Make the bot join your current voice channel.",
        "`!jw leave` — Disconnect the bot from voice chat.",
        "`!jw play <song_id>` — Play a Juice WRLD song in voice chat.",
        "`!jw radio` — Start radio mode (random songs until `!jw stop`).",
        "`!jw stop` — Stop playback and turn off radio mode.",
    ]
    embed.add_field(name="Core Commands", value="\n".join(core_lines), inline=False)

    search_lines = [
        "`!jw playfile <file_path>` — Play directly from a specific comp file path.",
        "`!jw playsearch <name>` — Search all comp files by name and play the best match.",
        "`!jw stusesh <name>` — Search Studio Sessions only and play the best match.",
        "`!jw og <name>` — Search Original Files only and play the best match.",
        "`!jw seshedits <name>` — Search Session Edits only and play the best match.",
        "`!jw stems <name>` — Search Stem Edits only and play the best match.",
        "`!jw comp <name>` — Search Compilation (released/unreleased/misc) and play the best match.",
    ]
    embed.add_field(name="Search & Comp Playback", value="\n".join(search_lines), inline=False)

    playlist_lines = [
        "`!jw pl` — List your playlists and a short preview.",
        "`!jw pl show <name>` — Show full contents of one playlist.",
        "`!jw pl play <name>` — Queue/play all tracks in a playlist.",
        "`!jw pl add <name> <song_id>` — Add a song (by ID) to a playlist.",
        "`!jw pl delete <name>` — Delete one of your playlists.",
        "`!jw pl rename <old> <new>` — Rename one of your playlists.",
        "`!jw pl remove <name> <index>` — Remove a track (1-based index).",
    ]
    embed.add_field(name="Playlists", value="\n".join(playlist_lines), inline=False)

    browse_lines = [
        "`!jw eras` — List all Juice WRLD musical eras.",
        "`!jw era <name>` — Browse songs from a specific era.",
        "`!jw similar` — Find songs similar to the currently playing track.",
    ]
    embed.add_field(name="Browse & Discover", value="\n".join(browse_lines), inline=False)

    misc_lines = [
        "`!jw stats` — View your personal listening stats.",
        "`!jw sotd #channel` — Set the Song of the Day channel (admin).",
        "`!jw emoji list|upload|delete` — Manage application emojis (admin).",
        "`!jw ver` — Show bot version and recent updates.",
    ]
    embed.add_field(name="Misc", value="\n".join(misc_lines), inline=False)

    embed.set_footer(text="Prefix: !jw  •  Example: !jw play 12345")

    await ctx.send(embed=embed)


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    """Simple health check.

    Sends a temporary "Pong!" message that auto-deletes after a few seconds.
    """

    await _send_temporary(ctx, "Pong!", delay=5)


@bot.command(name="sync")
@commands.has_permissions(administrator=True)
async def sync_commands(ctx: commands.Context):
    """Manually sync slash commands to Discord (admin only).
    
    This forces Discord to immediately register/update all slash commands.
    Use this after making changes to slash commands in the code.
    """
    
    msg = await ctx.send("Syncing slash commands...")
    
    try:
        # Clear existing commands
        bot.tree.clear_commands(guild=None)
        if ctx.guild:
            bot.tree.clear_commands(guild=ctx.guild)
        
        # Re-add the command group
        bot.tree.add_command(jw_group)
        
        # Sync to this guild first (instant)
        if ctx.guild:
            await bot.tree.sync(guild=ctx.guild)
            await msg.edit(content=f"✅ Synced slash commands to **{ctx.guild.name}**!\n\n"
                                   f"The `/jw` commands should now be available in this server.\n"
                                   f"Syncing globally (may take up to 1 hour)...")
        
        # Sync globally (takes up to 1 hour to propagate)
        await bot.tree.sync()
        
        await msg.edit(content=f"✅ Successfully synced slash commands!\n\n"
                               f"• **Guild sync**: Instant (commands available now in this server)\n"
                               f"• **Global sync**: Started (may take up to 1 hour for other servers)\n\n"
                               f"Try typing `/jw` to see the commands.")
        
        # Delete after 15 seconds
        asyncio.create_task(_delete_later(msg, 15))
        
    except Exception as e:
        await msg.edit(content=f"❌ Error syncing commands: {e}")
        print(f"Sync error: {e}", file=sys.stderr)


# Bot version info
BOT_VERSION = "2.1.0"
BOT_BUILD_DATE = "2026-02-24"


@bot.command(name="ver", aliases=["version"])
async def version_command(ctx: commands.Context):
    """Show bot version information."""
    
    embed = discord.Embed(
        title="JuiceAPI Bot Version",
        description=f"**Version:** {BOT_VERSION}\n**Build Date:** {BOT_BUILD_DATE}",
        colour=discord.Colour.green(),
    )
    embed.add_field(
        name="Recent Updates",
        value=(
            "• 🔗 Linked Roles — connect listening stats to Discord role requirements\n"
            "• 😀 Application Emojis — `!jw emoji` to manage app emojis\n"
            "• 🖱️ Context menus — right-click users/messages for quick actions\n"
            "• 📡 Webhook SOTD — Song of the Day posts with custom identity\n"
            "• 🎵 Rich Presence — album art, party size, radio/play icons\n"
            "• `!jw eras` / `!jw era` — Browse musical eras\n"
            "• `!jw similar` — Find similar songs to what's playing\n"
            "• `!jw stats` — Personal listening stats"
        ),
        inline=False,
    )
    embed.set_footer(text="Use !jw help for all commands")
    
    await _send_temporary(ctx, embed=embed, delay=15)


# --- Slash command equivalents for core commands (ephemeral responses) ---


@jw_group.command(name="ping", description="Check if the bot is alive.")
async def slash_ping(interaction: discord.Interaction) -> None:
    """Ephemeral equivalent of !jw ping."""

    await interaction.response.send_message("Pong!", ephemeral=True)


@jw_group.command(name="stats", description="View your personal listening stats.")
async def slash_stats(interaction: discord.Interaction) -> None:
    """Ephemeral equivalent of !jw stats."""

    embed = _build_stats_embed(interaction.user)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    _schedule_interaction_deletion(interaction, 30)


@jw_group.command(name="eras", description="List all Juice WRLD musical eras.")
async def slash_eras(interaction: discord.Interaction) -> None:
    """Ephemeral equivalent of !jw eras."""

    await interaction.response.defer(ephemeral=True, thinking=True)

    api = create_api_client()
    try:
        eras = api.get_eras()
    except JuiceWRLDAPIError as e:
        await interaction.followup.send(f"Error fetching eras: {e}", ephemeral=True)
        return
    finally:
        api.close()

    if not eras:
        await interaction.followup.send("No eras found.", ephemeral=True)
        return

    lines = []
    for era in eras:
        tf = f" ({era.time_frame})" if era.time_frame else ""
        lines.append(f"**{era.name}**{tf}")

    embed = discord.Embed(
        title="Juice WRLD Eras",
        description="\n".join(lines),
        colour=discord.Colour.purple(),
    )
    embed.set_footer(text="Use /jw era <name> to browse songs from an era.")
    await interaction.followup.send(embed=embed, ephemeral=True)
    _schedule_interaction_deletion(interaction, 30)


async def era_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """Autocomplete callback for era names."""
    try:
        api = create_api_client()
        try:
            eras = api.get_eras()
        finally:
            api.close()
        choices = []
        for era in eras:
            name = era.name or ""
            if current and current.lower() not in name.lower():
                continue
            display = f"{name} ({era.time_frame})" if era.time_frame else name
            if len(display) > 100:
                display = display[:97] + "..."
            choices.append(app_commands.Choice(name=display, value=name))
            if len(choices) >= 25:
                break
        return choices
    except Exception:
        return []


@jw_group.command(name="era", description="Browse songs from a specific era.")
@app_commands.describe(era_name="Name of the era to browse")
@app_commands.autocomplete(era_name=era_autocomplete)
async def slash_era(interaction: discord.Interaction, era_name: str) -> None:
    """Ephemeral equivalent of !jw era."""

    await interaction.response.defer(ephemeral=True, thinking=True)

    api = create_api_client()
    try:
        results = api.get_songs(era=era_name, page=1, page_size=25)
    except JuiceWRLDAPIError as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)
        return
    finally:
        api.close()

    songs = results.get("results") or []
    if not songs:
        await interaction.followup.send(f"No songs found for era `{era_name}`.", ephemeral=True)
        return

    ctx = await commands.Context.from_interaction(interaction)
    total = results.get("count") if isinstance(results, dict) else None
    view = SearchPaginationView(ctx=ctx, songs=songs, query=f"Era: {era_name}", total_count=total, is_ephemeral=True)
    embed = view.build_embed()
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@jw_group.command(name="similar", description="Find songs similar to the currently playing track.")
async def slash_similar(interaction: discord.Interaction) -> None:
    """Ephemeral equivalent of !jw similar."""

    await interaction.response.defer(ephemeral=True, thinking=True)

    guild = interaction.guild
    if not guild:
        await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
        return

    info = _guild_now_playing.get(guild.id)
    title = info.get("title") if info else None
    if not info or not title or title == "Nothing playing":
        await interaction.followup.send("Nothing is currently playing. Play a song first!", ephemeral=True)
        return

    meta = info.get("metadata") or {}
    era_val = meta.get("era")
    era_name = None
    if isinstance(era_val, dict):
        era_name = era_val.get("name")
    elif era_val:
        era_name = str(era_val)

    producers_str = meta.get("producers") or ""
    category = meta.get("category") or ""

    candidates: List[Any] = []
    api = create_api_client()
    try:
        if era_name:
            res = api.get_songs(era=era_name, page=1, page_size=25)
            candidates = res.get("results") or []
        if len(candidates) < 5 and category:
            res2 = api.get_songs(category=category, page=1, page_size=25)
            existing_ids = {getattr(s, "id", None) for s in candidates}
            for s in (res2.get("results") or []):
                if getattr(s, "id", None) not in existing_ids:
                    candidates.append(s)
    except JuiceWRLDAPIError as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)
        return
    finally:
        api.close()

    candidates = [s for s in candidates if getattr(s, "name", None) != title]

    def _score(song: Any) -> int:
        sc = 0
        s_era = getattr(getattr(song, "era", None), "name", "")
        if era_name and s_era == era_name:
            sc += 2
        s_prod = getattr(song, "producers", "") or ""
        if producers_str and s_prod and any(p.strip() in s_prod for p in producers_str.split(",") if p.strip()):
            sc += 3
        if category and getattr(song, "category", "") == category:
            sc += 1
        return sc

    candidates.sort(key=_score, reverse=True)
    top = candidates[:10]

    if not top:
        await interaction.followup.send(f"No similar songs found for **{title}**.", ephemeral=True)
        return

    ctx = await commands.Context.from_interaction(interaction)
    view = SearchPaginationView(ctx=ctx, songs=top, query=f"Similar to: {title}", total_count=len(top), is_ephemeral=True)
    embed = view.build_embed()
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# @jw_group.command(name="song", description="Get details for a specific song by ID.")
# @app_commands.describe(song_id="Numeric Juice WRLD song ID")
# async def slash_song(interaction: discord.Interaction, song_id: int) -> None:
#     """Ephemeral equivalent of !jw song <song_id>."""
# 
#     await interaction.response.defer(ephemeral=True, thinking=True)
# 
#     api = create_api_client()
#     try:
#         try:
#             song = api.get_song(song_id)
#         except NotFoundError:
#             await interaction.followup.send(
#                 f"No song found with ID `{song_id}`.", ephemeral=True
#             )
#             return
#         except JuiceWRLDAPIError as e:
#             await interaction.followup.send(
#                 f"Error while fetching song: {e}", ephemeral=True
#             )
#             return
#     finally:
#         api.close()
# 
#     name = getattr(song, "name", getattr(song, "title", "Unknown"))
#     category = getattr(song, "category", "?")
#     length = getattr(song, "length", "?")
#     era_name = getattr(getattr(song, "era", None), "name", "?")
#     producers = getattr(song, "producers", None)
# 
#     desc_lines = [
#         f"**{name}** (ID: `{song_id}`)",
#         f"Category: `{category}`",
#         f"Length: `{length}`",
#         f"Era: `{era_name}`",
#     ]
#     if producers:
#         desc_lines.append(f"Producers: {producers}")
# 
#     await interaction.followup.send("\n".join(desc_lines), ephemeral=True)


# @jw_group.command(name="play", description="Play a Juice WRLD song in voice chat by ID.")
# @app_commands.describe(song_id="Numeric Juice WRLD song ID")
# async def slash_play(interaction: discord.Interaction, song_id: int) -> None:
#     """Ephemeral wrapper that delegates to !jw play logic."""
# 
#     await interaction.response.defer(ephemeral=True, thinking=True)
# 
#     # Build a commands.Context from this interaction so we can reuse play_song.
#     ctx = await commands.Context.from_interaction(interaction)
#     await play_song(ctx, str(song_id))
# 
#     await interaction.followup.send(
#         f"Requested playback for song ID `{song_id}`.", ephemeral=True
#     )


async def song_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """Autocomplete callback for song search.
    
    Returns up to 25 song choices matching the current input.
    Only includes songs that have a duration (length), which is the
    strongest indicator that an audio file exists for the song.
    """
    try:
        api = create_api_client()
        try:
            if current and len(current) >= 2:
                results = api.get_songs(search=current, page=1, page_size=25)
            else:
                # No input yet — show a default page of songs so the user sees options.
                results = api.get_songs(page=1, page_size=25)
        finally:
            api.close()
        
        songs = results.get("results") or []
        choices = []
        
        for song in songs:
            if len(choices) >= 25:  # Discord allows max 25 choices
                break

            song_id = getattr(song, "id", None)
            if not song_id:
                continue

            length = (getattr(song, "length", "") or "").strip()

            # Skip songs with no duration — they almost never have
            # playable audio files regardless of category.
            if not length:
                continue

            name = getattr(song, "name", getattr(song, "title", "Unknown"))
            
            # Format: "Song Name - Duration" (max 100 chars for display)
            display_name = f"{name} - {length}"
            
            # Truncate if too long (Discord limit is 100 chars)
            if len(display_name) > 100:
                display_name = display_name[:97] + "..."
            
            choices.append(app_commands.Choice(name=display_name, value=str(song_id)))
        
        return choices
    except Exception:
        return []


@jw_group.command(name="play", description="Play a Juice WRLD song in voice chat.")
@app_commands.describe(query="Search for a song to play")
@app_commands.autocomplete(query=song_autocomplete)
async def slash_play(interaction: discord.Interaction, query: str) -> None:
    """Play a song with autocomplete search."""
    
    await interaction.response.defer(ephemeral=True, thinking=True)
    
    user = interaction.user
    if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
        await interaction.followup.send(
            "You need to be in a voice channel to play music.", ephemeral=True
        )
        return
    
    # Check if query is a song ID (from autocomplete) or a search term
    song_id = None
    if query.isdigit():
        song_id = query
    else:
        # Search for the song
        try:
            api = create_api_client()
            try:
                results = api.get_songs(search=query, page=1, page_size=1)
            finally:
                api.close()
            
            songs = results.get("results") or []
            if songs:
                song_id = str(getattr(songs[0], "id", None))
        except Exception as e:
            await interaction.followup.send(
                f"Error searching for song: {e}", ephemeral=True
            )
            return
    
    if not song_id:
        await interaction.followup.send(
            f"No song found for `{query}`.", ephemeral=True
        )
        return
    
    # Disable radio if active
    if interaction.guild:
        _guild_radio_enabled[interaction.guild.id] = False
    
    # Build a Context and play the song
    ctx = await commands.Context.from_interaction(interaction)
    await play_song(ctx, song_id)
    
    await _send_ephemeral_temporary(
        interaction,
        f"🎵 Playing song...",
        delay=3,
    )


@jw_group.command(name="search", description="Search for Juice WRLD songs.")
@app_commands.describe(query="Search query for song titles/content")
@app_commands.autocomplete(query=song_autocomplete)
async def slash_search(interaction: discord.Interaction, query: str) -> None:
    """Ephemeral, paginated search (equivalent to !jw search)."""

    await interaction.response.defer(ephemeral=True, thinking=True)

    # Build a Context to drive playback when buttons are pressed.
    ctx = await commands.Context.from_interaction(interaction)

    # Check if query is a song ID (from autocomplete selection)
    if query.isdigit():
        # Fetch the specific song by ID
        api = create_api_client()
        try:
            song = api.get_song(int(query))
            view = SingleSongResultView(ctx=ctx, song=song, query=query)
            embed = view.build_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            return
        except NotFoundError:
            await interaction.followup.send(
                f"No song found with ID `{query}`.", ephemeral=True
            )
            _schedule_interaction_deletion(interaction, 5)
            return
        except JuiceWRLDAPIError as e:
            await interaction.followup.send(
                f"Error fetching song: {e}", ephemeral=True
            )
            return
        finally:
            api.close()

    # Regular search query
    api = create_api_client()
    try:
        results = api.get_songs(search=query, page=1, page_size=25)
    except JuiceWRLDAPIError as e:
        await interaction.followup.send(
            f"Error while searching songs: {e}", ephemeral=True
        )
        return
    finally:
        api.close()

    songs = results.get("results") or []
    if not songs:
        await interaction.followup.send(
            f"No songs found for `{query}`.", ephemeral=True
        )
        _schedule_interaction_deletion(interaction, 5)
        return

    total = results.get("count") if isinstance(results, dict) else None
    
    # If only one result, show interactive single song view
    if len(songs) == 1:
        view = SingleSongResultView(ctx=ctx, song=songs[0], query=query)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        # Multiple results: show pagination view
        view = SearchPaginationView(ctx=ctx, songs=songs, query=query, total_count=total, is_ephemeral=True)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# @jw_group.command(name="join", description="Make the bot join your current voice channel.")
# async def slash_join(interaction: discord.Interaction) -> None:
#     """Ephemeral equivalent of !jw join."""
# 
#     await interaction.response.defer(ephemeral=True, thinking=True)
# 
#     user = interaction.user
#     if not isinstance(user, (discord.Member,)) or not user.voice or not user.voice.channel:
#         await interaction.followup.send(
#             "You need to be in a voice channel first.", ephemeral=True
#         )
#         return
# 
#     channel = user.voice.channel
#     voice: Optional[discord.VoiceClient] = interaction.guild.voice_client if interaction.guild else None
# 
#     try:
#         if voice and voice.is_connected():
#             if voice.channel != channel:
#                 await voice.move_to(channel)
#         else:
#             await channel.connect()
#     except Exception as e:
#         await interaction.followup.send(
#             f"Failed to join voice channel: {e}", ephemeral=True
#         )
#         return
# 
#     await interaction.followup.send(
#         f"Joined voice channel: {channel.name}", ephemeral=True
#     )


@jw_group.command(name="leave", description="Disconnect the bot from voice chat.")
async def slash_leave(interaction: discord.Interaction) -> None:
    """Ephemeral equivalent of !jw leave."""

    await interaction.response.defer(ephemeral=True, thinking=True)

    guild = interaction.guild
    voice: Optional[discord.VoiceClient] = guild.voice_client if guild else None
    if not voice or not voice.is_connected():
        await interaction.followup.send(
            "I'm not connected to a voice channel.", ephemeral=True
        )
        return

    if guild:
        _guild_radio_enabled[guild.id] = False
        _guild_radio_next.pop(guild.id, None)
        asyncio.create_task(_delete_now_playing_message_after_delay(guild.id, 1))

    await voice.disconnect()
    await _send_ephemeral_temporary(interaction, "Disconnected from voice channel.")


@jw_group.command(name="radio", description="Start radio mode: random songs until stopped.")
async def slash_radio(interaction: discord.Interaction) -> None:
    """Ephemeral equivalent of !jw radio (start)."""

    await interaction.response.defer(ephemeral=True, thinking=True)

    guild = interaction.guild
    user = interaction.user

    if not guild:
        await interaction.followup.send(
            "Radio mode can only be used in a guild.", ephemeral=True
        )
        return

    if not isinstance(user, (discord.Member,)) or not user.voice or not user.voice.channel:
        await interaction.followup.send(
            "You need to be in a voice channel to use radio.", ephemeral=True
        )
        return

    _guild_radio_enabled[guild.id] = True

    # Reuse existing radio logic via a Context.
    ctx = await commands.Context.from_interaction(interaction)

    # If something is already playing, let it finish naturally.
    # The after-callback will detect radio is enabled and start playing
    # random songs once the current track ends.
    voice: Optional[discord.VoiceClient] = guild.voice_client if guild else None
    if voice and (voice.is_playing() or voice.is_paused()):
        await _prefetch_next_radio_song(guild.id)
        await _send_temporary(ctx, "Radio enabled. Current song will finish, then radio starts.", delay=5)
    else:
        await _send_temporary(ctx, "Radio mode enabled. Playing random songs until you run `!jw stop`.")
        await _play_random_song_in_guild(ctx)


@jw_group.command(name="stop", description="Stop playback and disable radio mode.")
async def slash_stop(interaction: discord.Interaction) -> None:
    """Stop playback and disable radio mode."""

    # Acknowledge silently - stop_radio sends a temporary message that auto-deletes
    await interaction.response.defer(ephemeral=True)

    ctx = await commands.Context.from_interaction(interaction)
    await stop_radio(ctx)


@jw_group.command(name="playlists", description="List your playlists.")
async def slash_playlists(interaction: discord.Interaction) -> None:
    """Ephemeral equivalent of !jw playlists."""

    user = interaction.user
    playlists = _user_playlists.get(user.id) or {}

    if not playlists:
        await interaction.response.send_message(
            "You don't have any playlists yet. Use ❤ Like on the player to "
            "add the current song to your Likes playlist.",
            ephemeral=True,
        )
        return

    # Build a Context to drive playback when buttons are pressed.
    await interaction.response.defer(ephemeral=True, thinking=True)
    ctx = await commands.Context.from_interaction(interaction)

    view = PlaylistPaginationView(ctx=ctx, playlists=playlists, user=user, interaction=interaction)
    embed = view.build_embed()

    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# Deprecated: Use /jw playlists UI instead
# @jw_group.command(name="playlist_create", description="Create a new empty playlist.")
# @app_commands.describe(name="Name for the new playlist")
# async def slash_playlist_create(interaction: discord.Interaction, name: str) -> None:
#     """Ephemeral slash command to create a new empty playlist."""
# 
#     user = interaction.user
#     playlists = _get_or_create_user_playlists(user.id)
# 
#     if name in playlists:
#         await interaction.response.send_message(
#             f"You already have a playlist named `{name}`.", ephemeral=True
#         )
#         # Schedule deletion after 5 seconds
#         async def _delete_after_delay() -> None:
#             await asyncio.sleep(5)
#             try:
#                 await interaction.delete_original_response()
#             except Exception:
#                 pass
#         asyncio.create_task(_delete_after_delay())
#         return
# 
#     playlists[name] = []
#     _save_user_playlists_to_disk()
# 
#     await interaction.response.send_message(
#         f"Created empty playlist `{name}`.", ephemeral=True
#     )
#     
#     # Schedule deletion after 5 seconds
#     async def _delete_after_delay() -> None:
#         await asyncio.sleep(5)
#         try:
#             await interaction.delete_original_response()
#         except Exception:
#             pass
#     asyncio.create_task(_delete_after_delay())


# Deprecated: Use /jw playlists UI instead
# @jw_group.command(name="playlist_rename", description="Rename one of your playlists.")
# @app_commands.describe(old="Current playlist name", new="New playlist name")
# async def slash_playlist_rename(interaction: discord.Interaction, old: str, new: str) -> None:
#     """Ephemeral slash command to rename a playlist."""
# 
#     user = interaction.user
#     playlists = _user_playlists.get(user.id) or {}
# 
#     if old not in playlists:
#         await interaction.response.send_message(
#             f"No playlist named `{old}` found.", ephemeral=True
#         )
#         # Schedule deletion after 5 seconds
#         async def _delete_after_delay() -> None:
#             await asyncio.sleep(5)
#             try:
#                 await interaction.delete_original_response()
#             except Exception:
#                 pass
#         asyncio.create_task(_delete_after_delay())
#         return
# 
#     if new in playlists:
#         await interaction.response.send_message(
#             f"You already have a playlist named `{new}`.", ephemeral=True
#         )
#         # Schedule deletion after 5 seconds
#         async def _delete_after_delay() -> None:
#             await asyncio.sleep(5)
#             try:
#                 await interaction.delete_original_response()
#             except Exception:
#                 pass
#         asyncio.create_task(_delete_after_delay())
#         return
# 
#     playlists[new] = playlists.pop(old)
#     _save_user_playlists_to_disk()
# 
#     await interaction.response.send_message(
#         f"Renamed playlist `{old}` to `{new}`.", ephemeral=True
#     )
#     
#     # Schedule deletion after 5 seconds
#     async def _delete_after_delay() -> None:
#         await asyncio.sleep(5)
#         try:
#             await interaction.delete_original_response()
#         except Exception:
#             pass
#     asyncio.create_task(_delete_after_delay())


# Deprecated: Use /jw playlists UI instead
# @jw_group.command(name="playlist_delete", description="Delete one of your playlists.")
# @app_commands.describe(name="Name of the playlist to delete")
# async def slash_playlist_delete(interaction: discord.Interaction, name: str) -> None:
#     """Ephemeral slash command to delete a playlist."""
# 
#     user = interaction.user
#     playlists = _user_playlists.get(user.id) or {}
# 
#     if name not in playlists:
#         await interaction.response.send_message(
#             f"No playlist named `{name}` found.", ephemeral=True
#         )
#         # Schedule deletion after 5 seconds
#         async def _delete_after_delay() -> None:
#             await asyncio.sleep(5)
#             try:
#                 await interaction.delete_original_response()
#             except Exception:
#                 pass
#         asyncio.create_task(_delete_after_delay())
#         return
# 
#     del playlists[name]
#     if not playlists:
#         _user_playlists.pop(user.id, None)
# 
#     _save_user_playlists_to_disk()
# 
#     await interaction.response.send_message(
#         f"Deleted playlist `{name}`.", ephemeral=True
#     )
#     
#     # Schedule deletion after 5 seconds
#     async def _delete_after_delay() -> None:
#         await asyncio.sleep(5)
#         try:
#             await interaction.delete_original_response()
#         except Exception:
#             pass
#     asyncio.create_task(_delete_after_delay())


# Deprecated: Use /jw playlists UI instead
# @jw_group.command(name="playlist_add_song", description="Add a song to one of your playlists.")
# @app_commands.describe(playlist_name="Name of the playlist", song_id="Numeric Juice WRLD song ID")
# async def slash_playlist_add_song(interaction: discord.Interaction, playlist_name: str, song_id: int) -> None:
#     """Ephemeral slash command to add a song to a playlist."""
# 
#     await interaction.response.defer(ephemeral=True, thinking=True)
# 
#     user = interaction.user
#     playlists = _get_or_create_user_playlists(user.id)
#     playlist = playlists.setdefault(playlist_name, [])
# 
#     # Resolve a comp file path for this song using the player endpoint.
#     api = create_api_client()
#     try:
#         player_result = api.play_juicewrld_song(song_id)
#     finally:
#         api.close()
# 
#     status = player_result.get("status")
#     error_detail = player_result.get("error")
# 
#     if status == "not_found":
#         await interaction.followup.send(
#             f"No playable song found for ID `{song_id}` in the player endpoint; "
#             "it may not be available for streaming yet.",
#             ephemeral=True,
#         )
#         # Schedule deletion after 5 seconds
#         async def _delete_after_delay() -> None:
#             await asyncio.sleep(5)
#             try:
#                 await interaction.delete_original_response()
#             except Exception:
#                 pass
#         asyncio.create_task(_delete_after_delay())
#         return
# 
#     if status and status not in {"success", "file_not_found_but_url_provided"}:
#         if error_detail:
#             await interaction.followup.send(
#                 f"Could not get a playable file path for song `{song_id}` "
#                 f"(status: {status}). Details: {error_detail}",
#                 ephemeral=True,
#             )
#         else:
#             await interaction.followup.send(
#                 f"Could not get a playable file path for song `{song_id}` "
#                 f"(status: {status}).",
#                 ephemeral=True,
#             )
#         # Schedule deletion after 5 seconds
#         async def _delete_after_delay() -> None:
#             await asyncio.sleep(5)
#             try:
#                 await interaction.delete_original_response()
#             except Exception:
#                 pass
#         asyncio.create_task(_delete_after_delay())
#         return
# 
#     file_path = player_result.get("file_path")
#     if not file_path:
#         await interaction.followup.send(
#             f"Player endpoint did not return a comp file path for song `{song_id}`; "
#             "cannot add to playlist.",
#             ephemeral=True,
#         )
#         # Schedule deletion after 5 seconds
#         async def _delete_after_delay() -> None:
#             await asyncio.sleep(5)
#             try:
#                 await interaction.delete_original_response()
#             except Exception:
#                 pass
#         asyncio.create_task(_delete_after_delay())
#         return
# 
#     # Fetch full song metadata for display and future playback.
#     api = create_api_client()
#     try:
#         song_obj = api.get_song(song_id)
#     except Exception:
#         song_obj = None
#     finally:
#         api.close()
# 
#     if song_obj is not None:
#         image_url = getattr(song_obj, "image_url", None)
#         if image_url and isinstance(image_url, str) and image_url.startswith("/"):
#             image_url = f"{JUICEWRLD_API_BASE_URL}{image_url}"
#         meta = _build_song_metadata_from_song(
#             song_obj,
#             path=file_path,
#             image_url=image_url,
#         )
#     else:
#         meta = {"id": song_id, "path": file_path}
# 
#     title = str(meta.get("name") or f"Song {song_id}")
#     song_id_val = meta.get("id") or meta.get("song_id")
# 
#     # Avoid duplicates: match by song ID or path
#     for track in playlist:
#         if song_id_val is not None and track.get("id") == song_id_val:
#             await interaction.followup.send(
#                 f"`{title}` is already in playlist `{playlist_name}`.",
#                 ephemeral=True,
#             )
#             # Schedule deletion after 5 seconds
#             async def _delete_after_delay() -> None:
#                 await asyncio.sleep(5)
#                 try:
#                     await interaction.delete_original_response()
#                 except Exception:
#                     pass
#             asyncio.create_task(_delete_after_delay())
#             return
#         if file_path and track.get("path") == file_path:
#             await interaction.followup.send(
#                 f"`{title}` is already in playlist `{playlist_name}`.",
#                 ephemeral=True,
#             )
#             # Schedule deletion after 5 seconds
#             async def _delete_after_delay() -> None:
#                 await asyncio.sleep(5)
#                 try:
#                     await interaction.delete_original_response()
#                 except Exception:
#                     pass
#             asyncio.create_task(_delete_after_delay())
#             return
# 
#     playlist.append(
#         {
#             "id": song_id_val,
#             "name": title,
#             "path": file_path,
#             "metadata": meta,
#             "added_at": time.time(),
#         }
#     )
# 
#     _save_user_playlists_to_disk()
#     await interaction.followup.send(
#         f"Added `{title}` (ID `{song_id}`) to playlist `{playlist_name}`.",
#         ephemeral=True,
#     )
#     
#     # Schedule deletion after 5 seconds
#     async def _delete_after_delay() -> None:
#         await asyncio.sleep(5)
#         try:
#             await interaction.delete_original_response()
#         except Exception:
#             pass
#     asyncio.create_task(_delete_after_delay())


@bot.command(name="sotd")
@commands.has_permissions(administrator=True)
async def setup_sotd(ctx: commands.Context, channel: discord.TextChannel):
    """Set (or update) the Song of the Day channel for this server (admin only)."""

    if not ctx.guild:
        await _send_temporary(ctx, "This command can only be used in a server.")
        return

    _sotd_config[str(ctx.guild.id)] = channel.id
    _save_sotd_config()
    await _send_temporary(ctx, f"Song of the Day will be posted daily in {channel.mention}.")


@bot.command(name="eras")
async def list_eras(ctx: commands.Context):
    """List all Juice WRLD musical eras."""

    async with ctx.typing():
        api = create_api_client()
        try:
            eras = api.get_eras()
        except JuiceWRLDAPIError as e:
            await _send_temporary(ctx, f"Error fetching eras: {e}")
            return
        finally:
            api.close()

    if not eras:
        await _send_temporary(ctx, "No eras found.")
        return

    lines = []
    for era in eras:
        tf = f" ({era.time_frame})" if era.time_frame else ""
        lines.append(f"**{era.name}**{tf}")

    embed = discord.Embed(
        title="Juice WRLD Eras",
        description="\n".join(lines),
        colour=discord.Colour.purple(),
    )
    embed.set_footer(text="Use !jw era <name> to browse songs from an era.")
    await _send_temporary(ctx, embed=embed, delay=30)


@bot.command(name="era")
async def browse_era(ctx: commands.Context, *, era_name: str):
    """Browse songs from a specific era."""

    async with ctx.typing():
        api = create_api_client()
        try:
            results = api.get_songs(era=era_name, page=1, page_size=25)
        except JuiceWRLDAPIError as e:
            await _send_temporary(ctx, f"Error fetching songs for era: {e}")
            return
        finally:
            api.close()

    songs = results.get("results") or []
    if not songs:
        await _send_temporary(ctx, f"No songs found for era `{era_name}`.")
        return

    total = results.get("count") if isinstance(results, dict) else None
    view = SearchPaginationView(ctx=ctx, songs=songs, query=f"Era: {era_name}", total_count=total)
    embed = view.build_embed()
    view.message = await ctx.send(embed=embed, view=view)


@bot.command(name="similar")
async def similar_songs(ctx: commands.Context):
    """Find songs similar to the currently playing track."""

    if not ctx.guild:
        await _send_temporary(ctx, "This command can only be used in a server.")
        return

    info = _guild_now_playing.get(ctx.guild.id)
    title = info.get("title") if info else None
    if not info or not title or title == "Nothing playing":
        await _send_temporary(ctx, "Nothing is currently playing. Play a song first!")
        return

    meta = info.get("metadata") or {}
    era_val = meta.get("era")
    era_name = None
    if isinstance(era_val, dict):
        era_name = era_val.get("name")
    elif era_val:
        era_name = str(era_val)

    producers_str = meta.get("producers") or ""
    category = meta.get("category") or ""

    # Strategy: search by era first, fall back to category
    candidates: List[Any] = []
    async with ctx.typing():
        api = create_api_client()
        try:
            if era_name:
                res = api.get_songs(era=era_name, page=1, page_size=25)
                candidates = res.get("results") or []
            if len(candidates) < 5 and category:
                res2 = api.get_songs(category=category, page=1, page_size=25)
                existing_ids = {getattr(s, "id", None) for s in candidates}
                for s in (res2.get("results") or []):
                    if getattr(s, "id", None) not in existing_ids:
                        candidates.append(s)
        except JuiceWRLDAPIError as e:
            await _send_temporary(ctx, f"Error finding similar songs: {e}")
            return
        finally:
            api.close()

    # Remove the currently playing song from results
    candidates = [s for s in candidates if getattr(s, "name", None) != title]

    # Score: same producers > same era > same category
    def _score(song: Any) -> int:
        sc = 0
        s_era = getattr(getattr(song, "era", None), "name", "")
        if era_name and s_era == era_name:
            sc += 2
        s_prod = getattr(song, "producers", "") or ""
        if producers_str and s_prod and any(p.strip() in s_prod for p in producers_str.split(",") if p.strip()):
            sc += 3
        if category and getattr(song, "category", "") == category:
            sc += 1
        return sc

    candidates.sort(key=_score, reverse=True)
    top = candidates[:10]

    if not top:
        await _send_temporary(ctx, f"No similar songs found for **{title}**.")
        return

    view = SearchPaginationView(ctx=ctx, songs=top, query=f"Similar to: {title}", total_count=len(top))
    embed = view.build_embed()
    view.message = await ctx.send(embed=embed, view=view)


@bot.command(name="stats")
async def listening_stats(ctx: commands.Context):
    """Show the user's personal listening stats."""

    embed = _build_stats_embed(ctx.author)
    await _send_temporary(ctx, embed=embed, delay=30)


@bot.command(name="search")
async def search_songs(ctx: commands.Context, *, query: str):
    """Search for songs by text query and show paginated interactive results."""

    async with ctx.typing():
        api = create_api_client()
        try:
            # Fetch up to 25 results to keep the UI manageable.
            results = api.get_songs(search=query, page=1, page_size=25)
        except JuiceWRLDAPIError as e:
            await _send_temporary(ctx, f"Error while searching songs: {e}")
            return
        finally:
            api.close()

    songs = results.get("results") or []
    if not songs:
        await _send_temporary(
            ctx,
            f"No songs found for `" + query + "`.",
            delay=10,
        )
        return

    total = results.get("count") if isinstance(results, dict) else None

    view = SearchPaginationView(ctx=ctx, songs=songs, query=query, total_count=total)
    embed = view.build_embed()
    view.message = await ctx.send(embed=embed, view=view)


@bot.command(name="song")
async def song_details(ctx: commands.Context, song_id: str):
    """Get detailed info for a single song by ID.

    The song ID must be numeric; if it isn't, we show a helpful
    error message instead of raising a conversion error.
    """

    try:
        song_id_int = int(song_id)
    except ValueError:
        await ctx.send("Song ID must be a number. Example: `!jw song 123`.")
        return

    async with ctx.typing():
        api = create_api_client()
        try:
            song = api.get_song(song_id_int)
        except NotFoundError:
            await ctx.send(f"No song found with ID `{song_id_int}`.")
            return
        except JuiceWRLDAPIError as e:
            await ctx.send(f"Error while fetching song: {e}")
            return
        finally:
            api.close()

    name = getattr(song, "name", getattr(song, "title", "Unknown"))
    category = getattr(song, "category", "?")
    length = getattr(song, "length", "?")
    era_name = getattr(getattr(song, "era", None), "name", "?")
    producers = getattr(song, "producers", None)

    desc_lines = [
        f"**{name}** (ID: `{song_id_int}`)",
        f"Category: `{category}`",
        f"Length: `{length}`",
        f"Era: `{era_name}`",
    ]

    if producers:
        desc_lines.append(f"Producers: {producers}")

    await ctx.send("\n".join(desc_lines))


@bot.command(name="join")
async def join_voice(ctx: commands.Context):
    """Join the voice channel the command author is in."""

    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("You need to be in a voice channel first.")
        return

    channel = ctx.author.voice.channel
    voice: Optional[discord.VoiceClient] = ctx.voice_client

    if voice and voice.is_connected():
        if voice.channel != channel:
            await voice.move_to(channel)
    else:
        await channel.connect()

    await ctx.send(f"Joined voice channel: {channel.name}")


@bot.command(name="leave")
async def leave_voice(ctx: commands.Context):
    """Disconnect from the current voice channel."""

    voice: Optional[discord.VoiceClient] = ctx.voice_client
    if not voice or not voice.is_connected():
        await ctx.send("I'm not connected to a voice channel.")
        return

    # Turn off radio for this guild when leaving and delete the player message
    # after a short delay so users can see the final state briefly.
    if ctx.guild:
        _guild_radio_enabled[ctx.guild.id] = False
        _guild_radio_next.pop(ctx.guild.id, None)
        asyncio.create_task(_delete_now_playing_message_after_delay(ctx.guild.id, 1))

    await voice.disconnect()
    await _send_temporary(ctx, "Disconnected from voice channel.")


@bot.command(name="radio")
async def start_radio(ctx: commands.Context):
    """Start radio mode: continuously play random songs until stopped."""

    if not ctx.guild:
        await ctx.send("Radio mode can only be used in a guild.")
        return

    _guild_radio_enabled[ctx.guild.id] = True
    await _send_temporary(ctx, "Radio mode enabled. Playing random songs until you run `!jw stop`.")

    # If something is already playing, let it finish and the after-callback
    # (if any) will continue the radio. Otherwise, start immediately.
    voice: Optional[discord.VoiceClient] = ctx.voice_client
    if not voice or not voice.is_playing():
        await _play_random_song_in_guild(ctx)


@bot.command(name="stop")
async def stop_radio(ctx: commands.Context):
    """Stop playback and disable radio mode for this guild."""

    if ctx.guild:
        _guild_radio_enabled[ctx.guild.id] = False
        _guild_radio_next.pop(ctx.guild.id, None)

    voice: Optional[discord.VoiceClient] = ctx.voice_client
    if voice and (voice.is_playing() or voice.is_paused()):
        voice.stop()

    await _send_temporary(ctx, "Radio mode disabled and playback stopped.", delay=5)

    # Keep a static player message but show it as idle when radio stops.
    if ctx.guild:
        await _send_player_controls(
            ctx,
            title="Nothing playing",
            path=None,
            is_radio=False,
            metadata={},
            duration_seconds=None,
        )


@bot.command(name="play")
async def play_song(ctx: commands.Context, song_id: str):
    """Play a Juice WRLD song in the caller's voice channel by song ID.

    The song ID must be numeric; if it isn't, we show a helpful
    error message instead of raising a conversion error.
    """

    # Ensure the user is in a voice channel
    if not ctx.author.voice or not ctx.author.voice.channel:
        await _send_temporary(ctx, "You need to be in a voice channel to play music.", delay=5)
        return

    # If radio is currently on, disable it; the requested song will
    # either play next (if something else is already playing) or
    # immediately if nothing is playing.
    radio_was_on = _disable_radio_if_active(ctx)
    if radio_was_on:
        await _send_temporary(ctx, "Radio mode disabled because you requested a specific song.")

    # Support a short debug suffix: e.g. "123d" will enable debug mode
    # and use song ID 123. This keeps the command compact.
    debug = False
    raw_song_id = song_id.strip()
    if raw_song_id.lower().endswith("d"):
        debug = True
        raw_song_id = raw_song_id[:-1].strip()

    try:
        song_id_int = int(raw_song_id)
    except ValueError:
        await _send_temporary(ctx, "Song ID must be a number. Example: `!jw play 123`.", delay=5)
        return

    channel = ctx.author.voice.channel
    voice: Optional[discord.VoiceClient] = ctx.voice_client

    # Connect or move the bot to the caller's channel
    if voice and voice.is_connected():
        if voice.channel != channel:
            await voice.move_to(channel)
    else:
        voice = await channel.connect()

    # First attempt: use the player endpoint helper to resolve a concrete
    # file path / stream URL for this song ID.
    async with ctx.typing():
        api = create_api_client()
        try:
            player_result = api.play_juicewrld_song(song_id_int)
        finally:
            api.close()

    status = player_result.get("status")
    error_detail = player_result.get("error")

    stream_url: Optional[str] = None
    file_path: Optional[str] = player_result.get("file_path")
    path_for_meta: Optional[str] = file_path

    # Decide whether we should fall back to a comp-style resolution path.
    fallback_needed = False
    if status == "not_found":
        # Song is not in the player endpoint – we will try to resolve it via
        # the main song catalog + comp browser.
        fallback_needed = True
    elif status and status not in {"success", "file_not_found_but_url_provided"}:
        # API-level error from the player helper, prefer comp-style fallback.
        fallback_needed = True

    # If the player endpoint claims success or a soft file-not-found, try to
    # validate/stream its file path first.
    if not fallback_needed and file_path:
        async with ctx.typing():
            api = create_api_client()
            try:
                stream_result = api.stream_audio_file(file_path)
            finally:
                api.close()

        stream_status = stream_result.get("status")
        stream_error = stream_result.get("error")

        if stream_status == "success":
            stream_url = stream_result.get("stream_url")
            if not stream_url:
                # Missing URL from a "success" response – treat as fallback.
                fallback_needed = True
            else:
                path_for_meta = file_path
        else:
            # The derived comp path did not actually stream; fall back.
            fallback_needed = True

    elif not fallback_needed and not file_path:
        # No file path from player endpoint; try its direct stream_url.
        direct_url = player_result.get("stream_url")
        if direct_url:
            stream_url = direct_url
            path_for_meta = file_path
        else:
            fallback_needed = True

    # Second attempt: comp-style fallback using the main song catalog and
    # file browser (similar to !jw comp / _play_from_browse).
    if fallback_needed or not stream_url:
        async with ctx.typing():
            api = create_api_client()
            try:
                try:
                    song_obj = api.get_song(song_id_int)
                except NotFoundError:
                    await _send_temporary(
                        ctx,
                        f"No song found with ID `{song_id_int}` in the main catalog.",
                        delay=5,
                    )
                    return
                except JuiceWRLDAPIError as e:
                    await _send_temporary(
                        ctx,
                        f"Error while fetching song `{song_id_int}` from catalog: {e}",
                        delay=5,
                    )
                    return

                # Prefer an explicit comp path from the song object if present.
                comp_path = getattr(song_obj, "path", "") or None

                if comp_path:
                    file_path = comp_path
                    stream_result = api.stream_audio_file(file_path)
                else:
                    # No direct path on the song; search the comp browser by
                    # song title under the Compilation tree.
                    search_title = getattr(song_obj, "name", str(song_id_int))
                    directory = api.browse_files(path="Compilation", search=search_title)
                    files = [
                        item
                        for item in getattr(directory, "items", [])
                        if getattr(item, "type", "file") == "file"
                    ]
                    if not files:
                        await _send_temporary(
                            ctx,
                            f"Could not locate an audio file for song `{song_id_int}` "
                            "via the comp browser.",
                            delay=5,
                        )
                        return

                    target = files[0]
                    file_path = getattr(target, "path", None)
                    if not file_path:
                        await _send_temporary(
                            ctx,
                            "Found a matching comp item but it has no valid file path.",
                            delay=5,
                        )
                        return

                    stream_result = api.stream_audio_file(file_path)

                stream_status = stream_result.get("status")
                stream_error = stream_result.get("error")

                if stream_status != "success":
                    if stream_status == "file_not_found":
                        await _send_temporary(
                            ctx,
                            f"Audio file not found for song `{song_id_int}` (path `{file_path}`). ",
                            delay=5,
                        )
                    elif stream_status == "http_error":
                        await _send_temporary(
                            ctx,
                            f"Could not stream song `{song_id_int}` (HTTP error). "
                            f"Details: {stream_error or stream_status}",
                            delay=5,
                        )
                    else:
                        await _send_temporary(
                            ctx,
                            f"Could not stream song `{song_id_int}` (status: {stream_status}).",
                            delay=5,
                        )
                    return

                stream_url = stream_result.get("stream_url")
                if not stream_url:
                    await _send_temporary(
                        ctx,
                        f"API did not return a stream URL for song `{song_id_int}` (path `{file_path}`).",
                        delay=5,
                    )
                    return

                path_for_meta = file_path
                catalog_song_obj = song_obj
            finally:
                api.close()
    else:
        # We already have a usable stream_url from the player endpoint path
        # or its direct URL. We'll still fetch catalog metadata below.
        catalog_song_obj = None

    if not voice:
        await _send_temporary(ctx, "Internal error: voice client not available.", delay=5)
        return

    # Optional short debug output when the user used an ID like "123d".
    if debug:
        debug_lines = [
            f"Debug: song_id={song_id_int}",
            f"Debug: file_path={file_path or 'N/A'}",
            f"Debug: stream_url={stream_url}",
        ]
        await _send_temporary(ctx, "\n".join(debug_lines), delay=15)

    # Fetch full song metadata for richer Now Playing display.
    song_meta: Dict[str, Any] = {}
    duration_seconds: Optional[int] = None
    try:
        # Reuse the catalog song we fetched during fallback if available;
        # otherwise look it up now.
        if catalog_song_obj is None:
            api = create_api_client()
            try:
                catalog_song_obj = api.get_song(song_id_int)
            finally:
                api.close()

        song_obj = catalog_song_obj

        # Normalize image URL like radio: relative paths ("/assets/...")
        # should become absolute URLs against JUICEWRLD_API_BASE_URL.
        image_url = _normalize_image_url(song_obj.image_url)

        # Build metadata that mirrors the canonical Song JSON model.
        song_meta = _build_song_metadata_from_song(
            song_obj,
            path=path_for_meta,
            image_url=image_url,
        )
        duration_seconds = _parse_length_to_seconds(song_obj.length)
    except Exception:
        # If metadata lookup fails, continue with minimal info.
        song_meta = {"id": song_id_int}

    # Delegate to the shared queue/play helper so this song either queues
    # after the current track or starts immediately.
    await _queue_or_play_now(
        ctx,
        stream_url=stream_url,
        title=song_meta.get("name") or f"Song ID {song_id_int}",
        path=path_for_meta,
        metadata=song_meta,
        duration_seconds=duration_seconds,
    )


@bot.command(name="playfile")
async def play_file(ctx: commands.Context, *, file_path: str):
    """Play an audio file by its internal comp file path.

    This bypasses song IDs and uses the raw file path on the API side.
    """

    # Ensure the user is in a voice channel
    if not ctx.author.voice or not ctx.author.voice.channel:
        await _send_temporary(ctx, "You need to be in a voice channel to play music.", delay=5)
        return

    # (radio is already disabled in _play_from_browse for search/comp
    # commands, so we don't toggle it here again.)

    channel = ctx.author.voice.channel
    voice: Optional[discord.VoiceClient] = ctx.voice_client

    # Connect or move the bot to the caller's channel
    if voice and voice.is_connected():
        if voice.channel != channel:
            await voice.move_to(channel)
    else:
        voice = await channel.connect()

    async with ctx.typing():
        api = create_api_client()
        try:
            result = api.stream_audio_file(file_path)
        finally:
            api.close()

    status = result.get("status")
    error_detail = result.get("error")

    if status != "success":
        if status == "file_not_found":
            await _send_temporary(
                ctx,
                f"Audio file not found for path `{file_path}`. "
                "Double-check the path from the comp browser.",
                delay=5,
            )
        elif status == "http_error":
            await _send_temporary(
                ctx,
                f"Could not stream file `{file_path}` (HTTP error). "
                f"Details: {error_detail or status}",
                delay=5,
            )
        else:
            if error_detail:
                await _send_temporary(
                    ctx,
                    f"Could not stream file `{file_path}` (status: {status}). "
                    f"Details: {error_detail}",
                    delay=5,
                )
            else:
                await _send_temporary(
                    ctx,
                    f"Could not stream file `{file_path}` (status: {status}).",
                    delay=5,
                )
        return

    stream_url = result.get("stream_url")
    if not stream_url:
        await _send_temporary(
            ctx,
            f"API did not return a stream URL for file `{file_path}`.",
            delay=5,
        )
        return

    if not voice:
        await _send_temporary(ctx, "Internal error: voice client not available.", delay=5)
        return

    await _queue_or_play_now(
        ctx,
        stream_url=stream_url,
        title=f"File {file_path}",
        path=file_path,
        metadata={"path": file_path},
        duration_seconds=None,
    )


@bot.command(name="playsearch")
async def play_search(ctx: commands.Context, *, query: str):
    """Search all comp files by name and play the best match."""

    await _play_from_browse(ctx, query=query, base_path="", scope_description="the comp browser")


@bot.command(name="stusesh")
async def play_studio_session(ctx: commands.Context, *, query: str):
    """Search Studio Sessions only and play the best match."""

    await _play_from_browse(
        ctx,
        query=query,
        base_path="Studio Sessions",
        scope_description="Studio Sessions",
    )


@bot.command(name="og")
async def play_original_file(ctx: commands.Context, *, query: str):
    """Search Original Files only and play the best match."""

    await _play_from_browse(
        ctx,
        query=query,
        base_path="Original Files",
        scope_description="Original Files",
    )


@bot.command(name="seshedits")
async def play_session_edit(ctx: commands.Context, *, query: str):
    """Search Session Edits only and play the best match."""

    await _play_from_browse(
        ctx,
        query=query,
        base_path="Session Edits",
        scope_description="Session Edits",
    )


@bot.command(name="stems")
async def play_stem_edit(ctx: commands.Context, *, query: str):
    """Search Stem Edits only and play the best match."""

    await _play_from_browse(
        ctx,
        query=query,
        base_path="Stem Edits",
        scope_description="Stem Edits",
    )


@bot.command(name="comp")
async def play_compilation(ctx: commands.Context, *, query: str):
    """Search Compilation (released/unreleased/misc) and play the best match."""

    await _play_from_browse(
        ctx,
        query=query,
        base_path="Compilation",
        scope_description="Compilation (released/unreleased/misc)",
    )


@bot.command(name="playlists")
async def list_playlists(ctx: commands.Context):
    """List the invoking user's playlists with a short preview."""

    user = ctx.author
    playlists = _user_playlists.get(user.id) or {}
    if not playlists:
        await ctx.send(
            "You don't have any playlists yet. Use ❤ Like on the player to add "
            "the current song to your Likes playlist."
        )
        return

    embed = _build_playlists_embed_for_user(user, playlists)
    await ctx.send(embed=embed)


@bot.group(name="playlist", invoke_without_command=True)
async def playlist_group(ctx: commands.Context):
    """Playlist subcommands: show, play, add, delete, rename, remove."""

    usage = (
        "**Playlist commands:**\n"\
        "`!jw playlists` - List your playlists.\n"\
        "`!jw playlist show <name>` - Show full contents of one playlist.\n"\
        "`!jw playlist play <name>` - Queue/play all tracks in a playlist.\n"\
        "`!jw playlist add <name> <song_id>` - Add a song (by ID) to a playlist.\n"\
        "`!jw playlist delete <name>` - Delete one of your playlists.\n"\
        "`!jw playlist rename <old> <new>` - Rename one of your playlists.\n"\
        "`!jw playlist remove <name> <index>` - Remove a track (1-based index)."
    )
    await ctx.send(usage)


@bot.group(name="pl", invoke_without_command=True)
async def pl_group(ctx: commands.Context):
    """Short playlist aliases using !jw pl."""

    # Default: behave like !jw playlists (list playlists).
    user = ctx.author
    playlists = _user_playlists.get(user.id) or {}
    if not playlists:
        await ctx.send(
            "You don't have any playlists yet. Use ❤ Like on the player to add "
            "the current song to your Likes playlist."
        )
        return

    embed = _build_playlists_embed_for_user(user, playlists)
    await ctx.send(embed=embed)


@pl_group.command(name="show")
async def pl_show(ctx: commands.Context, *, name: str):
    await playlist_show(ctx, name=name)


@pl_group.command(name="play")
async def pl_play(ctx: commands.Context, *, name: str):
    await playlist_play(ctx, name=name)


@pl_group.command(name="add")
async def pl_add(ctx: commands.Context, *, name_and_id: str):
    await playlist_add(ctx, name_and_id=name_and_id)


@pl_group.command(name="delete")
async def pl_delete(ctx: commands.Context, *, name: str):
    await playlist_delete(ctx, name=name)


@pl_group.command(name="rename")
async def pl_rename(ctx: commands.Context, old: str, new: str):
    await playlist_rename(ctx, old=old, new=new)


@pl_group.command(name="remove")
async def pl_remove(ctx: commands.Context, name: str, index: int):
    await playlist_remove(ctx, name=name, index=index)


@playlist_group.command(name="show")
async def playlist_show(ctx: commands.Context, *, name: str):
    """Show all tracks in one of the user's playlists."""

    playlists = _user_playlists.get(ctx.author.id) or {}
    playlist = playlists.get(name)
    if not playlist:
        await ctx.send(f"No playlist named `{name}` found.")
        return

    if not playlist:
        await ctx.send(f"Playlist `{name}` is empty.")
        return

    lines = [f"Tracks in **{name}**:"]
    for idx, track in enumerate(playlist, start=1):
        tname = track.get("name") or track.get("id") or "Unknown"
        tid = track.get("id")
        path = track.get("path")
        piece = f"{idx}. {tname}"
        if tid is not None:
            piece += f" (ID: {tid})"
        if path:
            piece += f" – `{path}`"
        lines.append(piece)

    # Discord message limit is large; truncate defensively.
    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900] + "\n... (truncated)"

    await ctx.send(text)


@playlist_group.command(name="play")
async def playlist_play(ctx: commands.Context, *, name: str):
    """Queue or play all tracks from one of the user's playlists."""

    playlists = _user_playlists.get(ctx.author.id) or {}
    playlist = playlists.get(name)
    if not playlist:
        await ctx.send(f"No playlist named `{name}` found.")
        return

    if not playlist:
        await ctx.send(f"Playlist `{name}` is empty.")
        return

    # Ensure the user is in a voice channel
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("You need to be in a voice channel to play a playlist.")
        return

    # Disable radio if it is on for this guild so playlist has priority.
    _disable_radio_if_active(ctx)

    channel = ctx.author.voice.channel
    voice: Optional[discord.VoiceClient] = ctx.voice_client

    # Connect or move the bot to the caller's channel
    if voice and voice.is_connected():
        if voice.channel != channel:
            await voice.move_to(channel)
    else:
        voice = await channel.connect()

    if not voice:
        await ctx.send("Internal error: voice client not available.")
        return

    queued = 0
    for track in playlist:
        file_path = track.get("path")
        if not file_path:
            continue

        api = create_api_client()
        try:
            result = api.stream_audio_file(file_path)
        finally:
            api.close()

        status = result.get("status")
        if status != "success":
            continue

        stream_url = result.get("stream_url")
        if not stream_url:
            continue

        title = track.get("name") or f"Playlist {name} item"
        metadata = track.get("metadata") or {}
        duration_seconds = _extract_duration_seconds(metadata, track)

        await _queue_or_play_now(
            ctx,
            stream_url=stream_url,
            title=str(title),
            path=file_path,
            metadata=metadata,
            duration_seconds=duration_seconds,
            silent=True,
        )
        queued += 1

    if not queued:
        await ctx.send(
            f"Could not queue any tracks from playlist `{name}` (all items missing paths or failed to stream)."
        )
    else:
        await _send_temporary(ctx, f"Queued {queued} track(s) from playlist `{name}`.")


@playlist_group.command(name="add")
async def playlist_add(ctx: commands.Context, *, name_and_id: str):
    """Add a song (by ID) to one of the user's playlists.

    Usage: !jw playlist add <playlist_name> <song_id>
    You can use spaces in the playlist name; the last argument is treated as the song ID.
    """

    parts = name_and_id.strip().split()
    if len(parts) < 2:
        await ctx.send("Usage: `!jw playlist add <playlist_name> <song_id>`.")
        return

    song_id_str = parts[-1]
    playlist_name = " ".join(parts[:-1])

    if not playlist_name:
        await ctx.send("Playlist name cannot be empty.")
        return

    playlists = _get_or_create_user_playlists(ctx.author.id)
    playlist = playlists.setdefault(playlist_name, [])

    # Parse song ID
    try:
        song_id_int = int(song_id_str)
    except ValueError:
        await ctx.send("Song ID must be a number. Example: `!jw playlist add MyList 123`.")
        return

    # Resolve a comp file path for this song using the player endpoint.
    async with ctx.typing():
        api = create_api_client()
        try:
            player_result = api.play_juicewrld_song(song_id_int)
        finally:
            api.close()

    status = player_result.get("status")
    error_detail = player_result.get("error")

    if status == "not_found":
        await ctx.send(
            f"No playable song found for ID `{song_id_int}` in the player endpoint; "
            "it may not be available for streaming yet."
        )
        return

    if status and status not in {"success", "file_not_found_but_url_provided"}:
        if error_detail:
            await ctx.send(
                f"Could not get a playable file path for song `{song_id_int}` "
                f"(status: {status}). Details: {error_detail}"
            )
        else:
            await ctx.send(
                f"Could not get a playable file path for song `{song_id_int}` "
                f"(status: {status})."
            )
        return

    file_path = player_result.get("file_path")
    if not file_path:
        await ctx.send(
            f"Player endpoint did not return a comp file path for song `{song_id_int}`; "
            "cannot add to playlist."
        )
        return

    # Fetch full song metadata for display and future playback.
    async with ctx.typing():
        api = create_api_client()
        try:
            song_obj = api.get_song(song_id_int)
        except Exception:
            song_obj = None
        finally:
            api.close()

    if song_obj is not None:
        image_url = _normalize_image_url(getattr(song_obj, "image_url", None))
        meta = _build_song_metadata_from_song(
            song_obj,
            path=file_path,
            image_url=image_url,
        )
    else:
        meta = {"id": song_id_int, "path": file_path}

    title = str(meta.get("name") or f"Song {song_id_int}")
    song_id_val = meta.get("id") or meta.get("song_id")

    # Avoid duplicates: match by song ID or path
    for track in playlist:
        if song_id_val is not None and track.get("id") == song_id_val:
            await ctx.send(f"`{title}` is already in playlist `{playlist_name}`.")
            return
        if file_path and track.get("path") == file_path:
            await ctx.send(f"`{title}` is already in playlist `{playlist_name}`.")
            return

    playlist.append(
        {
            "id": song_id_val,
            "name": title,
            "path": file_path,
            "metadata": meta,
            "added_at": time.time(),
        }
    )

    _save_user_playlists_to_disk()
    await ctx.send(f"Added `{title}` (ID `{song_id_int}`) to playlist `{playlist_name}`.")


@playlist_group.command(name="delete")
async def playlist_delete(ctx: commands.Context, *, name: str):
    """Delete one of the user's playlists."""

    playlists = _user_playlists.get(ctx.author.id) or {}
    if name not in playlists:
        await ctx.send(f"No playlist named `{name}` found.")
        return

    del playlists[name]
    if not playlists:
        # If the user now has no playlists, remove their entry entirely.
        _user_playlists.pop(ctx.author.id, None)

    _save_user_playlists_to_disk()
    await ctx.send(f"Deleted playlist `{name}`.")


@playlist_group.command(name="rename")
async def playlist_rename(ctx: commands.Context, old: str, new: str):
    """Rename one of the user's playlists."""

    playlists = _user_playlists.get(ctx.author.id) or {}
    if old not in playlists:
        await ctx.send(f"No playlist named `{old}` found.")
        return

    if new in playlists:
        await ctx.send(f"You already have a playlist named `{new}`.")
        return

    playlists[new] = playlists.pop(old)
    _save_user_playlists_to_disk()
    await ctx.send(f"Renamed playlist `{old}` to `{new}`.")


@playlist_group.command(name="remove")
async def playlist_remove(ctx: commands.Context, name: str, index: int):
    """Remove a single track by 1-based index from a playlist."""

    playlists = _user_playlists.get(ctx.author.id) or {}
    playlist = playlists.get(name)
    if not playlist:
        await ctx.send(f"No playlist named `{name}` found.")
        return

    if index < 1 or index > len(playlist):
        await ctx.send(f"Index {index} is out of range for playlist `{name}` (size {len(playlist)}).")
        return

    removed = playlist.pop(index - 1)
    _save_user_playlists_to_disk()

    title = removed.get("name") or removed.get("id") or "Unknown track"
    await ctx.send(f"Removed `{title}` (index {index}) from playlist `{name}`.")


async def _play_from_browse(
    ctx: commands.Context,
    *,
    query: str,
    base_path: str,
    scope_description: str,
) -> None:
    """Shared helper to search the file browser and play the first match.

    Args:
        ctx: Discord context.
        query: Search term for the file name.
        base_path: Directory to search inside ("" for root/all).
        scope_description: Human-readable description of what we're searching.
    """

    # Ensure the user is in a voice channel
    if not ctx.author.voice or not ctx.author.voice.channel:
        await _send_temporary(ctx, "You need to be in a voice channel to play music.", delay=5)
        return

    # Any search-style playback should disable radio and then either play or
    # queue the requested track.
    radio_was_on = _disable_radio_if_active(ctx)
    if radio_was_on:
        await _send_temporary(
            ctx,
            "Radio mode disabled because you used a search/comp playback command.",
        )

    channel = ctx.author.voice.channel
    voice: Optional[discord.VoiceClient] = ctx.voice_client

    # Connect or move the bot to the caller's channel
    if voice and voice.is_connected():
        if voice.channel != channel:
            await voice.move_to(channel)
    else:
        voice = await channel.connect()

    async with ctx.typing():
        api = create_api_client()
        try:
            directory = api.browse_files(path=base_path, search=query)
            files = [item for item in directory.items if getattr(item, "type", "file") == "file"]
            if not files:
                await _send_temporary(
                    ctx,
                    f"No files found matching `{query}` in {scope_description}.",
                    delay=5,
                )
                return

            target = files[0]
            file_path = getattr(target, "path", None)
            if not file_path:
                await _send_temporary(
                    ctx,
                    "Found a matching item but it does not have a valid file path.",
                    delay=5,
                )
                return

            result = api.stream_audio_file(file_path)
        finally:
            api.close()

    status = result.get("status")
    error_detail = result.get("error")

    if status != "success":
        if status == "file_not_found":
            await _send_temporary(
                ctx,
                f"Audio file not found for search `{query}` (resolved path `{file_path}`). "
                "Double-check in the comp browser.",
                delay=5,
            )
        elif status == "http_error":
            await _send_temporary(
                ctx,
                f"Could not stream file for `{query}` (HTTP error). "
                f"Details: {error_detail or status}",
                delay=5,
            )
        else:
            if error_detail:
                await _send_temporary(
                    ctx,
                    f"Could not stream file for `{query}` (status: {status}). "
                    f"Details: {error_detail}",
                    delay=5,
                )
            else:
                await _send_temporary(
                    ctx,
                    f"Could not stream file for `{query}` (status: {status}).",
                    delay=5,
                )
        return

    stream_url = result.get("stream_url")
    if not stream_url:
        await _send_temporary(
            ctx,
            f"API did not return a stream URL for search `{query}` (resolved path `{file_path}`).",
            delay=5,
        )
        return

    if not voice:
        await _send_temporary(ctx, "Internal error: voice client not available.", delay=5)
        return

    display_name = getattr(target, "name", file_path)

    # Try to enrich with song metadata by searching the catalog by name.
    # Strip extension like ".mp3" so "Fresh Air.mp3" -> "Fresh Air" before
    # searching the songs endpoint.
    base_title, _ext = os.path.splitext(display_name)

    song_meta: Dict[str, Any] = {"length": None}
    duration_seconds: Optional[int] = None
    try:
        api = create_api_client()
        search_data = api.get_songs(search=base_title, page=1, page_size=1)
        results = search_data.get("results") or []
        if results:
            song_obj = results[0]
            image_url = _normalize_image_url(song_obj.image_url)

            # Build metadata mirroring the canonical Song model.
            song_meta = _build_song_metadata_from_song(
                song_obj,
                path=file_path,
                image_url=image_url,
            )
            duration_seconds = _parse_length_to_seconds(song_obj.length)
        else:
            # No song match; at least carry the path so the UI can show it.
            song_meta = {"path": file_path}
    except Exception:
        song_meta = {"path": file_path}

    await _queue_or_play_now(
        ctx,
        stream_url=stream_url,
        title=display_name,
        path=file_path,
        metadata=song_meta,
        duration_seconds=duration_seconds,
    )


async def _fetch_random_radio_song(include_stream_url: bool = True) -> Optional[Dict[str, Any]]:
    """Fetch a random radio song and return its data (title, stream_url, metadata, duration).
    
    Args:
        include_stream_url: If True, fetch and include stream_url. If False, only fetch metadata
                           (useful for pre-fetching to avoid stale URLs).
    
    Returns None if fetching fails.
    """
    api = create_api_client()
    try:
        # 1) Get a random radio song with metadata from /radio/random/.
        radio_data = api.get_random_radio_song()
        chosen_title = str(radio_data.get("title") or "Unknown")

        # According to docs, `path` and `id` are both the comp file path.
        file_path = (
            radio_data.get("path")
            or radio_data.get("id")
        )
        if not file_path:
            return None

        song_info = radio_data.get("song") or {}
        song_id = song_info.get("id")

        # Prefer the canonical song name from embedded song metadata.
        if song_info.get("name"):
            chosen_title = str(song_info.get("name"))

        stream_url = None
        if include_stream_url:
            # 2) Use the comp streaming helper to validate and build a stream URL.
            stream_result = api.stream_audio_file(file_path)
            status = stream_result.get("status")
            if status != "success":
                return None

            stream_url = stream_result.get("stream_url")
            if not stream_url:
                return None

        # 3) Build song metadata for artwork, duration, etc.
        duration_seconds: Optional[int] = None
        if song_info:
            length = song_info.get("length") or ""
            duration_seconds = _parse_length_to_seconds(length)

            song_meta = dict(song_info)
            song_meta["path"] = file_path
            song_meta["image_url"] = _normalize_image_url(song_meta.get("image_url"))
        else:
            song_meta = {"id": song_id, "path": file_path}

        return {
            "title": chosen_title,
            "stream_url": stream_url,
            "metadata": song_meta,
            "duration_seconds": duration_seconds,
            "path": file_path,
        }
    except Exception:
        return None
    finally:
        api.close()


async def _get_fresh_stream_url(file_path: str) -> Optional[str]:
    """Get a fresh stream URL for a file path."""
    api = create_api_client()
    try:
        stream_result = api.stream_audio_file(file_path)
        if stream_result.get("status") == "success":
            return stream_result.get("stream_url")
        return None
    except Exception:
        return None
    finally:
        api.close()


async def _prefetch_next_radio_song(guild_id: int) -> None:
    """Pre-fetch the next random radio song for a guild and store it.
    
    Only fetches metadata (not stream URL) to avoid stale URLs when the song is played later.
    """
    song_data = await _fetch_random_radio_song(include_stream_url=False)
    if song_data:
        _guild_radio_next[guild_id] = song_data


async def _play_random_song_in_guild(ctx: commands.Context) -> None:
    """Pick a random song from the radio endpoint and play it.

    Uses `/juicewrld/radio/random/` plus the player endpoint to get a
    streaming URL and rich metadata. If radio is disabled for the guild,
    this is a no-op.
    
    If a pre-fetched song exists in _guild_radio_next, it will be used
    instead of fetching a new random song.
    """

    if not ctx.guild or not _guild_radio_enabled.get(ctx.guild.id):
        return

    guild_id = ctx.guild.id

    # Ensure the user is in a voice channel
    if not ctx.author.voice or not ctx.author.voice.channel:
        await _send_temporary(ctx, "You need to be in a voice channel to use radio.", delay=5)
        return

    channel = ctx.author.voice.channel
    voice: Optional[discord.VoiceClient] = ctx.voice_client

    # Connect or move the bot to the caller's channel
    if voice and voice.is_connected():
        if voice.channel != channel:
            await voice.move_to(channel)
    else:
        voice = await channel.connect()

    # Check for pre-fetched song first
    prefetched = _guild_radio_next.pop(guild_id, None)
    
    if prefetched:
        # Use the pre-fetched song metadata but get a FRESH stream URL
        chosen_title = prefetched.get("title", "Unknown")
        song_meta = prefetched.get("metadata", {})
        duration_seconds = prefetched.get("duration_seconds")
        file_path = prefetched.get("path")
        
        # Get fresh stream URL to avoid stale/expired URLs
        if file_path:
            stream_url = await _get_fresh_stream_url(file_path)
        else:
            stream_url = None
    else:
        # Fetch a new random song (with stream URL)
        async with ctx.typing():
            song_data = await _fetch_random_radio_song(include_stream_url=True)
        
        if not song_data:
            await _send_temporary(
                ctx,
                "Radio: could not fetch a random song.",
                delay=5,
            )
            return
        
        stream_url = song_data.get("stream_url")
        chosen_title = song_data.get("title", "Unknown")
        song_meta = song_data.get("metadata", {})
        duration_seconds = song_data.get("duration_seconds")

    if not stream_url:
        await _send_temporary(
            ctx,
            "Radio: no stream URL available.",
            delay=5,
        )
        # Try again with a new song after a short delay
        await asyncio.sleep(1)
        if _guild_radio_enabled.get(guild_id):
            asyncio.create_task(_play_random_song_in_guild(ctx))
        return

    if not ctx.guild or not _guild_radio_enabled.get(ctx.guild.id):
        return

    if not voice:
        await _send_temporary(ctx, "Internal error: voice client not available.", delay=5)
        return

    if voice.is_playing() or voice.is_paused():
        # Something is already playing; don't interrupt it.
        # The after-callback will detect that radio is enabled and
        # call _play_random_song_in_guild once the current track ends.
        return

    ffmpeg_before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    ffmpeg_options = "-vn"

    try:
        source = discord.FFmpegPCMAudio(
            stream_url,
            before_options=ffmpeg_before,
            options=ffmpeg_options,
        )
    except Exception as e:  # pragma: no cover
        await _send_temporary(
            ctx,
            f"Radio: failed to create audio source: {e}",
            delay=5,
        )
        return

    # After callback to continue radio or, if radio was turned off while a
    # track was playing, fall back to the normal queue.
    def _after_playback(error: Optional[Exception]) -> None:
        if error:
            # Log error to stderr; Discord callbacks can't await
            print(f"Radio playback error: {error}", file=sys.stderr)

        if not ctx.guild:
            return

        guild_id = ctx.guild.id

        if _guild_radio_enabled.get(guild_id):
            # Add a small delay on error to prevent rapid looping through songs
            async def _continue_radio():
                if error:
                    await asyncio.sleep(2)  # Wait before retrying on error
                await _play_random_song_in_guild(ctx)
            
            fut = _continue_radio()
            asyncio.run_coroutine_threadsafe(fut, bot.loop)
            return

        # Radio is off; if there is anything queued, continue with the
        # regular queue playback.
        queue = _guild_queue.get(guild_id) or []
        if queue:
            fut = _play_next_from_queue(ctx)
            asyncio.run_coroutine_threadsafe(fut, bot.loop)

    voice.play(source, after=_after_playback)

    # Pre-fetch the next radio song BEFORE showing controls so "Up Next" is populated
    await _prefetch_next_radio_song(guild_id)

    radio_meta = song_meta.copy()
    radio_meta["source"] = "radio"

    await _send_player_controls(
        ctx,
        title=chosen_title,
        path=song_meta.get("path"),
        is_radio=True,
        metadata=radio_meta,
        duration_seconds=duration_seconds,
    )


# --- Context menu commands (right-click actions) ---


@bot.tree.context_menu(name="View Listening Stats")
async def context_view_stats(interaction: discord.Interaction, user: discord.Member) -> None:
    """Right-click a user to view their listening stats."""
    embed = _build_stats_embed(user)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    _schedule_interaction_deletion(interaction, 30)


@bot.tree.context_menu(name="Play This Song")
async def context_play_from_message(interaction: discord.Interaction, message: discord.Message) -> None:
    """Right-click a message (e.g. Now Playing / SOTD embed) to play that song."""

    user = interaction.user
    if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
        await interaction.response.send_message(
            "You need to be in a voice channel to play music.", ephemeral=True
        )
        return

    # Try to extract a song title from the message's embeds.
    song_title: Optional[str] = None
    for emb in message.embeds:
        # SOTD embed: title is "Song of the Day", song name is in description bold text.
        if emb.title and "Song of the Day" in emb.title and emb.description:
            # Description is like "**Song Name**"
            song_title = emb.description.strip("* ")
            break
        # Now Playing embed: title is "Now Playing", description is the song name.
        if emb.title == "Now Playing" and emb.description:
            song_title = emb.description.strip()
            break
        # Search result embed: description starts with **Name**
        if emb.description and emb.description.startswith("**"):
            # Extract the bold text: **Name** (ID: ...)
            bold_end = emb.description.find("**", 2)
            if bold_end > 2:
                song_title = emb.description[2:bold_end]
                break

    if not song_title:
        await interaction.response.send_message(
            "Could not find a song in this message.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    # Search the API for this song and try to play it.
    api = create_api_client()
    try:
        results = api.get_songs(search=song_title, page=1, page_size=1)
    except Exception as e:
        await interaction.followup.send(f"Error searching: {e}", ephemeral=True)
        return
    finally:
        api.close()

    songs = results.get("results") or []
    if not songs:
        await interaction.followup.send(
            f"No playable song found for `{song_title}`.", ephemeral=True
        )
        return

    song = songs[0]
    song_id = getattr(song, "id", None)
    if not song_id:
        await interaction.followup.send("Song has no ID.", ephemeral=True)
        return

    # Use the existing play logic via a Context.
    ctx = await commands.Context.from_interaction(interaction)
    await play_song(ctx, str(song_id))
    await interaction.followup.send(f"Playing **{song_title}**.", ephemeral=True)


# --- Application Emojis management (admin) ---


@bot.command(name="emoji")
@commands.has_permissions(administrator=True)
async def emoji_command(ctx: commands.Context, action: str = "list", *, name: str = ""):
    """Manage application emojis.

    Usage:
        !jw emoji list             — List all app emojis
        !jw emoji upload <name>    — Upload an attached image as an app emoji
        !jw emoji delete <name>    — Delete an app emoji by name
    """
    app_id = bot.user.id if bot.user else None
    if not app_id:
        await ctx.send("Bot is not ready yet.")
        return

    action = action.lower()

    if action == "list":
        await _emoji_list(ctx, app_id)
    elif action == "upload":
        await _emoji_upload(ctx, app_id, name.strip())
    elif action == "delete":
        await _emoji_delete(ctx, app_id, name.strip())
    else:
        await _send_temporary(ctx, "Usage: `!jw emoji list`, `!jw emoji upload <name>`, `!jw emoji delete <name>`")


async def _emoji_list(ctx: commands.Context, app_id: int) -> None:
    """List all application emojis."""
    url = f"https://discord.com/api/v10/applications/{app_id}/emojis"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                await ctx.send(f"Failed to fetch emojis: HTTP {resp.status}")
                return
            data = await resp.json()

    items = data.get("items", [])
    if not items:
        await ctx.send("No application emojis uploaded yet.")
        return

    lines = []
    for e in items:
        eid = e.get("id", "?")
        ename = e.get("name", "?")
        animated = e.get("animated", False)
        prefix = "a" if animated else ""
        lines.append(f"<{prefix}:{ename}:{eid}> `{ename}` (ID: {eid})")

    embed = discord.Embed(
        title=f"Application Emojis ({len(items)})",
        description="\n".join(lines),
        colour=discord.Colour.purple(),
    )
    await ctx.send(embed=embed)


async def _emoji_upload(ctx: commands.Context, app_id: int, name: str) -> None:
    """Upload an attached image as an application emoji."""
    if not name:
        await _send_temporary(ctx, "Provide a name: `!jw emoji upload my_emoji` (attach an image).")
        return

    if not ctx.message.attachments:
        await _send_temporary(ctx, "Attach an image file to upload as an emoji.")
        return

    attachment = ctx.message.attachments[0]
    if not attachment.content_type or not attachment.content_type.startswith("image/"):
        await _send_temporary(ctx, "The attachment must be an image (PNG, GIF, etc.).")
        return

    image_bytes = await attachment.read()
    if len(image_bytes) > 256 * 1024:
        await _send_temporary(ctx, "Image must be under 256 KB.")
        return

    # Determine MIME type for data URI.
    mime = attachment.content_type or "image/png"
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_uri = f"data:{mime};base64,{b64}"

    url = f"https://discord.com/api/v10/applications/{app_id}/emojis"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"name": name, "image": data_uri}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status in (200, 201):
                result = await resp.json()
                eid = result.get("id", "?")
                await ctx.send(f"✅ Emoji `{name}` uploaded! Use it as `<:{name}:{eid}>`")
            else:
                body = await resp.text()
                await ctx.send(f"❌ Upload failed: HTTP {resp.status}\n```{body[:500]}```")


async def _emoji_delete(ctx: commands.Context, app_id: int, name: str) -> None:
    """Delete an application emoji by name."""
    if not name:
        await _send_temporary(ctx, "Provide the emoji name: `!jw emoji delete my_emoji`")
        return

    # First, find the emoji ID by listing all.
    list_url = f"https://discord.com/api/v10/applications/{app_id}/emojis"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}

    async with aiohttp.ClientSession() as session:
        async with session.get(list_url, headers=headers) as resp:
            if resp.status != 200:
                await ctx.send(f"Failed to list emojis: HTTP {resp.status}")
                return
            data = await resp.json()

        items = data.get("items", [])
        target = None
        for e in items:
            if e.get("name", "").lower() == name.lower():
                target = e
                break

        if not target:
            await _send_temporary(ctx, f"No emoji named `{name}` found.")
            return

        eid = target["id"]
        del_url = f"https://discord.com/api/v10/applications/{app_id}/emojis/{eid}"
        async with session.delete(del_url, headers=headers) as resp:
            if resp.status == 204:
                await ctx.send(f"✅ Emoji `{name}` deleted.")
            else:
                body = await resp.text()
                await ctx.send(f"❌ Delete failed: HTTP {resp.status}\n```{body[:500]}```")


# --- Linked Roles server (started in on_ready) ---


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

    # Let the web server look up stats from the bot's in-memory data.
    def _get_user_stats(user_id: int) -> Optional[Dict[str, Any]]:
        return _user_listening_stats.get(user_id)

    set_stats_callback(_get_user_stats)

    # Register the connection metadata schema with Discord.
    if DISCORD_TOKEN:
        ok = await register_metadata_schema(DISCORD_TOKEN)
        if ok:
            print("[linked_roles] Metadata schema registered.")

    # Run uvicorn in the background on the existing event loop.
    config = uvicorn.Config(lr_app, host="0.0.0.0", port=LINKED_ROLES_PORT, log_level="warning")
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    print(f"[linked_roles] Web server started on port {LINKED_ROLES_PORT}.")


def main() -> None:
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN environment variable is not set.")
        sys.exit(1)

    # Register the /jw slash command group on the bot's tree.
    bot.tree.add_command(jw_group)

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
