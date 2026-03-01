# Juice WRLD Discord Bot

A feature-rich Discord bot for playing Juice WRLD music, managing playlists, and exploring his discography.

## Features

- 🎵 Play songs from the Juice WRLD API
- 📻 Radio mode with random song playback
- 📋 User playlists
- 🎲 Song shuffle
- ⏭️ Queue management (Play Now, Play Next, Add to Queue)
- 📊 Listening statistics
- 🌅 Song of the Day (SOTD)
- 📅 Leak Timeline browser
- 🎵 Era exploration
- 📝 Lyrics display (with Genius API fallback)
- 🎬 Snippets

## Environment Variables

Required:
- `DISCORD_TOKEN` - Your Discord bot token
- `CLIENT_ID` - Discord application client ID
- `GUILD_ID` - Discord server/guild ID
- `DISCORD_CLIENT_SECRET` - Discord application client secret
- `LINKED_ROLES_URL` - URL for linked roles

Optional:
- `JUICEWRLD_API_BASE_URL` - Base URL for Juice WRLD API (default: https://juicewrldapi.com)
- `GENIUS_API_TOKEN` - Genius API token for lyrics fallback

## Genius API Setup (Optional)

The bot uses the Juice WRLD API as the primary source for lyrics. If lyrics aren't available, it can fall back to Genius.com.

### Getting a Genius API Token

1. Go to https://genius.com/api-clients
2. Sign in or create an account
3. Click "New API Client"
4. Fill in the form:
   - **App Name**: Your bot name (e.g., "Juice WRLD Discord Bot")
   - **App Website URL**: Your website or GitHub repo
   - **Redirect URI**: Can be `http://localhost` for a bot
5. Click "Save"
6. Copy the **Client Access Token** (not the Client ID/Secret)
7. Add it to your environment variables as `GENIUS_API_TOKEN`

### Using in Docker

Add to your `.env` file:
```
GENIUS_API_TOKEN=your_token_here
```

Or in Portainer, add the environment variable in the container configuration.

**Note**: The Genius API token is completely optional. The bot will work fine without it, but won't have the lyrics fallback feature.

## Running the Bot

### Docker
```bash
docker build -t bryanr710/juicewrld-bot:latest .
docker push bryanr710/juicewrld-bot:latest
```

Then deploy using your docker-compose configuration or Portainer.

## Version

Current version: **3.5.3**
Build date: 2026-03-01

### Changelog (v3.5.3)
- **Genius Lyrics Search** — Improved matching by including artist name in search queries (e.g. "fresh air" now correctly finds "Fresh Air (Bel-Air)")
- **Info Button Fix** — Fixed the ℹ️ Info button in `/jw search` silently failing on ephemeral messages

## Commands

### Slash Commands
- `/jw play <song_id>` - Play a song by ID
- `/jw search <query>` - Search for songs
- `/jw radio` - Start radio mode
- `/jw leaks` - Browse leaked songs timeline
- `/jw eras` - View all musical eras
- `/jw sotd` - View current Song of the Day
- `/jw history` - Show recently played songs

### Text Commands
- `!jw help` - Show all commands
- `!jw play <song_id>` - Play a song
- `!jw search <query>` - Search songs
- `!jw radio` - Start radio mode
- `!jw stop` - Stop playback
- `!jw pl` - Manage playlists
- And many more...

See `!jw help` in Discord for the full command list.

## License

MIT
