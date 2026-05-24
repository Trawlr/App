"""
Django settings for trawlr project.
"""

import os
from pathlib import Path
from django.contrib.messages import constants as message_constants
from dotenv import load_dotenv
load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    raise ValueError(
        "SECRET_KEY environment variable is required. "
        "Generate one with: python -c \"from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())\""
    )

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DEBUG', 'False').lower() in ('true', '1', 'yes')
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

# Production security settings (enabled when DEBUG=False)
if not DEBUG:
    # Whether HTTPS is enabled (set HTTPS_ENABLED=True when serving over SSL)
    _https = os.environ.get('HTTPS_ENABLED', 'False').lower() in ('true', '1', 'yes')

    # HTTPS/SSL settings - trust X-Forwarded-Proto from reverse proxy (Traefik/Dokploy)
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', 'False').lower() in ('true', '1', 'yes')

    # HTTP Strict Transport Security (only when HTTPS is enabled)
    if _https:
        SECURE_HSTS_SECONDS = 31536000  # 1 year
        SECURE_HSTS_INCLUDE_SUBDOMAINS = True
        SECURE_HSTS_PRELOAD = True

    # Cookie security (Secure flag requires HTTPS)
    SESSION_COOKIE_SECURE = _https
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    CSRF_COOKIE_SECURE = _https
    CSRF_COOKIE_HTTPONLY = True
    CSRF_COOKIE_SAMESITE = 'Lax'

    # Other security headers
    X_FRAME_OPTIONS = 'DENY'
    SECURE_CONTENT_TYPE_NOSNIFF = True

# CSRF trusted origins
_csrf_env = os.environ.get('CSRF_TRUSTED_ORIGINS', '')
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_env.split(',') if o.strip()] if _csrf_env else []
for host in ALLOWED_HOSTS:
    h = host.strip()
    if h and h != '*':
        CSRF_TRUSTED_ORIGINS.append(f'https://{h}')
        CSRF_TRUSTED_ORIGINS.append(f'http://{h}')

# Custom CSRF failure view with diagnostic logging
CSRF_FAILURE_VIEW = 'trawlr.views.csrf_failure'


# Application definition
INSTALLED_APPS = [
    # Django core
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'django.contrib.postgres',

    # Third-party
    'django_bootstrap5',
    'channels',
    'django_htmx',
    'simple_history',
    'django_dramatiq',

    # REST API
    'rest_framework',
    'rest_framework.authtoken',
    'django_filters',
    'drf_spectacular',

    # Local apps
    'accounts',
    'audit',
    'downloads',
    'dashboard',
    'api',
    'events',
    'search',
    'reports',
    'graph',
    'storage',
    'ops',
    'notifications',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django_htmx.middleware.HtmxMiddleware',
    'simple_history.middleware.HistoryRequestMiddleware',
    'csp.middleware.CSPMiddleware',
]

# Content Security Policy (CSP)
CSP_DEFAULT_SRC = ("'self'",)
CSP_SCRIPT_SRC = ("'self'", "'unsafe-inline'", "https://unpkg.com", "https://cdn.jsdelivr.net")
CSP_STYLE_SRC = ("'self'", "'unsafe-inline'", "https://fonts.googleapis.com", "https://cdn.jsdelivr.net")
CSP_FONT_SRC = ("'self'", "https://fonts.gstatic.com", "https://cdn.jsdelivr.net")
CSP_IMG_SRC = ("'self'", "data:")
CSP_CONNECT_SRC = ("'self'", "ws:", "wss:")

ROOT_URLCONF = 'trawlr.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'trawlr.context_processors.sidebar_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'trawlr.wsgi.application'
ASGI_APPLICATION = 'trawlr.asgi.application'


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('POSTGRES_DB', 'trawlr'),
        'USER': os.environ.get('POSTGRES_USER', 'trawlr'),
        'PASSWORD': os.environ.get('POSTGRES_PASSWORD', 'trawlr'),
        'HOST': os.environ.get('POSTGRES_HOST', 'localhost'),
        'PORT': os.environ.get('POSTGRES_PORT', '5432'),
        'CONN_MAX_AGE': 5, # long CONN_MAX_AGE can exhaust connections
        'CONN_HEALTH_CHECKS': True,
    }
}

# RabbitMQ configuration (external broker)
RABBITMQ_URL = os.environ.get('RABBITMQ_URL', 'amqp://guest:guest@localhost:5672//')

# channels require URL-encoded vhost (%2F not //)
# Only replace the trailing // (vhost), not the :// after protocol
RABBITMQ_URL_CHANNELS = RABBITMQ_URL[:-2] + '/%2F' if RABBITMQ_URL.endswith('//') else RABBITMQ_URL

# RabbitMQ credentials and host (for management API and other direct access)
RABBITMQ_HOST = os.environ.get('RABBITMQ_HOST', 'rabbitmq')
RABBITMQ_USER = os.environ.get('RABBITMQ_DEFAULT_USER', 'guest')
RABBITMQ_PASSWORD = os.environ.get('RABBITMQ_DEFAULT_PASS', 'guest')
RABBITMQ_MANAGEMENT_PORT = int(os.environ.get('RABBITMQ_MANAGEMENT_PORT', '15672'))
RABBITMQ_MANAGEMENT_URL = f'http://{RABBITMQ_HOST}:{RABBITMQ_MANAGEMENT_PORT}/api'

# Dramatiq broker configuration (consumed by django_dramatiq.apps.DjangoDramatiqConfig.ready())
# Pika interprets a trailing '//' as empty vhost instead of default '/'; normalize to a single '/'.
_dramatiq_broker_url = RABBITMQ_URL[:-1] if RABBITMQ_URL.endswith('//') else RABBITMQ_URL
DRAMATIQ_BROKER = {
    'BROKER': 'dramatiq.brokers.rabbitmq.RabbitmqBroker',
    'OPTIONS': {
        'url': _dramatiq_broker_url,
        'confirm_delivery': True,
    },
    'MIDDLEWARE': [
        # DjangoDbConnectionMiddleware MUST be first so DB connections are
        # hygiened before any other middleware (including AdminMiddleware)
        # issues queries.
        'trawlr.dramatiq_middleware.DjangoDbConnectionMiddleware',
        'dramatiq.middleware.AgeLimit',
        'trawlr.dramatiq_middleware.TrawlrTimeLimit',
        'dramatiq.middleware.Callbacks',
        'dramatiq.middleware.Pipelines',
        'trawlr.dramatiq_middleware.TrawlrRetries',
        'trawlr.dramatiq_middleware.StartupRecoveryMiddleware',
        'trawlr.dramatiq_middleware.ClientPoolShutdownMiddleware',
        'django_dramatiq.middleware.AdminMiddleware',
    ],
}

# Keep the admin Task model rows for a week; cleanup is scheduled from
# trawlr.scheduler via the delete_old_tasks management command.
DRAMATIQ_TASKS_DATABASE = 'default'

# NOTE on rundramatiq: we keep the raw `dramatiq`/`dramatiq-gevent` launcher
# in compose files rather than switching to `python manage.py rundramatiq`.
# Reason: rundramatiq's actor auto-discovery iterates INSTALLED_APPS looking
# for ``<app>.<module>`` submodules, which would require ``tasks`` to be a
# registered Django app. Doing so broke because ``tasks/__init__.py`` eagerly
# imports actor modules which reference auth models before Django's app
# registry is ready (AppRegistryNotReady). The raw CLI works correctly with
# django-dramatiq — `trawlr.dramatiq_config` triggers `django.setup()`, which
# loads `django_dramatiq` whose AppConfig configures the broker with
# AdminMiddleware before any actor is registered.


# Django Channels configuration (using RabbitMQ)
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_rabbitmq.core.RabbitmqChannelLayer',
        'CONFIG': {
            'host': RABBITMQ_URL_CHANNELS,
        },
    },
}


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [
    BASE_DIR / 'static',
]

# WhiteNoise configuration for serving static files with ASGI servers like Daphne
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
}


# Media files (user uploads, downloaded files)
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'


# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Authentication settings
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = 'login'


# Message framework settings
MESSAGE_TAGS = {
    message_constants.DEBUG: 'info',
    message_constants.INFO: 'info',
    message_constants.SUCCESS: 'success',
    message_constants.WARNING: 'warning',
    message_constants.ERROR: 'danger',
}


# Trawlr-specific settings
TRAWLR_STORAGE_ROOT = os.environ.get('TRAWLR_STORAGE_ROOT', '/data/trawlr')
TRAWLR_SESSION_DIR = BASE_DIR / 'sessions'

# Notifications: allow private/loopback IPs as webhook destinations (testing only).
NOTIFICATIONS_SSRF_ALLOW_PRIVATE = (
    os.environ.get('NOTIFICATIONS_SSRF_ALLOW_PRIVATE', 'False').lower() == 'true'
)


# Django Silk (performance profiling)
INSTALLED_APPS += ['silk']

if DEBUG:
    MIDDLEWARE.insert(
        MIDDLEWARE.index('django.contrib.auth.middleware.AuthenticationMiddleware') + 1,
        'silk.middleware.SilkyMiddleware',
    )
    SILKY_PYTHON_PROFILER = False
    SILKY_MAX_RECORDED_REQUESTS = 10_000
    SILKY_MAX_RECORDED_REQUESTS_CHECK_PERCENT = 10


# Logging configuration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': os.environ.get('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
        'trawlr': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        # Suppress CancelledError noise from async operations (client disconnects)
        'asyncio': {
            'level': 'WARNING',
        },
    },
}


# Django REST Framework configuration
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'djangorestframework_camel_case.render.CamelCaseJSONRenderer',
        'djangorestframework_camel_case.render.CamelCaseBrowsableAPIRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'djangorestframework_camel_case.parser.CamelCaseJSONParser',
        'djangorestframework_camel_case.parser.CamelCaseFormParser',
        'djangorestframework_camel_case.parser.CamelCaseMultiPartParser',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'api.authentication.BearerTokenAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/hour',
        'user': '10000/hour',
    },
}

# DRF Spectacular (OpenAPI schema) configuration
SPECTACULAR_SETTINGS = {
    'TITLE': 'Trawlr API',
    'DESCRIPTION': 'REST API for Trawlr: an OSINT tool for telegram.',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'CAMELIZE_NAMES': True,
    'POSTPROCESSING_HOOKS': [
        'drf_spectacular.contrib.djangorestframework_camel_case.camelize_serializer_fields',
    ],
    'ENUM_NAME_OVERRIDES': {
        'FileTypeEnum': 'downloads.models.DownloadedFile.FILE_TYPE_CHOICES',
    },
}
