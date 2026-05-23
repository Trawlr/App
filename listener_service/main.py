"""
Entry point for the dedicated listener service.

Sets up Django, signal handlers, and runs the ListenerManager.
"""

import asyncio
import logging
import os
import signal
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Setup Django before importing models
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'trawlr.settings')

import django
django.setup()

from listener_service.config import get_config
from listener_service.health import HealthServer
from listener_service.manager import ListenerManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s %(asctime)s %(name)s %(message)s',
)
logger = logging.getLogger('trawlr.listener_service')


async def main():
    """Main entry point for the listener service."""
    logger.info("Starting Telegram Listener Service")

    # Load configuration
    try:
        config = get_config()
        logger.info(f"Configuration loaded: health_port={config.health_port}")
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)

    # Create manager and health server
    manager = ListenerManager()
    health_server = HealthServer(manager)

    # Setup signal handlers
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("Shutdown signal received")
        manager.request_shutdown()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            signal.signal(sig, lambda s, f: signal_handler())

    try:
        # Start health server
        await health_server.start()

        # Run the manager (blocks until shutdown)
        await manager.start()

    except asyncio.CancelledError:
        logger.info("Service cancelled")
    except Exception as e:
        logger.exception(f"Service error: {e}")
        raise
    finally:
        # Cleanup
        await health_server.stop()
        logger.info("Telegram Listener Service stopped")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
