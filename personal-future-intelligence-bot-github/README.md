# Personal Future Intelligence News Publisher

A Python service that monitors configured RSS and Atom feeds, filters for useful education, science, technology, funding, and career stories, verifies candidate articles against their publishers, and publishes qualifying items to one Telegram channel.

The channel is intended for news posts only. Diagnostics run locally and do not post to Telegram.

## Requirements

- Python 3.11 or newer
- A Telegram bot token
- A Telegram channel where the bot has permission to post
- Persistent writable storage for the SQLite database
- Network access to Telegram and the enabled feed publishers

The project uses Python's standard library and has no mandatory paid API dependency.

## Configure

1. Copy `.env.example` to `.env`.
2. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_DESTINATION_CHANNEL_ID` in `.env`. Keep that file private.
3. Add the bot to the destination channel as an administrator with permission to post.
4. Review `sources.json` and the enabled sources before starting.

Never commit `.env`, a real bot token, or the local database. The included `.gitignore` excludes these files.

## Run on Windows

In PowerShell, from the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
python bot.py --verify-destination
.\Start-NewsPublisher.ps1
```

Use `.Stop-NewsPublisher.ps1` to stop the background publisher. Keep only one running copy for a given Telegram bot token.

## Run on Linux with systemd

The `deploy/future-intelligence-bot.service` file is a systemd template. Review its paths and service account before installing. Store secrets in a private environment file outside the repository (the template expects `/etc/future-bot.env`). Set `BOT_DATABASE` to a persistent writable path, such as `/var/lib/future-bot/intelligence.sqlite3`, and make sure the service account can write there.

## Deploy as a Render Web Service

This project can expose a minimal health endpoint on the `PORT` supplied by Render while the news publisher runs. Configure the service with:

- Root Directory: `personal-future-intelligence-bot-github`
- Build Command: `echo "No third-party packages"`
- Start Command: `python bot.py --enable-monitoring && python bot.py`
- Uptime monitor URL: `https://YOUR-RENDER-SERVICE.onrender.com/health`

The `/health` response only confirms the process is responding; check Render logs for feed scans and publishing. Free Render services use ephemeral storage, so SQLite state can be lost when Render restarts or spins down the service. Use persistent storage if you need durable checkpoints and queue recovery.

## Safe checks

```text
python bot.py --check-sources
python bot.py --verify-destination
python bot.py --dry-run --dry-run-sample-size 5
python -m unittest discover -s tests -q
```

`--dry-run` is read-only and never publishes. On a source's first scan, its existing entries are recorded as a baseline and withheld; later scans consider new entries that pass the freshness, relevance, and verification checks.

The SQLite database stores monitoring state, feed checkpoints, deduplication records, and the publishing queue. Keep it on persistent storage when deploying or moving the service.

