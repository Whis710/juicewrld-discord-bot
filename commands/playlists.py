"""Playlist command Cog for the Juice WRLD Discord bot."""

import time
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from exceptions import JuiceWRLDAPIError, NotFoundError
import helpers
import state
from views.playlist import SharedPlaylistView


class PlaylistsCog(commands.Cog):
    """Playlist listing, creation, playback, and management commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def _playback(self):
        """Lazy reference to the PlaybackCog."""
        return self.bot.get_cog("PlaybackCog")

    @commands.group(name="playlist", aliases=["pl", "playlists"], invoke_without_command=True)
    async def playlist_group(self, ctx: commands.Context):
        """List playlists (bare invocation) or run a subcommand.

        Works with any of: !jw playlist, !jw pl, !jw playlists
        """

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

        # Paginate into embeds (20 tracks per page) to avoid truncation.
        per_page = 20
        total = len(playlist)
        pages: list[discord.Embed] = []
        for start in range(0, total, per_page):
            page_tracks = playlist[start : start + per_page]
            lines: list[str] = []
            for idx, track in enumerate(page_tracks, start=start + 1):
                tname = track.get("name") or track.get("id") or "Unknown"
                tid = track.get("id")
                piece = f"`{idx}.` {tname}"
                if tid is not None:
                    piece += f" (ID: {tid})"
                lines.append(piece)

            page_num = start // per_page + 1
            total_pages = -(-total // per_page)  # ceil division
            footer = f"Page {page_num}/{total_pages} • {total} track(s)" if total_pages > 1 else f"{total} track(s)"

            embed = discord.Embed(
                title=f"Playlist: {name}",
                description="\n".join(lines),
            )
            embed.set_footer(text=footer)
            pages.append(embed)

        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            # Send all pages; Discord handles multiple embeds fine for prefix commands.
            for embed in pages:
                await ctx.send(embed=embed)


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

        voice = await helpers.ensure_voice_connected(ctx.guild, ctx.author)

        if not voice:
            await ctx.send("Internal error: voice client not available.")
            return

        queued = 0
        for track in playlist:
            file_path = track.get("path")
            if not file_path:
                continue

            result = await helpers.get_api().stream_audio_file(file_path)

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
            player_result = await helpers.get_api().play_juicewrld_song(song_id_int)

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
            try:
                song_obj = await helpers.get_api().get_song(song_id_int)
            except Exception:
                song_obj = None

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


    @playlist_group.command(name="share")
    async def playlist_share(self, ctx: commands.Context, *, name: str):
        """Share a playlist publicly in the channel so others can copy or queue it."""

        playlists = state.user_playlists.get(ctx.author.id) or {}
        playlist = playlists.get(name)
        if playlist is None:
            await ctx.send(f"No playlist named `{name}` found.")
            return

        if not playlist:
            await ctx.send(f"Playlist `{name}` is empty.")
            return

        view = SharedPlaylistView(
            ctx=ctx,
            owner=ctx.author,
            playlist_name=name,
            tracks=list(playlist),
            queue_fn=self._playback._queue_or_play_now,
        )
        embed = view.build_embed()
        msg = await ctx.send(embed=embed, view=view)
        view.message = msg


    @playlist_group.command(name="import")
    async def playlist_import(self, ctx: commands.Context, user: discord.Member, *, name: str):
        """Copy another user's playlist to your own playlists.

        Usage: !jw pl import @user <playlist_name>
        """

        source_playlists = state.user_playlists.get(user.id) or {}
        source = source_playlists.get(name)
        if source is None:
            await ctx.send(f"{user.display_name} doesn't have a playlist named `{name}`.")
            return

        if not source:
            await ctx.send(f"{user.display_name}'s playlist `{name}` is empty.")
            return

        my_playlists = state.get_or_create_user_playlists(ctx.author.id)

        # Generate a unique name if there's a conflict.
        new_name = name
        counter = 1
        while new_name in my_playlists:
            new_name = f"{name} ({counter})"
            counter += 1

        copied = [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "path": t.get("path"),
                "metadata": t.get("metadata", {}),
                "added_at": time.time(),
            }
            for t in source
        ]
        my_playlists[new_name] = copied
        state.save_user_playlists_to_disk()

        await ctx.send(
            f"Imported `{name}` from {user.display_name} as `{new_name}` ({len(copied)} track(s))."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlaylistsCog(bot))
