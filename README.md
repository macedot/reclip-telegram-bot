# ReClip Telegram Bot

A self-hosted Telegram bot that downloads media from YouTube, TikTok, Instagram, Twitter, Reddit, and 1000+ other sites. Powered by [reclip](https://github.com/averygan/reclip) and yt-dlp.

Send a link, pick your format and quality, get the file delivered right in the chat.

![Bot conversation](images/bot.png)

## Features

- Multi-platform support (YouTube, TikTok, Instagram, Twitter, Reddit, and 1000+ more via yt-dlp)
- Format selection (MP4 video or MP3 audio)
- Quality picker with all available resolutions
- Real-time download progress (percentage)
- Thumbnail preview with metadata (title, platform, duration)
- Files up to 2GB via self-hosted Telegram Bot API
- Automatic file cleanup (configurable age and disk limits)
- Concurrent download limiting (prevents resource exhaustion)
- Admin dashboard with download stats, history, error tracking, and disk management
- Web UI included (reclip's built-in web interface)

![Admin dashboard](images/admin.png)

## Architecture

```
Telegram User ──> Self-hosted Bot API (2GB limit)
                        │
                        ▼
                   bot (python-telegram-bot + httpx)
                   │              │
          HTTP     │              │  HTTP events
                   ▼              ▼
              reclip (Flask)   dashboard (FastAPI)
              port 8899        port 8080
                               │
                               ▼
                            SQLite
```

Four Docker containers via docker-compose:
1. **reclip** - Media download engine with REST API and web UI
2. **bot** - Telegram bot that wraps reclip's API
3. **telegram-bot-api** - Self-hosted Telegram Bot API server for 2GB upload limit
4. **dashboard** - Admin panel with download stats, history, errors, and file management

## Quick Start

### Prerequisites

- Docker and Docker Compose
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Telegram API credentials (from [my.telegram.org](https://my.telegram.org))

### Setup

1. Clone this repository:
```bash
git clone https://github.com/gth-ai/reclip-telegram-bot.git
cd reclip_bot
```

2. Copy the example environment file:
```bash
cp .env.example .env
```

3. Edit `.env` with your credentials:
```bash
BOT_TOKEN=your-bot-token-from-botfather
TELEGRAM_API_ID=your-api-id
TELEGRAM_API_HASH=your-api-hash
```

To get Telegram API credentials:
- Go to https://my.telegram.org
- Log in with your phone number
- Go to "API Development Tools"
- Create a new application (any name/description works)
- Copy the `api_id` and `api_hash`

### Required secrets

A few values in `.env` must be generated before the first start. Run these commands and paste the output into `.env`:

```bash
# Dashboard login password — bcrypt hash, not plaintext.
# Replace 'your-password-here' with the actual password you will log in with.
python -c "import bcrypt; print(bcrypt.hashpw(b'your-password-here', bcrypt.gensalt()).decode())"
```

```bash
# Three independent random secrets.
# DASHBOARD_SECRET_KEY signs the dashboard session cookie.
# DASHBOARD_INTERNAL_TOKEN is shared between bot and dashboard.
# RECLIP_API_TOKEN gates reclip's /api/* endpoints and must match for both services.
python -c "import secrets; print(secrets.token_urlsafe(32))"  # DASHBOARD_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"  # DASHBOARD_INTERNAL_TOKEN
python -c "import secrets; print(secrets.token_urlsafe(32))"  # RECLIP_API_TOKEN
```

4. Start the services:
```bash
docker-compose up -d
```

5. Send a video link to your bot on Telegram.

### First Run Note

The self-hosted Bot API server downloads some data from Telegram on first startup. This can take a minute. The bot will start responding once the Bot API server is ready.

## Configuration

All configuration is via environment variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | (required) | Telegram bot token from @BotFather |
| `TELEGRAM_API_ID` | (required) | Telegram API ID from my.telegram.org |
| `TELEGRAM_API_HASH` | (required) | Telegram API hash from my.telegram.org |
| `GHCR_OWNER` | (required) | GitHub username/org that owns the GHCR packages |
| `IMAGE_TAG` | latest | Image tag to pull (e.g. `latest` or `dev-<sha>`) |
| `MAX_CONCURRENT_DOWNLOADS` | 3 | Max parallel downloads |
| `DOWNLOAD_TIMEOUT` | 900 | Hard timeout per download (seconds) |
| `MAX_JOBS` | 1000 | Max in-memory download jobs tracked by reclip |
| `MAX_FILESIZE` | 2G | Largest file size reclip will accept (yt-dlp format) |
| `MIN_FREE_DISK_MB` | 1024 | Minimum free disk space reclip requires before starting a download |
| `CLEANUP_MAX_AGE_HOURS` | 1 | Delete files older than this |
| `CLEANUP_MAX_DISK_MB` | 5000 | Max disk usage before cleanup |
| `CLEANUP_INTERVAL_SECONDS` | 300 | Cleanup check interval |
| `RECLIP_URL` | `http://reclip:8899` | URL the bot uses to reach the reclip API |
| `DOWNLOADS_PATH` | `/downloads` | Shared volume path for completed downloads |
| `DASHBOARD_USER` | (required) | Dashboard login username (must not be `admin`) |
| `DASHBOARD_PASSWORD_HASH` | (required) | Bcrypt hash of the dashboard login password (see [Required secrets](#required-secrets)) |
| `DASHBOARD_SECRET_KEY` | (required) | Cookie signing key (see [Required secrets](#required-secrets)) |
| `DASHBOARD_INTERNAL_TOKEN` | (required) | Shared secret between bot and dashboard (see [Required secrets](#required-secrets)) |
| `RECLIP_API_TOKEN` | (required) | Shared secret for `/api/*` endpoints (see [Required secrets](#required-secrets)) |
| `MAX_ACTIVE_DOWNLOADS` | 256 | Maximum active download jobs tracked in the dashboard |
| `LOGIN_RATE_LIMIT` | `5/minute` | Dashboard login rate limit (slowapi format) |
| `DASHBOARD_PORT` | 8080 | Dashboard port on host |
| `DASHBOARD_SECURE_COOKIES` | true | Set `Secure` flag on dashboard cookies (set `false` only for plain HTTP in a trusted network) |
| `ALLOWED_USER_IDS` | (required) | Comma-separated Telegram user IDs allowed to use the bot (fail-closed if empty) |

> **Note:** `DASHBOARD_USER`, `DASHBOARD_PASSWORD_HASH`, and `DASHBOARD_SECRET_KEY` are passed to the dashboard container as `ADMIN_USER`, `ADMIN_PASSWORD_HASH`, and `SECRET_KEY` respectively. For local development without Docker, use the container-side names (`ADMIN_USER`, `ADMIN_PASSWORD_HASH`, `SECRET_KEY`).

## Admin Dashboard

The admin dashboard is available at http://localhost:8080 after starting the services. Log in with `DASHBOARD_USER` and the **plaintext password** you used to generate `DASHBOARD_PASSWORD_HASH`.

Pages:
- **Dashboard** — downloads today, active users, disk usage, error rate, charts
- **History** — full download log with filters and pagination
- **Errors** — failed downloads with error messages
- **Admin** — file management, system info, purge controls

## Web UI

The reclip web UI is available if you uncomment the `reclip-web` service in `docker-compose.yml`:

```yaml
reclip-web:
  extends:
    service: reclip
  ports:
    - "8899:8899"
```

Then access it at http://localhost:8899.

## How It Works

1. You send a URL to the bot
2. Bot sends "Fetching info..." immediately
3. Bot calls reclip's API to get video metadata
4. Bot displays thumbnail, title, platform, and format buttons (MP4/MP3)
5. You tap MP4 to see quality options (1080p, 720p, etc.) or MP3 for audio
6. Bot starts the download and shows real-time progress
7. Bot uploads the file to the Telegram chat
8. Cleanup task removes old files automatically

## Development

### Running locally (without Docker)

```bash
# Start reclip
cd reclip && pip install flask yt-dlp && python app.py &

# Start the bot
cd bot && pip install -r requirements.txt
BOT_TOKEN=your-token RECLIP_URL=http://localhost:8899 DOWNLOADS_PATH=../reclip/downloads python bot.py
```

### Project structure

```
reclip_bot/
├── docker-compose.yml      # 4 services: reclip, bot, telegram-bot-api, dashboard
├── .env.example             # Environment variables template
├── bot/
│   ├── bot.py               # Bot entry point
│   ├── handlers.py          # Telegram message/callback handlers
│   ├── reclip_client.py     # Async HTTP client for reclip API
│   ├── event_client.py      # Fire-and-forget events to dashboard
│   ├── cleanup.py           # Background file cleanup task
│   ├── requirements.txt
│   └── Dockerfile
├── dashboard/
│   ├── main.py              # FastAPI app with background tasks
│   ├── db.py                # SQLite queries (async via aiosqlite)
│   ├── auth.py              # Session cookie auth
│   ├── routes/              # API + page routes
│   ├── templates/           # Jinja2 templates (dark theme)
│   ├── static/              # CSS + Chart.js frontend
│   ├── requirements.txt
│   └── Dockerfile
└── reclip/                  # Fork of averygan/reclip with enhancements
    ├── app.py               # Flask API + web UI (with progress hooks)
    ├── Dockerfile
    ├── templates/
    └── static/
```

## Credits

- [reclip](https://github.com/averygan/reclip) by averygan - The media download engine
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - The download backend
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) - Telegram bot framework

## License

MIT
