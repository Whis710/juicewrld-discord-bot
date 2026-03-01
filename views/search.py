"""Search-related UI views for the Juice WRLD Discord bot."""

import math
import time
from typing import Any, Callable, Dict, List, Optional

import discord
from discord.ext import commands

import helpers
import state
from views.player import NowPlayingInfoView, build_song_info_embed

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
        play_fn: Optional[Callable] = None,
        queue_fn: Optional[Callable] = None,
    ) -> None:
        super().__init__(timeout=60)
        self.ctx = ctx
        self.song = song
        self.query = query
        self._play_fn = play_fn
        self._queue_fn = queue_fn
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
        """Build rich song info embed matching the player ℹ button layout."""
        return build_song_info_embed(self.song)

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
            # Main mode: Play, Queue, Play Next, Add to Playlist, and Info buttons
            play_btn = discord.ui.Button(label="▶️ Play Now", style=discord.ButtonStyle.danger, row=0)
            play_btn.callback = self._on_play_now
            self.add_item(play_btn)
            
            play_next_btn = discord.ui.Button(label="⏭️ Play Next", style=discord.ButtonStyle.primary, row=0)
            play_next_btn.callback = self._on_play_next
            self.add_item(play_next_btn)

            if self._queue_fn:
                queue_btn = discord.ui.Button(label="📥 Add to Queue", style=discord.ButtonStyle.secondary, row=0)
                queue_btn.callback = self._on_queue
                self.add_item(queue_btn)
            
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

    async def _on_play_now(self, interaction: discord.Interaction) -> None:
        """Handle Play Now button press - interrupts current song."""
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

        # If radio is active, disable it
        if self.ctx.guild and state.guild_radio_enabled.get(self.ctx.guild.id):
            state.guild_radio_enabled[self.ctx.guild.id] = False

        # Play the song immediately (position="now")
        await self._play_fn(self.ctx, str(song_id), position="now")

        # Close the search result message
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

        self.stop()

    async def _on_play_next(self, interaction: discord.Interaction) -> None:
        """Handle Play Next button press - adds to front of queue."""
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
        await interaction.response.defer(ephemeral=True)
        
        # Play next (position="next")
        await self._play_fn(self.ctx, str(song_id), position="next")

        await helpers.send_ephemeral_temporary(
            interaction, f"⏭️ `{name}` will play next."
        )

    async def _on_play(self, interaction: discord.Interaction) -> None:
        """Handle Play button press (legacy - kept for compatibility)."""
        # Redirect to play_now
        await self._on_play_now(interaction)

    async def _on_queue(self, interaction: discord.Interaction) -> None:
        """Handle Queue button press — add to queue without disabling radio."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return

        song_id = getattr(self.song, "id", None)
        if song_id is None:
            await interaction.response.send_message(
                "This song does not have a valid ID to queue.",
                ephemeral=True,
            )
            return

        name = getattr(self.song, "name", getattr(self.song, "title", "Unknown"))
        await interaction.response.defer(ephemeral=True)
        await self._queue_fn(self.ctx, str(song_id))

        await helpers.send_ephemeral_temporary(
            interaction, f"📥 Added `{name}` to queue."
        )

    async def _on_add_to_playlist(self, interaction: discord.Interaction) -> None:
        """Handle Add to Playlist button press."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return
        
        # Load user's playlists
        user_playlists = state.get_or_create_user_playlists(interaction.user.id)
        self.playlist_items = list(user_playlists.items())
        self.current_page = 0
        self.mode = "select_playlist"
        
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_info(self, interaction: discord.Interaction) -> None:
        """Handle Info button press — shows song detail embed plus Lyrics/Snippets buttons."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return

        song_title = getattr(self.song, "name", getattr(self.song, "title", "Unknown"))
        song_meta = helpers.build_song_metadata_from_song(self.song)

        # Build rich info embed with Lyrics/Snippets buttons in a single
        # ephemeral message (avoids the broken edit_message + followup pattern).
        embed = build_song_info_embed(self.song)
        info_view = NowPlayingInfoView(song_title=song_title, song_metadata=song_meta, ctx=self.ctx, queue_fn=self._queue_fn)
        await interaction.response.send_message(embed=embed, view=info_view, ephemeral=True)

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
                helpers.schedule_interaction_deletion(interaction, 5)
                return
            if song_path and track.get("path") == song_path:
                await interaction.response.send_message(
                    f"`{song_name}` is already in playlist `{playlist_name}`.",
                    ephemeral=True,
                )
                helpers.schedule_interaction_deletion(interaction, 5)
                return

        # Build metadata
        metadata = helpers.build_song_metadata_from_song(self.song, path=song_path)
        
        # Add to playlist
        playlist_tracks.append({
            "id": song_id,
            "name": song_name,
            "path": song_path,
            "metadata": metadata,
            "added_at": time.time(),
        })
        
        state.save_user_playlists_to_disk()
        
        # Show success message and close
        await interaction.response.send_message(
            f"Added `{song_name}` to playlist `{playlist_name}`.",
            ephemeral=True,
        )
        helpers.schedule_interaction_deletion(interaction, 30)
        
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
        playlists = state.get_or_create_user_playlists(user.id)
        
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
        
        metadata = helpers.build_song_metadata_from_song(self.view.song, path=song_path)
        
        playlists[name].append({
            "id": song_id,
            "name": song_name,
            "path": song_path,
            "metadata": metadata,
            "added_at": time.time(),
        })
        
        state.save_user_playlists_to_disk()
        
        await interaction.response.send_message(
            f"Added `{song_name}` to `{name}` playlist.",
            ephemeral=True,
        )
        helpers.schedule_interaction_deletion(interaction, 5)
        
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
        play_fn: Optional[Callable] = None,
        queue_fn: Optional[Callable] = None,
    ) -> None:
        super().__init__(timeout=60)
        self.ctx = ctx
        self.songs = songs
        self.query = query
        self._play_fn = play_fn
        self._queue_fn = queue_fn
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
        """Build rich song info embed matching the player ℹ button layout."""
        if not self.selected_song:
            return discord.Embed(title="Error", description="No song selected.")
        return build_song_info_embed(self.selected_song)

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
        await self._play_fn(self.ctx, str(song_id))

        await helpers.send_ephemeral_temporary(
            interaction, f"Requested playback for `{name}` (ID `{song_id}`)."
        )

        # Delete or edit the search results message
        try:
            if self.is_ephemeral:
                embed = discord.Embed(title="Search Results", description="Song selected. Search closed.")
                await interaction.edit_original_response(embed=embed, view=None)
                helpers.schedule_interaction_deletion(interaction, 5)
            else:
                msg = self.message or interaction.message
                if msg:
                    await msg.delete()
        except discord.errors.NotFound:
            pass

        self.stop()

    async def _on_queue_selected(self, interaction: discord.Interaction) -> None:
        """Handle Queue button for selected song — add to queue without disabling radio."""
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
                "This result does not have a valid song ID to queue.",
                ephemeral=True,
            )
            return

        name = getattr(self.selected_song, "name", getattr(self.selected_song, "title", "Unknown"))
        await interaction.response.defer(ephemeral=True)
        await self._queue_fn(self.ctx, str(song_id))

        await helpers.send_ephemeral_temporary(
            interaction, f"📥 Added `{name}` to queue."
        )

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
        user_playlists = state.get_or_create_user_playlists(interaction.user.id)
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
        """Handle Info button for selected song — shows detail embed plus Lyrics/Snippets."""
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who ran this search can use these buttons.",
                ephemeral=True,
            )
            return

        song = self.selected_song
        if not song:
            await interaction.response.send_message("No song selected.", ephemeral=True)
            return

        song_title = getattr(song, "name", getattr(song, "title", "Unknown"))
        song_meta = helpers.build_song_metadata_from_song(song)

        # Build rich info embed with Lyrics/Snippets buttons in a single
        # ephemeral message (avoids the broken edit_message + followup pattern).
        embed = build_song_info_embed(song)
        info_view = NowPlayingInfoView(song_title=song_title, song_metadata=song_meta, ctx=self.ctx, queue_fn=self._queue_fn)
        await interaction.response.send_message(embed=embed, view=info_view, ephemeral=True)

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
        metadata = helpers.build_song_metadata_from_song(song, path=song_path)
        
        # Add to playlist
        playlist_tracks.append({
            "id": song_id,
            "name": song_name,
            "path": song_path,
            "metadata": metadata,
            "added_at": time.time(),
        })
        
        state.save_user_playlists_to_disk()
        
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
            # Song selected mode: Play, Queue, Add to Playlist, Info, Back buttons
            play_btn = discord.ui.Button(label="▶️ Play", style=discord.ButtonStyle.primary, row=0)
            play_btn.callback = self._on_play_selected
            self.add_item(play_btn)

            if self._queue_fn:
                queue_btn = discord.ui.Button(label="📥 Queue", style=discord.ButtonStyle.secondary, row=0)
                queue_btn.callback = self._on_queue_selected
                self.add_item(queue_btn)
            
            add_btn = discord.ui.Button(label="➕ Add to Playlist", style=discord.ButtonStyle.success, row=1)
            add_btn.callback = self._on_add_to_playlist_selected
            self.add_item(add_btn)
            
            info_btn = discord.ui.Button(label="ℹ️ Info", style=discord.ButtonStyle.secondary, row=1)
            info_btn.callback = self._on_info_selected
            self.add_item(info_btn)
            
            back_btn = discord.ui.Button(label="⬅ Back", style=discord.ButtonStyle.danger, row=1)
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


