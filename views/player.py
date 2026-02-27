"""Player-related UI views for the Juice WRLD Discord bot."""

import time
from typing import Any, Callable, Dict, List, Optional
import random
import discord
from discord.ext import commands

from constants import NOTHING_PLAYING
import helpers
import state
from views.playlist import PlaylistPaginationView


class LyricsPaginationView(discord.ui.View):
    """Ephemeral paginated view for displaying lyrics with section headers."""

    def __init__(self, *, title: str, lyrics: str, url: Optional[str] = None) -> None:
        super().__init__(timeout=120)
        self.title = title
        self.url = url
        self.pages = self._split_lyrics(lyrics)
        self.current_page = 0
        self.total_pages = len(self.pages)
        self._update_buttons()

    def _split_lyrics(self, lyrics: str) -> List[str]:
        """Split lyrics into pages at section headers, respecting the 4096 char limit."""
        import re
        # Split on section headers like [Chorus], [Verse 1], [Bridge] etc.
        sections = re.split(r'(\[.*?\])', lyrics)

        pages: List[str] = []
        current = ""

        for part in sections:
            candidate = current + part
            if len(candidate) > 3900:
                # Current page is full — save it and start a new one.
                if current.strip():
                    pages.append(current.strip())
                current = part
            else:
                current = candidate

        if current.strip():
            pages.append(current.strip())

        return pages if pages else [lyrics[:3900]]

    def _update_buttons(self) -> None:
        """Enable/disable nav buttons based on current page."""
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.custom_id == "lyrics_prev":
                    item.disabled = self.current_page == 0
                elif item.custom_id == "lyrics_next":
                    item.disabled = self.current_page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🎵 Lyrics — {self.title}",
            description=self.pages[self.current_page],
            colour=discord.Colour.yellow(),
        )
        footer = f"Page {self.current_page + 1}/{self.total_pages} • Powered by Genius"
        if self.url:
            footer += f" • Full lyrics: {self.url}"
        embed.set_footer(text=footer)
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="lyrics_prev")
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="lyrics_next")
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)



class LyricsSongSelectView(discord.ui.View):
    """Ephemeral dropdown letting the user pick which Genius result to show lyrics for."""

    def __init__(
        self,
        *,
        song_title: str,
        candidates: list,
    ) -> None:
        super().__init__(timeout=60)
        self.song_title = song_title
        self.candidates = candidates  # List of {id, title, url}

        options = [
            discord.SelectOption(
                label=c["title"][:100],
                value=str(c["id"]),
                description=c["url"][:100] if c.get("url") else None,
            )
            for c in candidates[:5]  # Discord select max 25, we cap at 5
        ]

        select = discord.ui.Select(
            placeholder="Choose the correct version…",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        """Fetch lyrics for the selected song and show the paginated view."""
        await interaction.response.defer(ephemeral=True)

        song_id = int(interaction.data["values"][0])
        # Find the matching candidate to get its title and url.
        candidate = next((c for c in self.candidates if c["id"] == song_id), None)
        display_title = candidate["title"] if candidate else self.song_title
        url = candidate["url"] if candidate else None

        genius = helpers.get_genius()
        if not genius:
            await interaction.followup.send(
                "Genius client not available.", ephemeral=True
            )
            return

        lyrics = await genius.get_lyrics_by_id(song_id)
        if not lyrics:
            await interaction.followup.send(
                f"Could not fetch lyrics for **{display_title}**.", ephemeral=True
            )
            return

        view = LyricsPaginationView(title=display_title, lyrics=lyrics, url=url)
        embed = view.build_embed()
        await interaction.edit_original_response(embed=embed, view=view)


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

        info = state.guild_now_playing.get(guild.id)
        if not info:
            await interaction.response.send_message(
                "Nothing is currently tracked as playing.", ephemeral=True
            )
            return

        title = str(info.get("title", "Unknown"))
        meta = info.get("metadata") or {}
        lyrics = meta.get("lyrics")

        # Defer early — Genius lookup can take a moment.
        await interaction.response.defer(ephemeral=True)

        if lyrics:
            # Stored lyrics — show directly without Genius search.
            view = LyricsPaginationView(title=title, lyrics=str(lyrics))
            embed = view.build_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            return

        # No stored lyrics — search Genius for candidates.
        genius = helpers.get_genius()
        if not genius:
            await interaction.followup.send(
                "No lyrics stored for this song and no Genius token is configured. "
                "Set the `GENIUS_TOKEN` environment variable to enable lyrics lookup.",
                ephemeral=True,
            )
            return

        candidates = await genius.search_candidates(title, max_results=5)

        if not candidates:
            await interaction.followup.send(
                f"No lyrics found for **{title}** on Genius.",
                ephemeral=True,
            )
            return

        if len(candidates) == 1:
            # Only one result — skip the dropdown and load lyrics directly.
            c = candidates[0]
            lyrics = await genius.get_lyrics_by_id(c["id"])
            if not lyrics:
                await interaction.followup.send(
                    f"Could not fetch lyrics for **{c['title']}**.", ephemeral=True
                )
                return
            view = LyricsPaginationView(title=c["title"], lyrics=lyrics, url=c.get("url"))
            embed = view.build_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            return

        # Multiple results — show dropdown so user picks the right version.
        select_view = LyricsSongSelectView(song_title=title, candidates=candidates)
        embed = discord.Embed(
            title=f"🎵 Lyrics — {title}",
            description=f"Found **{len(candidates)}** versions on Genius. Pick the correct one:",
            colour=discord.Colour.yellow(),
        )
        await interaction.followup.send(embed=embed, view=select_view, ephemeral=True)

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

        info = state.guild_now_playing.get(guild.id)
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

    def __init__(
        self,
        *,
        ctx: commands.Context,
        is_radio: bool,
        queue_fn: Optional[Callable] = None,
        send_controls_fn: Optional[Callable] = None,
        radio_fn: Optional[Callable] = None,
        prefetch_fn: Optional[Callable] = None,
    ) -> None:
        super().__init__(timeout=None)
        self.ctx = ctx
        self.is_radio = is_radio
        self._queue_fn = queue_fn
        self._send_controls_fn = send_controls_fn
        self._radio_fn = radio_fn
        self._prefetch_fn = prefetch_fn

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
            await helpers.send_ephemeral_temporary(interaction, "No active playback.")
            return

        if voice.is_playing():
            voice.pause()
            # Track when we paused
            if guild:
                info = state.guild_now_playing.get(guild.id, {})
                info["paused_at"] = time.time()
                state.guild_now_playing[guild.id] = info
            await helpers.send_ephemeral_temporary(interaction, "Paused playback.")
        elif voice.is_paused():
            voice.resume()
            # Add paused duration to total and clear paused_at
            if guild:
                info = state.guild_now_playing.get(guild.id, {})
                paused_at = info.get("paused_at")
                if paused_at:
                    paused_duration = time.time() - paused_at
                    info["total_paused_time"] = info.get("total_paused_time", 0) + paused_duration
                info["paused_at"] = None
                state.guild_now_playing[guild.id] = info
            await helpers.send_ephemeral_temporary(interaction, "Resumed playback.")
        else:
            await helpers.send_ephemeral_temporary(interaction, "Nothing is currently playing.")

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
            state.guild_radio_enabled[guild.id] = False
            # Clear the queue and pre-fetched radio song
            state.guild_queue[guild.id] = []
            state.guild_radio_next.pop(guild.id, None)

        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()

        if guild:
            # Mark the shared player as idle but keep the message for reuse.
            await self._send_controls_fn(
                self.ctx,
                title="Nothing playing",
                path=None,
                is_radio=False,
                metadata={},
                duration_seconds=None,
            )

        await helpers.send_ephemeral_temporary(interaction, "Stopped playback.")

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
            await helpers.send_ephemeral_temporary(interaction, "Guild context unavailable.")
            return

        prev_song = state.guild_previous_song.get(guild.id)
        if not prev_song:
            await helpers.send_ephemeral_temporary(interaction, "No previous song to replay.")
            return

        path = prev_song.get("path")
        if not path:
            await helpers.send_ephemeral_temporary(interaction, "Previous song has no path to replay.")
            return

        # Get stream URL for the previous song
        stream_result = await helpers.get_api().stream_audio_file(path)

        if stream_result.get("status") != "success":
            await helpers.send_ephemeral_temporary(
                interaction, f"Could not stream previous song: {stream_result.get('error', 'unknown error')}"
            )
            return

        stream_url = stream_result.get("stream_url")
        if not stream_url:
            await helpers.send_ephemeral_temporary(interaction, "No stream URL available for previous song.")
            return

        title = prev_song.get("title", "Unknown")
        metadata = prev_song.get("metadata", {})
        duration_seconds = prev_song.get("duration_seconds")

        # For radio mode, disable radio and play the previous song
        if self.is_radio:
            state.guild_radio_enabled[guild.id] = False
            state.guild_radio_next.pop(guild.id, None)

        # Stop current playback
        voice = await self._get_voice()
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()

        # Play the previous song using _queue_or_play_now
        await self._queue_fn(
            self.ctx,
            stream_url=stream_url,
            title=title,
            path=path,
            metadata=metadata,
            duration_seconds=duration_seconds,
        )

        await helpers.send_ephemeral_temporary(interaction, f"⏮ Replaying: {title}")

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
            await helpers.send_ephemeral_temporary(interaction, "Nothing to skip.")
            return

        if self.is_radio:
            # In radio mode, stopping the current track will trigger the
            # after-callback to queue the next random song.
            if voice.is_playing() or voice.is_paused():
                voice.stop()
            await helpers.send_ephemeral_temporary(interaction, "Skipping to the next radio track...")
        else:
            if voice.is_playing() or voice.is_paused():
                voice.stop()
            # Do not delete the player; the queue callback will either
            # start the next track or mark the player idle.
            await helpers.send_ephemeral_temporary(interaction, "Skipped current track.")

    @discord.ui.button(label="🔀 Shuffle", style=discord.ButtonStyle.secondary)
    async def shuffle_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:  # pragma: no cover - UI callback
        await interaction.response.defer(ephemeral=True)
        guild = self.ctx.guild
        
        if not guild:
            await helpers.send_ephemeral_temporary(interaction, "Guild context unavailable.")
            return
        
        queue = state.guild_queue.get(guild.id, [])
        if len(queue) < 2:
            await helpers.send_ephemeral_temporary(interaction, "Not enough songs in queue to shuffle.")
            return
        
        random.shuffle(queue)
        state.guild_queue[guild.id] = queue
        
        # Update the player embed to reflect the new queue order
        info = state.guild_now_playing.get(guild.id, {})
        await self._send_controls_fn(
            self.ctx,
            title=info.get("title", "Unknown"),
            path=info.get("path"),
            is_radio=self.is_radio,
            metadata=info.get("metadata", {}),
            duration_seconds=info.get("duration_seconds"),
        )
        
        await helpers.send_ephemeral_temporary(interaction, f"🔀 Shuffled {len(queue)} tracks in queue.")

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

        info = state.guild_now_playing.get(guild.id)
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
            await helpers.send_ephemeral_temporary(interaction, "Guild context unavailable.")
            return

        info = state.guild_now_playing.get(guild.id)
        if not info:
            await helpers.send_ephemeral_temporary(interaction, "Nothing is currently tracked as playing.")
            return

        # Guard: do not allow liking the idle "Nothing playing" sentinel.
        title_val = info.get("title", "")
        if not title_val or title_val == "Nothing playing":
            await helpers.send_ephemeral_temporary(interaction, "Nothing is currently playing to like.")
            return

        meta = info.get("metadata") or {}
        title = str(info.get("title", meta.get("name") or "Unknown"))
        path = meta.get("path") or info.get("path")
        song_id_val = meta.get("id") or meta.get("song_id")

        user = interaction.user
        playlists = state.get_or_create_user_playlists(user.id)
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
            await helpers.send_ephemeral_temporary(
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

        state.save_user_playlists_to_disk()

        await helpers.send_ephemeral_temporary(
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
        playlists = state.user_playlists.get(user.id) or {}

        if not playlists:
            await interaction.response.send_message(
                "You don't have any playlists yet. Use ❤ Like on the player to "
                "add the current song to your Likes playlist.",
                ephemeral=True,
            )
            return

        view = PlaylistPaginationView(
            ctx=self.ctx,
            playlists=playlists,
            user=user,
            interaction=interaction,
            queue_fn=self._queue_fn,
        )
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
            await helpers.send_ephemeral_temporary(interaction, "Guild context unavailable.")
            return

        user = interaction.user
        if not isinstance(user, (discord.Member,)) or not user.voice or not user.voice.channel:
            await helpers.send_ephemeral_temporary(interaction, "You need to be in a voice channel to use radio.")
            return

        state.guild_radio_enabled[guild.id] = True

        # If something is already playing, let it finish naturally.
        # The after-callback will detect radio is enabled and start playing
        # random songs once the current track ends.
        voice: Optional[discord.VoiceClient] = self.ctx.voice_client
        if voice and (voice.is_playing() or voice.is_paused()):
            # Pre-fetch so "Up Next" is ready when the current song ends
            await self._prefetch_fn(guild.id)
            # Refresh the player embed to show radio state and up-next song
            info = state.guild_now_playing.get(guild.id, {})
            await self._send_controls_fn(
                self.ctx,
                title=info.get("title", "Unknown"),
                path=info.get("path"),
                is_radio=True,
                metadata=info.get("metadata", {}),
                duration_seconds=info.get("duration_seconds"),
            )
            await helpers.send_ephemeral_temporary(interaction, "Radio enabled. Current song will finish, then radio starts.")
        else:
            # Nothing playing; start radio immediately
            await self._radio_fn(self.ctx)
            await helpers.send_ephemeral_temporary(interaction, "Radio started.")


def build_player_embed(
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
        progress = helpers.format_progress_bar(elapsed, duration_seconds)
        # Add paused indicator if currently paused
        if paused_at:
            progress = "⏸️ " + progress
        embed.add_field(name="Progress", value=progress, inline=False)

    # Previous song
    prev_song = state.guild_previous_song.get(guild_id)
    if prev_song:
        prev_title = prev_song.get("title", "Unknown")
        embed.add_field(name="Previous", value=f"**{prev_title}**", inline=True)

    # Queue count with next song name (Up Next)
    queue = state.guild_queue.get(guild_id, [])
    if queue:
        next_title = queue[0].get("title", "Unknown")
        queue_text = f"{len(queue)} track(s)\nUp Next: **{next_title}**"
        embed.add_field(name="Queue", value=queue_text, inline=True)
    elif is_radio:
        # Show pre-fetched radio next song if available
        radio_next = state.guild_radio_next.get(guild_id)
        if radio_next:
            next_title = radio_next.get("title", "Unknown")
            embed.add_field(name="Up Next", value=f"**{next_title}**", inline=True)

    # Radio mode indicator only in the footer
    if is_radio:
        embed.set_footer(text="Radio mode is ON")
    else:
        embed.set_footer(text="Radio mode is OFF")

    return embed


