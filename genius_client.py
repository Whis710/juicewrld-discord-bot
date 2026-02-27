"""Genius lyrics client using the lyricsgenius library."""

import asyncio
import sys
from typing import Optional


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

    def _search_sync(self, song_title: str):
        """Synchronous search with loose matching for subtitle variants.

        Searches by title only (no strict artist filter) then picks the
        first result where the primary artist is Juice WRLD. This handles
        cases like 'Fresh Air' matching 'Fresh Air (Bel Air)'.
        """
        genius = self._ensure_client()
        if not genius:
            return None

        try:
            hits = genius.search_songs(song_title)
            results = hits.get("hits", []) if isinstance(hits, dict) else []

            for hit in results:
                result = hit.get("result", {})
                artist_name = (
                    result.get("primary_artist", {}).get("name", "") or ""
                ).lower()

                # Accept any result where Juice WRLD is the primary artist.
                if any(alias in artist_name for alias in self.ARTIST_NAMES):
                    song_id = result.get("id")
                    if song_id:
                        # Fetch full song object to get lyrics.
                        return genius.song(song_id)

            # Fallback: standard search_song with artist name.
            return genius.search_song(song_title, "Juice WRLD")

        except Exception as e:
            print(f"[genius] Search error for '{song_title}': {e}", file=sys.stderr)
            return None

    async def close(self) -> None:
        """No-op — lyricsgenius has no persistent session to close."""
        pass

    async def get_song_lyrics(self, song_title: str) -> Optional[str]:
        """Fetch lyrics for a Juice WRLD song.

        Uses loose matching to handle subtitle variants like
        'Fresh Air' -> 'Fresh Air (Bel Air)'.
        """
        try:
            loop = asyncio.get_event_loop()
            song = await loop.run_in_executor(None, lambda: self._search_sync(song_title))
            if song and song.lyrics:
                return song.lyrics
            return None
        except Exception as e:
            print(f"[genius] Error fetching lyrics for '{song_title}': {e}", file=sys.stderr)
            return None

    async def get_lyrics_url(self, song_title: str) -> Optional[str]:
        """Get the Genius page URL for a song."""
        try:
            loop = asyncio.get_event_loop()
            song = await loop.run_in_executor(None, lambda: self._search_sync(song_title))
            if song:
                return song.url
            return None
        except Exception:
            return None
