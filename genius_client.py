"""Genius lyrics client using the lyricsgenius library."""

import asyncio
from typing import Optional


class GeniusClient:
    """Wrapper around lyricsgenius for fetching Juice WRLD song lyrics.

    lyricsgenius is synchronous, so all calls are run in an executor
    to avoid blocking the bot's async event loop.
    """

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
                    verbose=False,          # suppress console spam
                    remove_section_headers=False,  # keep [Chorus], [Verse] etc. for display
                    retries=2,
                )
            except ImportError:
                print("[genius] lyricsgenius not installed — run: pip install lyricsgenius")
        return self._genius

    async def close(self) -> None:
        """No-op — lyricsgenius has no persistent session to close."""
        pass

    async def get_song_lyrics(self, song_title: str) -> Optional[str]:
        """Fetch lyrics for a Juice WRLD song via lyricsgenius.

        Runs the synchronous lyricsgenius call in a thread executor so the
        bot's event loop is not blocked.

        Returns:
            Lyrics text (truncated to 4096 chars for Discord embeds), or None.
        """
        genius = self._ensure_client()
        if not genius:
            return None

        try:
            loop = asyncio.get_event_loop()
            song = await loop.run_in_executor(
                None,
                lambda: genius.search_song(song_title, "Juice WRLD"),
            )
            if song and song.lyrics:
                return song.lyrics
            return None
        except Exception as e:
            print(f"[genius] Error fetching lyrics for '{song_title}': {e}")
            return None

    async def get_lyrics_url(self, song_title: str) -> Optional[str]:
        """Get the Genius page URL for a song (without fetching full lyrics).

        Useful as a lightweight fallback when the full lyrics fetch fails.
        """
        genius = self._ensure_client()
        if not genius:
            return None

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: genius.search_song(song_title, "Juice WRLD", get_full_info=False),
            )
            if result:
                return result.url
            return None
        except Exception:
            return None
