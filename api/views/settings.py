from django.db.models import Sum, Count
from rest_framework import status, serializers
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from drf_spectacular.utils import extend_schema, inline_serializer

from accounts.models import GlobalSettings
from downloads.models import DownloadTask, DownloadedFile, ArchivedMessage
from audit.models import TelegramChannel, TelegramUser
from api.serializers.accounts import GlobalSettingsSerializer


class GlobalSettingsView(APIView):
    """
    Get Trawlr's global settings.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: GlobalSettingsSerializer})
    def get(self, request):
        settings = GlobalSettings.get_settings()
        serializer = GlobalSettingsSerializer(settings)
        return Response(serializer.data)

class StatsView(APIView):
    """
    Returns system wide metrics for Trawlr. This is computationally expensive and should be used sparingly.
    Roadmap item is to async the request
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: inline_serializer(
            name='StatsResponse',
            fields={
                'accounts': serializers.IntegerField(),
                'channels': serializers.IntegerField(),
                'monitored_channels': serializers.IntegerField(),
                'downloads': serializers.DictField(child=serializers.IntegerField()),
                'files': serializers.DictField(),
                'messages': serializers.DictField(child=serializers.IntegerField()),
                'tracked_users': serializers.IntegerField(),
            }
        )}
    )
    def get(self, request):
        user = request.user

        # Get user's channels (from active accounts only)
        user_channels = TelegramChannel.objects.from_active_accounts()
        channel_ids = user_channels.values_list('id', flat=True)

        # Calculate stats
        stats = {
            'accounts': user.telegram_accounts.count(),
            'channels': user_channels.count(),
            'monitored_channels': user.monitored_sources.count(),

            # Download stats
            'downloads': {
                'pending': DownloadTask.objects.filter(
                    channel_id__in=channel_ids, status='pending'
                ).count(),
                'downloading': DownloadTask.objects.filter(
                    channel_id__in=channel_ids, status='downloading'
                ).count(),
                'completed': DownloadTask.objects.filter(
                    channel_id__in=channel_ids, status='completed'
                ).count(),
                'failed': DownloadTask.objects.filter(
                    channel_id__in=channel_ids, status='failed'
                ).count(),
            },

            # File stats
            'files': {
                'total': DownloadedFile.objects.filter(
                    channel_id__in=channel_ids
                ).count(),
                'total_size': DownloadedFile.objects.filter(
                    channel_id__in=channel_ids
                ).aggregate(total=Sum('file_size'))['total'] or 0,
                'by_type': dict(DownloadedFile.objects.filter(
                    channel_id__in=channel_ids
                ).values('file_type').annotate(count=Count('id')).values_list('file_type', 'count')),
            },

            # Message stats
            'messages': {
                'total': ArchivedMessage.objects.filter(
                    channel_id__in=channel_ids
                ).count(),
                'with_media': ArchivedMessage.objects.filter(
                    channel_id__in=channel_ids, has_media=True
                ).count(),
            },

            # User stats
            'tracked_users': TelegramUser.objects.filter(
                memberships__channel_id__in=channel_ids
            ).distinct().count(),
        }

        return Response(stats)
