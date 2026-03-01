"""Genius lyrics client using the lyricsgenius library."""

import asyncio
import sys
from typing import Dict, List, Optional


class GeniusClient:
    """Wrapper around lyricsgenius for fetching Juice WRLD song lyrics.

    lyricsgenius is synchronous, so all calls are run in an executor
    to avoid blocking the bot's async event loop.
    """

    ARTIST_NAMES = {"juice wrld", "juice world", "juicewrld"}

    def __init__(self, access_token: Optional[str] = None) -> None:
        self.access_token = access_token
        self._genius = None

    def _ensure_client(self):
        """Lazily create the lyricsgenius client."""
        if self._genius is None and self.access_token:
            try:
                import lyricsgenius
                self._genius = lyricsgenius.Genius(
                    self.access_token,
                    skip_non_songs=True,
                    excluded_terms=["(Remix)", "(Live)"],
                    verbose=False,
                    remove_section_headers=False,  # keep [Chorus], [Verse] etc.
                    retries=2,
                )
            except ImportError:
                print("[genius] lyricsgenius not installed — run: pip install lyricsgenius")
        return self._genius

    def _search_candidates_sync(self, song_title: str, max_results: int = 5) -> List[Dict]:
        """Synchronous search returning up to max_results Juice WRLD candidates.

        Returns a list of dicts: {id, title, url} without fetching full lyrics.
        This is fast — no per-song HTTP calls beyond the initial search.
        """
        genius = self._ensure_client()
        if not genius:
            return []

        try:
            # Prepend artist name so Genius ranks the Juice WRLD version
            # higher — e.g. "fresh air" → "Juice WRLD fresh air" which
            # matches "Fresh Air (Bel-Air)" instead of unrelated songs.
            query = f"Juice WRLD {song_title}"
            hits = genius.search_songs(query)
            results = hits.get("hits", []) if isinstance(hits, dict) else []

            candidates = []
            for hit in results:
                if len(candidates) >= max_results:
                    break
                result = hit.get("result", {})
                artist_name = (
                    result.get("primary_artist", {}).get("name", "") or ""
                ).lower()
                if any(alias in artist_name for alias in self.ARTIST_NAMES):
                    candidates.append({
                        "id": result.get("id"),
                        "title": result.get("title", "Unknown"),
                        "url": result.get("url", ""),
                    })

            return candidates

        except Exception as e:
            print(f"[genius] Search error for '{song_title}': {e}", file=sys.stderr)
            return []

    def _get_lyrics_by_id_sync(self, song_id: int) -> Optional[str]:
        """Synchronous fetch of lyrics for a specific Genius song ID."""
        genius = self._ensure_client()
        if not genius:
            return None
        try:
            song = genius.song(song_id)
            if song and hasattr(song, "lyrics"):
                return song["lyrics"] if isinstance(song, dict) else song.lyrics
            return None
        except Exception as e:
            print(f"[genius] Error fetching lyrics for ID {song_id}: {e}", file=sys.stderr)
            return None

    async def close(self) -> None:
        """No-op — lyricsgenius has no persistent session to close."""
        pass

    async def search_candidates(self, song_title: str, max_results: int = 5) -> List[Dict]:
        """Async wrapper — returns up to max_results Juice WRLD candidates.

        Each candidate is a dict with keys: id, title, url.
        No lyrics are fetched at this stage — fast search only.
        """
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._search_candidates_sync(song_title, max_results),
            )
        except Exception as e:
            print(f"[genius] search_candidates error: {e}", file=sys.stderr)
            return []

    async def get_lyrics_by_id(self, song_id: int) -> Optional[str]:
        """Async wrapper — fetch full lyrics for a specific Genius song ID."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._get_lyrics_by_id_sync(song_id),
            )
        except Exception as e:
            print(f"[genius] get_lyrics_by_id error: {e}", file=sys.stderr)
            return None

    async def get_song_lyrics(self, song_title: str) -> Optional[str]:
        """Fetch lyrics for the top matching Juice WRLD song (auto-pick first result)."""
        candidates = await self.search_candidates(song_title, max_results=1)
        if not candidates:
            return None
        return await self.get_lyrics_by_id(candidates[0]["id"])

    async def get_lyrics_url(self, song_title: str) -> Optional[str]:
        """Get the Genius page URL for the top matching song."""
        candidates = await self.search_candidates(song_title, max_results=1)
        if candidates:
            return candidates[0]["url"]
        return None
