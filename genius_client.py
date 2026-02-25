"""Genius API client for fetching lyrics as a fallback."""

import aiohttp
from typing import Optional
import re


class GeniusClient:
    """Simple async client for Genius API to fetch song lyrics."""
    
    def __init__(self, access_token: Optional[str] = None):
        self.base_url = "https://api.genius.com"
        self.access_token = access_token
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _ensure_session(self) -> Optional[aiohttp.ClientSession]:
        """Lazily create session if token is available."""
        if not self.access_token:
            return None
        
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    'Authorization': f'Bearer {self.access_token}',
                    'User-Agent': 'JuiceWRLD-Discord-Bot/1.0'
                }
            )
        return self._session
    
    async def close(self):
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def search_song(self, artist: str, title: str) -> Optional[dict]:
        """Search for a song on Genius.
        
        Args:
            artist: Artist name (e.g., "Juice WRLD")
            title: Song title
            
        Returns:
            Song info dict with 'id', 'title', 'url', etc., or None if not found
        """
        session = await self._ensure_session()
        if not session:
            return None
        
        try:
            query = f"{artist} {title}"
            async with session.get(
                f"{self.base_url}/search",
                params={'q': query}
            ) as resp:
                if resp.status != 200:
                    return None
                
                data = await resp.json()
                hits = data.get('response', {}).get('hits', [])
                
                if not hits:
                    return None
                
                # Return the first result
                return hits[0].get('result')
        except Exception:
            return None
    
    async def get_lyrics_url(self, song_title: str) -> Optional[str]:
        """Get Genius lyrics URL for a Juice WRLD song.
        
        Args:
            song_title: The song title to search for
            
        Returns:
            URL to lyrics page on Genius, or None
        """
        song_info = await self.search_song("Juice WRLD", song_title)
        if song_info:
            return song_info.get('url')
        return None
    
    async def scrape_lyrics_from_url(self, url: str) -> Optional[str]:
        """Scrape lyrics from a Genius song page.
        
        Note: This is a simple scraper. The official Genius API doesn't provide
        lyrics directly, so this extracts them from the HTML page.
        
        Args:
            url: Genius song URL
            
        Returns:
            Lyrics text, or None if failed
        """
        # For now, we'll just return the URL since scraping would require
        # additional dependencies (BeautifulSoup4) and can be fragile.
        # Users can click the link to view lyrics on Genius.
        return None
    
    async def get_song_lyrics(self, song_title: str) -> Optional[str]:
        """Get lyrics for a Juice WRLD song.
        
        Args:
            song_title: The song title
            
        Returns:
            Lyrics text or Genius URL, or None if not found
        """
        url = await self.get_lyrics_url(song_title)
        if url:
            # For now, return a formatted message with the URL
            # since we're not scraping the actual lyrics
            return f"View lyrics on Genius:\n{url}"
        return None
