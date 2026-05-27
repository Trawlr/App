"""
Authentication endpoints for the API.

Provides a JSON login endpoint that exchanges username/password for an
auth token in a single round-trip, intended for the Expo mobile client.
"""

from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.authtoken.models import Token
from rest_framework.response import Response


class LoginView(ObtainAuthToken):
    """
    Exchange username/password for an auth token and basic user info.

    Unauthenticated endpoint. Default DRF throttling (AnonRateThrottle)
    still applies via project-wide DEFAULT_THROTTLE_CLASSES.
    """
    permission_classes = []
    authentication_classes = []

    def post(self, request, *args, **kwargs):
        serializer = self.serializer_class(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']
        token, _ = Token.objects.get_or_create(user=user)
        return Response({
            'token': token.key,
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'is_staff': user.is_staff,
                'is_superuser': user.is_superuser,
            },
        })
