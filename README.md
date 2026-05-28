# Trawlr

Trawlr is an open-source self-hosted data collection platform for Telegram data archival and analysis. Monitor multiple Telegram accounts, archive messages and media, track users and generate reports from a single web app.

## Features

- **Multi-Account Management** - Connect and manage multiple Telegram accounts with 2FA support, session storage, and per-account download concurrency limits
- **Real-Time Monitoring** - Long-lived Telegram connections capture messages, edits, and deletions as they happen
- **Message Archiving** - Full message history scanning with edit tracking, deletion detection and album grouping
- **Entity Extraction** - Automatically extract URLs, mentions, hashtags, emails, phone numbers, and code blocks from messages
- **Entity Notifications** - Watch for and notify on detected entities (URL, domain, hashtag, @mention, phone, etc) in message. Configure either a webhook (HMAC-signed) or RabbitMQ queue as the notification sink.
- **User OSINT** - Track users across channels with profile data, group memberships, activity metrics, and username history
- **Download Queue** - Priority-based download system with concurrent slots, progress tracking, automatic retries, and SHA256 deduplication via hardlinks
- **Full-Text Search** - PostgreSQL-powered search with boolean operators, field filters, date ranges, phrase matching, and CSV export
- **Analytics & Reports** - Content trends, user intelligence, source analytics, and investigation dashboards with export capabilities
- **REST API** - Full API with OpenAPI/Swagger documentation, token authentication, and filtering
- **Real-Time UI** - WebSocket-powered live updates, download progress streaming, and HTMX-driven dynamic pages

## Architecture
Each service is its own container.

| Service | Role |
|---------|------|
| **web** | Django + Daphne ASGI server |
| **downloader** | Downloads items that are sent to the queue for processing |
| **concierge** | History scans and member scans (single threaded) |
| **processor** | Processes incoming Telegram events from listener |
| **notifier** | Delivers entity-notification matches to user-configured webhooks or RabbitMQ queues |
| **listener** | Maintains persistent Telegram connections, publishes events to RabbitMQ |
| **scheduler** | APScheduler - triggers periodic tasks (sync, stats, recovery) |
| **nginx** | Reverse proxy for serving media through the file manager. Optional otherwise |
| **db** | PostgreSQL 18 |
| **rabbitmq** | Message broker for task queues and event pub/sub |

## Tech Stack

- **Backend:** Django, Django REST Framework, Django Channels, Daphne
- **Task Queue:** Dramatiq + RabbitMQ
- **Telegram:** Telethon
- **Database:** PostgreSQL with full-text search (GIN indexes)
- **Frontend:** Django Templates, HTMX, Bootstrap
- **Infrastructure:** Docker, Docker Compose, Nginx

## Installation

See **[INSTALL.md](INSTALL.md)** for full setup instructions. Two paths are supported:

- **Pre-built containers** — pull from `ghcr.io/trawlr/trawlr` (recommended)
- **Build from source** — clone and `docker compose -f docker-compose-dev.yml up -d --build`

## Configuration

All configuration is done through environment variables. See [.env.example](.env.example) for the full list.

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | Django secret key |
| `POSTGRES_PASSWORD` | Database password |
| `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS` | RabbitMQ credentials |
| `RABBITMQ_URL` | AMQP connection string |
| `TRAWLR_STORAGE_ROOT` | Path for downloaded media (default: `/data/trawlr`) |
| `ALLOWED_HOSTS` | Comma-separated list of allowed hostnames |
| `DEBUG` | Set to `False` in production |
| `SECURE_SSL_REDIRECT` | Set to `True` when using HTTPS |

Scheduler intervals, event processing settings, download concurrency, and other runtime options are configurable through **Global Settings** in the web UI.

## Deployment

| File | Use Case |
|------|----------|
| `docker-compose.prod.yml` | Production deployment using pre-built container images |
| `docker-compose-dev.yml` | Local development (builds from source) |
| `docker-compose.dokploy.yml` | Dokploy cloud deployment for advanced users |

Container images are automatically built and pushed to `ghcr.io/trawlr/trawlr` with semantic versioning based on commit prefixes (`fix:`, `feat:`, `major:`).

## Getting Started

Once Trawlr is deployed and reachable in a browser, work through the steps below to onboard your first data source and start collecting.

### 1. Create the admin user

If you didn't bootstrap a superuser during install, exec into the `web` container and run:

```bash
docker compose exec web python manage.py createsuperuser
```

Log in at `/` with those credentials. The first user is also used to own Telegram accounts you connect below.

### 2. Connect a Telegram account

Trawlr uses your own Telegram user accounts to read channels — not a bot.

1. Generate an API ID + hash at <https://my.telegram.org> → **API development tools**.
2. In Trawlr, go to **Accounts → Add Account** and enter the phone number, API ID, and API hash.
3. You'll be prompted to enter the login code Telegram sends to that number, and a 2FA password if one is set on the account.

Once authenticated the account row will show a green status. You can connect multiple accounts; each runs its own listener and has its own download concurrency limit (set on the account's settings page).

### 3. Onboard a data source

A "source" in Trawlr is any channel, group, or supergroup you want to archive.

- **Already a member?** Open the account, hit **Sync Channels**, and Trawlr will import every dialog the account can see. From the **Sources** list you can then enable collection on the ones you care about.
- **Not a member yet?** Use **Join Channel** (per-account or from the dashboard) with an invite link, `t.me/...` URL, or `@username`. Trawlr will join, sync dialogs, and run the standard onboarding tasks for the new source. Public channels are joined directly; private invite links are honored.

After a source appears, open **Source → Config** to choose what to collect:

- **Archive messages** — store text, edits, deletions, and extracted entities (URLs, mentions, hashtags, etc.).
- **Auto-download** — toggle per file type (photos, videos, files) with a priority order and a per-source priority (1–10) used by the queue scheduler.
- **Thumbnails** — download lightweight previews even when full media isn't being grabbed, so the UI is browsable.
- **Deduplication** — switch to SHA256 to hardlink duplicate files instead of storing copies.
- **Monitor / Pause / Bypass listener** — switches to live-track the source, pause its downloads, or skip real-time event processing.

### 4. Backfill history

The listener only captures *new* messages from the moment it starts. To bring in prior content, open a source and click **Scan History**. The `concierge` service walks the channel in order and queues messages (and downloads, if auto-download is on) according to that source's config. You can also use **Scan Members** on groups/supergroups to populate the user OSINT graph.

### 5. Tune auto-downloading globally

Settings → **Global Settings** controls instance-wide behavior. The fields most relevant to a fresh deployment:

| Setting | What it does |
|---------|--------------|
| `download_queue_interval` | How often the scheduler drains the download queue (default 10s) |
| `channel_sync_interval` | How often Trawlr re-syncs each account's dialog list |
| `channel_stats_interval` / `media_counts_interval` | Refresh member counts and per-source media totals |
| `availability_check_interval` | Detect deleted/banned channels |
| `forum_topics_sync_interval` | Re-pull topic lists for forum-style supergroups |
| `member_sync_interval` | Periodically refresh group member lists |
| `stuck_task_recovery_interval` | Re-queue tasks that have been stuck for too long |
| `event_processing_enabled` | Master switch — turn off to pause the listener pipeline without stopping services |
| `storage_root` / `filename_format` | Where downloaded media lives and how files are named on disk |

Per-account download concurrency is set on the **Account** page, not globally — raise it cautiously to avoid Telegram rate limits.

### 6. Set up entity notifications (optional)

To get alerted when a specific URL, domain, hashtag, mention, phone number, or email appears anywhere in a monitored source:

1. Go to **Notifications → Watchlist → Add Entry**.
2. Pick the **entity type** and the exact **entity value** to watch (e.g. hashtag `#blackmarket`, domain `example.com`).
3. Choose **mode** — `every` fires on every match, `new` fires only the first time.
4. Configure a sink:
   - **Webhook** — HMAC-signed POST to a URL of your choice. Set `secret` for signature verification.
   - **RabbitMQ** — publish to a queue/exchange/routing key. Useful when you want another service in your stack to consume matches directly.
5. Optionally set a **cooldown** in seconds to suppress repeated matches.

Deliveries are retried automatically; failed deliveries land in the **Deliveries** tab where they can be requeued or inspected.

### 7. Exclude noisy users (optional)

If certain users are flooding your sources (bots, spammers), add them under **Settings → Exclusions**. Exclusions can be global (every source) or per-source, and the listener will silently drop their messages before processing — useful for keeping storage and notifications focused.

### 8. Verify the pipeline is healthy

Worth checking after the first source is collecting:

- **Dashboard** — should show recent activity, queued/active downloads, and per-account listener status.
- **Tasks** page — queued, running, and failed task runs. Look here first if a scan or download seems stuck.
- **Ops → Queues** — RabbitMQ queue depths for each worker. Sustained backlogs usually mean a worker container needs more concurrency or has crashed.
- **Settings → Dead Letters** — anything ending up here failed all retries; requeue or purge from this page.

At this point new messages, media, and entities will start flowing in as the listener picks them up, and any history scans you started will catch up in the background.

## API

Trawlr provides a REST API with token authentication. Generate an API token from the web UI under account settings.

**Endpoints:**

- `/api/v1/accounts` - Telegram account management
- `/api/v1/channels` - Channel and source data
- `/api/v1/messages` - Archived messages with full-text search
- `/api/v1/files` - Downloaded files
- `/api/v1/users` - Telegram user data
- `/api/v1/entities` - Extracted entities (URLs, mentions, hashtags, etc.)
- `/api/v1/tags` - Tag management
- `/api/v1/resolve` - Resolve Telegram links to entity metadata
- `/api/v1/settings` - Global configuration
- `/api/v1/stats` - System statistics

Swagger UI is available at `/api/v1/docs`, ReDoc at `/api/v1/redoc`, and the raw OpenAPI schema at `/api/v1/schema`.

## Roadmap

- **Search improvements** - Apache Solr integration for faster full-text content search
- **Web UI fixes** - Ongoing usability and polish improvements (new UI)
- **Streamline setup process** - Improve Trawlr setup and account onboarding

## License

This project is open source. See [LICENSE](LICENSE) for details.
