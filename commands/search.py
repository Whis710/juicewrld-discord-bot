"""Search & browse command Cog for the Juice WRLD Discord bot."""

from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from exceptions import JuiceWRLDAPIError, NotFoundError
import helpers
import state
from commands import core as _core
from views.search import SearchPaginationView, SingleSongResultView


class SearchCog(commands.Cog):
    """Search, song details, eras, similar, and stats commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="eras")
    async def list_eras(self, ctx: commands.Context):
        """List all Juice WRLD musical eras."""

        async with ctx.typing():
            try:
                eras = await _core.fetch_eras()
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
                results = await _core.fetch_era_songs(era_name)
            except JuiceWRLDAPIError as e:
                await helpers.send_temporary(ctx, f"Error fetching songs for era: {e}")
                return

        songs = results.get("results") or []
        if not songs:
            await helpers.send_temporary(ctx, f"No songs found for era `{era_name}`.")
            return

        total = results.get("count") if isinstance(results, dict) else None
        view = SearchPaginationView(ctx=ctx, songs=songs, query=f"Era: {era_name}", total_count=total)
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
                title, top = await _core.find_similar_songs(ctx.guild.id)
            except JuiceWRLDAPIError as e:
                await helpers.send_temporary(ctx, f"Error finding similar songs: {e}")
                return

        if not title:
            await helpers.send_temporary(ctx, "Nothing is currently playing. Play a song first!")
            return

        if not top:
            await helpers.send_temporary(ctx, f"No similar songs found for **{title}**.")
            return

        view = SearchPaginationView(ctx=ctx, songs=top, query=f"Similar to: {title}", total_count=len(top))
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
                results = await _core.search_songs_api(query)
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

        view = SearchPaginationView(ctx=ctx, songs=songs, query=query, total_count=total)
        embed = view.build_embed()
        view.message = await ctx.send(embed=embed, view=view)


    @commands.command(name="song")
    async def song_details(self, ctx: commands.Context, song_id: str):
        """Get detailed info for a single song by ID.

        The song ID must be numeric; if it isn't, we show a helpful
        error message instead of raising a conversion error.
        """

        try:
            song_id_int = int(song_id)
        except ValueError:
            await ctx.send("Song ID must be a number. Example: `!jw song 123`.")
            return

        async with ctx.typing():
            api = helpers.create_api_client()
            try:
                song = api.get_song(song_id_int)
            except NotFoundError:
                await ctx.send(f"No song found with ID `{song_id_int}`.")
                return
            except JuiceWRLDAPIError as e:
                await ctx.send(f"Error while fetching song: {e}")
                return
            finally:
                api.close()

        name = getattr(song, "name", getattr(song, "title", "Unknown"))
        category = getattr(song, "category", "?")
        length = getattr(song, "length", "?")
        era_name = getattr(getattr(song, "era", None), "name", "?")
        producers = getattr(song, "producers", None)

        desc_lines = [
            f"**{name}** (ID: `{song_id_int}`)",
            f"Category: `{category}`",
            f"Length: `{length}`",
            f"Era: `{era_name}`",
        ]

        if producers:
            desc_lines.append(f"Producers: {producers}")

        await ctx.send("\n".join(desc_lines))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SearchCog(bot))
