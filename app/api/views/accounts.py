from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from accounts.models import TelegramAccount, GlobalSettings
from api.serializers.accounts import (
    TelegramAccountListSerializer,
    TelegramAccountSerializer,
    GlobalSettingsSerializer
)
from api.filters import TelegramAccountFilter
from django_filters.rest_framework import DjangoFilterBackend

class TelegramAccountViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Returns a list of Telegram accounts onboarded to Trawlr and their respective details.
    """
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = TelegramAccountFilter
    serializer_class = TelegramAccountSerializer

    def get_queryset(self):
        # Only return active accounts by default
        return TelegramAccount.objects.active()

    def get_serializer_class(self):
        if self.action == 'list':
            return TelegramAccountListSerializer
        return TelegramAccountSerializer
