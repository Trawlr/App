"""
Health check HTTP server for the listener service.

Provides endpoints for Docker/Kubernetes health checks
and status monitoring.
"""

import logging
from aiohttp import web

from .config import get_config

logger = logging.getLogger('trawlr.listener_service.health')


class HealthServer:
    """
    HTTP server for health checks and status endpoints.

    Endpoints:
    - GET /health - Simple health check (200 OK if running)
    - GET /status - Detailed status of all connections
    - GET /ready - Readiness check (200 if at least one connection active)
    """

    def __init__(self, manager):
        self.manager = manager
        self._config = get_config()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self):
        """Start the health check server."""
        app = web.Application()
        app.router.add_get('/health', self.health_check)
        app.router.add_get('/status', self.status)
        app.router.add_get('/ready', self.ready_check)

        self._runner = web.AppRunner(app)
        await self._runner.setup()

        self._site = web.TCPSite(
            self._runner,
            '0.0.0.0',
            self._config.health_port
        )
        await self._site.start()

        logger.info(f"Health server started on port {self._config.health_port}")

    async def stop(self):
        """Stop the health check server."""
        if self._runner:
            await self._runner.cleanup()
        logger.info("Health server stopped")

    async def health_check(self, request: web.Request) -> web.Response:
        """
        Simple health check endpoint.

        Returns 200 OK if the service is running.
        Used for Docker HEALTHCHECK and basic monitoring.
        """
        return web.json_response({
            'status': 'ok',
            'service': 'listener_service',
        })

    async def ready_check(self, request: web.Request) -> web.Response:
        """
        Readiness check endpoint.

        Returns 200 if the service is ready to handle work.
        Returns 503 if no connections are active and accounts should be connected.
        """
        status = self.manager.status

        # If we have connections configured but none are active, not ready
        if status['total_connections'] > 0 and status['active_connections'] == 0:
            return web.json_response(
                {
                    'status': 'not_ready',
                    'reason': 'No active connections',
                    'total': status['total_connections'],
                    'active': status['active_connections'],
                },
                status=503
            )

        return web.json_response({
            'status': 'ready',
            'total': status['total_connections'],
            'active': status['active_connections'],
        })

    async def status(self, request: web.Request) -> web.Response:
        """
        Detailed status endpoint.

        Returns full status of the manager and all connections.
        """
        return web.json_response(self.manager.status)
