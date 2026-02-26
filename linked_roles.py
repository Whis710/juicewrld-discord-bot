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
DISCORD_CLIENT_ID = os.getenv("CLIENT_ID", "")
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

# ---------------------------------------------------------------------------
# Shared HTML helpers
# ---------------------------------------------------------------------------

_BASE_STYLE = """
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet"/>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:       #0a0a0f;
    --surface:  #12121a;
    --border:   #1e1e2e;
    --accent:   #ff3c5f;
    --accent2:  #ff8c42;
    --text:     #e8e8f0;
    --muted:    #6b6b80;
    --success:  #39d98a;
    --error:    #ff3c5f;
  }

  html, body {
    min-height: 100vh;
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    font-weight: 300;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow-x: hidden;
  }

  /* Rotating wallpaper */
  #wallpaper {
    position: fixed;
    inset: 0;
    z-index: -1;
    background-size: cover;
    background-position: center;
    background-repeat: no-repeat;
    transition: opacity 1.5s ease-in-out;
    opacity: 0;
  }
  #wallpaper.visible { opacity: 1; }
  #wallpaper::after {
    content: '';
    position: absolute;
    inset: 0;
    background: rgba(0,0,0,0.55);
  }

  /* Animated grain overlay */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.04'/%3E%3C/svg%3E");
    pointer-events: none;
    z-index: 999;
    opacity: 0.4;
  }

  /* Glowing orbs in background */
  body::after {
    content: '';
    position: fixed;
    width: 600px; height: 600px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(255,60,95,0.07) 0%, transparent 70%);
    top: -150px; left: -150px;
    pointer-events: none;
    z-index: 0;
  }

  .orb2 {
    position: fixed;
    width: 500px; height: 500px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(255,140,66,0.06) 0%, transparent 70%);
    bottom: -100px; right: -100px;
    pointer-events: none;
    z-index: 0;
  }

  /* Card */
  .card {
    position: relative;
    z-index: 1;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 48px 44px;
    max-width: 480px;
    width: 90%;
    text-align: center;
    box-shadow: 0 0 0 1px rgba(255,60,95,0.05), 0 40px 80px rgba(0,0,0,0.6);
    animation: fadeUp 0.5s cubic-bezier(0.22,1,0.36,1) both;
  }

  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(24px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  /* Logo area */
  .logo-wrap {
    margin-bottom: 28px;
  }
  .logo-wrap img {
    width: 88px;
    height: 88px;
    border-radius: 50%;
    object-fit: cover;
    border: 2px solid var(--border);
    box-shadow: 0 0 24px rgba(255,60,95,0.25);
    display: block;
    margin: 0 auto 16px;
  }
  .logo-placeholder {
    width: 88px; height: 88px;
    border-radius: 50%;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    display: flex; align-items: center; justify-content: center;
    font-size: 36px;
    margin: 0 auto 16px;
    box-shadow: 0 0 32px rgba(255,60,95,0.3);
  }

  /* Banner image */
  .banner {
    width: 100%;
    height: 140px;
    object-fit: cover;
    border-radius: 12px;
    margin-bottom: 24px;
    display: block;
    border: 1px solid var(--border);
  }
  .banner-placeholder {
    width: 100%;
    height: 140px;
    background: linear-gradient(135deg, #1a0a12 0%, #0f0a1a 50%, #1a0a0a 100%);
    border-radius: 12px;
    margin-bottom: 24px;
    display: flex; align-items: center; justify-content: center;
    border: 1px solid var(--border);
    font-size: 12px;
    color: var(--muted);
    letter-spacing: 0.15em;
    text-transform: uppercase;
    font-weight: 500;
  }

  /* Typography */
  h1 {
    font-family: 'Bebas Neue', sans-serif;
    font-size: 2.6rem;
    letter-spacing: 0.04em;
    line-height: 1;
    margin-bottom: 12px;
    background: linear-gradient(135deg, #fff 30%, rgba(255,255,255,0.55));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }
  h2 {
    font-family: 'Bebas Neue', sans-serif;
    font-size: 1.9rem;
    letter-spacing: 0.04em;
    margin-bottom: 10px;
    color: var(--text);
  }
  p {
    font-size: 0.95rem;
    color: var(--muted);
    line-height: 1.65;
    margin-bottom: 8px;
  }
  .label {
    display: inline-block;
    font-size: 0.7rem;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    font-weight: 500;
    color: var(--accent);
    margin-bottom: 10px;
  }

  /* Divider */
  .divider {
    height: 1px;
    background: linear-gradient(to right, transparent, var(--border), transparent);
    margin: 28px 0;
  }

  /* CTA Button */
  .btn {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    background: linear-gradient(135deg, var(--accent), #c92948);
    color: #fff;
    font-family: 'DM Sans', sans-serif;
    font-size: 0.9rem;
    font-weight: 500;
    letter-spacing: 0.05em;
    padding: 14px 32px;
    border-radius: 50px;
    text-decoration: none;
    border: none;
    cursor: pointer;
    transition: transform 0.15s, box-shadow 0.15s, opacity 0.15s;
    box-shadow: 0 4px 20px rgba(255,60,95,0.35);
    margin-top: 12px;
  }
  .btn:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 28px rgba(255,60,95,0.5);
    opacity: 0.95;
  }
  .btn:active { transform: translateY(0); }

  .btn-secondary {
    background: transparent;
    color: var(--muted);
    border: 1px solid var(--border);
    box-shadow: none;
    font-size: 0.82rem;
    padding: 10px 24px;
    margin-top: 8px;
  }
  .btn-secondary:hover {
    color: var(--text);
    border-color: var(--muted);
    box-shadow: none;
    background: rgba(255,255,255,0.03);
  }

  /* Status icons */
  .status-icon {
    font-size: 3rem;
    margin-bottom: 16px;
    display: block;
  }
  .status-icon.success { filter: drop-shadow(0 0 12px var(--success)); }
  .status-icon.error   { filter: drop-shadow(0 0 12px var(--error)); }

  /* Stats chips */
  .chips {
    display: flex;
    gap: 8px;
    justify-content: center;
    flex-wrap: wrap;
    margin: 20px 0;
  }
  .chip {
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border);
    border-radius: 50px;
    padding: 6px 14px;
    font-size: 0.78rem;
    color: var(--muted);
    letter-spacing: 0.05em;
  }

  /* Error code block */
  pre {
    background: #0d0d16;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px;
    font-size: 0.78rem;
    color: var(--error);
    text-align: left;
    overflow-x: auto;
    margin-top: 16px;
    white-space: pre-wrap;
    word-break: break-all;
  }

  .footer-note {
    margin-top: 24px;
    font-size: 0.72rem;
    color: var(--muted);
    opacity: 0.6;
  }
</style>
"""

def _page(body: str) -> str:
    """Wrap body content in the full HTML shell."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>{_BASE_STYLE}<title>Juice WRLD Bot</title></head>
<body>
<div id="wallpaper"></div>
<div class="orb2"></div>
{body}
<script>
(function() {{
  var images = [
    'https://i.imgur.com/1fgMxFO.png',
    'https://i.imgur.com/j4SnxvT.jpeg',
    'https://i.imgur.com/jluAw1e.jpeg',
    'https://i.imgur.com/GHZH3JW.jpeg',
    'https://i.imgur.com/JSOyXet.jpeg',
    'https://i.imgur.com/VfvNKu8.jpeg'
  ];
  // Fisher-Yates shuffle
  for (var i = images.length - 1; i > 0; i--) {{
    var j = Math.floor(Math.random() * (i + 1));
    var tmp = images[i]; images[i] = images[j]; images[j] = tmp;
  }}
  var idx = 0;
  var el = document.getElementById('wallpaper');
  function show() {{
    el.classList.remove('visible');
    setTimeout(function() {{
      el.style.backgroundImage = 'url(' + images[idx] + ')';
      el.classList.add('visible');
      idx = (idx + 1) % images.length;
    }}, 600);
  }}
  show();
  setInterval(show, 6000);
}})();
</script>
</body>
</html>"""


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
        return HTMLResponse(_page("""
<div class="card">
  <span class="status-icon error">&#9888;&#65039;</span>
  <span class="label">Configuration Error</span>
  <h2>Not Configured</h2>
  <p>The bot owner needs to set the required environment variables before Linked Roles can be used.</p>
  <div class="divider"></div>
  <pre>DISCORD_CLIENT_ID
DISCORD_CLIENT_SECRET
LINKED_ROLES_URL</pre>
  <a href="/" class="btn btn-secondary">&#8592; Go Back</a>
</div>
"""), status_code=503)
    return RedirectResponse(_build_oauth_url())


@app.get("/callback")
async def oauth_callback(request: Request):
    """Handle the OAuth2 callback from Discord."""
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse(_page("""
<div class="card">
  <span class="status-icon error">&#10060;</span>
  <span class="label">Error 400</span>
  <h2>Missing Code</h2>
  <p>No authorization code was provided. Please try connecting again from Discord.</p>
  <a href="/" class="btn btn-secondary">&#8592; Go Back</a>
</div>
"""), status_code=400)

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
                return HTMLResponse(_page(f"""
<div class="card">
  <span class="status-icon error">&#10060;</span>
  <span class="label">Error {resp.status}</span>
  <h2>Token Exchange Failed</h2>
  <p>Could not exchange your authorization code for an access token. Please try again.</p>
  <pre>{resp.status}</pre>
  <a href="/" class="btn btn-secondary">&#8592; Go Back</a>
</div>
"""), status_code=400)
            token_data = await resp.json()

        access_token = token_data.get("access_token")
        if not access_token:
            return HTMLResponse(_page("""
<div class="card">
  <span class="status-icon error">&#10060;</span>
  <span class="label">Error 400</span>
  <h2>No Access Token</h2>
  <p>Discord did not return an access token. Please try connecting again.</p>
  <a href="/" class="btn btn-secondary">&#8592; Go Back</a>
</div>
"""), status_code=400)

        auth_headers = {"Authorization": f"Bearer {access_token}"}

        # Step 2: Fetch the user's identity
        async with session.get(f"{DISCORD_API}/users/@me", headers=auth_headers) as resp:
            if resp.status != 200:
                return HTMLResponse(_page("""
<div class="card">
  <span class="status-icon error">&#10060;</span>
  <span class="label">Error 400</span>
  <h2>User Fetch Failed</h2>
  <p>We could not retrieve your Discord profile. Please try again.</p>
  <a href="/" class="btn btn-secondary">&#8592; Go Back</a>
</div>
"""), status_code=400)
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
                plays = metadata_values["total_plays"]
                hours = metadata_values["total_listen_hours"]
                songs = metadata_values["unique_songs"]
                return HTMLResponse(_page(f"""
<div class="card">
  <div class="logo-wrap">
    <img src="https://i.imgur.com/walM89T.png" alt="Bot Logo">
  </div>
  <span class="status-icon success">&#10003;</span>
  <span class="label">All Set</span>
  <h2>You're Connected!</h2>
  <p>Welcome, <strong style="color: var(--text);">{username}</strong>. Your listening stats have been synced to Discord.</p>
  <div class="chips">
    <span class="chip">&#127911; {plays} Plays</span>
    <span class="chip">&#9202; {hours}h Listened</span>
    <span class="chip">&#127925; {songs} Songs</span>
  </div>
  <div class="divider"></div>
  <p style="font-size:0.85rem;">You can close this page and return to Discord. Your roles will update shortly.</p>
</div>
"""))
            body = await resp.text()
            return HTMLResponse(_page(f"""
<div class="card">
  <span class="status-icon error">&#10060;</span>
  <span class="label">Error 500</span>
  <h2>Sync Failed</h2>
  <p>We were unable to update your role connection data on Discord. Please try again later.</p>
  <pre>{resp.status}: {body}</pre>
  <a href="/" class="btn btn-secondary">&#8592; Go Back</a>
</div>
"""), status_code=500)


@app.get("/")
async def index():
    """Landing page."""
    return HTMLResponse(_page("""
<div class="card">
  <div class="logo-wrap">
    <img src="https://i.imgur.com/walM89T.png" alt="Bot Logo">
    <span class="label">Music Bot</span>
  </div>

  <img src="https://i.imgur.com/8jo57P9.jpeg" alt="Banner" class="banner">

  <h1>Juice WRLD Bot</h1>
  <p>Track your listening stats and unlock exclusive Discord roles based on how much you vibe.</p>

  <div class="chips">
    <span class="chip">&#127911; Track Plays</span>
    <span class="chip">&#9202; Listen Hours</span>
    <span class="chip">&#127925; Unique Songs</span>
  </div>

  <div class="divider"></div>

  <a href="/linked-roles" class="btn">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 7h3a5 5 0 0 1 5 5 5 5 0 0 1-5 5h-3m-6 0H6a5 5 0 0 1-5-5 5 5 0 0 1 5-5h3"/><line x1="8" y1="12" x2="16" y2="12"/></svg>
    Connect Linked Roles
  </a>
  <p class="footer-note">You will be redirected to Discord to authorize.</p>
</div>
"""))
