"""Leak timeline view for browsing leaked songs chronologically."""

import math
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import discord
from discord import ui
from discord.ext import commands

import helpers
import state


class LeakTimelineView(ui.View):
    """Interactive view for browsing leaked songs in chronological order."""

    def __init__(
        self,
        *,
        ctx: commands.Context,
        songs: List[Any],
        play_fn: Optional[Callable] = None,
        queue_fn: Optional[Callable] = None,
        era_filter: Optional[str] = None,
        year_filter: Optional[str] = None,
    ):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.all_songs = songs
        self._play_fn = play_fn
        self._queue_fn = queue_fn
        self.era_filter = era_filter
        self.year_filter = year_filter
        self.per_page = 10
        self.current_page = 0
        self.selected_song = None
        self.mode = "list"  # "list" or "song_detail"
        
        # Sort songs by leak date (newest first)
        self.all_songs.sort(
            key=lambda s: self._parse_leak_date(getattr(s, "date_leaked", "")),
            reverse=True
        )
        
        self.total_pages = max(1, math.ceil(len(self.all_songs) / self.per_page))
        self._rebuild_buttons()

    def _parse_leak_date(self, date_str: str) -> datetime:
        """Parse leak date string to datetime for sorting."""
        if not date_str:
            return datetime.min
        
        # Try to extract date from various formats
        # Examples: "Surfaced\nJanuary 16, 2026.", "January 16, 2026"
        try:
            # Remove common prefixes
            date_str = date_str.replace("Surfaced\n", "").replace("Surfaced", "").strip()
            date_str = date_str.rstrip(".")
            
            # Try parsing common formats
            for fmt in ["%B %d, %Y", "%B %Y", "%Y"]:
                try:
                    return datetime.strptime(date_str, fmt)
                except ValueError:
                    continue
        except Exception:
            pass
        
        return datetime.min

    def build_embed(self) -> discord.Embed:
        """Build the timeline embed."""
        if self.mode == "song_detail":
            return self._build_song_detail_embed()
        
        # List mode
        start = self.current_page * self.per_page
        page_songs = self.all_songs[start:start + self.per_page]
        
        title = "📅 Leak Timeline"
        if self.era_filter:
            title += f" - {self.era_filter}"
        if self.year_filter:
            title += f" ({self.year_filter})"
        
        embed = discord.Embed(
            title=title,
            description=f"Page {self.current_page + 1}/{self.total_pages} • {len(self.all_songs)} songs",
            colour=discord.Colour.orange(),
        )
        
        if not page_songs:
            embed.description = "No leaked songs found with the selected filters."
            return embed
        
        for song in page_songs:
            name = getattr(song, "name", "Unknown")
            leak_date = getattr(song, "date_leaked", "Unknown")
            leak_type = getattr(song, "leak_type", "")
            era_name = getattr(getattr(song, "era", None), "name", "")
            song_id = getattr(song, "id", "")
            
            # Clean up leak date
            leak_date = leak_date.replace("Surfaced\n", "").replace("Surfaced", "").strip()
            if not leak_date:
                leak_date = "Unknown"
            
            # Build field value
            field_lines = []
            if leak_date != "Unknown":
                field_lines.append(f"📅 {leak_date}")
            if leak_type:
                field_lines.append(f"🏷️ {leak_type}")
            if era_name:
                field_lines.append(f"💿 {era_name}")
            field_lines.append(f"🆔 `{song_id}`")
            
            # Check for groupbuy info
            metadata = getattr(song, "additional_information", "") or ""
            if "groupbuy" in metadata.lower() or "group buy" in metadata.lower():
                field_lines.append("💰 Groupbuy")
            
            embed.add_field(
                name=name,
                value="\n".join(field_lines),
                inline=False
            )
        
        footer_parts = []
        if self.era_filter:
            footer_parts.append(f"Era: {self.era_filter}")
        if self.year_filter:
            footer_parts.append(f"Year: {self.year_filter}")
        
        if footer_parts:
            embed.set_footer(text=" | ".join(footer_parts))
        else:
            embed.set_footer(text="Select a song from the dropdown to view details")
        
        return embed
    
    def _build_song_detail_embed(self) -> discord.Embed:
        """Build detailed view for selected song."""
        if not self.selected_song:
            return self.build_embed()
        
        song = self.selected_song
        name = getattr(song, "name", "Unknown")
        sid = getattr(song, "id", "?")
        category = getattr(song, "category", "Unknown")
        length = getattr(song, "length", "?")
        era_name = getattr(getattr(song, "era", None), "name", "Unknown")
        
        embed = discord.Embed(
            title="🎵 Now Playing" if self.mode == "song_detail" else "Song Details",
            description=f"**{name}**",
            colour=discord.Colour.green(),
        )
        
        # Basic info
        embed.add_field(name="ID", value=f"`{sid}`", inline=True)
        embed.add_field(name="Public ID", value=getattr(song, "public_id", "N/A") or "N/A", inline=True)
        embed.add_field(name="Original Key", value=getattr(song, "original_key", "N/A") or "N/A", inline=True)
        
        embed.add_field(name="Category", value=category, inline=True)
        if length:
            embed.add_field(name="Length", value=length, inline=True)
        
        # Era info
        embed.add_field(name="Era", value=era_name, inline=True)
        
        # Credits
        producers = getattr(song, "producers", "")
        if producers:
            embed.add_field(name="Producers", value=producers, inline=False)
        
        credited_artists = getattr(song, "credited_artists", "")
        if credited_artists:
            embed.add_field(name="Credited Artists", value=credited_artists, inline=False)
        
        engineers = getattr(song, "engineers", "")
        if engineers:
            embed.add_field(name="Engineers", value=engineers, inline=False)
        
        # Recording info
        recording_locations = getattr(song, "recording_locations", "")
        if recording_locations:
            embed.add_field(name="Recording Locations", value=recording_locations, inline=False)
        
        record_dates = getattr(song, "record_dates", "")
        if record_dates:
            embed.add_field(name="Record Dates", value=record_dates, inline=False)
        
        # Leak info
        leak_date = getattr(song, "date_leaked", "")
        if leak_date:
            leak_date = leak_date.replace("Surfaced\n", "").replace("Surfaced", "").strip()
            embed.add_field(name="Leak Date", value=leak_date, inline=True)
        
        leak_type = getattr(song, "leak_type", "")
        if leak_type:
            embed.add_field(name="Leak Type", value=leak_type, inline=True)
        
        # Image
        image_url = getattr(song, "image_url", "")
        if image_url:
            embed.set_thumbnail(url=image_url)
        
        embed.set_footer(text="Use the buttons below to play or add to playlist")
        return embed

    def _rebuild_buttons(self):
        """Rebuild navigation buttons."""
        self.clear_items()
        
        if self.mode == "list":
            # Song selection dropdown
            start = self.current_page * self.per_page
            page_songs = self.all_songs[start:start + self.per_page]
            
            if page_songs:
                select_menu = ui.Select(
                    placeholder="Select a song to view details...",
                    min_values=1,
                    max_values=1,
                    row=0
                )
                
                for song in page_songs[:25]:  # Discord limit
                    name = getattr(song, "name", "Unknown")
                    song_id = getattr(song, "id", "")
                    leak_date = getattr(song, "date_leaked", "")
                    leak_date = leak_date.replace("Surfaced\n", "").replace("Surfaced", "").strip()
                    
                    label = name[:100] if len(name) <= 100 else name[:97] + "..."
                    description = f"ID: {song_id}"
                    if leak_date:
                        description += f" • {leak_date[:20]}"
                    
                    select_menu.add_option(
                        label=label,
                        value=str(song_id),
                        description=description[:100]
                    )
                
                select_menu.callback = self._on_song_select
                self.add_item(select_menu)
            
            # Row 1: Pagination
            if self.current_page > 0:
                prev_btn = ui.Button(label="◀ Previous", style=discord.ButtonStyle.secondary, row=1)
                prev_btn.callback = lambda i: self._change_page(i, -1)
                self.add_item(prev_btn)
            
            if self.current_page < self.total_pages - 1:
                next_btn = ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, row=1)
                next_btn.callback = lambda i: self._change_page(i, +1)
                self.add_item(next_btn)
        
        else:
            # Song detail mode - action buttons
            # Row 0: Playback actions
            play_now_btn = ui.Button(label="▶️ Play Now", style=discord.ButtonStyle.danger, row=0)
            play_now_btn.callback = self._on_play_now
            self.add_item(play_now_btn)
            
            play_next_btn = ui.Button(label="⏭️ Play Next", style=discord.ButtonStyle.primary, row=0)
            play_next_btn.callback = self._on_play_next
            self.add_item(play_next_btn)
            
            queue_btn = ui.Button(label="📥 Add to Queue", style=discord.ButtonStyle.secondary, row=0)
            queue_btn.callback = self._on_queue
            self.add_item(queue_btn)
            
            # Row 1: Other actions
            playlist_btn = ui.Button(label="➕ Playlist", style=discord.ButtonStyle.success, row=1)
            playlist_btn.callback = self._on_add_to_playlist
            self.add_item(playlist_btn)
            
            lyrics_btn = ui.Button(label="📝 Lyrics", style=discord.ButtonStyle.secondary, row=1)
            lyrics_btn.callback = self._on_lyrics
            self.add_item(lyrics_btn)
            
            snippets_btn = ui.Button(label="🎬 Snippets", style=discord.ButtonStyle.secondary, row=1)
            snippets_btn.callback = self._on_snippets
            self.add_item(snippets_btn)
            
            back_btn = ui.Button(label="⬅ Back", style=discord.ButtonStyle.danger, row=1)
            back_btn.callback = self._on_back
            self.add_item(back_btn)

    async def _change_page(self, interaction: discord.Interaction, delta: int):
        """Handle page navigation."""
        new_page = self.current_page + delta
        if 0 <= new_page < self.total_pages:
            self.current_page = new_page
            self._rebuild_buttons()
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()

    async def _show_era_filter(self, interaction: discord.Interaction):
        """Show era filter options."""
        await interaction.response.send_message(
            "Era filtering is coming soon! Use `/jw era <name>` to browse specific eras for now.",
            ephemeral=True
        )
        helpers.schedule_interaction_deletion(interaction, 5)

    async def _show_year_filter(self, interaction: discord.Interaction):
        """Show year filter options."""
        await interaction.response.send_message(
            "Year filtering is coming soon!",
            ephemeral=True
        )
        helpers.schedule_interaction_deletion(interaction, 5)

    async def _clear_filters(self, interaction: discord.Interaction):
        """Clear all filters."""
        self.era_filter = None
        self.year_filter = None
        self.current_page = 0
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def _on_song_select(self, interaction: discord.Interaction):
        """Handle song selection from dropdown."""
        selected_id = int(interaction.data["values"][0])
        
        # Find the selected song
        for song in self.all_songs:
            if getattr(song, "id", None) == selected_id:
                self.selected_song = song
                break
        
        self.mode = "song_detail"
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def _on_back(self, interaction: discord.Interaction):
        """Return to list view."""
        self.mode = "list"
        self.selected_song = None
        self._rebuild_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def _on_play_now(self, interaction: discord.Interaction):
        """Play selected song now."""
        if not self.selected_song or not self._queue_fn:
            await interaction.response.defer()
            return
        
        song_id = getattr(self.selected_song, "id", None)
        if song_id:
            await self._queue_fn(self.ctx, song_id, position="now")
            await interaction.response.send_message(f"Playing **{getattr(self.selected_song, 'name', 'song')}** now!", ephemeral=True)
            helpers.schedule_interaction_deletion(interaction, 3)
    
    async def _on_play_next(self, interaction: discord.Interaction):
        """Queue selected song to play next."""
        if not self.selected_song or not self._queue_fn:
            await interaction.response.defer()
            return
        
        song_id = getattr(self.selected_song, "id", None)
        if song_id:
            await self._queue_fn(self.ctx, song_id, position="next")
            await interaction.response.send_message(f"**{getattr(self.selected_song, 'name', 'song')}** will play next!", ephemeral=True)
            helpers.schedule_interaction_deletion(interaction, 3)
    
    async def _on_queue(self, interaction: discord.Interaction):
        """Add selected song to end of queue."""
        if not self.selected_song or not self._queue_fn:
            await interaction.response.defer()
            return
        
        song_id = getattr(self.selected_song, "id", None)
        if song_id:
            await self._queue_fn(self.ctx, song_id, position="end")
            await interaction.response.send_message(f"Added **{getattr(self.selected_song, 'name', 'song')}** to queue!", ephemeral=True)
            helpers.schedule_interaction_deletion(interaction, 3)
    
    async def _on_add_to_playlist(self, interaction: discord.Interaction):
        """Add song to user's playlist."""
        await interaction.response.send_message(
            "Playlist management coming soon! Use `/jw pl add <name> <song_id>` for now.",
            ephemeral=True
        )
        helpers.schedule_interaction_deletion(interaction, 5)
    
    async def _on_lyrics(self, interaction: discord.Interaction):
        """Show lyrics for selected song."""
        if not self.selected_song:
            await interaction.response.defer()
            return
        
        name = getattr(self.selected_song, "name", "Unknown")
        lyrics = getattr(self.selected_song, "lyrics", None)
        
        if not lyrics:
            await interaction.response.send_message(
                f"No lyrics stored for **{name}**.",
                ephemeral=True
            )
            helpers.schedule_interaction_deletion(interaction, 5)
            return
        
        # Truncate to Discord's embed description limit
        lyrics_text = str(lyrics)[:4096]
        
        embed = discord.Embed(
            title=f"📝 Lyrics - {name}",
            description=lyrics_text,
            colour=discord.Colour.blue()
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        helpers.schedule_interaction_deletion(interaction, 60)
    
    async def _on_snippets(self, interaction: discord.Interaction):
        """Show snippets for selected song."""
        if not self.selected_song:
            await interaction.response.defer()
            return
        
        name = getattr(self.selected_song, "name", "Unknown")
        snippets = getattr(self.selected_song, "snippets", None)
        
        if not snippets:
            await interaction.response.send_message(
                f"No snippets stored for **{name}**.",
                ephemeral=True
            )
            helpers.schedule_interaction_deletion(interaction, 5)
            return
        
        # Format snippets list
        lines = []
        if isinstance(snippets, (list, tuple)):
            for snip in snippets:
                if isinstance(snip, dict):
                    label = snip.get("label") or snip.get("name") or snip.get("id") or str(snip)
                    lines.append(f"- {label}")
                else:
                    lines.append(f"- {snip}")
        else:
            lines.append(str(snippets))
        
        body = "\n".join(lines)[:4096]
        
        embed = discord.Embed(
            title=f"🎬 Snippets - {name}",
            description=body,
            colour=discord.Colour.purple()
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        helpers.schedule_interaction_deletion(interaction, 60)
