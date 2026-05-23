"""
Custom Dramatiq middleware for Trawlr.

Referenced by string path from DRAMATIQ_BROKER["MIDDLEWARE"] in settings.py.
django-dramatiq's AppConfig.ready() imports these at broker bootstrap time.
"""

import logging

from dramatiq.middleware import Middleware, Retries, TimeLimit

logger = logging.getLogger(__name__)


class DjangoDbConnectionMiddleware(Middleware):
    """
    Close Django DB connections around each actor run.

    Equivalent to django-dramatiq's DbConnectionsMiddleware except that
    after_process_message uses connections.close_all() instead of
    close_old_connections(). This is critical for gevent workers: with 20
    greenlets each holding a "fresh" (sub-CONN_MAX_AGE) connection,
    close_old_connections() never actually releases anything and the
    PostgreSQL pool drains until connections are exhausted.

    close_all() is safe here because it runs AFTER the task completes.
    """

    def before_process_message(self, broker, message):
        from django.db import close_old_connections
        close_old_connections()

    def after_process_message(self, broker, message, *, result=None, exception=None):
        from django.db import connections
        connections.close_all()

    def after_skip_message(self, broker, message):
        from django.db import close_old_connections
        close_old_connections()


class StartupRecoveryMiddleware(Middleware):
    """
    On worker boot, mark TaskRun rows stuck in running/downloading as failed.

    Trawlr-specific (operates on the app's TaskRun table). No django-dramatiq
    equivalent. Runs once per worker process, before any actors are consumed.
    """

    def before_worker_boot(self, broker, worker):
        logger.info("Worker starting - running stuck task recovery...")
        try:
            from tasks import recover_stuck_tasks
            recovered = recover_stuck_tasks()
            if recovered['task_runs'] or recovered['download_tasks']:
                logger.info(
                    f"Startup recovery: {recovered['task_runs']} TaskRuns failed, "
                    f"{recovered['download_tasks']} DownloadTasks reset"
                )
            else:
                logger.info("Startup recovery: No stuck tasks found")
        except Exception as e:
            logger.error(f"Startup recovery failed: {e}")


class ClientPoolShutdownMiddleware(Middleware):
    """
    Tear down the shared Telethon client pool on worker shutdown.

    Prevents orphaned Telegram connections and "Event loop is closed" errors
    when workers are stopped/replaced.
    """

    def after_worker_shutdown(self, broker, worker):
        logger.info("Worker shutting down - cleaning up client pool...")
        try:
            from accounts.client_pool import client_pool
            client_pool.shutdown()
        except Exception as e:
            logger.error(f"Client pool shutdown failed: {e}")


class TrawlrTimeLimit(TimeLimit):
    """TimeLimit pre-configured for 4-hour long-running scans."""

    def __init__(self):
        super().__init__(time_limit=14400000)  # 4 hours in ms


class TrawlrRetries(Retries):
    """Retries pre-configured with Trawlr's backoff schedule."""

    def __init__(self):
        super().__init__(max_retries=3, min_backoff=60000, max_backoff=300000)
