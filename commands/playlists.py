"""Playlist command Cog for the Juice WRLD Discord bot."""

import time
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from exceptions import JuiceWRLDAPIError, NotFoundError
import helpers
import state


class PlaylistsCog(commands.Cog):
    """Playlist listing, creation, playback, and management commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def _playback(self):
        """Lazy reference to the PlaybackCog."""
        return self.bot.get_cog("PlaybackCog")

    @commands.command(name="playlists")
    async def list_playlists(self, ctx: commands.Context):
        """List the invoking user's playlists with a short preview."""

        user = ctx.author
        playlists = state.user_playlists.get(user.id) or {}
        if not playlists:
            await ctx.send(
                "You don't have any playlists yet. Use ❤ Like on the player to add "
                "the current song to your Likes playlist."
            )
            return

        embed = helpers.build_playlists_embed_for_user(user, playlists)
        await ctx.send(embed=embed)


    @commands.group(name="playlist", invoke_without_command=True)
    async def playlist_group(self, ctx: commands.Context):
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


    @commands.group(name="pl", invoke_without_command=True)
    async def pl_group(self, ctx: commands.Context):
        """Short playlist aliases using !jw pl."""

        # Default: behave like !jw playlists (list playlists).
        user = ctx.author
        playlists = state.user_playlists.get(user.id) or {}
        if not playlists:
            await ctx.send(
                "You don't have any playlists yet. Use ❤ Like on the player to add "
                "the current song to your Likes playlist."
            )
            return

        embed = helpers.build_playlists_embed_for_user(user, playlists)
        await ctx.send(embed=embed)


    @pl_group.command(name="show")
    async def pl_show(self, ctx: commands.Context, *, name: str):
        await self.playlist_show(ctx, name=name)


    @pl_group.command(name="play")
    async def pl_play(self, ctx: commands.Context, *, name: str):
        await self.playlist_play(ctx, name=name)


    @pl_group.command(name="add")
    async def pl_add(self, ctx: commands.Context, *, name_and_id: str):
        await self.playlist_add(ctx, name_and_id=name_and_id)


    @pl_group.command(name="delete")
    async def pl_delete(self, ctx: commands.Context, *, name: str):
        await self.playlist_delete(ctx, name=name)


    @pl_group.command(name="rename")
    async def pl_rename(self, ctx: commands.Context, old: str, new: str):
        await self.playlist_rename(ctx, old=old, new=new)


    @pl_group.command(name="remove")
    async def pl_remove(self, ctx: commands.Context, name: str, index: int):
        await self.playlist_remove(ctx, name=name, index=index)


    @playlist_group.command(name="show")
    async def playlist_show(self, ctx: commands.Context, *, name: str):
        """Show all tracks in one of the user's playlists."""

        playlists = state.user_playlists.get(ctx.author.id) or {}
        playlist = playlists.get(name)
        if playlist is None:
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
    async def playlist_play(self, ctx: commands.Context, *, name: str):
        """Queue or play all tracks from one of the user's playlists."""

        playlists = state.user_playlists.get(ctx.author.id) or {}
        playlist = playlists.get(name)
        if playlist is None:
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
        self._playback._disable_radio_if_active(ctx)

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

            api = helpers.create_api_client()
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
            duration_seconds = helpers.extract_duration_seconds(metadata, track)

            await self._playback._queue_or_play_now(
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
            await helpers.send_temporary(ctx, f"Queued {queued} track(s) from playlist `{name}`.")


    @playlist_group.command(name="add")
    async def playlist_add(self, ctx: commands.Context, *, name_and_id: str):
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

        playlists = state.get_or_create_user_playlists(ctx.author.id)
        playlist = playlists.setdefault(playlist_name, [])

        # Parse song ID
        try:
            song_id_int = int(song_id_str)
        except ValueError:
            await ctx.send("Song ID must be a number. Example: `!jw playlist add MyList 123`.")
            return

        # Resolve a comp file path for this song using the player endpoint.
        async with ctx.typing():
            api = helpers.create_api_client()
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
            api = helpers.create_api_client()
            try:
                song_obj = api.get_song(song_id_int)
            except Exception:
                song_obj = None
            finally:
                api.close()

        if song_obj is not None:
            image_url = helpers.normalize_image_url(getattr(song_obj, "image_url", None))
            meta = helpers.build_song_metadata_from_song(
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

        state.save_user_playlists_to_disk()
        await ctx.send(f"Added `{title}` (ID `{song_id_int}`) to playlist `{playlist_name}`.")


    @playlist_group.command(name="delete")
    async def playlist_delete(self, ctx: commands.Context, *, name: str):
        """Delete one of the user's playlists."""

        playlists = state.user_playlists.get(ctx.author.id) or {}
        if name not in playlists:
            await ctx.send(f"No playlist named `{name}` found.")
            return

        del playlists[name]
        if not playlists:
            # If the user now has no playlists, remove their entry entirely.
            state.user_playlists.pop(ctx.author.id, None)

        state.save_user_playlists_to_disk()
        await ctx.send(f"Deleted playlist `{name}`.")


    @playlist_group.command(name="rename")
    async def playlist_rename(self, ctx: commands.Context, old: str, new: str):
        """Rename one of the user's playlists."""

        playlists = state.user_playlists.get(ctx.author.id) or {}
        if old not in playlists:
            await ctx.send(f"No playlist named `{old}` found.")
            return

        if new in playlists:
            await ctx.send(f"You already have a playlist named `{new}`.")
            return

        playlists[new] = playlists.pop(old)
        state.save_user_playlists_to_disk()
        await ctx.send(f"Renamed playlist `{old}` to `{new}`.")


    @playlist_group.command(name="remove")
    async def playlist_remove(self, ctx: commands.Context, name: str, index: int):
        """Remove a single track by 1-based index from a playlist."""

        playlists = state.user_playlists.get(ctx.author.id) or {}
        playlist = playlists.get(name)
        if not playlist:
            await ctx.send(f"No playlist named `{name}` found.")
            return

        if index < 1 or index > len(playlist):
            await ctx.send(f"Index {index} is out of range for playlist `{name}` (size {len(playlist)}).")
            return

        removed = playlist.pop(index - 1)
        state.save_user_playlists_to_disk()

        title = removed.get("name") or removed.get("id") or "Unknown track"
        await ctx.send(f"Removed `{title}` (index {index}) from playlist `{name}`.")



async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlaylistsCog(bot))
