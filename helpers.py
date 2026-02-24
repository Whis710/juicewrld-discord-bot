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
from constants import JUICEWRLD_API_BASE_URL, NOTHING_PLAYING
import state


# ── API client factory ───────────────────────────────────────────────

def create_api_client() -> JuiceWRLDAPI:
    """Create a new JuiceWRLDAPI client instance."""
    return JuiceWRLDAPI(base_url=JUICEWRLD_API_BASE_URL)


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
