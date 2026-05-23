"""
ListenerManager - Orchestrates all Telegram listener connections.

Manages connection lifecycle, responds to RabbitMQ commands,
and performs periodic health checks on all connections.
"""

import asyncio
import json
import logging
from datetime import datetime

import aio_pika

from accounts.models import TelegramAccount

from .config import get_config
from .connection import TelegramConnection
from .db import run_sync_callable
from .event_publisher import EventPublisher

logger = logging.getLogger('trawlr.listener_service.manager')


class ListenerManager:
    """
    Orchestrates all Telegram listener connections.

    Responsibilities:
    - Start/stop individual account listeners
    - Monitor connection health
    - Handle reconnection with exponential backoff
    - Respond to RabbitMQ commands (start/stop/restart)
    - Periodic database sync for new/removed accounts
    - Graceful shutdown
    """

    def __init__(self):
        self._config = get_config()
        self.connections: dict[int, TelegramConnection] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self._shutdown_event = asyncio.Event()
        self._rabbitmq_connection: aio_pika.Connection | None = None
        self._command_channel: aio_pika.Channel | None = None
        self._started_at: datetime | None = None
        self._event_publisher: EventPublisher | None = None

    @property
    def status(self) -> dict:
        """Get manager status summary."""
        return {
            'started_at': self._started_at.isoformat() if self._started_at else None,
            'total_connections': len(self.connections),
            'active_connections': sum(1 for c in self.connections.values() if c.is_connected),
            'connections': [c.status for c in self.connections.values()],
        }

    async def start(self):
        """Main entry point - runs forever until shutdown."""
        self._started_at = datetime.utcnow()
        logger.info("Starting ListenerManager")

        try:
            # Connect to RabbitMQ for commands
            await self._connect_rabbitmq()

            # Connect EventPublisher for event queueing
            await self._connect_event_publisher()

            # Load initial accounts
            await self._load_initial_accounts()

            # Run concurrent tasks
            await asyncio.gather(
                self._account_poll_loop(),
                self._command_listener(),
                self._wait_for_shutdown(),
            )
        except asyncio.CancelledError:
            logger.info("ListenerManager cancelled")
        except Exception as e:
            logger.exception(f"ListenerManager error: {e}")
            raise
        finally:
            await self.shutdown()

    async def _connect_rabbitmq(self):
        """Connect to RabbitMQ for command pub/sub."""
        logger.info("Connecting to RabbitMQ")

        self._rabbitmq_connection = await aio_pika.connect_robust(
            self._config.rabbitmq_url
        )

        self._command_channel = await self._rabbitmq_connection.channel()

        # Declare exchange and queue for commands
        exchange = await self._command_channel.declare_exchange(
            self._config.command_exchange,
            aio_pika.ExchangeType.FANOUT,
            durable=True
        )

        queue = await self._command_channel.declare_queue(
            self._config.command_queue,
            durable=True
        )

        await queue.bind(exchange)
        logger.info("RabbitMQ connected and queue bound")

    async def _connect_event_publisher(self):
        """Connect the event publisher for queueing events."""
        logger.info("Connecting EventPublisher")
        self._event_publisher = EventPublisher(self._config.rabbitmq_url)
        await self._event_publisher.connect()
        logger.info("EventPublisher connected")

    async def _load_initial_accounts(self):
        """Load all active accounts from database on startup."""
        logger.info("Loading initial accounts")

        accounts = await run_sync_callable(
            lambda: list(TelegramAccount.objects.filter(
                is_active=True,
                is_authenticated=True,
                listener_mode='service',
            ))
        )

        logger.info(f"Found {len(accounts)} accounts to start")

        for account in accounts:
            await self.start_listener(account.pk)

    async def _account_poll_loop(self):
        """Periodically check for new/changed accounts."""
        while not self._shutdown_event.is_set():
            try:
                await self._sync_accounts()
            except Exception as e:
                logger.exception(f"Error syncing accounts: {e}")

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._config.account_poll_interval
                )
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Normal timeout, continue polling

    async def _sync_accounts(self):
        """Sync running listeners with database state."""
        # Get accounts that should be running
        # Only include accounts that aren't manually stopped
        should_run = await run_sync_callable(
            lambda: set(TelegramAccount.objects.filter(
                is_active=True,
                is_authenticated=True,
                listener_mode='service',
            ).exclude(
                listener_status='stopped'
            ).values_list('pk', flat=True))
        )

        currently_running = set(self.connections.keys())

        # Start new accounts
        to_start = should_run - currently_running
        for account_id in to_start:
            logger.info(f"Starting new account {account_id}")
            await self.start_listener(account_id)

        # Stop removed accounts
        to_stop = currently_running - should_run
        for account_id in to_stop:
            logger.info(f"Stopping removed account {account_id}")
            await self.stop_listener(account_id)

    async def _command_listener(self):
        """Listen for commands from RabbitMQ."""
        if not self._command_channel:
            logger.warning("RabbitMQ not connected, skipping command listener")
            return

        queue = await self._command_channel.get_queue(self._config.command_queue)

        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                if self._shutdown_event.is_set():
                    break

                async with message.process():
                    try:
                        await self._handle_command(message.body)
                    except Exception as e:
                        logger.exception(f"Error handling command: {e}")

    async def _handle_command(self, body: bytes):
        """Handle a command message from RabbitMQ."""
        try:
            cmd = json.loads(body.decode())
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON command: {body}")
            return

        command = cmd.get('command')
        account_id = cmd.get('account_id')

        logger.info(f"Received command: {command} for account {account_id}")

        if command == 'start' and account_id:
            await self.start_listener(account_id)
        elif command == 'stop' and account_id:
            await self.stop_listener(account_id)
        elif command == 'restart' and account_id:
            await self.stop_listener(account_id)
            await asyncio.sleep(1)
            await self.start_listener(account_id)
        elif command == 'restart_all':
            await self._restart_all()
        elif command == 'status':
            logger.info(f"Status: {self.status}")
        else:
            logger.warning(f"Unknown command: {cmd}")

    async def _restart_all(self):
        """Restart all listeners."""
        logger.info("Restarting all listeners")

        account_ids = list(self.connections.keys())

        # Stop all
        for account_id in account_ids:
            await self.stop_listener(account_id)

        await asyncio.sleep(2)

        # Reload from database
        await self._load_initial_accounts()

    async def _wait_for_shutdown(self):
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()

    async def start_listener(self, account_id: int):
        """Start a listener for a specific account."""
        if account_id in self.connections:
            logger.debug(f"Listener for account {account_id} already running")
            return

        conn = TelegramConnection(account_id, event_publisher=self._event_publisher)
        self.connections[account_id] = conn

        # Start the connection task
        task = asyncio.create_task(conn.run_forever())
        self.tasks[account_id] = task

        # Handle task completion
        task.add_done_callback(
            lambda t, aid=account_id: asyncio.create_task(
                self._on_task_done(aid, t)
            )
        )

        logger.info(f"Started listener for account {account_id}")

    async def _on_task_done(self, account_id: int, task: asyncio.Task):
        """Handle when a connection task completes."""
        # Clean up references
        self.connections.pop(account_id, None)
        self.tasks.pop(account_id, None)

        # Check if we should restart
        if not self._shutdown_event.is_set():
            try:
                exc = task.exception()
                if exc:
                    logger.error(f"Account {account_id} task failed: {exc}")
            except asyncio.CancelledError:
                pass

    async def stop_listener(self, account_id: int):
        """Stop a specific account listener."""
        if account_id not in self.connections:
            return

        logger.info(f"Stopping listener for account {account_id}")

        conn = self.connections.get(account_id)
        if conn:
            await conn.disconnect()

        task = self.tasks.get(account_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self.connections.pop(account_id, None)
        self.tasks.pop(account_id, None)

    async def shutdown(self):
        """Graceful shutdown of all connections."""
        logger.info("Shutting down ListenerManager")
        self._shutdown_event.set()

        # Stop all listeners concurrently
        if self.connections:
            await asyncio.gather(
                *[self.stop_listener(aid) for aid in list(self.connections.keys())],
                return_exceptions=True
            )

        # Close RabbitMQ connection
        if self._rabbitmq_connection:
            await self._rabbitmq_connection.close()

        # Close EventPublisher
        if self._event_publisher:
            await self._event_publisher.disconnect()

        logger.info("ListenerManager shutdown complete")

    def request_shutdown(self):
        """Request shutdown from external signal handler."""
        self._shutdown_event.set()
