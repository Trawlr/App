from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from accounts.models import TelegramAccount, GlobalSettings


class TelegramAccountListSerializer(serializers.ModelSerializer):
    """Serializer for TelegramAccount list view (summary)"""
    class Meta:
        model = TelegramAccount
        fields = [
            'id', 'display_name', 'is_active', 'is_authenticated',
            'listener_status'
        ]

class TelegramAccountSerializer(serializers.ModelSerializer):
    """Serializer for TelegramAccount detail view (full fields)."""
    channel_count = serializers.SerializerMethodField()
    name = serializers.CharField(read_only=True)
    is_flood_wait_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = TelegramAccount
        fields = [
            'id', 'phone_number', 'display_name', 'name',
            'is_active', 'is_authenticated',
            'listener_status', 'listener_mode', 'listener_error',
            'flood_wait_until', 'is_flood_wait_active',
            'max_concurrent_downloads', 'download_profile_photos',
            'process_events', 'channel_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'phone_number', 'is_authenticated',
            'listener_status', 'listener_error', 'flood_wait_until',
            'created_at', 'updated_at'
        ]

    @extend_schema_field(serializers.IntegerField())
    def get_channel_count(self, obj) -> int:
        return obj.channels.count()


class GlobalSettingsSerializer(serializers.ModelSerializer):
    """Serializer for GlobalSettings."""
    s3_configured = serializers.SerializerMethodField()
    azure_configured = serializers.SerializerMethodField()

    class Meta:
        model = GlobalSettings
        exclude = [
            'id',
            's3_access_key_id',
            's3_secret_access_key',
            'azure_connection_string',
        ]
        read_only_fields = []

    @extend_schema_field(serializers.BooleanField())
    def get_s3_configured(self, obj) -> bool:
        return bool(obj.s3_access_key_id and obj.s3_secret_access_key)

    @extend_schema_field(serializers.BooleanField())
    def get_azure_configured(self, obj) -> bool:
        return bool(obj.azure_connection_string)
