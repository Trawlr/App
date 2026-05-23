# Trawlr

Trawlr is an open-source self-hosted data collection platform for Telegram data archival and analysis. Monitor multiple Telegram accounts, archive messages and media, track users and generate reports from a single web app.

## Features

- **Multi-Account Management** - Connect and manage multiple Telegram accounts with 2FA support, session storage, and per-account download concurrency limits
- **Real-Time Monitoring** - Long-lived Telegram connections capture messages, edits, and deletions as they happen
- **Message Archiving** - Full message history scanning with edit tracking, deletion detection and album grouping
- **Entity Extraction** - Automatically extract URLs, mentions, hashtags, emails, phone numbers, and code blocks from messages
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

- **Entity notifications** - Filter and alert on the detection of specified entities via webhook or API
- **Search improvements** - Apache Solr integration for faster full-text content search
- **Web UI fixes** - Ongoing usability and polish improvements (new UI)
- **Streamline setup process** - Improve Trawlr setup and account onboarding

## License

This project is open source. See [LICENSE](LICENSE) for details.
