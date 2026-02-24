"""Slash command Cog for the Juice WRLD Discord bot (/jw group)."""

from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from exceptions import JuiceWRLDAPIError, NotFoundError
import helpers
import state
from commands import core as _core
from views.search import SearchPaginationView, SingleSongResultView
from views.playlist import PlaylistPaginationView


async def era_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """Autocomplete callback for era names."""
    try:
        api = helpers.create_api_client()
        try:
            eras = api.get_eras()
        finally:
            api.close()
        choices = []
        for era in eras:
            name = era.name or ""
            if current and current.lower() not in name.lower():
                continue
            display = f"{name} ({era.time_frame})" if era.time_frame else name
            if len(display) > 100:
                display = display[:97] + "..."
            choices.append(app_commands.Choice(name=display, value=name))
            if len(choices) >= 25:
                break
        return choices
    except Exception:
        return []

async def song_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """Autocomplete callback for song search.
    
    Returns up to 25 song choices matching the current input.
    Only includes songs that have a duration (length), which is the
    strongest indicator that an audio file exists for the song.
    """
    try:
        api = helpers.create_api_client()
        try:
            if current and len(current) >= 2:
                results = api.get_songs(search=current, page=1, page_size=25)
            else:
                # No input yet — show a default page of songs so the user sees options.
                results = api.get_songs(page=1, page_size=25)
        finally:
            api.close()
        
        songs = results.get("results") or []
        choices = []
        
        for song in songs:
            if len(choices) >= 25:  # Discord allows max 25 choices
                break

            song_id = getattr(song, "id", None)
            if not song_id:
                continue

            length = (getattr(song, "length", "") or "").strip()

            # Skip songs with no duration — they almost never have
            # playable audio files regardless of category.
            if not length:
                continue

            name = getattr(song, "name", getattr(song, "title", "Unknown"))
            
            # Format: "Song Name - Duration" (max 100 chars for display)
            display_name = f"{name} - {length}"
            
            # Truncate if too long (Discord limit is 100 chars)
            if len(display_name) > 100:
                display_name = display_name[:97] + "..."
            
            choices.append(app_commands.Choice(name=display_name, value=str(song_id)))
        
        return choices
    except Exception:
        return []



class SlashCog(commands.GroupCog, group_name="jw"):
    """All /jw slash commands."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    @property
    def _playback(self):
        return self.bot.get_cog("PlaybackCog")

    @app_commands.command(name="ping", description="Check if the bot is alive.")
    async def slash_ping(self, interaction: discord.Interaction) -> None:
        """Ephemeral equivalent of !jw ping."""

        await interaction.response.send_message("Pong!", ephemeral=True)


    @app_commands.command(name="stats", description="View your personal listening stats.")
    async def slash_stats(self, interaction: discord.Interaction) -> None:
        """Ephemeral equivalent of !jw stats."""

        embed = helpers.build_stats_embed(interaction.user)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        helpers.schedule_interaction_deletion(interaction, 30)


    @app_commands.command(name="eras", description="List all Juice WRLD musical eras.")
    async def slash_eras(self, interaction: discord.Interaction) -> None:
        """Ephemeral equivalent of !jw eras."""

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            eras = await _core.fetch_eras()
        except JuiceWRLDAPIError as e:
            await interaction.followup.send(f"Error fetching eras: {e}", ephemeral=True)
            return

        if not eras:
            await interaction.followup.send("No eras found.", ephemeral=True)
            return

        lines = []
        for era in eras:
            tf = f" ({era.time_frame})" if era.time_frame else ""
            lines.append(f"**{era.name}**{tf}")

        embed = discord.Embed(
            title="Juice WRLD Eras",
            description="\n".join(lines),
            colour=discord.Colour.purple(),
        )
        embed.set_footer(text="Use /jw era <name> to browse songs from an era.")
        await interaction.followup.send(embed=embed, ephemeral=True)
        helpers.schedule_interaction_deletion(interaction, 30)


    @app_commands.command(name="era", description="Browse songs from a specific era.")
    @app_commands.describe(era_name="Name of the era to browse")
    @app_commands.autocomplete(era_name=era_autocomplete)
    async def slash_era(self, interaction: discord.Interaction, era_name: str) -> None:
        """Ephemeral equivalent of !jw era."""

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            results = await _core.fetch_era_songs(era_name)
        except JuiceWRLDAPIError as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

        songs = results.get("results") or []
        if not songs:
            await interaction.followup.send(f"No songs found for era `{era_name}`.", ephemeral=True)
            return

        ctx = await commands.Context.from_interaction(interaction)
        total = results.get("count") if isinstance(results, dict) else None
        view = SearchPaginationView(ctx=ctx, songs=songs, query=f"Era: {era_name}", total_count=total, is_ephemeral=True)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


    @app_commands.command(name="similar", description="Find songs similar to the currently playing track.")
    async def slash_similar(self, interaction: discord.Interaction) -> None:
        """Ephemeral equivalent of !jw similar."""

        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        title, top = await _core.find_similar_songs(guild.id)
        if not title:
            await interaction.followup.send("Nothing is currently playing. Play a song first!", ephemeral=True)
            return

        if not top:
            await interaction.followup.send(f"No similar songs found for **{title}**.", ephemeral=True)
            return

        ctx = await commands.Context.from_interaction(interaction)
        view = SearchPaginationView(ctx=ctx, songs=top, query=f"Similar to: {title}", total_count=len(top), is_ephemeral=True)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


    @app_commands.command(name="play", description="Play a Juice WRLD song in voice chat.")
    @app_commands.describe(query="Search for a song to play")
    @app_commands.autocomplete(query=song_autocomplete)
    async def slash_play(self, interaction: discord.Interaction, query: str) -> None:
        """Play a song with autocomplete search."""
    
        await interaction.response.defer(ephemeral=True, thinking=True)
    
        user = interaction.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await interaction.followup.send(
                "You need to be in a voice channel to play music.", ephemeral=True
            )
            return
    
        # Check if query is a song ID (from autocomplete) or a search term
        song_id = None
        if query.isdigit():
            song_id = query
        else:
            # Search for the song
            try:
                api = helpers.create_api_client()
                try:
                    results = api.get_songs(search=query, page=1, page_size=1)
                finally:
                    api.close()
            
                songs = results.get("results") or []
                if songs:
                    song_id = str(getattr(songs[0], "id", None))
            except Exception as e:
                await interaction.followup.send(
                    f"Error searching for song: {e}", ephemeral=True
                )
                return
    
        if not song_id:
            await interaction.followup.send(
                f"No song found for `{query}`.", ephemeral=True
            )
            return
    
        # Disable radio if active
        if interaction.guild:
            state.guild_radio_enabled[interaction.guild.id] = False
    
        # Build a Context and play the song
        ctx = await commands.Context.from_interaction(interaction)
        await self._playback.play_song(ctx, song_id)
    
        await helpers.send_ephemeral_temporary(
            interaction,
            f"🎵 Playing song...",
            delay=3,
        )


    @app_commands.command(name="search", description="Search for Juice WRLD songs.")
    @app_commands.describe(query="Search query for song titles/content")
    @app_commands.autocomplete(query=song_autocomplete)
    async def slash_search(self, interaction: discord.Interaction, query: str) -> None:
        """Ephemeral, paginated search (equivalent to !jw search)."""

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Build a Context to drive playback when buttons are pressed.
        ctx = await commands.Context.from_interaction(interaction)

        # Check if query is a song ID (from autocomplete selection)
        if query.isdigit():
            # Fetch the specific song by ID
            api = helpers.create_api_client()
            try:
                song = api.get_song(int(query))
                view = SingleSongResultView(ctx=ctx, song=song, query=query)
                embed = view.build_embed()
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)
                return
            except NotFoundError:
                await interaction.followup.send(
                    f"No song found with ID `{query}`.", ephemeral=True
                )
                helpers.schedule_interaction_deletion(interaction, 5)
                return
            except JuiceWRLDAPIError as e:
                await interaction.followup.send(
                    f"Error fetching song: {e}", ephemeral=True
                )
                return
            finally:
                api.close()

        # Regular search query
        api = helpers.create_api_client()
        try:
            results = api.get_songs(search=query, page=1, page_size=25)
        except JuiceWRLDAPIError as e:
            await interaction.followup.send(
                f"Error while searching songs: {e}", ephemeral=True
            )
            return
        finally:
            api.close()

        songs = results.get("results") or []
        if not songs:
            await interaction.followup.send(
                f"No songs found for `{query}`.", ephemeral=True
            )
            helpers.schedule_interaction_deletion(interaction, 5)
            return

        total = results.get("count") if isinstance(results, dict) else None
    
        # If only one result, show interactive single song view
        if len(songs) == 1:
            view = SingleSongResultView(ctx=ctx, song=songs[0], query=query)
            embed = view.build_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            # Multiple results: show pagination view
            view = SearchPaginationView(ctx=ctx, songs=songs, query=query, total_count=total, is_ephemeral=True)
            embed = view.build_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)


    @app_commands.command(name="leave", description="Disconnect the bot from voice chat.")
    async def slash_leave(self, interaction: discord.Interaction) -> None:
        """Ephemeral equivalent of !jw leave."""

        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        voice: Optional[discord.VoiceClient] = guild.voice_client if guild else None

        disconnected = await _core.leave_voice_channel(
            guild, voice, delete_np_callback=_delete_now_playing_message_after_delay,
        )
        if not disconnected:
            await interaction.followup.send(
                "I'm not connected to a voice channel.", ephemeral=True
            )
            return

        await helpers.send_ephemeral_temporary(interaction, "Disconnected from voice channel.")


    @app_commands.command(name="radio", description="Start radio mode: random songs until stopped.")
    async def slash_radio(self, interaction: discord.Interaction) -> None:
        """Ephemeral equivalent of !jw radio (start)."""

        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        user = interaction.user

        if not guild:
            await interaction.followup.send(
                "Radio mode can only be used in a guild.", ephemeral=True
            )
            return

        if not isinstance(user, (discord.Member,)) or not user.voice or not user.voice.channel:
            await interaction.followup.send(
                "You need to be in a voice channel to use radio.", ephemeral=True
            )
            return

        state.guild_radio_enabled[guild.id] = True

        # Reuse existing radio logic via a Context.
        ctx = await commands.Context.from_interaction(interaction)

        # If something is already playing, let it finish naturally.
        # The after-callback will detect radio is enabled and start playing
        # random songs once the current track ends.
        voice: Optional[discord.VoiceClient] = guild.voice_client if guild else None
        if voice and (voice.is_playing() or voice.is_paused()):
            await self._playback._prefetch_next_radio_song(guild.id)
            await helpers.send_temporary(ctx, "Radio enabled. Current song will finish, then radio starts.", delay=5)
        else:
            await helpers.send_temporary(ctx, "Radio mode enabled. Playing random songs until you run `!jw stop`.")
            await self._playback._play_random_song_in_guild(ctx)


    @app_commands.command(name="stop", description="Stop playback and disable radio mode.")
    async def slash_stop(self, interaction: discord.Interaction) -> None:
        """Stop playback and disable radio mode."""

        # Acknowledge silently - stop_radio sends a temporary message that auto-deletes
        await interaction.response.defer(ephemeral=True)

        ctx = await commands.Context.from_interaction(interaction)
        await self._playback.stop_radio(ctx)


    @app_commands.command(name="playlists", description="List your playlists.")
    async def slash_playlists(self, interaction: discord.Interaction) -> None:
        """Ephemeral equivalent of !jw playlists."""

        user = interaction.user
        playlists = state.user_playlists.get(user.id) or {}

        if not playlists:
            await interaction.response.send_message(
                "You don't have any playlists yet. Use ❤ Like on the player to "
                "add the current song to your Likes playlist.",
                ephemeral=True,
            )
            return

        # Build a Context to drive playback when buttons are pressed.
        await interaction.response.defer(ephemeral=True, thinking=True)
        ctx = await commands.Context.from_interaction(interaction)

        view = PlaylistPaginationView(ctx=ctx, playlists=playlists, user=user, interaction=interaction)
        embed = view.build_embed()

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)




async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SlashCog(bot))
