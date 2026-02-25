"""Era browsing view with interactive select menu."""

from typing import Any, Dict, List, Optional

import discord
from discord import ui

import helpers


class EraSelectView(ui.View):
    """View with a select menu for choosing an era to view details."""

    def __init__(self, eras: List[Any], interaction: discord.Interaction):
        super().__init__(timeout=300)
        self.eras = eras
        self.interaction = interaction

        # Build select menu options
        options = []
        for era in eras[:25]:  # Discord limit is 25 options
            name = era.name or "Unknown"
            # Truncate long names
            if len(name) > 100:
                name = name[:97] + "..."
            
            # Description shows time frame
            description = era.time_frame if era.time_frame else "Era info"
            if len(description) > 100:
                description = description[:97] + "..."
            
            options.append(
                discord.SelectOption(
                    label=name,
                    description=description,
                    value=str(era.id) if hasattr(era, 'id') else name,
                )
            )

        if options:
            self.select = ui.Select(
                placeholder="Select an era to view details...",
                options=options,
                custom_id="era_select",
            )
            self.select.callback = self.select_callback
            self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction) -> None:
        """Handle era selection."""
        await interaction.response.defer()

        # Find selected era
        selected_value = self.select.values[0]
        selected_era = None
        for era in self.eras:
            era_id = str(era.id) if hasattr(era, 'id') else era.name
            if era_id == selected_value:
                selected_era = era
                break

        if not selected_era:
            await interaction.followup.send("Era not found.", ephemeral=True)
            return

        # Build detailed embed
        embed = discord.Embed(
            title=f"📀 {selected_era.name}",
            colour=discord.Colour.purple(),
        )

        # Add fields
        if selected_era.time_frame:
            embed.add_field(name="Time Frame", value=selected_era.time_frame, inline=False)

        if hasattr(selected_era, 'description') and selected_era.description:
            desc = selected_era.description
            if len(desc) > 1024:
                desc = desc[:1021] + "..."
            embed.add_field(name="Description", value=desc, inline=False)

        # Get song count for this era
        try:
            api = helpers.get_api()
            results = await api.get_songs(era=selected_era.name, page=1, page_size=1)
            total_songs = results.get("count", 0) if isinstance(results, dict) else 0
            if total_songs:
                embed.add_field(name="Total Songs", value=str(total_songs), inline=True)
        except Exception:
            pass

        # Add play count if available
        if hasattr(selected_era, 'play_count') and selected_era.play_count:
            embed.add_field(name="Play Count", value=str(selected_era.play_count), inline=True)

        embed.set_footer(text=f"Use /jw era {selected_era.name} to browse songs from this era")

        await interaction.followup.send(embed=embed, ephemeral=True)
        helpers.schedule_interaction_deletion(interaction, 60)


def build_eras_list_embed(eras: List[Any]) -> discord.Embed:
    """Build the main eras list embed."""
    lines = []
    for era in eras:
        name = era.name or "Unknown"
        tf = f" • {era.time_frame}" if era.time_frame else ""
        lines.append(f"**{name}**{tf}")

    embed = discord.Embed(
        title="🎵 Juice WRLD Eras",
        description="\n".join(lines) if lines else "No eras found.",
        colour=discord.Colour.purple(),
    )
    embed.set_footer(text="Select an era below to view detailed information")
    return embed
