"""Song of the Day view for the Juice WRLD Discord bot."""

from typing import Any, Callable, Dict, Optional

import discord

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

        channel = user.voice.channel
        voice: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if voice and voice.is_connected():
            if voice.channel != channel:
                await voice.move_to(channel)
        else:
            voice = await channel.connect()

        file_path = self.song_data.get("path")
        if not file_path:
            await interaction.followup.send("No playable file for this song.", ephemeral=True)
            return

        stream_url = await self._stream_fn(file_path)
        if not stream_url:
            await interaction.followup.send("Could not get a stream URL for this song.", ephemeral=True)
            return

        # Build a fake ctx from the interaction so queue_or_play_now works.
        ctx = await interaction.client.get_context(await interaction.channel.fetch_message(interaction.message.id))
        ctx.author = user  # type: ignore[assignment]

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


