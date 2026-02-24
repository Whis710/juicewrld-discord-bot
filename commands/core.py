"""Single source of truth for command logic.

Every public function here is a **pure async helper** that performs the
real work for a bot command.  Both the prefix (``!jw``) and slash
(``/jw``) handlers call into these functions so there is exactly *one*
implementation to maintain.

Functions here must NOT reference the ``bot`` instance or any decorator.
They receive whatever context they need (guild, user, voice client …)
as explicit arguments.
"""

from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from constants import NOTHING_PLAYING
from exceptions import JuiceWRLDAPIError
from helpers import (
    create_api_client,
    build_stats_embed,
    build_playlists_embed_for_user,
    send_temporary,
)
import state


# ── Eras ─────────────────────────────────────────────────────────────

async def fetch_eras() -> List[Any]:
    """Fetch all musical eras from the API.  Returns a list of era objects."""
    api = create_api_client()
    try:
        return api.get_eras()
    finally:
        api.close()


async def fetch_era_songs(era_name: str, page: int = 1, page_size: int = 25) -> Dict[str, Any]:
    """Fetch songs for a given era.  Returns the raw results dict."""
    api = create_api_client()
    try:
        return api.get_songs(era=era_name, page=page, page_size=page_size)
    finally:
        api.close()


# ── Similar songs ────────────────────────────────────────────────────

def score_similarity(
    song: Any,
    *,
    era_name: Optional[str],
    producers_str: str,
    category: str,
) -> int:
    """Score a candidate song's similarity to a reference track."""
    sc = 0
    s_era = getattr(getattr(song, "era", None), "name", "")
    if era_name and s_era == era_name:
        sc += 2
    s_prod = getattr(song, "producers", "") or ""
    if producers_str and s_prod and any(
        p.strip() in s_prod for p in producers_str.split(",") if p.strip()
    ):
        sc += 3
    if category and getattr(song, "category", "") == category:
        sc += 1
    return sc


async def find_similar_songs(
    guild_id: int,
) -> tuple[Optional[str], List[Any]]:
    """Find songs similar to the currently-playing track.

    Returns ``(current_title, sorted_candidates)`` or ``(None, [])``
    if nothing is playing.
    """
    info = state.guild_now_playing.get(guild_id)
    title = info.get("title") if info else None
    if not info or not title or title == NOTHING_PLAYING:
        return None, []

    meta = info.get("metadata") or {}
    era_val = meta.get("era")
    era_name: Optional[str] = None
    if isinstance(era_val, dict):
        era_name = era_val.get("name")
    elif era_val:
        era_name = str(era_val)

    producers_str = meta.get("producers") or ""
    category = meta.get("category") or ""

    candidates: List[Any] = []
    api = create_api_client()
    try:
        if era_name:
            res = api.get_songs(era=era_name, page=1, page_size=25)
            candidates = res.get("results") or []
        if len(candidates) < 5 and category:
            res2 = api.get_songs(category=category, page=1, page_size=25)
            existing_ids = {getattr(s, "id", None) for s in candidates}
            for s in (res2.get("results") or []):
                if getattr(s, "id", None) not in existing_ids:
                    candidates.append(s)
    except JuiceWRLDAPIError:
        return title, []
    finally:
        api.close()

    candidates = [s for s in candidates if getattr(s, "name", None) != title]
    candidates.sort(
        key=lambda s: score_similarity(
            s,
            era_name=era_name,
            producers_str=producers_str,
            category=category,
        ),
        reverse=True,
    )
    return title, candidates[:10]


# ── Search ───────────────────────────────────────────────────────────

async def search_songs_api(query: str, page: int = 1, page_size: int = 25) -> Dict[str, Any]:
    """Search for songs via the API.  Returns the raw results dict."""
    api = create_api_client()
    try:
        return api.get_songs(search=query, page=page, page_size=page_size)
    finally:
        api.close()


# ── Leave voice ──────────────────────────────────────────────────────

async def leave_voice_channel(
    guild: Optional[discord.Guild],
    voice: Optional[discord.VoiceClient],
    *,
    delete_np_callback,
) -> bool:
    """Disconnect from voice and clean up state.

    Returns ``True`` if the bot was connected and disconnected.
    ``delete_np_callback`` should be an async callable that deletes the
    Now Playing message for a guild id after a short delay.
    """
    if not voice or not voice.is_connected():
        return False

    if guild:
        state.guild_radio_enabled[guild.id] = False
        state.guild_radio_next.pop(guild.id, None)
        import asyncio
        asyncio.create_task(delete_np_callback(guild.id, 1))

    await voice.disconnect()
    return True
