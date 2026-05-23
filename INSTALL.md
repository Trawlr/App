# Installing Trawlr

Trawlr ships (HA!) as a small Docker Compose stack containing: the Django web app, Dramatiq workers, listening service, a scheduler, PostgreSQL, RabbitMQ and an nginx media proxy.

There are two supported installation paths:

1. **[Pre-built containers](#1-pre-built-containers-recommended)** *(recommended)*: pull official images from GitHub Container Registry. Fastest, no build required.
2. **[Build from source](#2-build-from-source)**: clone the repo and build the image locally. Use this if you want to modify the code or run an unreleased branch.

---

## Prerequisites

Both paths require:

- **Docker** 24+ and **Docker Compose** v2
- **8 GB RAM minimum** (16GB+ recommended)
- A Telegram **`api_id`** and **`api_hash`**. Create one at [my.telegram.org](https://my.telegram.org) → API development tools

---

## 1. Pre-built containers (recommended)

Container images are published to `ghcr.io/trawlr/trawlr` on every release tag.

### 1.1 Fetch the compose file and env template

```bash
mkdir trawlr && cd trawlr
curl -O https://raw.githubusercontent.com/Trawlr/App/main/docker-compose.prod.yml
curl -o .env https://raw.githubusercontent.com/Trawlr/App/main/.env.example
```

The nginx config is embedded directly inside `docker-compose.prod.yml`.

### 1.2 Configure the environment

Edit `.env` and replace every `<CHANGEME>` placeholder. At minimum set:

| Variable | What to set |
|---|---|
| `SECRET_KEY` | Generate with: `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"` |
| `POSTGRES_PASSWORD` | Strong random password |
| `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS` | Strong random credentials |
| `RABBITMQ_URL` | `amqp://<user>:<pass>@rabbitmq:5672//` — using the same user/pass from above |
| `ALLOWED_HOSTS` | Comma-separated list of hostnames/IPs you will reach Trawlr from |

### 1.3 Start the stack

```bash
docker compose -f docker-compose.prod.yml up -d
```

### 1.4 Create the admin user and verify Postgres extensions

```bash
docker compose -f docker-compose.prod.yml exec web \
    python manage.py setup --username <your-username> --password <your-secure-password>
```

The `setup` command verifies the `pg_trgm` extension is installed and creates a Django superuser.

### 1.5 Check the stack is healthy

```bash
docker compose -f docker-compose.prod.yml ps
```

You should see `web`, `downloader`, `concierge`, `processor`, `listener`, `scheduler`, `db`, `rabbitmq`, and `nginx` all running.

Skip to **[Post-install: connect a Telegram account](#post-install-connect-a-telegram-account)**.

---

## 2. Build from source

Use this path when you intend to modify Trawlr or test a branch that isn't yet released.

### 2.1 Clone the repository

```bash
git clone https://github.com/Trawlr/App.git
cd App
cp .env.example .env
```

### 2.2 Configure the environment

Edit `.env` and replace every `<CHANGEME>` placeholder (see the table in [1.2](#12-configure-the-environment) above).

### 2.3 Build and start the stack

```bash
docker compose -f docker-compose-dev.yml up -d --build
```

The first build takes a few minutes while python installs its dependencies.

### 2.4 Create the admin user

```bash
docker compose -f docker-compose-dev.yml exec web \
    python manage.py setup --username admin --password <your-secure-password>
```

### 2.5 Check the stack is healthy

```bash
docker compose -f docker-compose-dev.yml ps
```

### 2.6 Iterating on code

After editing source files, rebuild the affected service(s):

```bash
docker compose -f docker-compose-dev.yml up -d --build web downloader
```

The dev compose file exposes Postgres on `localhost:5432` so you can attach a SQL client directly.

---

## Post-install: connect a Telegram account

1. Open the web UI: **http://localhost:8000** (or whatever you put in `ALLOWED_HOSTS`).
2. Log in with the admin credentials you created with `manage.py setup`.
3. Go to **Accounts** → **Add Account**.
4. Enter the phone number (with country code) and the `api_id` / `api_hash` you got from my.telegram.org.
5. Complete the SMS code prompt — and a 2FA password if your account uses one.
6. Once authenticated, click **Sync Channels** to import the channels and groups the account is already a member of. You can also add specific targets by username or `t.me/...` URL.
7. From a channel detail page, configure media type preferences and run **Scan History** to start backfilling messages and files.

---

## Useful URLs

| Service | URL |
|---|---|
| Web UI | http://localhost:8000 |
| Django admin | http://localhost:8000/admin/ |
| API docs (Swagger) | http://localhost:8000/api/v1/docs/ |
| OpenAPI schema | http://localhost:8000/api/v1/schema |
| RabbitMQ management (disabled in production docker compose) | http://localhost:15672 |

---

## Production hardening

The default compose files are aimed at single-host self-hosted deployments, it is **NOT** recommended to expose Trawlr to the internet.

- Put it behind a **reverse proxy with TLS** (Caddy, nginx, Traefik)
- Set `DEBUG=False` and `SECURE_SSL_REDIRECT=True` in your `.env` file.
- Set `ALLOWED_HOSTS` to your real hostname only — never use `*`.
- Set `HTTPS_ENABLED=True` in the environment to enable HSTS, secure cookies, and the SSL proxy header.
- Restrict outbound connectivity as needed. Trawlr only connects to Telegram's MTProto endpoints (DCs) and your cloud storage provided of choice (if selected, local by default).
- Keep PostgreSQL and RabbitMQ on the internal Docker network unless you plan to harden them seperately (the production compose file does not publish their ports).

---

## Updating

### Pre-built containers

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

### From source

```bash
git pull
docker compose -f docker-compose-dev.yml up -d --build
```

---

## Need help?

- File an issue: https://github.com/Trawlr/App/issues
- Roadmap and feature status: see [README.md](README.md#roadmap)
