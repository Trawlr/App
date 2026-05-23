from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from audit.models import TelegramChannel, ChannelConfig, UserMonitoredSource
from api.serializers.tags import TagCompactSerializer


class ChannelConfigSerializer(serializers.ModelSerializer):
    """Serializer for ChannelConfig."""
    download_progress_percent = serializers.FloatField(read_only=True)

    class Meta:
        model = ChannelConfig
        fields = [
            'auto_download_enabled', 'download_photos', 'download_videos',
            'download_files', 'file_type_priority', 'priority',
            'deduplication_mode',
            'download_thumbnails', 'thumbnail_size',
            'is_paused', 'bypass_listener',
            'last_downloaded_message_id', 'total_messages', 'downloaded_messages',
            'download_progress_percent'
        ]
        read_only_fields = [
            'last_downloaded_message_id', 'total_messages',
            'downloaded_messages', 'download_progress_percent'
        ]


class TelegramChannelListSerializer(serializers.ModelSerializer):
    """Serializer for TelegramChannel list views."""
    account_id = serializers.IntegerField(source='account.id', read_only=True)
    tags = TagCompactSerializer(many=True, read_only=True)

    class Meta:
        model = TelegramChannel
        fields = [
            'id', 'telegram_id', 'title', 'username',
            'channel_type', 'is_private', 'member_count',
            'account_id', 'is_verified', 'is_restricted',
            'availability_status', 'joined_at', 'tags'
        ]


class TelegramChannelDetailSerializer(serializers.ModelSerializer):
    """Serializer for TelegramChannel detail view."""
    account_id = serializers.IntegerField(source='account.id', read_only=True)
    config = ChannelConfigSerializer(read_only=True)
    is_monitored = serializers.SerializerMethodField()
    stats = serializers.SerializerMethodField()
    telegram_link = serializers.CharField(read_only=True)
    tags = TagCompactSerializer(many=True, read_only=True)

    class Meta:
        model = TelegramChannel
        fields = [
            'id', 'telegram_id', 'title', 'username',
            'channel_type', 'is_private', 'member_count',
            'account_id', 'config', 'is_monitored', 'stats',
            'is_verified', 'is_scam', 'is_fake', 'is_restricted',
            'is_broadcast', 'is_megagroup', 'is_gigagroup',
            'has_signatures', 'has_linked_chat', 'slowmode_seconds',
            'is_forum', 'noforwards', 'join_to_send', 'join_request',
            'boost_level', 'has_left',
            'availability_status', 'availability_error', 'availability_checked_at',
            'telegram_photo_count', 'telegram_video_count', 'telegram_file_count',
            'telegram_counts_updated_at',
            'telegram_link', 'joined_at', 'created_at', 'updated_at', 'tags'
        ]

    @extend_schema_field(serializers.BooleanField())
    def get_is_monitored(self, obj) -> bool:
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.monitored_by.filter(user=request.user).exists()
        return False

    @extend_schema_field(serializers.DictField(child=serializers.IntegerField()))
    def get_stats(self, obj) -> dict:
        return {
            'total_messages': obj.archived_messages.count(),
            'total_files': obj.downloaded_files.count(),
            'pending_downloads': obj.download_tasks.filter(status='pending').count(),
        }


class UserMonitoredSourceSerializer(serializers.ModelSerializer):
    """Serializer for UserMonitoredSource."""
    channel = TelegramChannelListSerializer(read_only=True)

    class Meta:
        model = UserMonitoredSource
        fields = ['id', 'channel', 'created_at']
