"""Async API client for the Juice WRLD API (aiohttp-based)."""

import aiohttp
import time
from datetime import datetime
from typing import List, Optional, Dict, Any, Union
from urllib.parse import quote

from models import Song, Artist, Album, Era, FileInfo, DirectoryInfo, Stats
from exceptions import JuiceWRLDAPIError, RateLimitError, NotFoundError, AuthenticationError, ValidationError


class JuiceWRLDAPI:
    """Async Python wrapper for the Juice WRLD API.

    Uses a single ``aiohttp.ClientSession`` for the lifetime of the client,
    keeping a persistent connection pool so multiple guilds don't block
    each other.
    """

    def __init__(self, base_url: str = "https://juicewrldapi.com", timeout: int = 30):
        self.base_url = base_url.rstrip('/')
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self.rate_limit_remaining = 100
        self.rate_limit_reset = time.time() + 60

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Lazily create the session (must happen inside an async context)."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    'User-Agent': 'JuiceWRLD-API-Wrapper/2.0.0',
                    'Accept': 'application/json',
                },
                timeout=self._timeout,
            )
        return self._session

    # -- Low-level HTTP ------------------------------------------------

    async def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        session = await self._ensure_session()
        url = f"{self.base_url}{endpoint}"
        try:
            async with session.request(method, url, **kwargs) as resp:
                if resp.status == 429:
                    raise RateLimitError("Rate limit exceeded")
                if resp.status == 404:
                    raise NotFoundError("Resource not found")
                if resp.status == 401:
                    raise AuthenticationError("Authentication required")
                if resp.status >= 400:
                    body = await resp.text()
                    raise JuiceWRLDAPIError(f"API error: {resp.status} - {body}")
                return await resp.json()
        except aiohttp.ClientError as e:
            raise JuiceWRLDAPIError(f"Request failed: {e}")

    async def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self._make_request('GET', endpoint, params=params)

    async def _post(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self._make_request('POST', endpoint, json=data)

    # -- Public API methods --------------------------------------------

    async def get_api_overview(self) -> Dict[str, Any]:
        data = await self._get('/juicewrld/')
        return {
            'endpoints': data,
            'title': 'Juice WRLD API',
            'description': 'Comprehensive API for Juice WRLD discography and content',
            'version': '2.0.0',
        }

    async def get_artists(self) -> List[Artist]:
        data = await self._get('/juicewrld/artists/')
        return [Artist(**artist) for artist in data.get('results', [])]

    async def get_artist(self, artist_id: int) -> Artist:
        data = await self._get(f'/juicewrld/artists/{artist_id}/')
        return Artist(**data)

    async def get_albums(self) -> List[Album]:
        data = await self._get('/juicewrld/albums/')
        return [Album(**album) for album in data.get('results', [])]

    async def get_album(self, album_id: int) -> Album:
        data = await self._get(f'/juicewrld/albums/{album_id}/')
        return Album(**data)

    async def get_songs(
        self,
        page: int = 1,
        category: Optional[str] = None,
        era: Optional[str] = None,
        search: Optional[str] = None,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {'page': page, 'page_size': page_size}
        if category:
            params['category'] = category
        if era:
            params['era'] = era
        if search:
            params['search'] = search

        data = await self._get('/juicewrld/songs/', params=params)
        
        if 'results' in data:
            songs = []
            for song_data in data['results']:
                if isinstance(song_data, dict):
                    era_raw = song_data.get('era') or {}
                    era_obj = Era(
                        id=era_raw.get('id', 0),
                        name=era_raw.get('name', 'Unknown'),
                        description=era_raw.get('description', ''),
                        time_frame=era_raw.get('time_frame', ''),
                    )
                    
                    song = Song(
                        id=song_data.get('id', 0),
                        name=song_data.get('name', 'Unknown'),
                        original_key=song_data.get('original_key', ''),
                        category=song_data.get('category', 'unknown'),
                        era=era_obj,
                        track_titles=song_data.get('track_titles', []),
                        credited_artists=song_data.get('credited_artists', ''),
                        producers=song_data.get('producers', ''),
                        engineers=song_data.get('engineers', ''),
                        additional_information=song_data.get('additional_information', ''),
                        file_names=song_data.get('file_names', ''),
                        instrumentals=song_data.get('instrumentals', ''),
                        recording_locations=song_data.get('recording_locations', ''),
                        record_dates=song_data.get('record_dates', ''),
                        preview_date=song_data.get('preview_date', ''),
                        release_date=song_data.get('release_date', ''),
                        dates=song_data.get('dates', ''),
                        length=song_data.get('length', ''),
                        leak_type=song_data.get('leak_type', ''),
                        date_leaked=song_data.get('date_leaked', ''),
                        notes=song_data.get('notes', ''),
                        image_url=song_data.get('image_url', ''),
                        session_titles=song_data.get('session_titles', ''),
                        session_tracking=song_data.get('session_tracking', ''),
                        instrumental_names=song_data.get('instrumental_names', ''),
                        public_id=song_data.get('public_id', ''),
                        lyrics=song_data.get('lyrics'),
                        snippets=song_data.get('snippets'),
                    )
                    songs.append(song)
            
            return {
                'results': songs,
                'count': data.get('count', 0),
                'next': data.get('next'),
                'previous': data.get('previous')
            }
        
        return data

    async def get_song(self, song_id: int) -> Song:
        data = await self._get(f'/juicewrld/songs/{song_id}/')

        # Construct the Era and Song objects explicitly to avoid unexpected
        # keyword arguments from extra fields returned by the API (e.g.,
        # "bitrate", "groupbuy_info", etc.).
        era_data = data.get('era', {}) if isinstance(data, dict) else {}
        era_obj = Era(
            id=era_data.get('id', 0),
            name=era_data.get('name', 'Unknown'),
            description=era_data.get('description', ''),
            time_frame=era_data.get('time_frame', ''),
        )

        return Song(
            id=data.get('id', 0),
            name=data.get('name', 'Unknown'),
            original_key=data.get('original_key', ''),
            category=data.get('category', 'unknown'),
            era=era_obj,
            track_titles=data.get('track_titles', []),
            credited_artists=data.get('credited_artists', ''),
            producers=data.get('producers', ''),
            engineers=data.get('engineers', ''),
            additional_information=data.get('additional_information', ''),
            file_names=data.get('file_names', ''),
            instrumentals=data.get('instrumentals', ''),
            recording_locations=data.get('recording_locations', ''),
            record_dates=data.get('record_dates', ''),
            preview_date=data.get('preview_date', ''),
            release_date=data.get('release_date', ''),
            dates=data.get('dates', ''),
            length=data.get('length', ''),
            leak_type=data.get('leak_type', ''),
            date_leaked=data.get('date_leaked', ''),
            notes=data.get('notes', ''),
            image_url=data.get('image_url', ''),
            session_titles=data.get('session_titles', ''),
            session_tracking=data.get('session_tracking', ''),
            instrumental_names=data.get('instrumental_names', ''),
            public_id=data.get('public_id', ''),
            path=data.get('path', ''),
            bitrate=data.get('bitrate'),
            lyrics=data.get('lyrics'),
            snippets=data.get('snippets'),
        )

    async def get_eras(self) -> List[Era]:
        data = await self._get('/juicewrld/eras/')
        eras = []
        for era_data in data.get('results', []):
            if isinstance(era_data, dict):
                eras.append(Era(
                    id=era_data.get('id', 0),
                    name=era_data.get('name', 'Unknown'),
                    description=era_data.get('description', ''),
                    time_frame=era_data.get('time_frame', ''),
                    play_count=era_data.get('play_count', 0),
                ))
        return eras

    async def get_era(self, era_id: int) -> Era:
        data = await self._get(f'/juicewrld/eras/{era_id}/')
        return Era(
            id=data.get('id', 0),
            name=data.get('name', 'Unknown'),
            description=data.get('description', ''),
            time_frame=data.get('time_frame', ''),
            play_count=data.get('play_count', 0),
        )

    async def get_stats(self) -> Stats:
        data = await self._get('/juicewrld/stats/')
        return Stats(**data)

    async def get_categories(self) -> List[Dict[str, str]]:
        data = await self._get('/juicewrld/categories/')
        return data.get('categories', [])

    async def get_random_radio_song(self) -> Dict[str, Any]:
        """Get a random radio song with full metadata from the API."""
        return await self._get('/juicewrld/radio/random/')

    async def get_juicewrld_songs(self, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        params = {'page': page, 'page_size': page_size}
        return await self._get('/juicewrld/player/songs/', params=params)

    async def get_juicewrld_song(self, song_id: int) -> Dict[str, Any]:
        return await self._get(f'/juicewrld/player/songs/{song_id}/')

    async def play_juicewrld_song(self, song_id: int) -> Dict[str, Any]:
        """Get streaming information for a song.  Never raises for normal errors."""
        try:
            try:
                song_data = await self.get_juicewrld_song(song_id)
            except NotFoundError:
                return {'error': 'Song not found in player endpoint (404)', 'song_id': song_id, 'status': 'not_found'}
            except JuiceWRLDAPIError as e:
                return {'error': str(e), 'song_id': song_id, 'status': 'api_error'}

            if 'file' not in song_data:
                return {'error': 'Song file information not found', 'song_id': song_id, 'status': 'no_file_info'}

            file_url = song_data['file']
            if '/media/' in file_url:
                file_path = file_url.split('/media/')[-1]
            else:
                return {'error': 'Invalid file URL format', 'song_id': song_id, 'status': 'invalid_url'}

            possible_paths = [
                f"Compilation/1. Released Discography/{song_data.get('album', '')}/{song_data.get('title', '')}.mp3",
                f"Compilation/2. Unreleased Discography/{song_data.get('title', '')}.mp3",
                f"Snippets/{song_data.get('title', '')}/{song_data.get('title', '')}.mp4",
                f"Session Edits/{song_data.get('title', '')}.mp3",
            ]

            session = await self._ensure_session()
            probe_timeout = aiohttp.ClientTimeout(total=5)
            for test_path in possible_paths:
                try:
                    stream_url = f"{self.base_url}/juicewrld/files/download/?path={quote(test_path)}"
                    async with session.get(stream_url, headers={'Range': 'bytes=0-0'}, timeout=probe_timeout) as resp:
                        if resp.status in (200, 206):
                            return {
                                'status': 'success',
                                'song_id': song_id,
                                'stream_url': stream_url,
                                'file_path': test_path,
                                'content_type': resp.headers.get('content-type', 'audio/mpeg'),
                            }
                except Exception:
                    continue

            stream_url = f"{self.base_url}/juicewrld/files/download/?path={quote(file_path)}"
            return {
                'status': 'file_not_found_but_url_provided',
                'song_id': song_id,
                'stream_url': stream_url,
                'file_path': file_path,
                'note': 'File may not exist at this path, but streaming URL is provided',
            }

        except Exception as e:
            return {'error': f'Unexpected request failure: {e}', 'song_id': song_id, 'status': 'request_error'}

    async def stream_audio_file(self, file_path: str) -> Dict[str, Any]:
        """Check if an audio file exists and return streaming info."""
        try:
            session = await self._ensure_session()
            stream_url = f"{self.base_url}/juicewrld/files/download/?path={quote(file_path)}"
            async with session.get(stream_url, headers={'Range': 'bytes=0-0'}) as resp:
                if resp.status in (200, 206):
                    return {
                        'status': 'success',
                        'stream_url': stream_url,
                        'file_path': file_path,
                        'content_type': resp.headers.get('content-type', 'audio/mpeg'),
                        'content_length': resp.headers.get('content-length'),
                        'supports_range': 'bytes' in resp.headers.get('accept-ranges', ''),
                    }
                if resp.status == 404:
                    return {'error': 'Audio file not found', 'file_path': file_path, 'status': 'file_not_found'}
                return {'error': f'HTTP {resp.status}', 'file_path': file_path, 'status': 'http_error'}
        except Exception as e:
            return {'error': f'Request failed: {e}', 'file_path': file_path, 'status': 'request_error'}

    async def browse_files(self, path: str = '', search: Optional[str] = None) -> DirectoryInfo:
        params: Dict[str, Any] = {}
        if path:
            params['path'] = path
        if search:
            params['search'] = search

        data = await self._get('/juicewrld/files/browse/', params=params)
        
        items = []
        for item in data.get('items', []):
            if item['type'] == 'file':
                try:
                    created = datetime.fromisoformat(item.get('created', '')) if item.get('created') else datetime.now()
                    modified = datetime.fromisoformat(item.get('modified', '')) if item.get('modified') else datetime.now()
                except (ValueError, TypeError):
                    created = datetime.now()
                    modified = datetime.now()
                
                items.append(FileInfo(
                    name=item.get('name', ''),
                    type=item.get('type', 'file'),
                    size=item.get('size', 0),
                    size_human=item.get('size_human', ''),
                    path=item.get('path', ''),
                    extension=item.get('extension', ''),
                    mime_type=item.get('mime_type', ''),
                    created=created,
                    modified=modified,
                    encoding=item.get('encoding')
                ))
            else:
                try:
                    modified = datetime.fromisoformat(item.get('modified', '')) if item.get('modified') else datetime.now()
                except (ValueError, TypeError):
                    modified = datetime.now()
                
                items.append(FileInfo(
                    name=item['name'],
                    type='directory',
                    size=0,
                    size_human='',
                    path=item['path'],
                    extension='',
                    mime_type='',
                    created=modified,
                    modified=modified,
                    encoding=None
                ))
        
        return DirectoryInfo(
            current_path=data.get('current_path', ''),
            path_parts=data.get('path_parts', []),
            items=items,
            total_files=data.get('total_files', 0),
            total_directories=data.get('total_directories', 0),
            search_query=data.get('search_query'),
            is_recursive_search=data.get('is_recursive_search', False)
        )

    async def get_file_info(self, file_path: str) -> FileInfo:
        data = await self._get('/juicewrld/files/info/', {'path': file_path})
        return FileInfo(**data)

    async def download_file(self, file_path: str, save_path: Optional[str] = None) -> Union[bytes, str]:
        session = await self._ensure_session()
        url = f"{self.base_url}/juicewrld/files/download/?path={quote(file_path)}"
        try:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise JuiceWRLDAPIError(f"Download failed: HTTP {resp.status}")
                if save_path:
                    with open(save_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(8192):
                            f.write(chunk)
                    return save_path
                return await resp.read()
        except aiohttp.ClientError as e:
            raise JuiceWRLDAPIError(f"Download failed: {e}")

    async def get_cover_art(self, file_path: str) -> bytes:
        session = await self._ensure_session()
        url = f"{self.base_url}/juicewrld/files/cover-art/?path={quote(file_path)}"
        try:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise JuiceWRLDAPIError(f"Cover art retrieval failed: HTTP {resp.status}")
                return await resp.read()
        except aiohttp.ClientError as e:
            raise JuiceWRLDAPIError(f"Cover art retrieval failed: {e}")

    async def create_zip(self, file_paths: List[str]) -> bytes:
        session = await self._ensure_session()
        try:
            async with session.post(
                f"{self.base_url}/juicewrld/files/zip-selection/",
                json={'paths': file_paths},
            ) as resp:
                if resp.status >= 400:
                    raise JuiceWRLDAPIError(f"ZIP creation failed: HTTP {resp.status}")
                return await resp.read()
        except aiohttp.ClientError as e:
            raise JuiceWRLDAPIError(f"ZIP creation failed: {e}")

    async def start_zip_job(self, file_paths: List[str]) -> str:
        data = await self._post('/juicewrld/start-zip-job/', {'paths': file_paths})
        return data.get('job_id')

    async def get_zip_job_status(self, job_id: str) -> Dict[str, Any]:
        return await self._get(f'/juicewrld/zip-job-status/{job_id}/')

    async def cancel_zip_job(self, job_id: str) -> bool:
        try:
            await self._post(f'/juicewrld/cancel-zip-job/{job_id}/')
            return True
        except Exception:
            return False

    # -- Lifecycle -----------------------------------------------------

    async def close(self):
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

