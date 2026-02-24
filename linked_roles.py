"""Linked Roles web server for Discord OAuth2 connection metadata.

Runs a small FastAPI app alongside the bot to handle the OAuth2 flow
required for Discord's Linked Roles feature.

Environment variables:
    DISCORD_CLIENT_ID       - Application (client) ID
    DISCORD_CLIENT_SECRET   - OAuth2 client secret
    LINKED_ROLES_URL        - Public base URL (e.g. https://yourdomain.com)
    LINKED_ROLES_PORT       - Port to listen on (default 8080)
"""

import os
import urllib.parse
from typing import Any, Callable, Dict, Optional

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

DISCORD_API = "https://discord.com/api/v10"
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
LINKED_ROLES_URL = os.getenv("LINKED_ROLES_URL", "http://localhost:8080")
LINKED_ROLES_PORT = int(os.getenv("LINKED_ROLES_PORT", "8080"))

# The bot sets this callback so we can look up listening stats.
_stats_callback: Optional[Callable[[int], Optional[Dict[str, Any]]]] = None

# Connection metadata schema registered with Discord.
METADATA_SCHEMA = [
    {
        "key": "total_plays",
        "name": "Total Plays",
        "description": "Number of songs played",
        "type": 2,  # integer_greater_than_or_equal
    },
    {
        "key": "total_listen_hours",
        "name": "Listen Hours",
        "description": "Total hours of music listened to",
        "type": 2,
    },
    {
        "key": "unique_songs",
        "name": "Unique Songs",
        "description": "Number of unique songs played",
        "type": 2,
    },
]

app = FastAPI(title="Juice WRLD Bot - Linked Roles", docs_url=None, redoc_url=None)


def set_stats_callback(cb: Callable[[int], Optional[Dict[str, Any]]]) -> None:
    """Register a callback that returns user stats given a Discord user ID."""
    global _stats_callback
    _stats_callback = cb


async def register_metadata_schema(bot_token: str) -> bool:
    """Push the connection metadata schema to Discord.

    Should be called once on bot startup. Returns True on success.
    """
    url = f"{DISCORD_API}/applications/{DISCORD_CLIENT_ID}/role-connections/metadata"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.put(url, json=METADATA_SCHEMA, headers=headers) as resp:
            if resp.status == 200:
                return True
            body = await resp.text()
            print(f"[linked_roles] Failed to register metadata schema: {resp.status} {body}")
            return False


def _build_oauth_url() -> str:
    """Build the Discord OAuth2 authorize URL."""
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": f"{LINKED_ROLES_URL}/callback",
        "response_type": "code",
        "scope": "role_connections.write identify",
    }
    return f"https://discord.com/api/oauth2/authorize?{urllib.parse.urlencode(params)}"


@app.get("/linked-roles")
async def linked_roles_redirect():
    """Redirect the user to Discord's OAuth2 consent page."""
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
        return HTMLResponse(
            "<h3>Linked Roles not configured.</h3>"
            "<p>The bot owner needs to set DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, "
            "and LINKED_ROLES_URL environment variables.</p>",
            status_code=503,
        )
    return RedirectResponse(_build_oauth_url())


@app.get("/callback")
async def oauth_callback(request: Request):
    """Handle the OAuth2 callback from Discord."""
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("<h3>Missing authorization code.</h3>", status_code=400)

    # Exchange the code for an access token.
    token_url = f"{DISCORD_API}/oauth2/token"
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": f"{LINKED_ROLES_URL}/callback",
    }

    async with aiohttp.ClientSession() as session:
        # Step 1: Exchange code for token
        async with session.post(token_url, data=data) as resp:
            if resp.status != 200:
                body = await resp.text()
                return HTMLResponse(
                    f"<h3>Token exchange failed.</h3><pre>{resp.status}</pre>",
                    status_code=400,
                )
            token_data = await resp.json()

        access_token = token_data.get("access_token")
        if not access_token:
            return HTMLResponse("<h3>No access token received.</h3>", status_code=400)

        auth_headers = {"Authorization": f"Bearer {access_token}"}

        # Step 2: Fetch the user's identity
        async with session.get(f"{DISCORD_API}/users/@me", headers=auth_headers) as resp:
            if resp.status != 200:
                return HTMLResponse("<h3>Failed to fetch user info.</h3>", status_code=400)
            user_data = await resp.json()

        user_id = int(user_data["id"])
        username = user_data.get("username", "Unknown")

        # Step 3: Look up listening stats via the bot callback
        metadata_values: Dict[str, int] = {
            "total_plays": 0,
            "total_listen_hours": 0,
            "unique_songs": 0,
        }
        if _stats_callback:
            stats = _stats_callback(user_id)
            if stats:
                metadata_values["total_plays"] = stats.get("total_plays", 0)
                total_secs = stats.get("total_seconds", 0)
                metadata_values["total_listen_hours"] = total_secs // 3600
                songs_dict = stats.get("songs", {})
                metadata_values["unique_songs"] = len(songs_dict)

        # Step 4: Push metadata to Discord
        metadata_url = (
            f"{DISCORD_API}/users/@me/applications/{DISCORD_CLIENT_ID}/role-connection"
        )
        payload = {
            "platform_name": "Juice WRLD Bot",
            "platform_username": username,
            "metadata": metadata_values,
        }
        async with session.put(metadata_url, json=payload, headers=auth_headers) as resp:
            if resp.status == 200:
                return HTMLResponse(
                    "<h2>✅ Linked Roles connected!</h2>"
                    f"<p>Welcome, <b>{username}</b>. Your listening stats have been synced.</p>"
                    "<p>You can close this page and return to Discord.</p>"
                )
            body = await resp.text()
            return HTMLResponse(
                f"<h3>Failed to update role connection.</h3><pre>{resp.status}: {body}</pre>",
                status_code=500,
            )


@app.get("/")
async def index():
    """Simple landing page."""
    return HTMLResponse(
        "<h2>Juice WRLD Discord Bot</h2>"
        '<p><a href="/linked-roles">Connect Linked Roles</a></p>'
    )
