"""
Custom authentication classes for the API
Extends TokenAuthentication to support 'Bearer' tokens
"""

import logging
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import AuthenticationFailed

logger = logging.getLogger('trawlr.api.auth')

class BearerTokenAuthentication(TokenAuthentication):
    """
    Token authentication supporting both 'Token' and 'Bearer' keywords because DRF

    Auth header can be sent in two formats:
        Authorization: Token 9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b
        Authorization: Bearer 9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b
    """

    keyword = 'Bearer'

    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')

        if not auth_header:
            logger.debug("No Authorization header present")
            return None

        # Split the header
        parts = auth_header.split()

        if len(parts) == 0:
            return None

        keyword = parts[0].lower()

        # Accept both 'Bearer' and 'Token' keywords
        if keyword not in ('bearer', 'token'):
            return None

        if len(parts) == 1:
            raise AuthenticationFailed('Invalid token header. No token provided.')

        if len(parts) > 2:
            raise AuthenticationFailed('Invalid token header. Token string should not contain spaces.')

        token_key = parts[1]

        return self.authenticate_credentials(token_key)

    def authenticate_credentials(self, key):
        try:
            token = Token.objects.select_related('user').get(key=key)
            logger.debug(f"Token found for user: {token.user.username} (id={token.user.id})")
        except Token.DoesNotExist:
            logger.warning("Authentication failed: invalid token")
            raise AuthenticationFailed('Invalid token.')

        if not token.user.is_active:
            logger.warning(f"User {token.user.username} is inactive")
            raise AuthenticationFailed('User inactive or deleted.')

        logger.info(f"Successfully authenticated user: {token.user.username}")
        return (token.user, token)
