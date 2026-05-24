from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = 'notifications'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):  # pragma: no cover
        from . import signals  # noqa: F401
