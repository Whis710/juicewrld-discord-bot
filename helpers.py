"""Shared utility / helper functions for the Juice WRLD Discord bot.

Every function here is a pure utility that does NOT depend on the ``bot``
instance, the ``jw_group``, or any command decorator.  This keeps them
importable from anywhere without circular-import issues.
"""

import asyncio
import os
import time
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from client import JuiceWRLDAPI
from genius_client import GeniusClient
from constants import JUICEWRLD_API_BASE_URL, NOTHING_PLAYING, GENIUS_API_TOKEN as GENIUS_TOKEN
from exceptions import JuiceWRLDAPIError
import state


# ── Singleton API client ─────────────────────────────────────────────

_api_client: Optional[JuiceWRLDAPI] = None


def get_api() -> JuiceWRLDAPI:
    """Return the shared async API client (created on first call)."""
    global _api_client
    if _api_client is None:
        _api_client = JuiceWRLDAPI(base_url=JUICEWRLD_API_BASE_URL)
    return _api_client


async def close_api() -> None:
    """Close the shared API client (call during bot shutdown)."""
    global _api_client
    if _api_client is not None:
        await _api_client.close()
        _api_client = None


# ── Singleton Genius client ──────────────────────────────────────────

_genius_client: Optional[GeniusClient] = None


def get_genius() -> Optional[GeniusClient]:
    """Return the shared Genius client, or None if no token is configured."""
    global _genius_client
    if _genius_client is None and GENIUS_TOKEN:
        _genius_client = GeniusClient(access_token=GENIUS_TOKEN)
    return _genius_client


# ── Parsing / formatting helpers ─────────────────────────────────────

def parse_length_to_seconds(length: str) -> Optional[int]:
    """Convert a length string like ``"3:45"`` or ``"01:02:03"`` to seconds."""
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


def format_progress_bar(current: int, total: Optional[int], width: int = 10) -> str:
    """Return a simple text progress bar and time display."""
    if not total or total <= 0 or current <= 0:
        return f"00:00 / {time.strftime('%M:%S', time.gmtime(total)) if total else '?:??'}"

    current = max(0, min(current, total))
    filled = int(width * (current / total))
    bar = "▮" * filled + "▯" * (width - filled)
    return f"{bar} {time.strftime('%M:%S', time.gmtime(current))} / {time.strftime('%M:%S', time.gmtime(total))}"


def normalize_image_url(image_url: Optional[str]) -> Optional[str]:
    """Convert relative image URLs to absolute URLs."""
    if image_url and isinstance(image_url, str) and image_url.startswith("/"):
        return f"{JUICEWRLD_API_BASE_URL}{image_url}"
    return image_url


def extract_duration_seconds(
    metadata: Dict[str, Any], track: Optional[Dict[str, Any]] = None
) -> Optional[int]:
    """Extract duration in seconds from metadata or track dict."""
    length_str = metadata.get("length") or (track.get("length") if track else None)
    if length_str:
        return parse_length_to_seconds(length_str)
    return None


# ── Song metadata builder ────────────────────────────────────────────

def build_song_metadata_from_song(
    song_obj: Any,
    *,
    path: Optional[str] = None,
    image_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a metadata dict that mirrors the canonical Song model JSON."""

    era_obj = getattr(song_obj, "era", None)
    era_dict: Optional[Dict[str, Any]] = None
    if era_obj is not None:
        era_dict = {
            "id": getattr(era_obj, "id", None),
            "name": getattr(era_obj, "name", None),
            "description": getattr(era_obj, "description", None),
            "time_frame": getattr(era_obj, "time_frame", None),
            "play_count": getattr(era_obj, "play_count", 0),
        }

    final_image_url = image_url if image_url is not None else getattr(song_obj, "image_url", None)

    meta: Dict[str, Any] = {
        "id": getattr(song_obj, "id", None),
        "public_id": getattr(song_obj, "public_id", None),
        "name": getattr(song_obj, "name", None),
        "original_key": getattr(song_obj, "original_key", None),
        "category": getattr(song_obj, "category", None),
        "path": path,
        "era": era_dict,
        "track_titles": getattr(song_obj, "track_titles", None),
        "session_titles": getattr(song_obj, "session_titles", None),
        "session_tracking": getattr(song_obj, "session_tracking", None),
        "credited_artists": getattr(song_obj, "credited_artists", None),
        "producers": getattr(song_obj, "producers", None),
        "engineers": getattr(song_obj, "engineers", None),
        "recording_locations": getattr(song_obj, "recording_locations", None),
        "record_dates": getattr(song_obj, "record_dates", None),
        "dates": getattr(song_obj, "dates", None),
        "length": getattr(song_obj, "length", None),
        "bitrate": getattr(song_obj, "bitrate", None),
        "instrumentals": getattr(song_obj, "instrumentals", None),
        "instrumental_names": getattr(song_obj, "instrumental_names", None),
        "file_names": getattr(song_obj, "file_names", None),
        "preview_date": getattr(song_obj, "preview_date", None),
        "release_date": getattr(song_obj, "release_date", None),
        "date_leaked": getattr(song_obj, "date_leaked", None),
        "leak_type": getattr(song_obj, "leak_type", None),
        "additional_information": getattr(song_obj, "additional_information", None),
        "notes": getattr(song_obj, "notes", None),
        "lyrics": getattr(song_obj, "lyrics", None),
        "image_url": final_image_url,
        "snippets": getattr(song_obj, "snippets", None),
    }
    return meta


# ── Voice connection helper ───────────────────────────────────────────

async def ensure_voice_connected(
    guild: discord.Guild,
    user: discord.Member,
) -> Optional[discord.VoiceClient]:
    """Connect or move to the user's voice channel.

    Returns the :class:`discord.VoiceClient` on success, or ``None`` if
    the user is not currently in a voice channel.
    """
    if not user.voice or not user.voice.channel:
        return None
    channel = user.voice.channel
    voice: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
    if voice and voice.is_connected():
        if voice.channel != channel:
            await voice.move_to(channel)
        return voice
    return await channel.connect()


# ── Stream error helper ───────────────────────────────────────────────

async def handle_stream_error(
    ctx: commands.Context,
    *,
    status: str,
    error_detail: Optional[str],
    subject: str,
) -> None:
    """Send the appropriate error message for a failed stream attempt.

    *subject* is interpolated into the message, e.g.
    ``"song `123`"`` or ``"file `path/to/song.mp3`"``.
    """
    if status == "file_not_found":
        await send_temporary(ctx, f"Audio file not found for {subject}.", delay=5)
    elif status == "http_error":
        await send_temporary(
            ctx,
            f"Could not stream {subject} (HTTP error). "
            f"Details: {error_detail or status}",
            delay=5,
        )
    else:
        detail_suffix = f" Details: {error_detail}" if error_detail else ""
        await send_temporary(
            ctx,
            f"Could not stream {subject} (status: {status}).{detail_suffix}",
            delay=5,
        )


# ── Similar songs ────────────────────────────────────────────────────

def score_similarity(
    song: Any,
    *,
    era_name: Optional[str],
    producers_str: str,
    category: str,
) -> int:
    """Score a candidate song's similarity to a reference track."""
    sc = 0
    s_era = getattr(getattr(song, "era", None), "name", "")
    if era_name and s_era == era_name:
        sc += 2
    s_prod = getattr(song, "producers", "") or ""
    if producers_str and s_prod and any(
        p.strip() in s_prod for p in producers_str.split(",") if p.strip()
    ):
        sc += 3
    if category and getattr(song, "category", "") == category:
        sc += 1
    return sc


async def find_similar_songs(
    guild_id: int,
) -> tuple:
    """Find songs similar to the currently-playing track.

    Returns ``(current_title, sorted_candidates)`` or ``(None, [])``
    if nothing is playing.
    """
    info = state.guild_now_playing.get(guild_id)
    title = info.get("title") if info else None
    if not info or not title or title == NOTHING_PLAYING:
        return None, []

    meta = info.get("metadata") or {}
    era_val = meta.get("era")
    era_name: Optional[str] = None
    if isinstance(era_val, dict):
        era_name = era_val.get("name")
    elif era_val:
        era_name = str(era_val)

    producers_str = meta.get("producers") or ""
    category = meta.get("category") or ""

    candidates: List[Any] = []
    api = get_api()
    try:
        if era_name:
            res = await api.get_songs(era=era_name, page=1, page_size=25)
            candidates = res.get("results") or []
        if len(candidates) < 5 and category:
            res2 = await api.get_songs(category=category, page=1, page_size=25)
            existing_ids = {getattr(s, "id", None) for s in candidates}
            for s in (res2.get("results") or []):
                if getattr(s, "id", None) not in existing_ids:
                    candidates.append(s)
    except JuiceWRLDAPIError:
        return title, []

    candidates = [s for s in candidates if getattr(s, "name", None) != title]
    candidates.sort(
        key=lambda s: score_similarity(
            s,
            era_name=era_name,
            producers_str=producers_str,
            category=category,
        ),
        reverse=True,
    )
    return title, candidates[:10]


# ── Leave voice ─────────────────────────────────────────────────────

async def leave_voice_channel(
    guild: Optional[discord.Guild],
    voice: Optional[discord.VoiceClient],
    *,
    delete_np_callback,
) -> bool:
    """Disconnect from voice and clean up state.

    Returns ``True`` if the bot was connected and disconnected.
    *delete_np_callback* should be an async callable that accepts
    ``(guild_id, delay)`` and deletes the Now Playing message.
    """
    if not voice or not voice.is_connected():
        return False

    if guild:
        state.guild_radio_enabled[guild.id] = False
        state.guild_radio_next.pop(guild.id, None)
        asyncio.create_task(delete_np_callback(guild.id, 1))

    await voice.disconnect()
    return True


# ── Discord message helpers ──────────────────────────────────────────

async def delete_later(message: discord.Message, delay: int) -> None:
    """Delete a message after a delay, ignoring failures."""
    try:
        await asyncio.sleep(delay)
        await message.delete()
    except Exception:
        return


async def send_temporary(
    ctx: commands.Context, content: str = None, delay: int = 10, embed: discord.Embed = None
) -> None:
    """Send a status message that auto-deletes after *delay* seconds."""
    msg = await ctx.send(content, embed=embed)
    asyncio.create_task(delete_later(msg, delay))


async def send_ephemeral_temporary(
    interaction: discord.Interaction, content: str, delay: int = 5
) -> None:
    """Send an ephemeral followup message that auto-deletes after *delay* seconds."""
    msg = await interaction.followup.send(content, ephemeral=True, wait=True)
    asyncio.create_task(delete_later(msg, delay))


def schedule_interaction_deletion(interaction: discord.Interaction, delay: int) -> None:
    """Schedule an interaction's original response to be deleted after a delay."""
    async def _delete_after_delay() -> None:
        await asyncio.sleep(delay)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass
    asyncio.create_task(_delete_after_delay())


# ── Embed builders ───────────────────────────────────────────────────

def build_playlists_embed_for_user(
    user: discord.abc.User, playlists: Dict[str, List[Dict[str, Any]]]
) -> discord.Embed:
    """Construct an embed summarising a user's playlists."""
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


def build_stats_embed(user: discord.abc.User) -> discord.Embed:
    """Construct an embed showing a user's personal listening stats."""
    stats = state.user_listening_stats.get(getattr(user, "id", 0))
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

    if songs:
        top_songs = sorted(songs.items(), key=lambda x: x[1], reverse=True)[:5]
        lines = [
            f"`{i}.` **{name}** — {count} play{'s' if count != 1 else ''}"
            for i, (name, count) in enumerate(top_songs, 1)
        ]
        embed.add_field(name="Top Songs", value="\n".join(lines), inline=False)

    if eras:
        top_eras = sorted(eras.items(), key=lambda x: x[1], reverse=True)[:3]
        lines = [
            f"`{i}.` **{name}** — {count} play{'s' if count != 1 else ''}"
            for i, (name, count) in enumerate(top_eras, 1)
        ]
        embed.add_field(name="Top Eras", value="\n".join(lines), inline=False)

    return embed
