"""Search & browse command Cog for the Juice WRLD Discord bot."""

from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from exceptions import JuiceWRLDAPIError, NotFoundError
import helpers
import state
from views.search import SearchPaginationView, SingleSongResultView


class SearchCog(commands.Cog):
    """Search, song details, eras, similar, and stats commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def _play_fn(self):
        """Return the PlaybackCog.play_song method for view callbacks."""
        return self.bot.get_cog("PlaybackCog").play_song

    @property
    def _queue_fn(self):
        """Return the PlaybackCog.queue_song method for view callbacks."""
        return self.bot.get_cog("PlaybackCog").queue_song

    @commands.command(name="eras")
    async def list_eras(self, ctx: commands.Context):
        """List all Juice WRLD musical eras."""

        async with ctx.typing():
            try:
                eras = await helpers.get_api().get_eras()
            except JuiceWRLDAPIError as e:
                await helpers.send_temporary(ctx, f"Error fetching eras: {e}")
                return

        if not eras:
            await helpers.send_temporary(ctx, "No eras found.")
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
        embed.set_footer(text="Use !jw era <name> to browse songs from an era.")
        await helpers.send_temporary(ctx, embed=embed, delay=30)


    @commands.command(name="era")
    async def browse_era(self, ctx: commands.Context, *, era_name: str):
        """Browse songs from a specific era."""

        async with ctx.typing():
            try:
                results = await helpers.get_api().get_songs(era=era_name, page=1, page_size=25)
            except JuiceWRLDAPIError as e:
                await helpers.send_temporary(ctx, f"Error fetching songs for era: {e}")
                return

        songs = results.get("results") or []
        if not songs:
            await helpers.send_temporary(ctx, f"No songs found for era `{era_name}`.")
            return

        total = results.get("count") if isinstance(results, dict) else None
        view = SearchPaginationView(ctx=ctx, songs=songs, query=f"Era: {era_name}", total_count=total, play_fn=self._play_fn, queue_fn=self._queue_fn)
        embed = view.build_embed()
        view.message = await ctx.send(embed=embed, view=view)


    @commands.command(name="similar")
    async def similar_songs(self, ctx: commands.Context):
        """Find songs similar to the currently playing track."""

        if not ctx.guild:
            await helpers.send_temporary(ctx, "This command can only be used in a server.")
            return

        async with ctx.typing():
            try:
                title, top = await helpers.find_similar_songs(ctx.guild.id)
            except JuiceWRLDAPIError as e:
                await helpers.send_temporary(ctx, f"Error finding similar songs: {e}")
                return

        if not title:
            await helpers.send_temporary(ctx, "Nothing is currently playing. Play a song first!")
            return

        if not top:
            await helpers.send_temporary(ctx, f"No similar songs found for **{title}**.")
            return

        view = SearchPaginationView(ctx=ctx, songs=top, query=f"Similar to: {title}", total_count=len(top), play_fn=self._play_fn, queue_fn=self._queue_fn)
        embed = view.build_embed()
        view.message = await ctx.send(embed=embed, view=view)


    @commands.command(name="stats")
    async def listening_stats(self, ctx: commands.Context):
        """Show the user's personal listening stats."""

        embed = helpers.build_stats_embed(ctx.author)
        await helpers.send_temporary(ctx, embed=embed, delay=30)


    @commands.command(name="search")
    async def search_songs(self, ctx: commands.Context, *, query: str):
        """Search for songs by text query and show paginated interactive results."""

        async with ctx.typing():
            try:
                results = await helpers.get_api().get_songs(search=query, page=1, page_size=25)
            except JuiceWRLDAPIError as e:
                await helpers.send_temporary(ctx, f"Error while searching songs: {e}")
                return

        songs = results.get("results") or []
        if not songs:
            await helpers.send_temporary(
                ctx,
                f"No songs found for `" + query + "`.",
                delay=10,
            )
            return

        total = results.get("count") if isinstance(results, dict) else None

        view = SearchPaginationView(ctx=ctx, songs=songs, query=query, total_count=total, play_fn=self._play_fn, queue_fn=self._queue_fn)
        embed = view.build_embed()
        view.message = await ctx.send(embed=embed, view=view)


    @commands.command(name="song")
    async def song_details(self, ctx: commands.Context, song_id: str):
        """Get detailed info for a single song by ID.

        Shows an interactive view with Play / Add to Playlist / Info
        buttons — the same experience as selecting a search result.
        """

        try:
            song_id_int = int(song_id)
        except ValueError:
            await ctx.send("Song ID must be a number. Example: `!jw song 123`.")
            return

        async with ctx.typing():
            try:
                song = await helpers.get_api().get_song(song_id_int)
            except NotFoundError:
                await ctx.send(f"No song found with ID `{song_id_int}`.")
                return
            except JuiceWRLDAPIError as e:
                await ctx.send(f"Error while fetching song: {e}")
                return

        view = SingleSongResultView(ctx=ctx, song=song, query=song_id, play_fn=self._play_fn, queue_fn=self._queue_fn)
        embed = view.build_embed()
        await ctx.send(embed=embed, view=view)


    @commands.command(name="history")
    async def play_history(self, ctx: commands.Context):
        """Show the last 10 songs played in this server."""

        if not ctx.guild:
            await helpers.send_temporary(ctx, "This command can only be used in a server.")
            return

        history = state.guild_history.get(ctx.guild.id, [])
        if not history:
            await helpers.send_temporary(ctx, "No songs have been played in this server yet.")
            return

        lines = []
        for i, entry in enumerate(history, 1):
            title = entry.get("title", "Unknown")
            meta = entry.get("metadata") or {}
            era_val = meta.get("era")
            era_text = ""
            if isinstance(era_val, dict) and era_val.get("name"):
                era_text = f" · {era_val['name']}"
            elif era_val:
                era_text = f" · {era_val}"
            lines.append(f"`{i}.` {title}{era_text}")

        embed = discord.Embed(
            title="Recently Played",
            description="\n".join(lines),
            colour=discord.Colour.purple(),
        )
        embed.set_footer(text=f"Last {len(history)} song(s) in this server")
        await helpers.send_temporary(ctx, embed=embed, delay=30)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SearchCog(bot))
