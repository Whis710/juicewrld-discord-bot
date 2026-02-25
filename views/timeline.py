"""Leak timeline view for browsing leaked songs chronologically."""

import math
from datetime import datetime
from typing import Any, Dict, List, Optional

import discord
from discord import ui
from discord.ext import commands

import helpers


class LeakTimelineView(ui.View):
    """Interactive view for browsing leaked songs in chronological order."""

    def __init__(
        self,
        *,
        ctx: commands.Context,
        songs: List[Any],
        era_filter: Optional[str] = None,
        year_filter: Optional[str] = None,
    ):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.all_songs = songs
        self.era_filter = era_filter
        self.year_filter = year_filter
        self.per_page = 10
        self.current_page = 0
        
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
            embed.set_footer(text="Use the buttons to filter by era or year")
        
        return embed

    def _rebuild_buttons(self):
        """Rebuild navigation buttons."""
        self.clear_items()
        
        # Row 0: Pagination
        if self.current_page > 0:
            prev_btn = ui.Button(label="◀ Previous", style=discord.ButtonStyle.secondary, row=0)
            prev_btn.callback = lambda i: self._change_page(i, -1)
            self.add_item(prev_btn)
        
        if self.current_page < self.total_pages - 1:
            next_btn = ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, row=0)
            next_btn.callback = lambda i: self._change_page(i, +1)
            self.add_item(next_btn)
        
        # Row 1: Era filter button
        era_btn = ui.Button(label="🎵 Filter by Era", style=discord.ButtonStyle.primary, row=1)
        era_btn.callback = self._show_era_filter
        self.add_item(era_btn)
        
        # Row 1: Year filter button  
        year_btn = ui.Button(label="📅 Filter by Year", style=discord.ButtonStyle.primary, row=1)
        year_btn.callback = self._show_year_filter
        self.add_item(year_btn)
        
        # Row 1: Clear filters
        if self.era_filter or self.year_filter:
            clear_btn = ui.Button(label="✖ Clear Filters", style=discord.ButtonStyle.danger, row=1)
            clear_btn.callback = self._clear_filters
            self.add_item(clear_btn)

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
