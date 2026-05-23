"""
Per-account Telethon client pool for reusing TelegramClient connections.

Manages persistent OS threads (via gevent.threadpool.ThreadPool) with
per-thread asyncio event loops and TelegramClient instances. This avoids
creating/destroying clients for every download task.

Only used by download tasks (download_file, download_thumbnail,
download_profile_photo). Long-running tasks (scans, syncs) continue
to use TelegramService directly.
"""

import _thread
import asyncio
import logging

from gevent.threadpool import ThreadPool
from telethon import TelegramClient
from telethon.errors import AuthKeyDuplicatedError, AuthKeyUnregisteredError
from telethon.sessions import StringSession

logger = logging.getLogger('trawlr.client_pool')


class _AccountWorker:
    """
    Manages a ThreadPool and per-thread TelegramClients for a single account.

    Each OS thread in the pool maintains its own asyncio event loop and
    TelegramClient. Threads are created on demand by gevent's ThreadPool.
    """

    def __init__(self, account_id, api_id, api_hash, session_string, max_threads, on_change=None):
        self._account_id = account_id
        self._api_id = int(api_id)
        self._api_hash = api_hash
        self._session_string = session_string
        self._pool = ThreadPool(maxsize=max_threads)
        # dict[int -> (asyncio.EventLoop, TelegramClient)]
        # Keyed by _thread.get_ident(). Safe: each thread only accesses its own entry.
        self._thread_clients = {}
        self._invalidated = False
        self._on_change = on_change

    def execute(self, coro_factory):
        """
        Execute an async operation using a pooled TelegramClient.

        Args:
            coro_factory: async def fn(client) -> result

        Returns:
            The result of the coroutine.

        Raises:
            Any exception from the coroutine, including FloodWaitError.
        """
        return self._pool.apply(self._run_in_thread, args=(coro_factory,))

    def _run_in_thread(self, coro_factory):
        """Runs inside a real OS thread from the ThreadPool."""
        tid = _thread.get_ident()

        loop, client = self._get_or_create_client(tid)

        try:
            return loop.run_until_complete(coro_factory(client))
        except (AuthKeyDuplicatedError, AuthKeyUnregisteredError):
            # Auth key invalid — destroy client so next task gets a fresh one
            self._remove_thread_client(tid)
            raise
        except (ConnectionError, OSError):
            # Connection dropped mid-task — destroy client to avoid corrupted
            # internal state (send/recv loops, DC sender cache). The Dramatiq
            # task will retry and get a fresh client.
            logger.warning(f"Pool: Connection error for account {self._account_id}, destroying client")
            self._remove_thread_client(tid)
            raise

    def _get_or_create_client(self, tid):
        """Get existing loop+client for this thread, or create new ones. Reconnects if idle-disconnected."""
        entry = self._thread_clients.get(tid)
        if entry is None:
            # First use of this thread — create loop + client
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            client = self._create_client()
            try:
                loop.run_until_complete(client.connect())
            except Exception:
                # Connect failed — clean up the loop so we don't leak it
                try:
                    loop.close()
                except Exception:
                    pass
                raise
            self._thread_clients[tid] = (loop, client)
            logger.info(f"Pool: Created client for account {self._account_id} on thread {tid}")
            self._notify_change()
            return loop, client

        loop, client = entry
        asyncio.set_event_loop(loop)

        if not client.is_connected():
            # Telegram dropped the idle connection — try to reconnect
            try:
                loop.run_until_complete(client.connect())
                logger.debug(f"Pool: Reconnected client for account {self._account_id}")
            except Exception:
                # Reconnect failed — destroy and recreate from scratch
                logger.warning(f"Pool: Reconnect failed for account {self._account_id}, recreating client")
                self._remove_thread_client(tid)
                return self._get_or_create_client(tid)

        return loop, client

    def _create_client(self):
        """Create a new TelegramClient (not yet connected)."""
        return TelegramClient(
            StringSession(self._session_string),
            self._api_id,
            self._api_hash,
            device_model="Trawlr",
            system_version="1.0",
            app_version="1.0",
            timeout=30,
            request_retries=2,
            connection_retries=2,
            receive_updates=False,
        )

    def _remove_thread_client(self, tid):
        """Remove and disconnect a thread's client."""
        entry = self._thread_clients.pop(tid, None)
        if entry:
            loop, client = entry
            try:
                if client.is_connected():
                    loop.run_until_complete(client.disconnect())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            self._notify_change()

    def _notify_change(self):
        """Notify the parent pool that connection state changed."""
        if self._on_change:
            try:
                self._on_change()
            except Exception as e:
                logger.warning(f"Pool: Stats update failed: {e}")

    def invalidate_all(self):
        """Disconnect and remove all clients. Called on auth errors or session changes."""
        self._invalidated = True
        for tid in list(self._thread_clients.keys()):
            self._remove_thread_client(tid)

    def shutdown(self):
        """Disconnect all clients and kill the ThreadPool."""
        self.invalidate_all()
        try:
            self._pool.kill()
        except Exception:
            pass
        logger.info(f"Pool: Shut down worker for account {self._account_id}")


class ClientPool:
    """
    Module-level singleton managing per-account _AccountWorker instances.
    """

    def __init__(self):
        # dict[int -> _AccountWorker] keyed by account.pk
        self._workers = {}
        # _thread.allocate_lock() — real OS lock, not gevent-patched
        self._lock = _thread.allocate_lock()

    def execute(self, account, coro_factory):
        """
        Execute an async operation using a pooled client for the given account.

        Args:
            account: TelegramAccount model instance
            coro_factory: async def fn(client) -> result

        Returns:
            Result of the coroutine.
        """
        worker = self._get_or_create_worker(account)
        return worker.execute(coro_factory)

    def _get_or_create_worker(self, account):
        """Get existing worker or create a new one for the account."""
        with self._lock:
            worker = self._workers.get(account.pk)
            if worker is None or worker._invalidated:
                if worker is not None:
                    worker.shutdown()
                worker = _AccountWorker(
                    account_id=account.pk,
                    api_id=account.api_id,
                    api_hash=account.api_hash,
                    session_string=(account.session_string or '').strip(),
                    max_threads=account.max_concurrent_downloads,
                    on_change=self._update_stats,
                )
                self._workers[account.pk] = worker
                logger.info(
                    f"Pool: Created worker for account {account.pk} "
                    f"(max_threads={account.max_concurrent_downloads})"
                )
            return worker

    def invalidate_account(self, account_id):
        """Invalidate all clients for an account (e.g., on auth error)."""
        with self._lock:
            worker = self._workers.get(account_id)
            if worker:
                worker.invalidate_all()
                logger.info(f"Pool: Invalidated all clients for account {account_id}")

    def _update_stats(self):
        """Update pool_clients on TelegramAccount rows in the database."""
        try:
            from accounts.models import TelegramAccount
            for account_id, worker in list(self._workers.items()):
                count = len(worker._thread_clients)
                TelegramAccount.objects.filter(pk=account_id).update(pool_clients=count)
        except Exception as e:
            logger.warning(f"Pool: Failed to update stats: {e}")

    def shutdown(self):
        """Shut down all workers. Called during worker shutdown."""
        with self._lock:
            for account_id, worker in self._workers.items():
                try:
                    worker.shutdown()
                except Exception as e:
                    logger.warning(f"Pool: Error shutting down worker for account {account_id}: {e}")
            self._workers.clear()
        self._update_stats()
        logger.info("Pool: All workers shut down")


# Module-level singleton
client_pool = ClientPool()
