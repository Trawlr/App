"""
WebSocket token-auth middleware for Channels.

Lets clients authenticate WebSocket connections via a ?token=<key> query
parameter (used by the Expo mobile companion app) while keeping the
existing cookie/session-based auth working for the web UI.
"""

from urllib.parse import parse_qs

from channels.auth import AuthMiddlewareStack
from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser
from rest_framework.authtoken.models import Token


@database_sync_to_async
def _get_user_from_token(key):
    try:
        return Token.objects.select_related('user').get(key=key).user
    except Token.DoesNotExist:
        return AnonymousUser()


class TokenAuthMiddleware(BaseMiddleware):
    """
    Authenticate a WebSocket connection from a ?token=<key> query param.

    If no token is provided or the token is invalid, scope['user'] is set
    to AnonymousUser. Downstream consumers are responsible for rejecting
    anonymous connections as needed.
    """

    async def __call__(self, scope, receive, send):
        query_string = scope.get('query_string', b'').decode()
        params = parse_qs(query_string)
        token_values = params.get('token')

        if token_values:
            scope['user'] = await _get_user_from_token(token_values[0])
        else:
            scope['user'] = AnonymousUser()

        return await super().__call__(scope, receive, send)


def TokenAuthMiddlewareStack(inner):
    """
    Wrap `inner` with both token-based and cookie/session-based auth so
    either form of credential works on the WebSocket layer.
    """
    return TokenAuthMiddleware(AuthMiddlewareStack(inner))
