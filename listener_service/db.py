"""
Database utilities for the listener service.

Provides wrappers around sync_to_async that properly close Django database
connections after each operation. This prevents connection pool exhaustion
when running async code with Django ORM.

IMPORTANT: We use connections.close_all() instead of close_old_connections()
because close_old_connections() only closes connections past their CONN_MAX_AGE.
With high throughput, connections are constantly "fresh" and never get closed.
close_all() forcefully closes ALL connections after each operation.
"""

import functools
from asgiref.sync import sync_to_async
from django.db import connections


def sync_to_async_with_cleanup(func):
    """
    Decorator that wraps a sync function for async use with DB cleanup.

    This is a drop-in replacement for @sync_to_async that ensures Django
    database connections are closed after each call. Without this, each
    thread in the async executor pool can hold a connection indefinitely,
    eventually exhausting PostgreSQL's connection pool.

    Usage:
        @sync_to_async_with_cleanup
        def get_account(pk):
            return TelegramAccount.objects.get(pk=pk)

        # Then call it:
        account = await get_account(123)
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        @sync_to_async
        def inner():
            try:
                return func(*args, **kwargs)
            finally:
                connections.close_all()
        return await inner()
    return wrapper


async def run_sync(func, *args, **kwargs):
    """
    Run a sync function in an async context with DB cleanup.

    This is for one-off calls where you don't want to define a decorated function.

    Usage:
        account = await run_sync(
            TelegramAccount.objects.get,
            pk=123
        )

        # Or with lambdas for more complex queries:
        accounts = await run_sync(
            lambda: list(TelegramAccount.objects.filter(is_active=True))
        )
    """
    @sync_to_async
    def inner():
        try:
            return func(*args, **kwargs)
        finally:
            connections.close_all()
    return await inner()


async def run_sync_callable(callable_func):
    """
    Run a callable (like a lambda) in an async context with DB cleanup.

    Useful when you need to chain multiple ORM operations or use complex queries.

    Usage:
        accounts = await run_sync_callable(
            lambda: list(TelegramAccount.objects.filter(is_active=True))
        )

        # Or with a nested function:
        async def get_config():
            def _inner():
                channel = TelegramChannel.objects.get(pk=123)
                return channel.config
            return await run_sync_callable(_inner)
    """
    @sync_to_async
    def inner():
        try:
            return callable_func()
        finally:
            connections.close_all()
    return await inner()
