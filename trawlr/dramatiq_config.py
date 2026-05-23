"""
Django bootstrap shim for Dramatiq workers.

Workers launched via the raw ``dramatiq``/``dramatiq-gevent`` CLI don't know
about Django. Modules that register actors (``tasks/base.py``,
``events/processor.py``) import this module at the top to ensure
``django.setup()`` has run — which in turn triggers
``django_dramatiq.apps.DjangoDramatiqConfig.ready()``, which reads
``DRAMATIQ_BROKER`` from settings and configures the RabbitMQ broker
(including AdminMiddleware + all Trawlr custom middleware).

The broker setup and custom middleware classes used to live here; they have
moved to ``trawlr/dramatiq_middleware.py`` and ``settings.py``.
"""

import os

# Set Django settings before importing anything that might use Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'trawlr.settings')

# Allow sync DB operations in async context (required for gevent pool)
os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')

import django
from django.apps import apps as _apps

# Guard against re-entrant setup: when the `tasks` package is loaded as a
# Django app (for rundramatiq discovery), this module gets imported during
# apps.populate() — calling django.setup() again there raises
# "populate() isn't reentrant". Only set up if apps aren't ready/loading.
if not _apps.ready and not _apps.loading:
    django.setup()


# --- Queue name constants (still imported elsewhere as a convenience) ---
QUEUE_DEFAULT = 'trawlr.default'
QUEUE_DOWNLOADS = 'trawlr.downloads'
QUEUE_SCANS_HISTORY = 'trawlr.scans.history'
QUEUE_SCANS_MEMBERS = 'trawlr.scans.members'
QUEUE_EVENTS = 'trawlr.events.telegram'
