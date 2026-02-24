"""Song of the Day view for the Juice WRLD Discord bot."""

from typing import Any, Callable, Dict, Optional

import discord
from discord.ext import commands

import helpers

class SongOfTheDayView(discord.ui.View):
    """View with a Play button for the Song of the Day embed."""

    def __init__(
        self,
        *,
        song_data: Dict[str, Any],
        queue_fn: Optional[Callable] = None,
        stream_fn: Optional[Callable] = None,
    ) -> None:
        super().__init__(timeout=None)  # persistent — no timeout
        self.song_data = song_data
        self._queue_fn = queue_fn
        self._stream_fn = stream_fn

    @discord.ui.button(label="▶️ Play", style=discord.ButtonStyle.primary)
    async def play_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        user = interaction.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await interaction.response.send_message(
                "You need to be in a voice channel to play this.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            return

        voice = await helpers.ensure_voice_connected(guild, user)

        file_path = self.song_data.get("path")
        if not file_path:
            await interaction.followup.send("No playable file for this song.", ephemeral=True)
            return

        stream_url = await self._stream_fn(file_path)
        if not stream_url:
            await interaction.followup.send("Could not get a stream URL for this song.", ephemeral=True)
            return

        # Build a lightweight ctx from the interaction for queue_or_play_now.
        ctx = await commands.Context.from_interaction(interaction)

        title = self.song_data.get("title", "Unknown")
        metadata = self.song_data.get("metadata", {})
        duration_seconds = self.song_data.get("duration_seconds")

        await self._queue_fn(
            ctx,
            stream_url=stream_url,
            title=title,
            path=file_path,
            metadata=metadata,
            duration_seconds=duration_seconds,
        )
        await interaction.followup.send(f"Now playing: **{title}**", ephemeral=True)


