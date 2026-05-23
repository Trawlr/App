# FROM python:3.14-slim
FROM python:3.14.3-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Create directories for sessions and media
RUN mkdir -p /app/static /app/media /data/trawlr

# Collect static files (initial collection, entrypoint will refresh)
RUN python manage.py collectstatic --noinput --clear || true

# Expose port
EXPOSE 8000

# Default command - run with daphne for ASGI/WebSocket support
CMD ["daphne", "--proxy-headers", "-b", "0.0.0.0", "-p", "8000", "trawlr.asgi:application"]
