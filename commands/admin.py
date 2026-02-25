"""Admin command Cog for the Juice WRLD Discord bot."""

import asyncio
import base64
import datetime
import io
import sys
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from constants import (
    BOT_VERSION,
    BOT_BUILD_DATE,
    DISCORD_TOKEN,
)
from exceptions import JuiceWRLDAPIError
import helpers
import state
from views.sotd import SongOfTheDayView


class AdminCog(commands.Cog):
    """Admin, utility, and SOTD commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._song_of_the_day_task.start()

        # Context menus must be registered manually in Cogs.
        self._ctx_menu_stats = app_commands.ContextMenu(
            name="View Listening Stats",
            callback=self.context_view_stats,
        )
        self._ctx_menu_play = app_commands.ContextMenu(
            name="Play This Song",
            callback=self.context_play_from_message,
        )
        self.bot.tree.add_command(self._ctx_menu_stats)
        self.bot.tree.add_command(self._ctx_menu_play)

    def cog_unload(self) -> None:
        self._song_of_the_day_task.cancel()
        self.bot.tree.remove_command(self._ctx_menu_stats.name, type=self._ctx_menu_stats.type)
        self.bot.tree.remove_command(self._ctx_menu_play.name, type=self._ctx_menu_play.type)

    @property
    def _playback(self):
        """Lazy reference to PlaybackCog for cross-cog calls."""
        return self.bot.get_cog("PlaybackCog")

    @tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=datetime.timezone.utc))
    async def _song_of_the_day_task(self) -> None:
        """Post a random Song of the Day to configured channels at noon UTC daily."""

        if not state.sotd_config:
            print("[sotd] No channels configured — skipping.")
            return

        playback = self._playback
        if not playback:
            print("[sotd] PlaybackCog not loaded — skipping.", file=sys.stderr)
            return

        song_data = await playback._fetch_random_radio_song(include_stream_url=False)
        if not song_data:
            print("[sotd] Failed to fetch a random song.", file=sys.stderr)
            return

        print(f"[sotd] Posting Song of the Day: {song_data.get('title', '?')}")

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

        view = SongOfTheDayView(song_data=song_data, queue_fn=playback._queue_or_play_now, stream_fn=playback._get_fresh_stream_url)

        for guild_id_str, channel_id in list(state.sotd_config.items()):
            guild_obj = self.bot.get_guild(int(guild_id_str))
            if not guild_obj:
                continue
            chan = guild_obj.get_channel(channel_id)
            if not isinstance(chan, discord.TextChannel):
                continue
            try:
                # Try webhook-based posting for a custom "Juice WRLD Radio" identity.
                webhook = await self._get_or_create_sotd_webhook(chan)
                if webhook:
                    await webhook.send(
                        embed=embed,
                        view=view,
                        username="Juice WRLD Radio",
                        avatar_url=image_url if image_url else None,
                    )
                    print(f"[sotd] Posted via webhook to #{chan.name} in {guild_obj.name}")
                else:
                    await chan.send(embed=embed, view=view)
                    print(f"[sotd] Posted to #{chan.name} in {guild_obj.name}")
            except Exception as exc:
                # Fall back to normal bot message on any failure.
                print(f"[sotd] Webhook failed for #{chan.name}: {exc}", file=sys.stderr)
                try:
                    await chan.send(embed=embed, view=view)
                    print(f"[sotd] Fallback post to #{chan.name} in {guild_obj.name}")
                except Exception as exc2:
                    print(f"[sotd] Failed to post to #{chan.name}: {exc2}", file=sys.stderr)
                    continue


    async def _get_or_create_sotd_webhook(self, channel: discord.TextChannel) -> Optional[discord.Webhook]:
        """Get or create a webhook in the channel for SOTD posts."""
        try:
            webhooks = await channel.webhooks()
            for wh in webhooks:
                if wh.name == "JuiceWRLD-SOTD" and wh.user == self.bot.user:
                    return wh
            return await channel.create_webhook(name="JuiceWRLD-SOTD")
        except Exception:
            return None


    @_song_of_the_day_task.before_loop
    async def _before_sotd(self) -> None:
        await self.bot.wait_until_ready()


    @commands.command(name="help")
    async def help_command(self, ctx: commands.Context):
        """Show help for the bot commands in a clean, organized embed."""

        embed = discord.Embed(title="Juice WRLD Bot Help", colour=discord.Colour.purple())

        core_lines = [
            "`!jw play <song_id>` — Play a song by ID in your voice channel.",
            "`!jw search <query>` — Search songs with interactive Play/Playlist/Info buttons.",
            "`!jw song <song_id>` — View song details with Play/Playlist/Info buttons.",
            "`!jw radio` — Start radio mode (random songs until `!jw stop`).",
            "`!jw stop` — Stop playback and turn off radio mode.",
            "`!jw join` — Make the bot join your voice channel.",
            "`!jw leave` — Disconnect the bot from voice.",
            "`!jw ping` — Check if the bot is alive.",
        ]
        embed.add_field(name="Core Commands", value="\n".join(core_lines), inline=False)

        comp_lines = [
            "`!jw comp <name>` — Search Compilation and play the best match.",
            "`!jw stusesh <name>` — Search Studio Sessions only.",
            "`!jw og <name>` — Search Original Files only.",
            "`!jw seshedits <name>` — Search Session Edits only.",
            "`!jw stems <name>` — Search Stem Edits only.",
            "`!jw playsearch <name>` — Search all comp files.",
            "`!jw playfile <path>` — Play directly from a comp file path.",
        ]
        embed.add_field(name="Comp Playback", value="\n".join(comp_lines), inline=False)

        playlist_lines = [
            "`!jw pl` — List your playlists (also `!jw playlist` or `!jw playlists`).",
            "`!jw pl show <name>` — Show full contents of a playlist.",
            "`!jw pl play <name>` — Queue/play all tracks in a playlist.",
            "`!jw pl add <name> <song_id>` — Add a song (by ID) to a playlist.",
            "`!jw pl create <name>` — Create a new empty playlist.",
            "`!jw pl delete <name>` — Delete a playlist.",
            "`!jw pl rename <old> <new>` — Rename a playlist.",
            "`!jw pl remove <name> <index>` — Remove a track by index.",
            "`!jw pl share <name>` — Share a playlist publicly in the channel.",
            "`!jw pl import @user <name>` — Copy another user's playlist.",
        ]
        embed.add_field(name="Playlists", value="\n".join(playlist_lines), inline=False)

        browse_lines = [
            "`!jw eras` — List all Juice WRLD musical eras.",
            "`!jw era <name>` — Browse songs from a specific era.",
            "`!jw similar` — Find songs similar to the currently playing track.",
            "`!jw stats` — View your personal listening stats.",
            "`!jw history` — Show the last 10 songs played in this server.",
        ]
        embed.add_field(name="Browse & Discover", value="\n".join(browse_lines), inline=False)

        admin_lines = [
            "`!jw sotd #channel` — Set the Song of the Day channel.",
            "`!jw emoji list|upload|delete` — Manage application emojis.",
            "`!jw sync` — Force-sync slash commands to Discord.",
            "`!jw ver` — Show bot version and recent updates.",
        ]
        embed.add_field(name="Admin", value="\n".join(admin_lines), inline=False)

        slash_lines = [
            "All commands are also available as `/jw <command>`.",
            "Type `/jw` in chat to see the full slash command list.",
        ]
        embed.add_field(name="Slash Commands", value="\n".join(slash_lines), inline=False)

        embed.set_footer(text="Prefix: !jw  •  Aliases: !jw pl = !jw playlist = !jw playlists")

        await ctx.send(embed=embed)


    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context):
        """Simple health check.

        Sends a temporary "Pong!" message that auto-deletes after a few seconds.
        """

        await helpers.send_temporary(ctx, "Pong!", delay=5)


    @commands.command(name="sync")
    @commands.has_permissions(administrator=True)
    async def sync_commands(self, ctx: commands.Context):
        """Manually sync slash commands to Discord (admin only).
    
        This forces Discord to immediately register/update all slash commands.
        Use this after making changes to slash commands in the code.
        """
    
        msg = await ctx.send("Syncing slash commands...")
    
        try:
            # Sync to this guild first (instant)
            if ctx.guild:
                await self.bot.tree.sync(guild=ctx.guild)
                await msg.edit(content=f"✅ Synced slash commands to **{ctx.guild.name}**!\n\n"
                                       f"The `/jw` commands should now be available in this server.\n"
                                       f"Syncing globally (may take up to 1 hour)...")

            # Sync globally (takes up to 1 hour to propagate)
            await self.bot.tree.sync()

            await msg.edit(content=f"✅ Successfully synced slash commands!\n\n"
                                   f"• **Guild sync**: Instant (commands available now in this server)\n"
                                   f"• **Global sync**: Started (may take up to 1 hour for other servers)\n\n"
                                   f"Try typing `/jw` to see the commands.")

            # Delete after 15 seconds
            asyncio.create_task(helpers.delete_later(msg, 15))

        except Exception as e:
            await msg.edit(content=f"❌ Error syncing commands: {e}")
            print(f"Sync error: {e}", file=sys.stderr)


    @commands.command(name="ver", aliases=["version"])
    async def version_command(self, ctx: commands.Context):
        """Show bot version information."""
    
        embed = discord.Embed(
            title="JuiceAPI Bot Version",
            description=f"**Version:** {BOT_VERSION}\n**Build Date:** {BOT_BUILD_DATE}",
            colour=discord.Colour.green(),
        )
        embed.add_field(
            name="Recent Updates (v3.2.0)",
            value=(
                "• 📜 History — `!jw history` / `/jw history` shows last 10 songs played\n"
                "• 📤 Playlist sharing — `!jw pl share` posts a playlist publicly for others\n"
                "• 📥 Playlist import — `!jw pl import @user <name>` copies another user's playlist\n"
                "• 📥 Queue button — search results now have a Queue button (doesn't interrupt radio)\n"
                "• ⚡ Play/queue refactor — cleaner internal play pipeline"
            ),
            inline=False,
        )
        embed.set_footer(text="Use !jw help for all commands")
    
        await helpers.send_temporary(ctx, embed=embed, delay=15)


    @commands.command(name="sotd")
    @commands.has_permissions(administrator=True)
    async def setup_sotd(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set (or update) the Song of the Day channel for this server (admin only)."""

        if not ctx.guild:
            await helpers.send_temporary(ctx, "This command can only be used in a server.")
            return

        state.sotd_config[str(ctx.guild.id)] = channel.id
        state.save_sotd_config()
        await helpers.send_temporary(ctx, f"Song of the Day will be posted daily in {channel.mention}.")


    async def context_view_stats(self, interaction: discord.Interaction, user: discord.Member) -> None:
        """Right-click a user to view their listening stats."""
        embed = helpers.build_stats_embed(user)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        helpers.schedule_interaction_deletion(interaction, 30)


    async def context_play_from_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Right-click a message (e.g. Now Playing / SOTD embed) to play that song."""

        user = interaction.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await interaction.response.send_message(
                "You need to be in a voice channel to play music.", ephemeral=True
            )
            return

        # Try to extract a song title from the message's embeds.
        song_title: Optional[str] = None
        for emb in message.embeds:
            # SOTD embed: title is "Song of the Day", song name is in description bold text.
            if emb.title and "Song of the Day" in emb.title and emb.description:
                # Description is like "**Song Name**"
                song_title = emb.description.strip("* ")
                break
            # Now Playing embed: title is "Now Playing", description is the song name.
            if emb.title == "Now Playing" and emb.description:
                song_title = emb.description.strip()
                break
            # Search result embed: description starts with **Name**
            if emb.description and emb.description.startswith("**"):
                # Extract the bold text: **Name** (ID: ...)
                bold_end = emb.description.find("**", 2)
                if bold_end > 2:
                    song_title = emb.description[2:bold_end]
                    break

        if not song_title:
            await interaction.response.send_message(
                "Could not find a song in this message.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Search the API for this song and try to play it.
        try:
            results = await helpers.get_api().get_songs(search=song_title, page=1, page_size=1)
        except Exception as e:
            await interaction.followup.send(f"Error searching: {e}", ephemeral=True)
            return

        songs = results.get("results") or []
        if not songs:
            await interaction.followup.send(
                f"No playable song found for `{song_title}`.", ephemeral=True
            )
            return

        song = songs[0]
        song_id = getattr(song, "id", None)
        if not song_id:
            await interaction.followup.send("Song has no ID.", ephemeral=True)
            return

        # Use the existing play logic via a Context.
        ctx = await commands.Context.from_interaction(interaction)
        playback = self._playback
        if not playback:
            await interaction.followup.send("Playback system is not available.", ephemeral=True)
            return
        await playback.play_song(ctx, str(song_id))
        await interaction.followup.send(f"Playing **{song_title}**.", ephemeral=True)


    # --- Application Emojis management (admin) ---


    @commands.command(name="emoji")
    @commands.has_permissions(administrator=True)
    async def emoji_command(self, ctx: commands.Context, action: str = "list", *, name: str = ""):
        """Manage application emojis.

        Usage:
            !jw emoji list             — List all app emojis
            !jw emoji upload <name>    — Upload an attached image as an app emoji
            !jw emoji delete <name>    — Delete an app emoji by name
        """
        app_id = self.bot.user.id if self.bot.user else None
        if not app_id:
            await ctx.send("Bot is not ready yet.")
            return

        action = action.lower()

        if action == "list":
            await self._emoji_list(ctx, app_id)
        elif action == "upload":
            await self._emoji_upload(ctx, app_id, name.strip())
        elif action == "delete":
            await self._emoji_delete(ctx, app_id, name.strip())
        else:
            await helpers.send_temporary(ctx, "Usage: `!jw emoji list`, `!jw emoji upload <name>`, `!jw emoji delete <name>`")


    async def _emoji_list(self, ctx: commands.Context, app_id: int) -> None:
        """List all application emojis."""
        url = f"https://discord.com/api/v10/applications/{app_id}/emojis"
        headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
        session = await helpers.get_discord_session()

        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                await ctx.send(f"Failed to fetch emojis: HTTP {resp.status}")
                return
            data = await resp.json()

        items = data.get("items", [])
        if not items:
            await ctx.send("No application emojis uploaded yet.")
            return

        lines = []
        for e in items:
            eid = e.get("id", "?")
            ename = e.get("name", "?")
            animated = e.get("animated", False)
            prefix = "a" if animated else ""
            lines.append(f"<{prefix}:{ename}:{eid}> `{ename}` (ID: {eid})")

        embed = discord.Embed(
            title=f"Application Emojis ({len(items)})",
            description="\n".join(lines),
            colour=discord.Colour.purple(),
        )
        await ctx.send(embed=embed)


    async def _emoji_upload(self, ctx: commands.Context, app_id: int, name: str) -> None:
        """Upload an attached image as an application emoji."""
        if not name:
            await helpers.send_temporary(ctx, "Provide a name: `!jw emoji upload my_emoji` (attach an image).")
            return

        if not ctx.message.attachments:
            await helpers.send_temporary(ctx, "Attach an image file to upload as an emoji.")
            return

        attachment = ctx.message.attachments[0]
        if not attachment.content_type or not attachment.content_type.startswith("image/"):
            await helpers.send_temporary(ctx, "The attachment must be an image (PNG, GIF, etc.).")
            return

        image_bytes = await attachment.read()
        if len(image_bytes) > 256 * 1024:
            await helpers.send_temporary(ctx, "Image must be under 256 KB.")
            return

        # Determine MIME type for data URI.
        mime = attachment.content_type or "image/png"
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_uri = f"data:{mime};base64,{b64}"

        url = f"https://discord.com/api/v10/applications/{app_id}/emojis"
        headers = {
            "Authorization": f"Bot {DISCORD_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {"name": name, "image": data_uri}

        session = await helpers.get_discord_session()
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status in (200, 201):
                result = await resp.json()
                eid = result.get("id", "?")
                await ctx.send(f"✅ Emoji `{name}` uploaded! Use it as `<:{name}:{eid}>`")
            else:
                body = await resp.text()
                await ctx.send(f"❌ Upload failed: HTTP {resp.status}\n```{body[:500]}```")


    async def _emoji_delete(self, ctx: commands.Context, app_id: int, name: str) -> None:
        """Delete an application emoji by name."""
        if not name:
            await helpers.send_temporary(ctx, "Provide the emoji name: `!jw emoji delete my_emoji`")
            return

        # First, find the emoji ID by listing all.
        list_url = f"https://discord.com/api/v10/applications/{app_id}/emojis"
        headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
        session = await helpers.get_discord_session()

        async with session.get(list_url, headers=headers) as resp:
            if resp.status != 200:
                await ctx.send(f"Failed to list emojis: HTTP {resp.status}")
                return
            data = await resp.json()

        items = data.get("items", [])
        target = None
        for e in items:
            if e.get("name", "").lower() == name.lower():
                target = e
                break

        if not target:
            await helpers.send_temporary(ctx, f"No emoji named `{name}` found.")
            return

        eid = target["id"]
        del_url = f"https://discord.com/api/v10/applications/{app_id}/emojis/{eid}"
        async with session.delete(del_url, headers=headers) as resp:
            if resp.status == 204:
                await ctx.send(f"✅ Emoji `{name}` deleted.")
            else:
                body = await resp.text()
                await ctx.send(f"❌ Delete failed: HTTP {resp.status}\n```{body[:500]}```")



async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
