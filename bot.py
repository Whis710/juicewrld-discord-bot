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
from typing import Optional, Dict, Any, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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

# Per-user playlists: user_id -> playlist_name -> list of track dicts.
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


_load_user_playlists_from_disk()


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


async def _send_temporary(ctx: commands.Context, content: str, delay: int = 10) -> None:
    """Send a status message that auto-deletes after `delay` seconds."""

    msg = await ctx.send(content)
    asyncio.create_task(_delete_later(msg, delay))


async def _send_ephemeral_temporary(
    interaction: discord.Interaction, content: str, delay: int = 5
) -> None:
    """Send an ephemeral followup message that auto-deletes after `delay` seconds."""

    msg = await interaction.followup.send(content, ephemeral=True, wait=True)
    asyncio.create_task(_delete_later(msg, delay))


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
    existing.update(
        {
            "title": title,
            "path": path,
            "is_radio": is_radio,
            "requester": getattr(ctx.author, "mention", str(ctx.author)),
            "metadata": metadata or {},
            "duration_seconds": duration_seconds,
            "started_at": time.time(),
        }
    )
    _guild_now_playing[guild_id] = existing


class SearchPaginationView(discord.ui.View):
    """Paginated search results with 5 songs per page and play buttons.
    
    Modes:
    - "play": Shows 1-5 buttons to play a song
    - "add": Shows ➕1-5 buttons to select a song to add to playlist
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
        self.mode = "play"  # "play", "add", or "select_playlist"
        self.song_to_add: Optional[Any] = None  # Song selected for adding to playlist
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
        if self.mode == "add":
            embed.set_footer(text="Select a song (1–5) to add to a playlist.")
        else:
            embed.set_footer(text="Use buttons 1–5 below to play a song from this page.")
        return embed

    def _build_playlist_select_embed(self) -> discord.Embed:
        """Build embed for playlist selection mode."""
        song_name = getattr(self.song_to_add, "name", "Unknown") if self.song_to_add else "Unknown"
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
        self._update_button_states()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _handle_play(self, interaction: discord.Interaction, slot_index: int) -> None:
        """Handle pressing a numbered button (1–5)."""

        # Only allow the user who ran the original command to trigger playback.
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use the play buttons.",
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

        song = self.songs[global_index]
        song_id = getattr(song, "id", None)
        if song_id is None:
            await interaction.response.send_message(
                "This result does not have a valid song ID to play.",
                ephemeral=True,
            )
            return

        name = getattr(song, "name", getattr(song, "title", "Unknown"))

        # Defer the interaction first to avoid timeout (Discord requires response within 3s)
        await interaction.response.defer(ephemeral=True)

        # Reuse the existing play_song command logic for playback.
        await play_song(self.ctx, str(song_id))

        await _send_ephemeral_temporary(
            interaction, f"Requested playback for `{name}` (ID `{song_id}`)."
        )

        # Delete or edit the search results message
        try:
            if self.is_ephemeral:
                # Edit to show closed message, then delete after 5 seconds
                embed = discord.Embed(title="Search Results", description="Song selected. Search closed.")
                await interaction.edit_original_response(embed=embed, view=None)
                
                async def _delete_after_delay() -> None:
                    await asyncio.sleep(5)
                    try:
                        await interaction.delete_original_response()
                    except Exception:
                        pass
                asyncio.create_task(_delete_after_delay())
            else:
                # Regular messages can be deleted immediately
                msg = self.message or interaction.message
                if msg:
                    await msg.delete()
        except discord.errors.NotFound:
            pass  # Message already deleted

        self.stop()

    async def _handle_add_select(self, interaction: discord.Interaction, slot_index: int) -> None:
        """Handle selecting a song to add to a playlist."""
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

        self.song_to_add = self.songs[global_index]
        
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
        song = self.song_to_add
        
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
        
        # Return to play mode and update the search view
        self.mode = "play"
        self.song_to_add = None
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
            back_btn.callback = self._on_back_to_search
            self.add_item(back_btn)
            
            # Row 1: playlist selection buttons (only for items that exist)
            for slot in range(5):
                global_index = self.playlist_page * self.per_page + slot
                if global_index >= total:
                    break
                btn = discord.ui.Button(label=str(slot + 1), style=discord.ButtonStyle.success, row=1)
                btn.callback = self._make_playlist_select_callback(slot)
                self.add_item(btn)
        else:
            # Play or Add mode
            # Row 0: nav buttons (only if needed) + add to playlist toggle
            if self.current_page > 0:
                prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, row=0)
                prev_btn.callback = lambda i: self._change_page(i, -1)
                self.add_item(prev_btn)
            
            if self.current_page < self.total_pages - 1:
                next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, row=0)
                next_btn.callback = lambda i: self._change_page(i, +1)
                self.add_item(next_btn)
            
            if self.mode == "add":
                # Show "Back" button to return to play mode
                back_btn = discord.ui.Button(label="⬅ Back", style=discord.ButtonStyle.danger, row=0)
                back_btn.callback = self._on_back_to_play
                self.add_item(back_btn)
            else:
                # Show "Add to Playlist" toggle button
                add_btn = discord.ui.Button(label="➕ Add to Playlist", style=discord.ButtonStyle.success, row=0)
                add_btn.callback = self._on_add_mode
                self.add_item(add_btn)
            
            # Row 1: numbered buttons (only for items that exist)
            total = len(self.songs)
            for slot in range(5):
                global_index = self.current_page * self.per_page + slot
                if global_index >= total:
                    break
                
                if self.mode == "add":
                    label = f"➕{slot + 1}"
                    style = discord.ButtonStyle.success
                    callback = self._make_add_select_callback(slot)
                else:
                    label = str(slot + 1)
                    style = discord.ButtonStyle.primary
                    callback = self._make_play_callback(slot)
                
                btn = discord.ui.Button(label=label, style=style, row=1)
                btn.callback = callback
                self.add_item(btn)

    def _make_play_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_play(interaction, slot_index)
        return callback

    def _make_add_select_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_add_select(interaction, slot_index)
        return callback

    def _make_playlist_select_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_playlist_select(interaction, slot_index)
        return callback

    async def _on_add_mode(self, interaction: discord.Interaction) -> None:
        """Switch to add mode."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return
        self.mode = "add"
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_back_to_play(self, interaction: discord.Interaction) -> None:
        """Switch back to play mode from add mode."""
        self.mode = "play"
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_back_to_search(self, interaction: discord.Interaction) -> None:
        """Switch back to add mode from playlist selection."""
        self.mode = "add"
        self.song_to_add = None
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
    """

    def __init__(
        self,
        *,
        ctx: commands.Context,
        playlists: Dict[str, List[Dict[str, Any]]],
        user: discord.abc.User,
        mode: str = "menu",
    ) -> None:
        super().__init__(timeout=120)
        self.ctx = ctx
        self.user = user
        self.mode = mode
        # Convert dict to list of (name, tracks) tuples for pagination
        self.playlist_items: List[tuple] = list(playlists.items())
        self.per_page = 5
        self.current_page = 0
        self.total_pages = max(1, math.ceil(len(self.playlist_items) / self.per_page))
        self._rebuild_buttons()

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
            # Row 0: pagination
            prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, row=0, disabled=self.current_page == 0)
            prev_btn.callback = lambda i: self._change_page(i, -1)
            self.add_item(prev_btn)
            
            next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, row=0, disabled=self.current_page >= self.total_pages - 1)
            next_btn.callback = lambda i: self._change_page(i, +1)
            self.add_item(next_btn)
            
            back_btn = discord.ui.Button(label="⬅ Back", style=discord.ButtonStyle.danger, row=0)
            back_btn.callback = self._on_back
            self.add_item(back_btn)
            
            # Row 1: numbered buttons based on mode
            for slot in range(5):
                global_index = self.current_page * self.per_page + slot
                disabled = global_index >= total
                
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
                else:
                    continue
                
                btn = discord.ui.Button(label=label, style=style, row=1, disabled=disabled)
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
        for track in tracks:
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

            title = track.get("name") or f"Playlist {playlist_name} item"
            metadata = track.get("metadata") or {}

            await _queue_or_play_now(
                self.ctx,
                stream_url=stream_url,
                title=str(title),
                path=file_path,
                metadata=metadata,
                duration_seconds=None,
                silent=True,
            )
            queued += 1

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
            playing_embed.set_footer(text="This message will disappear in 10 seconds.")
            try:
                await interaction.edit_original_response(embed=playing_embed, view=None)
                # Schedule deletion after 10 seconds
                async def _delete_after_delay() -> None:
                    await asyncio.sleep(10)
                    try:
                        await interaction.delete_original_response()
                    except Exception:
                        pass
                asyncio.create_task(_delete_after_delay())
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
            # Schedule deletion after 5 seconds
            async def _delete_after_delay() -> None:
                await asyncio.sleep(5)
                try:
                    await interaction.delete_original_response()
                except Exception:
                    pass
            asyncio.create_task(_delete_after_delay())
            return

        target_playlist_name, target_tracks = self.playlist_items[global_index]

        # Get the currently playing song
        guild = self.ctx.guild
        if not guild:
            await interaction.response.send_message(
                "Guild context unavailable.", ephemeral=True
            )
            # Schedule deletion after 5 seconds
            async def _delete_after_delay() -> None:
                await asyncio.sleep(5)
                try:
                    await interaction.delete_original_response()
                except Exception:
                    pass
            asyncio.create_task(_delete_after_delay())
            return

        info = _guild_now_playing.get(guild.id)
        if not info:
            await interaction.response.send_message(
                "Nothing is currently playing.", ephemeral=True
            )
            # Schedule deletion after 5 seconds
            async def _delete_after_delay() -> None:
                await asyncio.sleep(5)
                try:
                    await interaction.delete_original_response()
                except Exception:
                    pass
            asyncio.create_task(_delete_after_delay())
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
            # Schedule deletion after 5 seconds
            async def _delete_after_delay() -> None:
                await asyncio.sleep(5)
                try:
                    await msg.delete()
                except Exception:
                    pass
            asyncio.create_task(_delete_after_delay())
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
            # Schedule deletion after 5 seconds
            async def _delete_after_delay() -> None:
                await asyncio.sleep(5)
                try:
                    await msg.delete()
                except Exception:
                    pass
            asyncio.create_task(_delete_after_delay())
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
        async def _delete_after_delay() -> None:
            await asyncio.sleep(5)
            try:
                await msg.delete()
            except Exception:
                pass
        asyncio.create_task(_delete_after_delay())

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
        if not voice:
            await _send_ephemeral_temporary(interaction, "No active playback.")
            return

        if voice.is_playing():
            voice.pause()
            await _send_ephemeral_temporary(interaction, "Paused playback.")
        elif voice.is_paused():
            voice.resume()
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
            # Clear the queue so nothing plays after stopping
            _guild_queue[guild.id] = []

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

        view = PlaylistPaginationView(ctx=self.ctx, playlists=playlists, user=user)
        embed = view.build_embed()

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


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

    # Build embed with rich metadata and a progress bar if we know duration.
    meta = info.get("metadata") or {}
    stored_duration = info.get("duration_seconds")
    started_at = info.get("started_at")

    embed = discord.Embed(title="Now Playing", description=title)

    # Album art / cover ("album art")
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

    # Duration + progress bar only (no static length field here)
    total_seconds = duration_seconds or stored_duration
    if total_seconds and started_at:
        elapsed = int(time.time() - started_at)
        progress = _format_progress_bar(elapsed, total_seconds)
        embed.add_field(name="Progress", value=progress, inline=False)

    # Radio mode indicator only in the footer
    if is_radio:
        embed.set_footer(text="Radio mode is ON")
    else:
        embed.set_footer(text="Radio mode is OFF")

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

        # Re-use the same embed construction logic as _send_player_controls.
        # We call it in "update" mode by bypassing message_id/channel_id
        # handling and directly editing the fetched message.
        meta = metadata
        stored_duration = duration_seconds
        started_at = info.get("started_at")

        embed = discord.Embed(title="Now Playing", description=title)

        # Album art / cover ("album art")
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

        # Duration + progress bar only (no static length field here)
        total_seconds = stored_duration
        if total_seconds and started_at:
            elapsed = int(time.time() - started_at)
            progress = _format_progress_bar(elapsed, total_seconds)
            embed.add_field(name="Progress", value=progress, inline=False)

        # Radio mode indicator only in the footer
        if is_radio:
            embed.set_footer(text="Radio mode is ON")
        else:
            embed.set_footer(text="Radio mode is OFF")

        view = PlayerView(ctx=info.get("ctx"), is_radio=is_radio) if info.get("ctx") else None
        try:
            await msg.edit(embed=embed, view=view)
        except Exception:
            continue


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    if not _update_player_messages.is_running():
        _update_player_messages.start()

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

    embed.set_footer(text="Prefix: !jw  •  Example: !jw play 12345")

    await ctx.send(embed=embed)


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    """Simple health check.

    Sends a temporary "Pong!" message that auto-deletes after a few seconds.
    """

    await _send_temporary(ctx, "Pong!", delay=5)


# --- Slash command equivalents for core commands (ephemeral responses) ---


@jw_group.command(name="ping", description="Check if the bot is alive.")
async def slash_ping(interaction: discord.Interaction) -> None:
    """Ephemeral equivalent of !jw ping."""

    await interaction.response.send_message("Pong!", ephemeral=True)


@jw_group.command(name="song", description="Get details for a specific song by ID.")
@app_commands.describe(song_id="Numeric Juice WRLD song ID")
async def slash_song(interaction: discord.Interaction, song_id: int) -> None:
    """Ephemeral equivalent of !jw song <song_id>."""

    await interaction.response.defer(ephemeral=True, thinking=True)

    api = create_api_client()
    try:
        try:
            song = api.get_song(song_id)
        except NotFoundError:
            await interaction.followup.send(
                f"No song found with ID `{song_id}`.", ephemeral=True
            )
            return
        except JuiceWRLDAPIError as e:
            await interaction.followup.send(
                f"Error while fetching song: {e}", ephemeral=True
            )
            return
    finally:
        api.close()

    name = getattr(song, "name", getattr(song, "title", "Unknown"))
    category = getattr(song, "category", "?")
    length = getattr(song, "length", "?")
    era_name = getattr(getattr(song, "era", None), "name", "?")
    producers = getattr(song, "producers", None)

    desc_lines = [
        f"**{name}** (ID: `{song_id}`)",
        f"Category: `{category}`",
        f"Length: `{length}`",
        f"Era: `{era_name}`",
    ]
    if producers:
        desc_lines.append(f"Producers: {producers}")

    await interaction.followup.send("\n".join(desc_lines), ephemeral=True)


@jw_group.command(name="play", description="Play a Juice WRLD song in voice chat by ID.")
@app_commands.describe(song_id="Numeric Juice WRLD song ID")
async def slash_play(interaction: discord.Interaction, song_id: int) -> None:
    """Ephemeral wrapper that delegates to !jw play logic."""

    await interaction.response.defer(ephemeral=True, thinking=True)

    # Build a commands.Context from this interaction so we can reuse play_song.
    ctx = await commands.Context.from_interaction(interaction)
    await play_song(ctx, str(song_id))

    await interaction.followup.send(
        f"Requested playback for song ID `{song_id}`.", ephemeral=True
    )


@jw_group.command(name="search", description="Search for Juice WRLD songs.")
@app_commands.describe(query="Search query for song titles/content")
async def slash_search(interaction: discord.Interaction, query: str) -> None:
    """Ephemeral, paginated search (equivalent to !jw search)."""

    await interaction.response.defer(ephemeral=True, thinking=True)

    async with interaction.channel.typing() if isinstance(interaction.channel, discord.abc.Messageable) else asyncio.sleep(0):
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
            f"No songs found for `" + query + "`.", ephemeral=True
        )
        return

    total = results.get("count") if isinstance(results, dict) else None

    # Build a Context to drive playback when buttons are pressed.
    ctx = await commands.Context.from_interaction(interaction)
    view = SearchPaginationView(ctx=ctx, songs=songs, query=query, total_count=total, is_ephemeral=True)
    embed = view.build_embed()

    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@jw_group.command(name="join", description="Make the bot join your current voice channel.")
async def slash_join(interaction: discord.Interaction) -> None:
    """Ephemeral equivalent of !jw join."""

    await interaction.response.defer(ephemeral=True, thinking=True)

    user = interaction.user
    if not isinstance(user, (discord.Member,)) or not user.voice or not user.voice.channel:
        await interaction.followup.send(
            "You need to be in a voice channel first.", ephemeral=True
        )
        return

    channel = user.voice.channel
    voice: Optional[discord.VoiceClient] = interaction.guild.voice_client if interaction.guild else None

    try:
        if voice and voice.is_connected():
            if voice.channel != channel:
                await voice.move_to(channel)
        else:
            await channel.connect()
    except Exception as e:
        await interaction.followup.send(
            f"Failed to join voice channel: {e}", ephemeral=True
        )
        return

    await interaction.followup.send(
        f"Joined voice channel: {channel.name}", ephemeral=True
    )


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

    view = PlaylistPaginationView(ctx=ctx, playlists=playlists, user=user)
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
        image_url = song_obj.image_url
        if image_url and image_url.startswith("/"):
            image_url = f"{JUICEWRLD_API_BASE_URL}{image_url}"

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

        await _queue_or_play_now(
            ctx,
            stream_url=stream_url,
            title=str(title),
            path=file_path,
            metadata=metadata,
            duration_seconds=None,
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
        image_url = getattr(song_obj, "image_url", None)
        if image_url and isinstance(image_url, str) and image_url.startswith("/"):
            image_url = f"{JUICEWRLD_API_BASE_URL}{image_url}"
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

            image_url = song_obj.image_url
            if image_url and image_url.startswith("/"):
                image_url = f"{JUICEWRLD_API_BASE_URL}{image_url}"

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


async def _play_random_song_in_guild(ctx: commands.Context) -> None:
    """Pick a random song from the radio endpoint and play it.

    Uses `/juicewrld/radio/random/` plus the player endpoint to get a
    streaming URL and rich metadata. If radio is disabled for the guild,
    this is a no-op.
    """

    if not ctx.guild or not _guild_radio_enabled.get(ctx.guild.id):
        return

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

    stream_url: Optional[str] = None
    chosen_title: str = "Unknown"
    song_meta: Dict[str, Any] = {}
    duration_seconds: Optional[int] = None

    async with ctx.typing():
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
                await _send_temporary(
                    ctx,
                    "Radio: random endpoint did not return a valid file path.",
                    delay=5,
                )
                return

            song_info = radio_data.get("song") or {}
            song_id = song_info.get("id")

            # Prefer the canonical song name from embedded song metadata.
            if song_info.get("name"):
                chosen_title = str(song_info.get("name"))

            # 2) Use the comp streaming helper to validate and build a stream URL.
            stream_result = api.stream_audio_file(file_path)
            status = stream_result.get("status")
            if status != "success":
                await _send_temporary(
                    ctx,
                    "Radio: could not stream randomly selected file "
                    f"`{chosen_title}` (path `{file_path}`) (status: {status}).",
                    delay=5,
                )
                return

            stream_url = stream_result.get("stream_url")
            if not stream_url:
                await _send_temporary(
                    ctx,
                    "Radio: API did not return a stream URL for the random song.",
                    delay=5,
                )
                return

            # 3) Build song metadata for artwork, duration, etc., from the
            # embedded `song` object in the radio response.
            if song_info:
                length = song_info.get("length") or ""
                duration_seconds = _parse_length_to_seconds(length)

                # Start from the raw song dict so we keep all keys the API
                # provides (including any future fields like bitrate/snippets).
                song_meta = dict(song_info)
                song_meta["path"] = file_path

                # image_url in docs is relative (e.g., "/assets/youtube.webp").
                image_url = song_meta.get("image_url") or ""
                if isinstance(image_url, str) and image_url.startswith("/"):
                    image_url = f"{JUICEWRLD_API_BASE_URL}{image_url}"
                song_meta["image_url"] = image_url
            else:
                # Minimal fallback if the radio payload is missing a song object.
                song_meta = {"id": song_id, "path": file_path}
        finally:
            api.close()

    if not ctx.guild or not _guild_radio_enabled.get(ctx.guild.id):
        return

    if not voice:
        await _send_temporary(ctx, "Internal error: voice client not available.", delay=5)
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
            fut = _play_random_song_in_guild(ctx)
            asyncio.run_coroutine_threadsafe(fut, bot.loop)
            return

        # Radio is off; if there is anything queued, continue with the
        # regular queue playback.
        queue = _guild_queue.get(guild_id) or []
        if queue:
            fut = _play_next_from_queue(ctx)
            asyncio.run_coroutine_threadsafe(fut, bot.loop)

    voice.play(source, after=_after_playback)

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


def main() -> None:
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN environment variable is not set.")
        sys.exit(1)

    # Register the /jw slash command group on the bot's tree.
    bot.tree.add_command(jw_group)

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
