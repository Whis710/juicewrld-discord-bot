"""Playlist-related UI views for the Juice WRLD Discord bot."""

import asyncio
import io
import math
import os
import random
import sys
import time
from typing import Any, Callable, Dict, List, Optional

import aiohttp
import discord
from discord.ext import commands

import helpers
import state

class PlaylistPaginationView(discord.ui.View):
    """Paginated playlists with menu-first UI.
    
    Modes:
    - "menu": Shows 4 action buttons (Queue, Add, Edit, Download)
    - "queue": Shows 1-5 buttons to queue a playlist
    - "shuffle": Shows 🔀1-5 buttons to shuffle and queue a playlist
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
            elif self.mode == "shuffle":
                footer = "Press 🔀1–5 to shuffle and queue that playlist."
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
            
            shuffle_btn = discord.ui.Button(label="🔀 Shuffle & Queue", style=discord.ButtonStyle.primary, row=0)
            shuffle_btn.callback = self._on_shuffle_mode
            self.add_item(shuffle_btn)
            
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
                elif self.mode == "shuffle":
                    label = f"🔀{slot + 1}"
                    style = discord.ButtonStyle.primary
                    callback = self._make_shuffle_callback(slot)
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
            await self._handle_play(interaction, slot_index, shuffle=False)
        return callback
    
    def _make_shuffle_callback(self, slot_index: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_play(interaction, slot_index, shuffle=True)
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
    
    async def _on_shuffle_mode(self, interaction: discord.Interaction) -> None:
        self.mode = "shuffle"
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

    async def _handle_play(self, interaction: discord.Interaction, slot_index: int, shuffle: bool = False) -> None:
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
        
        # Shuffle tracks if requested
        if shuffle:
            tracks = list(tracks)  # Make a copy
            random.shuffle(tracks)

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
            state.guild_radio_enabled[self.ctx.guild.id] = False

        voice = await helpers.ensure_voice_connected(self.ctx.guild, user)

        queued = 0
        errors = 0
        for track in tracks:
            file_path = track.get("path")
            if not file_path:
                continue

            try:
                result = await helpers.get_api().stream_audio_file(file_path)

                if result.get("status") != "success":
                    errors += 1
                    continue

                stream_url = result.get("stream_url")
                if not stream_url:
                    errors += 1
                    continue

                title = track.get("name") or f"Playlist {playlist_name} item"
                metadata = track.get("metadata") or {}
                duration_seconds = helpers.extract_duration_seconds(metadata, track)

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
            shuffle_text = " (shuffled)" if shuffle else ""
            playing_embed = discord.Embed(
                title="🎵 Now Playing",
                description=f"Playing playlist **{playlist_name}**{shuffle_text} ({queued} track(s) queued).",
                color=discord.Color.green(),
            )
            playing_embed.set_footer(text="This message will disappear in 5 seconds.")
            try:
                await interaction.edit_original_response(embed=playing_embed, view=None)
                helpers.schedule_interaction_deletion(interaction, 5)
            except Exception:
                # Fallback if edit fails - use ephemeral temporary
                await helpers.send_ephemeral_temporary(
                    interaction,
                    f"🎵 Now playing playlist `{playlist_name}`{shuffle_text} ({queued} track(s) queued).",
                    delay=5,
                )

    async def _handle_add_to_playlist(self, interaction: discord.Interaction, slot_index: int) -> None:
        """Handle pressing an 'Add to Playlist' button (➕1–5) to add currently playing song."""
        global_index = self.current_page * self.per_page + slot_index
        if global_index < 0 or global_index >= len(self.playlist_items):
            await interaction.response.send_message(
                "No playlist in that position.", ephemeral=True
            )
            helpers.schedule_interaction_deletion(interaction, 5)
            return

        target_playlist_name, target_tracks = self.playlist_items[global_index]

        # Get the currently playing song
        guild = self.ctx.guild
        if not guild:
            await interaction.response.send_message(
                "Guild context unavailable.", ephemeral=True
            )
            helpers.schedule_interaction_deletion(interaction, 5)
            return

        info = state.guild_now_playing.get(guild.id)
        if not info:
            await interaction.response.send_message(
                "Nothing is currently playing.", ephemeral=True
            )
            helpers.schedule_interaction_deletion(interaction, 5)
            return

        await interaction.response.defer(ephemeral=True)

        meta = info.get("metadata") or {}
        title = str(info.get("title", meta.get("name") or "Unknown"))
        path = meta.get("path") or info.get("path")
        song_id_val = meta.get("id") or meta.get("song_id")

        user = interaction.user
        playlists = state.get_or_createstate.user_playlists(user.id)
        playlist = playlists.get(target_playlist_name)
        
        if playlist is None:
            msg = await interaction.followup.send(
                f"Playlist `{target_playlist_name}` not found.", ephemeral=True, wait=True
            )
            asyncio.create_task(helpers.delete_later(msg, 5))
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
            asyncio.create_task(helpers.delete_later(msg, 5))
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

        state.savestate.user_playlists_to_disk()

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
        asyncio.create_task(helpers.delete_later(msg, 5))

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
        playlists = state.user_playlists.get(user.id) or {}
        
        if playlist_name in playlists:
            del playlists[playlist_name]
            if not playlists:
                state.user_playlists.pop(user.id, None)
            state.savestate.user_playlists_to_disk()
        
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
            zip_content = await helpers.get_api().create_zip(file_paths)

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
            state.guild_radio_enabled[self.ctx.guild.id] = False

        voice = await helpers.ensure_voice_connected(self.ctx.guild, user)

        queued = 0
        for track in self.tracks:
            file_path = track.get("path")
            if not file_path:
                continue

            result = await helpers.get_api().stream_audio_file(file_path)

            if result.get("status") != "success":
                continue

            stream_url = result.get("stream_url")
            if not stream_url:
                continue

            title = track.get("name") or f"Track from {self.playlist_name}"
            metadata = track.get("metadata") or {}
            duration_seconds = helpers.extract_duration_seconds(metadata, track)

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
            asyncio.create_task(helpers.delete_later(queue_msg, 5))
            # Delete the shared playlist message after 120 seconds (only on success)
            await self._schedule_message_deletion()

    async def _on_copy(self, interaction: discord.Interaction) -> None:
        """Copy this playlist to the user's own playlists."""
        user = interaction.user
        user_playlists = state.get_or_createstate.user_playlists(user.id)
        
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
        state.savestate.user_playlists_to_disk()
        
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
            zip_content = await helpers.get_api().create_zip(file_paths)

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
            asyncio.create_task(helpers.delete_later(self.message, 120))
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
        user_playlists = state.user_playlists.get(self.user.id) or {}
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
        playlists = state.user_playlists.get(user.id) or {}
        
        if self.playlist_name in playlists:
            del playlists[self.playlist_name]
            if not playlists:
                state.user_playlists.pop(user.id, None)
            state.savestate.user_playlists_to_disk()
        
        # Go back to parent view with refreshed data
        user_playlists = state.user_playlists.get(user.id) or {}
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
        playlists = state.user_playlists.get(user.id) or {}
        playlist = playlists.get(self.playlist_name)
        
        if playlist is None or global_index >= len(playlist):
            await interaction.response.send_message("Track not found.", ephemeral=True)
            return
        
        removed_track = playlist.pop(global_index)
        self.tracks = playlist  # Update local reference
        state.savestate.user_playlists_to_disk()
        
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
        playlists = state.user_playlists.get(user.id) or {}
        old_name = self.edit_view.playlist_name
        
        if new_name == old_name:
            await interaction.response.defer()
            return
        
        if new_name in playlists:
            await interaction.response.send_message(f"A playlist named `{new_name}` already exists.", ephemeral=True)
            return
        
        if old_name in playlists:
            playlists[new_name] = playlists.pop(old_name)
            state.savestate.user_playlists_to_disk()
        
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
        playlists = state.get_or_createstate.user_playlists(user.id)
        
        if name in playlists:
            await interaction.response.send_message(
                f"You already have a playlist named `{name}`.", ephemeral=True
            )
            return
        
        playlists[name] = []
        state.savestate.user_playlists_to_disk()
        
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
        playlists = state.user_playlists.get(user.id) or {}
        
        if new_name == self.old_name:
            await interaction.response.defer()
            return
        
        if new_name in playlists:
            await interaction.response.send_message(f"A playlist named `{new_name}` already exists.", ephemeral=True)
            return
        
        if self.old_name in playlists:
            playlists[new_name] = playlists.pop(self.old_name)
            state.savestate.user_playlists_to_disk()
        
        # Refresh the view
        self.pagination_view.playlist_items = list(playlists.items())
        self.pagination_view.total_pages = max(1, math.ceil(len(self.pagination_view.playlist_items) / self.pagination_view.per_page))
        self.pagination_view.mode = "menu"
        self.pagination_view._rebuild_buttons()
        
        embed = self.pagination_view.build_embed()
        await interaction.response.edit_message(embed=embed, view=self.pagination_view)


