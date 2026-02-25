"""Slash command Cog for the Juice WRLD Discord bot (/jw group)."""

from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from exceptions import JuiceWRLDAPIError, NotFoundError
import helpers
import state
from views.search import SearchPaginationView, SingleSongResultView
from views.playlist import PlaylistPaginationView


async def era_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """Autocomplete callback for era names."""
    try:
        eras = await helpers.get_api().get_eras()
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
        api = helpers.get_api()
        if current and len(current) >= 2:
            results = await api.get_songs(search=current, page=1, page_size=25)
        else:
            # No input yet — show a default page of songs so the user sees options.
            results = await api.get_songs(page=1, page_size=25)
        
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

    @property
    def _queue_fn(self):
        return self.bot.get_cog("PlaybackCog").queue_song

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
        """Interactive eras list with select menu for detailed info."""

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            eras = await helpers.get_api().get_eras()
        except JuiceWRLDAPIError as e:
            await interaction.followup.send(f"Error fetching eras: {e}", ephemeral=True)
            return

        if not eras:
            await interaction.followup.send("No eras found.", ephemeral=True)
            return

        # Import the era view
        from views.era import EraSelectView, build_eras_list_embed

        view = EraSelectView(eras=eras, interaction=interaction)
        embed = build_eras_list_embed(eras)
        
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


    @app_commands.command(name="era", description="Browse songs from a specific era.")
    @app_commands.describe(era_name="Name of the era to browse")
    @app_commands.autocomplete(era_name=era_autocomplete)
    async def slash_era(self, interaction: discord.Interaction, era_name: str) -> None:
        """Ephemeral equivalent of !jw era."""

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            results = await helpers.get_api().get_songs(era=era_name, page=1, page_size=25)
        except JuiceWRLDAPIError as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

        songs = results.get("results") or []
        if not songs:
            await interaction.followup.send(f"No songs found for era `{era_name}`.", ephemeral=True)
            return

        ctx = await commands.Context.from_interaction(interaction)
        total = results.get("count") if isinstance(results, dict) else None
        view = SearchPaginationView(ctx=ctx, songs=songs, query=f"Era: {era_name}", total_count=total, is_ephemeral=True, play_fn=self._playback.play_song, queue_fn=self._queue_fn)
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

        title, top = await helpers.find_similar_songs(guild.id)
        if not title:
            await interaction.followup.send("Nothing is currently playing. Play a song first!", ephemeral=True)
            return

        if not top:
            await interaction.followup.send(f"No similar songs found for **{title}**.", ephemeral=True)
            return

        ctx = await commands.Context.from_interaction(interaction)
        view = SearchPaginationView(ctx=ctx, songs=top, query=f"Similar to: {title}", total_count=len(top), is_ephemeral=True, play_fn=self._playback.play_song, queue_fn=self._queue_fn)
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
                results = await helpers.get_api().get_songs(search=query, page=1, page_size=1)
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
        api = helpers.get_api()
        if query.isdigit():
            # Fetch the specific song by ID
            try:
                song = await api.get_song(int(query))
                view = SingleSongResultView(ctx=ctx, song=song, query=query, play_fn=self._playback.play_song, queue_fn=self._queue_fn)
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

        # Regular search query
        try:
            results = await api.get_songs(search=query, page=1, page_size=25)
        except JuiceWRLDAPIError as e:
            await interaction.followup.send(
                f"Error while searching songs: {e}", ephemeral=True
            )
            return

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
            view = SingleSongResultView(ctx=ctx, song=songs[0], query=query, play_fn=self._playback.play_song, queue_fn=self._queue_fn)
            embed = view.build_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            # Multiple results: show pagination view
            view = SearchPaginationView(ctx=ctx, songs=songs, query=query, total_count=total, is_ephemeral=True, play_fn=self._playback.play_song, queue_fn=self._queue_fn)
            embed = view.build_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)


    @app_commands.command(name="song", description="Get detailed info for a song by ID.")
    @app_commands.describe(song_id="The numeric song ID")
    async def slash_song(self, interaction: discord.Interaction, song_id: int) -> None:
        """Ephemeral equivalent of !jw song."""

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            song = await helpers.get_api().get_song(song_id)
        except NotFoundError:
            await interaction.followup.send(
                f"No song found with ID `{song_id}`.", ephemeral=True
            )
            return
        except JuiceWRLDAPIError as e:
            await interaction.followup.send(
                f"Error fetching song: {e}", ephemeral=True
            )
            return

        ctx = await commands.Context.from_interaction(interaction)
        view = SingleSongResultView(ctx=ctx, song=song, query=str(song_id), play_fn=self._playback.play_song, queue_fn=self._queue_fn)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


    @app_commands.command(name="join", description="Make the bot join your voice channel.")
    async def slash_join(self, interaction: discord.Interaction) -> None:
        """Ephemeral equivalent of !jw join."""

        user = interaction.user
        guild = interaction.guild
        if not isinstance(user, discord.Member) or not guild:
            await interaction.response.send_message(
                "You need to be in a voice channel first.", ephemeral=True
            )
            return

        voice = await helpers.ensure_voice_connected(guild, user)
        if not voice:
            await interaction.response.send_message(
                "You need to be in a voice channel first.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Joined voice channel: {voice.channel.name}", ephemeral=True
        )


    @app_commands.command(name="leave", description="Disconnect the bot from voice chat.")
    async def slash_leave(self, interaction: discord.Interaction) -> None:
        """Ephemeral equivalent of !jw leave."""

        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        voice: Optional[discord.VoiceClient] = guild.voice_client if guild else None

        disconnected = await helpers.leave_voice_channel(
            guild, voice, delete_np_callback=self._playback._delete_now_playing_message_after_delay,
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
            await helpers.send_ephemeral_temporary(interaction, "Radio enabled. Current song will finish, then radio starts.", delay=5)
        else:
            await helpers.send_ephemeral_temporary(interaction, "Radio mode enabled. Playing random songs until you run `/jw stop`.")
            await self._playback._play_random_song_in_guild(ctx)


    @app_commands.command(name="stop", description="Stop playback and disable radio mode.")
    async def slash_stop(self, interaction: discord.Interaction) -> None:
        """Stop playback and disable radio mode."""

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild:
            state.guild_radio_enabled[guild.id] = False
            state.guild_radio_next.pop(guild.id, None)

        voice: Optional[discord.VoiceClient] = guild.voice_client if guild else None
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()

        await helpers.send_ephemeral_temporary(interaction, "Radio mode disabled and playback stopped.", delay=5)

        # Update the player embed to show idle state.
        if guild:
            ctx = await commands.Context.from_interaction(interaction)
            await self._playback._send_player_controls(
                ctx,
                title="Nothing playing",
                path=None,
                is_radio=False,
                metadata={},
                duration_seconds=None,
            )


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

    @app_commands.command(name="sotd", description="View the current Song of the Day.")
    async def slash_sotd_view(self, interaction: discord.Interaction) -> None:
        """View the current Song of the Day."""

        if not state.current_sotd:
            await interaction.response.send_message(
                "No Song of the Day has been posted yet today. Check back later!",
                ephemeral=True,
            )
            return

        song_data = state.current_sotd
        title = song_data.get("title", "Unknown")
        metadata = song_data.get("metadata", {})
        duration_seconds = song_data.get("duration_seconds")

        embed = discord.Embed(
            title="🎵 Song of the Day",
            description=f"**{title}**",
            colour=discord.Colour.gold(),
        )
        
        image_url = metadata.get("image_url")
        if image_url:
            embed.set_thumbnail(url=image_url)
        
        if metadata.get("category"):
            embed.add_field(name="Category", value=str(metadata["category"]), inline=True)
        
        era_val = metadata.get("era")
        if era_val:
            era_text = era_val.get("name") if isinstance(era_val, dict) else str(era_val)
            if era_text:
                embed.add_field(name="Era", value=era_text, inline=True)
        
        producers = metadata.get("producers")
        if producers:
            embed.add_field(name="Producers", value=str(producers), inline=True)
        
        if duration_seconds:
            m, s = divmod(duration_seconds, 60)
            embed.add_field(name="Length", value=f"{m}:{s:02d}", inline=True)
        
        embed.set_footer(text="Press Play to listen!")

        # Import the view here to avoid circular imports
        from views.sotd import SongOfTheDayView
        playback = self._playback
        view = SongOfTheDayView(
            song_data=song_data,
            queue_fn=playback._queue_or_play_now,
            stream_fn=playback._get_fresh_stream_url,
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        helpers.schedule_interaction_deletion(interaction, 60)


    @app_commands.command(name="history", description="Show the last 10 songs played in this server.")
    async def slash_history(self, interaction: discord.Interaction) -> None:
        """Ephemeral equivalent of !jw history."""

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        history = state.guild_history.get(guild.id, [])
        if not history:
            await interaction.response.send_message(
                "No songs have been played in this server yet.", ephemeral=True
            )
            return

        lines = []
        for i, entry in enumerate(history, 1):
            title = entry.get("title", "Unknown")
            meta = entry.get("metadata") or {}
            era_val = meta.get("era")
            era_text = ""
            if isinstance(era_val, dict) and era_val.get("name"):
                era_text = f" \u00b7 {era_val['name']}"
            elif era_val:
                era_text = f" \u00b7 {era_val}"
            lines.append(f"`{i}.` {title}{era_text}")

        embed = discord.Embed(
            title="Recently Played",
            description="\n".join(lines),
            colour=discord.Colour.purple(),
        )
        embed.set_footer(text=f"Last {len(history)} song(s) in this server")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        helpers.schedule_interaction_deletion(interaction, 30)

    @app_commands.command(name="leaks", description="Browse leaked songs in chronological order.")
    @app_commands.describe(
        category="Filter by category (optional)",
        limit="Number of songs to fetch (default: 100, max: 500)"
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="Unreleased", value="unreleased"),
        app_commands.Choice(name="All", value=""),
    ])
    async def slash_leaks(self, interaction: discord.Interaction, category: app_commands.Choice[str] = None, limit: int = 100) -> None:
        """Browse leaked songs chronologically with filters."""
        
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        # Limit to reasonable range
        limit = min(max(10, limit), 500)
        
        try:
            # Fetch songs with leak dates
            api = helpers.get_api()
            category_filter = category.value if category else None
            
            # Fetch multiple pages to get more songs
            all_songs = []
            page = 1
            while len(all_songs) < limit:
                results = await api.get_songs(
                    page=page,
                    page_size=min(100, limit - len(all_songs)),
                    category=category_filter if category_filter else None
                )
                songs = results.get("results") or []
                if not songs:
                    break
                
                # Only include songs with leak dates
                leaked_songs = [
                    s for s in songs 
                    if getattr(s, "date_leaked", "").strip()
                ]
                all_songs.extend(leaked_songs)
                
                page += 1
                if page > 5:  # Don't fetch too many pages
                    break
            
            if not all_songs:
                await interaction.followup.send(
                    "No leaked songs found.",
                    ephemeral=True
                )
                return
            
            # Create timeline view
            from views.timeline import LeakTimelineView
            ctx = await commands.Context.from_interaction(interaction)
            view = LeakTimelineView(ctx=ctx, songs=all_songs)
            embed = view.build_embed()
            
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            
        except JuiceWRLDAPIError as e:
            await interaction.followup.send(
                f"Error fetching leaked songs: {e}",
                ephemeral=True
            )


    @app_commands.command(name="comp", description="Search comp files by name and play the best match.")
    @app_commands.describe(
        query="Search term for the file name",
        scope="Which section to search (default: all)",
    )
    @app_commands.choices(scope=[
        app_commands.Choice(name="All", value=""),
        app_commands.Choice(name="Compilation", value="Compilation"),
        app_commands.Choice(name="Studio Sessions", value="Studio Sessions"),
        app_commands.Choice(name="Original Files", value="Original Files"),
        app_commands.Choice(name="Session Edits", value="Session Edits"),
        app_commands.Choice(name="Stem Edits", value="Stem Edits"),
    ])
    async def slash_comp(
        self,
        interaction: discord.Interaction,
        query: str,
        scope: app_commands.Choice[str] = None,
    ) -> None:
        """Slash equivalent of !jw comp / stusesh / og / seshedits / stems."""

        user = interaction.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await interaction.response.send_message(
                "You need to be in a voice channel to play music.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        base_path = scope.value if scope else ""
        scope_label = scope.name if scope else "the comp browser"

        ctx = await commands.Context.from_interaction(interaction)
        await self._playback._play_from_browse(
            ctx,
            query=query,
            base_path=base_path,
            scope_description=scope_label,
        )

        await helpers.send_ephemeral_temporary(
            interaction,
            f"Searching {scope_label} for `{query}`…",
            delay=3,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SlashCog(bot))
