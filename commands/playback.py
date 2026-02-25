"""Playback command Cog for the Juice WRLD Discord bot."""

import asyncio
import os
import sys
import time
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands, tasks

from constants import AUTO_LEAVE_IDLE_SECONDS, BOT_VERSION, NOTHING_PLAYING
from exceptions import JuiceWRLDAPIError, NotFoundError
import helpers
import state
from views.player import PlayerView, build_player_embed

# FFmpeg options shared by all playback paths.
_FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
_FFMPEG_OPTIONS = "-vn"


class PlaybackCog(commands.Cog):
    """Voice playback, radio, queue management, and related commands."""

    # Rotating idle status messages.
    _IDLE_STATUSES: List[str] = []
    _idle_status_index: int = 0

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        PlaybackCog._IDLE_STATUSES = [f"v{BOT_VERSION}", 'try "/jw"', "Idle play me"]
        self._update_player_messages.start()
        self._idle_auto_leave.start()
        self._rotate_idle_presence.start()

    def cog_unload(self) -> None:
        self._update_player_messages.cancel()
        self._idle_auto_leave.cancel()
        self._rotate_idle_presence.cancel()

    async def _delete_now_playing_message(self, guild_id: int) -> None:
        """Best-effort deletion of the tracked Now Playing message for a guild."""

        info = state.guild_now_playing.get(guild_id)
        if not info:
            return

        msg = info.get("message_obj")
        if msg is not None:
            try:
                await msg.delete()
            except Exception:
                pass
        else:
            # Fallback: no cached object, try fetching by ID.
            message_id = info.get("message_id")
            channel_id = info.get("channel_id")
            if message_id is not None and channel_id is not None:
                guild_obj = self.bot.get_guild(guild_id)
                if guild_obj:
                    chan = guild_obj.get_channel(channel_id) or self.bot.get_channel(channel_id)
                    if isinstance(chan, discord.TextChannel):
                        try:
                            fetched = await chan.fetch_message(message_id)
                            await fetched.delete()
                        except Exception:
                            pass

        state.guild_now_playing.pop(guild_id, None)


    async def _delete_now_playing_message_after_delay(self, guild_id: int, delay: int) -> None:
        """Sleep for `delay` seconds, then delete the guild's Now Playing message."""

        await asyncio.sleep(delay)
        await self._delete_now_playing_message(guild_id)



    def _disable_radio_if_active(self, ctx: commands.Context) -> bool:
        """Turn off radio mode for this guild if it is currently enabled.

        Returns True if radio was active and is now disabled.
        """

        if not ctx.guild:
            return False

        guild_id = ctx.guild.id
        if state.guild_radio_enabled.get(guild_id):
            state.guild_radio_enabled[guild_id] = False
            return True
        return False


    async def _play_next_from_queue(self, ctx: commands.Context) -> None:
        """Play the next queued track for this guild, if any.

        This is invoked from after-callbacks when a non-radio track finishes.
        """

        if not ctx.guild:
            return

        guild_id = ctx.guild.id

        # If radio was re-enabled mid-queue, hand control back to radio.
        if state.guild_radio_enabled.get(guild_id):
            await self._play_random_song_in_guild(ctx)
            return

        queue = state.ensure_queue(guild_id)
        if not queue:
            # Nothing left to play; keep the player message but show it as idle.
            await self._send_player_controls(
                ctx,
                title="Nothing playing",
                path=None,
                is_radio=False,
                metadata={},
                duration_seconds=None,
            )
            return

        entry = queue.pop(0)
        stream_url = entry.get("stream_url")
        title = str(entry.get("title") or "Unknown")
        path = entry.get("path")
        metadata = entry.get("metadata") or {}
        duration_seconds = entry.get("duration_seconds")

        voice: Optional[discord.VoiceClient] = ctx.voice_client
        if not voice or not stream_url:
            return

        if voice.is_playing():
            voice.stop()

        try:
            source = discord.FFmpegPCMAudio(
                stream_url,
                before_options=_FFMPEG_BEFORE,
                options=_FFMPEG_OPTIONS,
            )
        except Exception as e:  # pragma: no cover
            print(f"Queue playback error creating source: {e}", file=sys.stderr)
            # Try the next track in the queue, if any.
            await self._play_next_from_queue(ctx)
            return

        def _after_playback(error: Optional[Exception]) -> None:
            if error:
                print(f"Queue playback error: {error}", file=sys.stderr)
            fut = self._play_next_from_queue(ctx)
            asyncio.run_coroutine_threadsafe(fut, self.bot.loop)

        voice.play(source, after=_after_playback)
        await self._send_player_controls(
            ctx,
            title=title,
            path=path,
            is_radio=False,
            metadata=metadata,
            duration_seconds=duration_seconds,
        )


    async def _queue_or_play_now(
        self,
        ctx: commands.Context,
        *,
        stream_url: str,
        title: str,
        path: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
        duration_seconds: Optional[int] = None,
        silent: bool = False,
    ) -> None:
        """If something is already playing, queue this track; otherwise play now.

        This is used by search/comp/play commands for on-demand playback.
        If silent=True, suppress individual "added to queue" messages (useful for batch adds).
        """

        if not ctx.guild:
            return

        guild_id = ctx.guild.id
        voice: Optional[discord.VoiceClient] = ctx.voice_client

        queue = state.ensure_queue(guild_id)

        # If there is active playback (song or radio), enqueue this track.
        if voice and (voice.is_playing() or voice.is_paused()):
            queue.append(
                {
                    "title": title,
                    "path": path,
                    "stream_url": stream_url,
                    "metadata": metadata or {},
                    "duration_seconds": duration_seconds,
                }
            )
            if not silent:
                await helpers.send_temporary(
                    ctx,
                    f"Added to queue at position {len(queue)}: `{title}`.",
                )
            return

        # Nothing is playing; start immediately and wire up the queue callback.
        if not voice or not voice.is_connected():
            voice = await helpers.ensure_voice_connected(ctx.guild, ctx.author)
            if not voice:
                await ctx.send("You need to be in a voice channel to play music.")
                return

        try:
            source = discord.FFmpegPCMAudio(
                stream_url,
                before_options=_FFMPEG_BEFORE,
                options=_FFMPEG_OPTIONS,
            )
        except Exception as e:  # pragma: no cover
            await ctx.send(f"Failed to create audio source: {e}")
            return

        def _after_playback(error: Optional[Exception]) -> None:
            if error:
                print(f"Playback error: {error}", file=sys.stderr)
            fut = self._play_next_from_queue(ctx)
            asyncio.run_coroutine_threadsafe(fut, self.bot.loop)

        voice.play(source, after=_after_playback)
        await self._send_player_controls(
            ctx,
            title=title,
            path=path,
            is_radio=False,
            metadata=metadata,
            duration_seconds=duration_seconds,
        )


    def _set_now_playing(
        self,
        ctx: commands.Context,
        *,
        title: str,
        path: Optional[str],
        is_radio: bool,
        metadata: Optional[Dict[str, Any]] = None,
        duration_seconds: Optional[int] = None,
    ) -> None:
        """Record the currently playing track for a guild.

        We keep any existing player message metadata (message_id/channel_id)
        so that subsequent tracks can reuse and edit the same message.
        """

        if not ctx.guild:
            return

        guild_id = ctx.guild.id
        existing = state.guild_now_playing.get(guild_id, {})
    
        # Save current song as previous and push to history.
        if existing.get("title") and existing.get("title") != "Nothing playing":
            prev_entry = {
                "title": existing.get("title"),
                "path": existing.get("path"),
                "metadata": existing.get("metadata", {}),
                "duration_seconds": existing.get("duration_seconds"),
            }
            state.guild_previous_song[guild_id] = prev_entry
            state.push_history(guild_id, prev_entry)
    
        existing.update(
            {
                "title": title,
                "path": path,
                "is_radio": is_radio,
                "requester": getattr(ctx.author, "mention", str(ctx.author)),
                "metadata": metadata or {},
                "duration_seconds": duration_seconds,
                "started_at": time.time(),
                "paused_at": None,  # Track when paused
                "total_paused_time": 0,  # Accumulated pause time
            }
        )
        state.guild_now_playing[guild_id] = existing

        # Mark activity so the idle auto-leave timer resets.
        state.touch_activity(guild_id)

        # Record the listen for the requester's stats.
        if title and title != "Nothing playing":
            user_id = getattr(ctx.author, "id", None)
            if user_id:
                era_name = None
                meta = metadata or {}
                era_val = meta.get("era")
                if isinstance(era_val, dict):
                    era_name = era_val.get("name")
                elif era_val:
                    era_name = str(era_val)
                state.record_listen(user_id, title, era_name, duration_seconds)

        # Update the bot's Discord Rich Presence to show the current song.
        if title and title != "Nothing playing":
            now = time.time()
            # Build timestamps for a live elapsed/remaining timer.
            # Discord Gateway expects millisecond epoch timestamps.
            timestamps: Dict[str, Any] = {"start": int(now * 1000)}
            if duration_seconds and duration_seconds > 0:
                timestamps["end"] = int((now + duration_seconds) * 1000)

            # Compose the "state" line: era + category.
            meta = metadata or {}
            state_parts: List[str] = []
            era_val = meta.get("era")
            if isinstance(era_val, dict) and era_val.get("name"):
                state_parts.append(era_val["name"])
            elif era_val:
                state_parts.append(str(era_val))
            cat = meta.get("category")
            if cat:
                state_parts.append(str(cat))
            state_text = " · ".join(state_parts) if state_parts else None

            # Duration text for the name field.
            if duration_seconds and duration_seconds > 0:
                dm, ds = divmod(duration_seconds, 60)
                activity_name = f"{title} [{dm}:{ds:02d}]"
            else:
                activity_name = title
            if len(activity_name) > 128:
                activity_name = activity_name[:125] + "..."

            # Album art: prefer the song's own image_url; fall back to the
            # app-level Rich Presence asset "juicewrld-cover" from Developer Portal.
            image_url = meta.get("image_url")
            assets: Dict[str, str] = {
                "large_image": image_url or "juicewrld-cover",
                "large_text": title,
            }

            # Small image: radio vs play icon (upload as "radio-icon" / "play-icon").
            if is_radio:
                assets["small_image"] = "radio-icon"
                assets["small_text"] = "Radio Mode"
            else:
                assets["small_image"] = "play-icon"
                assets["small_text"] = "Now Playing"

            # Party size: show how many people are in the voice channel.
            party: Optional[Dict[str, Any]] = None
            voice_client: Optional[discord.VoiceClient] = ctx.voice_client
            if voice_client and voice_client.channel:
                humans = [m for m in voice_client.channel.members if not m.bot]
                if humans:
                    party = {"size": [len(humans), voice_client.channel.user_limit or len(humans)]}

            # Rich Presence buttons (up to 2 URL buttons on the activity card).
            buttons = ["Invite Bot"]

            activity = discord.Activity(
                type=discord.ActivityType.playing,
                name=activity_name,
                application_id=self.bot.application_id,
                details=title,
                state=state_text,
                timestamps=timestamps,
                assets=assets,
                party=party,
                buttons=buttons,
            )
        else:
            # Use current rotating idle status.
            idle_name = self._IDLE_STATUSES[self._idle_status_index % len(self._IDLE_STATUSES)]
            activity = discord.Activity(type=discord.ActivityType.playing, name=idle_name)
        asyncio.create_task(self.bot.change_presence(activity=activity))

    async def _send_player_controls(
        self,
        ctx: commands.Context,
        *,
        title: str,
        path: Optional[str],
        is_radio: bool,
        metadata: Optional[Dict[str, Any]] = None,
        duration_seconds: Optional[int] = None,
    ) -> None:
        """Send or update the Now Playing embed with interactive controls.

        This keeps a single player message per guild and edits it when tracks
        change instead of sending a new message every time.
        """

        if not ctx.guild:
            return

        self._set_now_playing(
            ctx,
            title=title,
            path=path,
            is_radio=is_radio,
            metadata=metadata,
            duration_seconds=duration_seconds,
        )

        guild_id = ctx.guild.id
        info = state.guild_now_playing.get(guild_id, {})
        # Store a lightweight reference to ctx so the background task can
        # reconstruct the view. Avoid storing the full object tree.
        info.setdefault("ctx", ctx)
        state.guild_now_playing[guild_id] = info
        message_id = info.get("message_id")
        channel_id = info.get("channel_id")

        # Build embed using the centralized function
        embed = build_player_embed(
            guild_id,
            title=title,
            metadata=info.get("metadata"),
            duration_seconds=duration_seconds or info.get("duration_seconds"),
            started_at=info.get("started_at"),
            paused_at=info.get("paused_at"),
            total_paused_time=info.get("total_paused_time", 0),
            is_radio=is_radio,
        )

        view = PlayerView(ctx=ctx, is_radio=is_radio, queue_fn=self._queue_or_play_now, send_controls_fn=self._send_player_controls, radio_fn=self._play_random_song_in_guild, prefetch_fn=self._prefetch_next_radio_song)

        # If we have a previously-sent player message, try to edit it.
        target_channel = ctx.channel
        if channel_id is not None and ctx.guild is not None:
            chan = ctx.guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
            if isinstance(chan, discord.TextChannel):
                target_channel = chan

        cached_msg = info.get("message_obj")
        if cached_msg is not None:
            try:
                await cached_msg.edit(embed=embed, view=view)
                return
            except Exception:
                # Cached object is stale (deleted, etc.) — fall through to send new.
                pass
        elif message_id is not None and isinstance(target_channel, discord.abc.Messageable):
            try:
                msg = await target_channel.fetch_message(message_id)
                await msg.edit(embed=embed, view=view)
                state.guild_now_playing[guild_id]["message_obj"] = msg
                return
            except Exception:
                pass

        sent = await target_channel.send(embed=embed, view=view)
        # Persist the message metadata so we can edit next time.
        state.guild_now_playing[guild_id]["message_id"] = sent.id
        state.guild_now_playing[guild_id]["channel_id"] = sent.channel.id
        state.guild_now_playing[guild_id]["message_obj"] = sent


    @tasks.loop(seconds=5)
    async def _update_player_messages(self) -> None:
        """Periodically refresh Now Playing embeds with updated progress.

        This keeps the progress bar and elapsed time roughly in sync with
        playback. Runs every 5 seconds for all guilds with a tracked
        now-playing message.
        """

        for guild in list(state.guild_now_playing.keys()):
            info = state.guild_now_playing.get(guild)
            if not info:
                continue

            message_id = info.get("message_id")
            channel_id = info.get("channel_id")
            title = info.get("title")
            path = info.get("path")
            is_radio = bool(info.get("is_radio"))
            metadata = info.get("metadata") or {}
            duration_seconds = info.get("duration_seconds")

            if message_id is None or channel_id is None or not title:
                continue

            guild_obj = self.bot.get_guild(guild)
            if not guild_obj:
                continue

            # Skip guilds where the bot isn't actively playing.
            voice: Optional[discord.VoiceClient] = guild_obj.voice_client  # type: ignore[assignment]
            if not voice or not voice.is_connected() or not (voice.is_playing() or voice.is_paused()):
                continue

            # Use cached message object; fall back to fetch if not available.
            msg = info.get("message_obj")
            if msg is None:
                chan = guild_obj.get_channel(channel_id) or self.bot.get_channel(channel_id)
                if not isinstance(chan, discord.TextChannel):
                    continue
                try:
                    msg = await chan.fetch_message(message_id)
                    info["message_obj"] = msg
                except Exception:
                    # Message may have been deleted; stop tracking it.
                    continue

            # Build embed using the centralized function
            embed = build_player_embed(
                guild,
                title=title,
                metadata=metadata,
                duration_seconds=duration_seconds,
                started_at=info.get("started_at"),
                paused_at=info.get("paused_at"),
                total_paused_time=info.get("total_paused_time", 0),
                is_radio=is_radio,
            )

            view = PlayerView(ctx=info.get("ctx"), is_radio=is_radio, queue_fn=self._queue_or_play_now, send_controls_fn=self._send_player_controls, radio_fn=self._play_random_song_in_guild, prefetch_fn=self._prefetch_next_radio_song) if info.get("ctx") else None
            try:
                await msg.edit(embed=embed, view=view)
            except Exception:
                # Edit failed — message may be deleted. Clear cached object
                # so the next tick falls back to fetch (or discovers it's gone).
                info.pop("message_obj", None)
                continue


    async def _auto_disconnect_guild(self, guild: discord.Guild, reason: str = "inactivity") -> None:
        """Cleanly disconnect the bot from voice in a guild and reset state."""

        guild_id = guild.id
        state.guild_radio_enabled[guild_id] = False
        state.guild_radio_next.pop(guild_id, None)
        state.guild_last_activity.pop(guild_id, None)

        voice: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if voice and voice.is_connected():
            if voice.is_playing() or voice.is_paused():
                voice.stop()
            await voice.disconnect()

        # Clear the bot's Discord activity status to idle rotation.
        idle_name = self._IDLE_STATUSES[self._idle_status_index % len(self._IDLE_STATUSES)]
        asyncio.create_task(
            self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=idle_name))
        )

        # Delete the Now Playing message after a brief moment.
        asyncio.create_task(self._delete_now_playing_message_after_delay(guild_id, 1))

        # Try to notify a text channel.
        info = state.guild_now_playing.get(guild_id)
        channel_id = info.get("channel_id") if info else None
        if channel_id:
            chan = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
            if isinstance(chan, discord.TextChannel):
                try:
                    msg = await chan.send(f"Disconnected due to {reason}.")
                    asyncio.create_task(helpers.delete_later(msg, 10))
                except Exception:
                    pass


    @tasks.loop(seconds=60)
    async def _idle_auto_leave(self) -> None:
        """Periodically check for guilds where the bot is idle and auto-leave."""

        now = time.time()
        for guild in list(self.bot.guilds):
            voice: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
            if not voice or not voice.is_connected():
                continue

            # If actively playing, refresh the timestamp and skip.
            if voice.is_playing():
                state.touch_activity(guild.id)
                continue

            last = state.guild_last_activity.get(guild.id)
            if last is None:
                # First time we're checking — initialise and give it a full window.
                state.touch_activity(guild.id)
                continue

            if now - last >= AUTO_LEAVE_IDLE_SECONDS:
                await self._auto_disconnect_guild(guild, reason="30 minutes of inactivity")


    @_idle_auto_leave.before_loop
    async def _before_idle_auto_leave(self) -> None:
        await self.bot.wait_until_ready()


    @tasks.loop(seconds=15)
    async def _rotate_idle_presence(self) -> None:
        """Rotate the idle status message every 15 seconds when not playing."""

        # Check if any guild is actively playing.
        for guild in self.bot.guilds:
            voice: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
            if voice and voice.is_connected() and (voice.is_playing() or voice.is_paused()):
                return  # Something is playing; the song presence is active.

        # Nothing playing anywhere — rotate idle status.
        PlaybackCog._idle_status_index = (self._idle_status_index + 1) % len(self._IDLE_STATUSES)
        idle_name = self._IDLE_STATUSES[self._idle_status_index]
        activity = discord.Activity(type=discord.ActivityType.playing, name=idle_name)
        await self.bot.change_presence(activity=activity)


    @_rotate_idle_presence.before_loop
    async def _before_rotate_idle(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Auto-leave when the bot is the only member left in a voice channel."""

        # We only care about users leaving a channel (not the bot itself).
        if member.bot:
            return

        # A user left or moved away from a channel.
        if before.channel is None:
            return

        guild = member.guild
        voice: Optional[discord.VoiceClient] = guild.voice_client  # type: ignore[assignment]
        if not voice or not voice.is_connected():
            return

        # Check if the channel the user left is the one the bot is in.
        if voice.channel != before.channel:
            return

        # Count non-bot members remaining.
        human_members = [m for m in voice.channel.members if not m.bot]
        if len(human_members) == 0:
            await self._auto_disconnect_guild(guild, reason="everyone left the voice channel")

    @commands.command(name="join")
    async def join_voice(self, ctx: commands.Context):
        """Join the voice channel the command author is in."""

        voice = await helpers.ensure_voice_connected(ctx.guild, ctx.author)
        if not voice:
            await ctx.send("You need to be in a voice channel first.")
            return

        await ctx.send(f"Joined voice channel: {voice.channel.name}")


    @commands.command(name="leave")
    async def leave_voice(self, ctx: commands.Context):
        """Disconnect from the current voice channel."""

        voice: Optional[discord.VoiceClient] = ctx.voice_client
        disconnected = await helpers.leave_voice_channel(
            ctx.guild, voice, delete_np_callback=self._delete_now_playing_message_after_delay,
        )
        if not disconnected:
            await ctx.send("I'm not connected to a voice channel.")
            return

        await helpers.send_temporary(ctx, "Disconnected from voice channel.")


    @commands.command(name="radio")
    async def start_radio(self, ctx: commands.Context):
        """Start radio mode: continuously play random songs until stopped."""

        if not ctx.guild:
            await ctx.send("Radio mode can only be used in a guild.")
            return

        state.guild_radio_enabled[ctx.guild.id] = True
        await helpers.send_temporary(ctx, "Radio mode enabled. Playing random songs until you run `!jw stop`.")

        # If something is already playing, let it finish and the after-callback
        # (if any) will continue the radio. Otherwise, start immediately.
        voice: Optional[discord.VoiceClient] = ctx.voice_client
        if not voice or not voice.is_playing():
            await self._play_random_song_in_guild(ctx)


    @commands.command(name="stop")
    async def stop_radio(self, ctx: commands.Context):
        """Stop playback and disable radio mode for this guild."""

        if ctx.guild:
            state.guild_radio_enabled[ctx.guild.id] = False
            state.guild_radio_next.pop(ctx.guild.id, None)

        voice: Optional[discord.VoiceClient] = ctx.voice_client
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()

        await helpers.send_temporary(ctx, "Radio mode disabled and playback stopped.", delay=5)

        # Keep a static player message but show it as idle when radio stops.
        if ctx.guild:
            await self._send_player_controls(
                ctx,
                title="Nothing playing",
                path=None,
                is_radio=False,
                metadata={},
                duration_seconds=None,
            )

    @commands.command(name="play")
    async def play_song(self, ctx: commands.Context, song_id: str):
        """Play a Juice WRLD song in the caller's voice channel by song ID."""
        await self._play_song_impl(ctx, song_id, disable_radio=True)

    async def queue_song(self, ctx: commands.Context, song_id: str):
        """Queue a song without disabling radio mode (used by search Queue buttons)."""
        await self._play_song_impl(ctx, song_id, disable_radio=False)

    async def _play_song_impl(self, ctx: commands.Context, song_id: str, *, disable_radio: bool = True):
        """Core implementation for play/queue a song by ID.

        The song ID must be numeric; if it isn't, we show a helpful
        error message instead of raising a conversion error.
        """

        # Ensure the user is in a voice channel
        if not ctx.author.voice or not ctx.author.voice.channel:
            await helpers.send_temporary(ctx, "You need to be in a voice channel to play music.", delay=5)
            return

        # If radio is currently on, disable it; the requested song will
        # either play next (if something else is already playing) or
        # immediately if nothing is playing.
        if disable_radio:
            radio_was_on = self._disable_radio_if_active(ctx)
            if radio_was_on:
                await helpers.send_temporary(ctx, "Radio mode disabled because you requested a specific song.")

        # Support a short debug suffix: e.g. "123d" will enable debug mode
        # and use song ID 123. This keeps the command compact.
        debug = False
        raw_song_id = song_id.strip()
        if raw_song_id.lower().endswith("d"):
            debug = True
            raw_song_id = raw_song_id[:-1].strip()

        try:
            song_id_int = int(raw_song_id)
        except ValueError:
            await helpers.send_temporary(ctx, "Song ID must be a number. Example: `!jw play 123`.", delay=5)
            return

        voice = await helpers.ensure_voice_connected(ctx.guild, ctx.author)

        # First attempt: use the player endpoint helper to resolve a concrete
        # file path / stream URL for this song ID.
        async with ctx.typing():
            api = helpers.get_api()
            player_result = await api.play_juicewrld_song(song_id_int)

        status = player_result.get("status")
        error_detail = player_result.get("error")

        stream_url: Optional[str] = None
        file_path: Optional[str] = player_result.get("file_path")
        path_for_meta: Optional[str] = file_path

        # Decide whether we should fall back to a comp-style resolution path.
        fallback_needed = False
        if status == "not_found":
            # Song is not in the player endpoint – we will try to resolve it via
            # the main song catalog + comp browser.
            fallback_needed = True
        elif status and status not in {"success", "file_not_found_but_url_provided"}:
            # API-level error from the player helper, prefer comp-style fallback.
            fallback_needed = True

        # If the player endpoint claims success or a soft file-not-found, try to
        # validate/stream its file path first.
        if not fallback_needed and file_path:
            async with ctx.typing():
                stream_result = await api.stream_audio_file(file_path)

            stream_status = stream_result.get("status")
            stream_error = stream_result.get("error")

            if stream_status == "success":
                stream_url = stream_result.get("stream_url")
                if not stream_url:
                    # Missing URL from a "success" response – treat as fallback.
                    fallback_needed = True
                else:
                    path_for_meta = file_path
            else:
                # The derived comp path did not actually stream; fall back.
                fallback_needed = True

        elif not fallback_needed and not file_path:
            # No file path from player endpoint; try its direct stream_url.
            direct_url = player_result.get("stream_url")
            if direct_url:
                stream_url = direct_url
                path_for_meta = file_path
            else:
                fallback_needed = True

        # Second attempt: comp-style fallback using the main song catalog and
        # file browser (similar to !jw comp / _play_from_browse).
        if fallback_needed or not stream_url:
            async with ctx.typing():
                api = helpers.get_api()
                try:
                    song_obj = await api.get_song(song_id_int)
                except NotFoundError:
                    await helpers.send_temporary(
                        ctx,
                        f"No song found with ID `{song_id_int}` in the main catalog.",
                        delay=5,
                    )
                    return
                except JuiceWRLDAPIError as e:
                    await helpers.send_temporary(
                        ctx,
                        f"Error while fetching song `{song_id_int}` from catalog: {e}",
                        delay=5,
                    )
                    return

                # Prefer an explicit comp path from the song object if present.
                comp_path = getattr(song_obj, "path", "") or None

                if comp_path:
                    file_path = comp_path
                    stream_result = await api.stream_audio_file(file_path)
                else:
                    # No direct path on the song; search the comp browser by
                    # song title under the Compilation tree.
                    search_title = getattr(song_obj, "name", str(song_id_int))
                    directory = await api.browse_files(path="Compilation", search=search_title)
                    files = [
                        item
                        for item in getattr(directory, "items", [])
                        if getattr(item, "type", "file") == "file"
                    ]
                    if not files:
                        await helpers.send_temporary(
                            ctx,
                            f"Could not locate an audio file for song `{song_id_int}` "
                            "via the comp browser.",
                            delay=5,
                        )
                        return

                    target = files[0]
                    file_path = getattr(target, "path", None)
                    if not file_path:
                        await helpers.send_temporary(
                            ctx,
                            "Found a matching comp item but it has no valid file path.",
                            delay=5,
                        )
                        return

                    stream_result = await api.stream_audio_file(file_path)

                stream_status = stream_result.get("status")
                stream_error = stream_result.get("error")

                if stream_status != "success":
                    await helpers.handle_stream_error(
                        ctx,
                        status=stream_status,
                        error_detail=stream_error,
                        subject=f"song `{song_id_int}`",
                    )
                    return

                stream_url = stream_result.get("stream_url")
                if not stream_url:
                    await helpers.send_temporary(
                        ctx,
                        f"API did not return a stream URL for song `{song_id_int}` (path `{file_path}`).",
                        delay=5,
                    )
                    return

                path_for_meta = file_path
                catalog_song_obj = song_obj
        else:
            # We already have a usable stream_url from the player endpoint path
            # or its direct URL. We'll still fetch catalog metadata below.
            catalog_song_obj = None

        if not voice:
            await helpers.send_temporary(ctx, "Internal error: voice client not available.", delay=5)
            return

        # Optional short debug output when the user used an ID like "123d".
        if debug:
            debug_lines = [
                f"Debug: song_id={song_id_int}",
                f"Debug: file_path={file_path or 'N/A'}",
                f"Debug: stream_url={stream_url}",
            ]
            await helpers.send_temporary(ctx, "\n".join(debug_lines), delay=15)

        # Fetch full song metadata for richer Now Playing display.
        song_meta: Dict[str, Any] = {}
        duration_seconds: Optional[int] = None
        try:
            # Reuse the catalog song we fetched during fallback if available;
            # otherwise look it up now.
            if catalog_song_obj is None:
                catalog_song_obj = await helpers.get_api().get_song(song_id_int)

            song_obj = catalog_song_obj

            # Normalize image URL like radio: relative paths ("/assets/...")
            # should become absolute URLs against JUICEWRLD_API_BASE_URL.
            image_url = helpers.normalize_image_url(song_obj.image_url)

            # Build metadata that mirrors the canonical Song JSON model.
            song_meta = helpers.build_song_metadata_from_song(
                song_obj,
                path=path_for_meta,
                image_url=image_url,
            )
            duration_seconds = helpers.parse_length_to_seconds(song_obj.length)
        except Exception:
            # If metadata lookup fails, continue with minimal info.
            song_meta = {"id": song_id_int}

        # Delegate to the shared queue/play helper so this song either queues
        # after the current track or starts immediately.
        await self._queue_or_play_now(
            ctx,
            stream_url=stream_url,
            title=song_meta.get("name") or f"Song ID {song_id_int}",
            path=path_for_meta,
            metadata=song_meta,
            duration_seconds=duration_seconds,
        )


    @commands.command(name="playfile")
    async def play_file(self, ctx: commands.Context, *, file_path: str):
        """Play an audio file by its internal comp file path.

        This bypasses song IDs and uses the raw file path on the API side.
        """

        # Ensure the user is in a voice channel
        if not ctx.author.voice or not ctx.author.voice.channel:
            await helpers.send_temporary(ctx, "You need to be in a voice channel to play music.", delay=5)
            return

        # (radio is already disabled in _play_from_browse for search/comp
        # commands, so we don't toggle it here again.)

        voice = await helpers.ensure_voice_connected(ctx.guild, ctx.author)

        async with ctx.typing():
            result = await helpers.get_api().stream_audio_file(file_path)

        status = result.get("status")
        error_detail = result.get("error")

        if status != "success":
            await helpers.handle_stream_error(
                ctx,
                status=status,
                error_detail=error_detail,
                subject=f"file `{file_path}`",
            )
            return

        stream_url = result.get("stream_url")
        if not stream_url:
            await helpers.send_temporary(
                ctx,
                f"API did not return a stream URL for file `{file_path}`.",
                delay=5,
            )
            return

        if not voice:
            await helpers.send_temporary(ctx, "Internal error: voice client not available.", delay=5)
            return

        await self._queue_or_play_now(
            ctx,
            stream_url=stream_url,
            title=f"File {file_path}",
            path=file_path,
            metadata={"path": file_path},
            duration_seconds=None,
        )


    @commands.command(name="playsearch")
    async def play_search(self, ctx: commands.Context, *, query: str):
        """Search all comp files by name and play the best match."""

        await self._play_from_browse(ctx, query=query, base_path="", scope_description="the comp browser")


    @commands.command(name="stusesh")
    async def play_studio_session(self, ctx: commands.Context, *, query: str):
        """Search Studio Sessions only and play the best match."""

        await self._play_from_browse(
            ctx,
            query=query,
            base_path="Studio Sessions",
            scope_description="Studio Sessions",
        )


    @commands.command(name="og")
    async def play_original_file(self, ctx: commands.Context, *, query: str):
        """Search Original Files only and play the best match."""

        await self._play_from_browse(
            ctx,
            query=query,
            base_path="Original Files",
            scope_description="Original Files",
        )


    @commands.command(name="seshedits")
    async def play_session_edit(self, ctx: commands.Context, *, query: str):
        """Search Session Edits only and play the best match."""

        await self._play_from_browse(
            ctx,
            query=query,
            base_path="Session Edits",
            scope_description="Session Edits",
        )


    @commands.command(name="stems")
    async def play_stem_edit(self, ctx: commands.Context, *, query: str):
        """Search Stem Edits only and play the best match."""

        await self._play_from_browse(
            ctx,
            query=query,
            base_path="Stem Edits",
            scope_description="Stem Edits",
        )


    @commands.command(name="comp")
    async def play_compilation(self, ctx: commands.Context, *, query: str):
        """Search Compilation (released/unreleased/misc) and play the best match."""

        await self._play_from_browse(
            ctx,
            query=query,
            base_path="Compilation",
            scope_description="Compilation (released/unreleased/misc)",
        )


    async def _play_from_browse(
        self,
        ctx: commands.Context,
        *,
        query: str,
        base_path: str,
        scope_description: str,
    ) -> None:
        """Shared helper to search the file browser and play the first match.

        Args:
            ctx: Discord context.
            query: Search term for the file name.
            base_path: Directory to search inside ("" for root/all).
            scope_description: Human-readable description of what we're searching.
        """

        # Ensure the user is in a voice channel
        if not ctx.author.voice or not ctx.author.voice.channel:
            await helpers.send_temporary(ctx, "You need to be in a voice channel to play music.", delay=5)
            return

        # Any search-style playback should disable radio and then either play or
        # queue the requested track.
        radio_was_on = self._disable_radio_if_active(ctx)
        if radio_was_on:
            await helpers.send_temporary(
                ctx,
                "Radio mode disabled because you used a search/comp playback command.",
            )

        voice = await helpers.ensure_voice_connected(ctx.guild, ctx.author)

        async with ctx.typing():
            api = helpers.get_api()
            directory = await api.browse_files(path=base_path, search=query)
            files = [item for item in directory.items if getattr(item, "type", "file") == "file"]
            if not files:
                await helpers.send_temporary(
                    ctx,
                    f"No files found matching `{query}` in {scope_description}.",
                    delay=5,
                )
                return

            target = files[0]
            file_path = getattr(target, "path", None)
            if not file_path:
                await helpers.send_temporary(
                    ctx,
                    "Found a matching item but it does not have a valid file path.",
                    delay=5,
                )
                return

            result = await api.stream_audio_file(file_path)

        status = result.get("status")
        error_detail = result.get("error")

        if status != "success":
            await helpers.handle_stream_error(
                ctx,
                status=status,
                error_detail=error_detail,
                subject=f"search `{query}` (path `{file_path}`)",
            )
            return

        stream_url = result.get("stream_url")
        if not stream_url:
            await helpers.send_temporary(
                ctx,
                f"API did not return a stream URL for search `{query}` (resolved path `{file_path}`).",
                delay=5,
            )
            return

        if not voice:
            await helpers.send_temporary(ctx, "Internal error: voice client not available.", delay=5)
            return

        display_name = getattr(target, "name", file_path)

        # Try to enrich with song metadata by searching the catalog by name.
        # Strip extension like ".mp3" so "Fresh Air.mp3" -> "Fresh Air" before
        # searching the songs endpoint.
        base_title, _ext = os.path.splitext(display_name)

        song_meta: Dict[str, Any] = {"length": None}
        duration_seconds: Optional[int] = None
        try:
            search_data = await helpers.get_api().get_songs(search=base_title, page=1, page_size=1)
            results = search_data.get("results") or []
            if results:
                song_obj = results[0]
                image_url = helpers.normalize_image_url(song_obj.image_url)

                # Build metadata mirroring the canonical Song model.
                song_meta = helpers.build_song_metadata_from_song(
                    song_obj,
                    path=file_path,
                    image_url=image_url,
                )
                duration_seconds = helpers.parse_length_to_seconds(song_obj.length)
            else:
                # No song match; at least carry the path so the UI can show it.
                song_meta = {"path": file_path}
        except Exception:
            song_meta = {"path": file_path}

        await self._queue_or_play_now(
            ctx,
            stream_url=stream_url,
            title=display_name,
            path=file_path,
            metadata=song_meta,
            duration_seconds=duration_seconds,
        )


    async def _fetch_random_radio_song(self, include_stream_url: bool = True) -> Optional[Dict[str, Any]]:
        """Fetch a random radio song and return its data (title, stream_url, metadata, duration).
    
        Args:
            include_stream_url: If True, fetch and include stream_url. If False, only fetch metadata
                               (useful for pre-fetching to avoid stale URLs).
    
        Returns None if fetching fails.
        """
        api = helpers.get_api()
        try:
            # 1) Get a random radio song with metadata from /radio/random/.
            radio_data = await api.get_random_radio_song()
            chosen_title = str(radio_data.get("title") or "Unknown")

            # According to docs, `path` and `id` are both the comp file path.
            file_path = (
                radio_data.get("path")
                or radio_data.get("id")
            )
            if not file_path:
                return None

            song_info = radio_data.get("song") or {}
            song_id = song_info.get("id")

            # Prefer the canonical song name from embedded song metadata.
            if song_info.get("name"):
                chosen_title = str(song_info.get("name"))

            stream_url = None
            if include_stream_url:
                # 2) Use the comp streaming helper to validate and build a stream URL.
                stream_result = await api.stream_audio_file(file_path)
                status = stream_result.get("status")
                if status != "success":
                    return None

                stream_url = stream_result.get("stream_url")
                if not stream_url:
                    return None

            # 3) Build song metadata for artwork, duration, etc.
            duration_seconds: Optional[int] = None
            if song_info:
                length = song_info.get("length") or ""
                duration_seconds = helpers.parse_length_to_seconds(length)

                song_meta = dict(song_info)
                song_meta["path"] = file_path
                song_meta["image_url"] = helpers.normalize_image_url(song_meta.get("image_url"))
            else:
                song_meta = {"id": song_id, "path": file_path}

            return {
                "title": chosen_title,
                "stream_url": stream_url,
                "metadata": song_meta,
                "duration_seconds": duration_seconds,
                "path": file_path,
            }
        except Exception as e:
            print(f"Radio: failed to fetch random song: {e}", file=sys.stderr)
            return None


    async def _get_fresh_stream_url(self, file_path: str) -> Optional[str]:
        """Get a fresh stream URL for a file path."""
        try:
            stream_result = await helpers.get_api().stream_audio_file(file_path)
            if stream_result.get("status") == "success":
                return stream_result.get("stream_url")
            return None
        except Exception as e:
            print(f"Radio: failed to get stream URL for {file_path}: {e}", file=sys.stderr)
            return None


    async def _prefetch_next_radio_song(self, guild_id: int) -> None:
        """Pre-fetch the next random radio song for a guild and store it.
    
        Only fetches metadata (not stream URL) to avoid stale URLs when the song is played later.
        """
        song_data = await self._fetch_random_radio_song(include_stream_url=False)
        if song_data:
            state.guild_radio_next[guild_id] = song_data


    async def _play_random_song_in_guild(self, ctx: commands.Context) -> None:
        """Pick a random song from the radio endpoint and play it.

        Uses `/juicewrld/radio/random/` plus the player endpoint to get a
        streaming URL and rich metadata. If radio is disabled for the guild,
        this is a no-op.
    
        If a pre-fetched song exists in state.guild_radio_next, it will be used
        instead of fetching a new random song.
        """

        if not ctx.guild or not state.guild_radio_enabled.get(ctx.guild.id):
            return

        guild_id = ctx.guild.id

        # Ensure the user is in a voice channel
        if not ctx.author.voice or not ctx.author.voice.channel:
            await helpers.send_temporary(ctx, "You need to be in a voice channel to use radio.", delay=5)
            return

        voice = await helpers.ensure_voice_connected(ctx.guild, ctx.author)

        # Check for pre-fetched song first
        prefetched = state.guild_radio_next.pop(guild_id, None)
    
        if prefetched:
            # Use the pre-fetched song metadata but get a FRESH stream URL
            chosen_title = prefetched.get("title", "Unknown")
            song_meta = prefetched.get("metadata", {})
            duration_seconds = prefetched.get("duration_seconds")
            file_path = prefetched.get("path")
        
            # Get fresh stream URL to avoid stale/expired URLs
            if file_path:
                stream_url = await self._get_fresh_stream_url(file_path)
            else:
                stream_url = None
        else:
            # Fetch a new random song (with stream URL)
            async with ctx.typing():
                song_data = await self._fetch_random_radio_song(include_stream_url=True)
        
            if not song_data:
                await helpers.send_temporary(
                    ctx,
                    "Radio: could not fetch a random song.",
                    delay=5,
                )
                return
        
            stream_url = song_data.get("stream_url")
            chosen_title = song_data.get("title", "Unknown")
            song_meta = song_data.get("metadata", {})
            duration_seconds = song_data.get("duration_seconds")

        if not stream_url:
            await helpers.send_temporary(
                ctx,
                "Radio: no stream URL available.",
                delay=5,
            )
            # Try again with a new song after a short delay
            await asyncio.sleep(1)
            if state.guild_radio_enabled.get(guild_id):
                asyncio.create_task(self._play_random_song_in_guild(ctx))
            return

        if not ctx.guild or not state.guild_radio_enabled.get(ctx.guild.id):
            return

        if not voice:
            await helpers.send_temporary(ctx, "Internal error: voice client not available.", delay=5)
            return

        if voice.is_playing() or voice.is_paused():
            # Something is already playing; don't interrupt it.
            # The after-callback will detect that radio is enabled and
            # call _play_random_song_in_guild once the current track ends.
            return

        try:
            source = discord.FFmpegPCMAudio(
                stream_url,
                before_options=_FFMPEG_BEFORE,
                options=_FFMPEG_OPTIONS,
            )
        except Exception as e:  # pragma: no cover
            await helpers.send_temporary(
                ctx,
                f"Radio: failed to create audio source: {e}",
                delay=5,
            )
            return

        # After callback to continue radio or, if radio was turned off while a
        # track was playing, fall back to the normal queue.
        def _after_playback(error: Optional[Exception]) -> None:
            if error:
                # Log error to stderr; Discord callbacks can't await
                print(f"Radio playback error: {error}", file=sys.stderr)

            if not ctx.guild:
                return

            guild_id = ctx.guild.id

            if state.guild_radio_enabled.get(guild_id):
                # Add a small delay on error to prevent rapid looping through songs
                async def _continue_radio():
                    if error:
                        await asyncio.sleep(2)  # Wait before retrying on error
                    await self._play_random_song_in_guild(ctx)
            
                fut = _continue_radio()
                asyncio.run_coroutine_threadsafe(fut, self.bot.loop)
                return

            # Radio is off; if there is anything queued, continue with the
            # regular queue playback.
            queue = state.guild_queue.get(guild_id) or []
            if queue:
                fut = self._play_next_from_queue(ctx)
                asyncio.run_coroutine_threadsafe(fut, self.bot.loop)

        voice.play(source, after=_after_playback)

        # Pre-fetch the next radio song BEFORE showing controls so "Up Next" is populated
        await self._prefetch_next_radio_song(guild_id)

        radio_meta = song_meta.copy()
        radio_meta["source"] = "radio"

        await self._send_player_controls(
            ctx,
            title=chosen_title,
            path=song_meta.get("path"),
            is_radio=True,
            metadata=radio_meta,
            duration_seconds=duration_seconds,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlaybackCog(bot))
