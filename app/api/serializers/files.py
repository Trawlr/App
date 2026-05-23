from rest_framework import serializers
from downloads.models import DownloadedFile


class DownloadedFileSerializer(serializers.ModelSerializer):
    """Serializer for DownloadedFile."""
    channel_id = serializers.IntegerField(source='channel.id', read_only=True)
    channel_title = serializers.CharField(source='channel.title', read_only=True)
    file_url = serializers.CharField(read_only=True)
    thumbnail_url = serializers.CharField(read_only=True)

    class Meta:
        model = DownloadedFile
        fields = [
            'id', 'channel_id', 'channel_title', 'message_id',
            'original_filename', 'stored_filename', 'file_path',
            'file_type', 'file_size', 'mime_type',
            'sha256_hash', 'is_duplicate', 'original_file',
            'thumbnail_path', 'telegram_file_id', 'telegram_date',
            'downloaded_at', 'deleted_from_disk',
            'file_url', 'thumbnail_url'
        ]
        read_only_fields = fields


class DownloadedFileListSerializer(serializers.ModelSerializer):
    """Lighter serializer for file list views."""
    channel_id = serializers.IntegerField(source='channel.id', read_only=True)
    channel_title = serializers.CharField(source='channel.title', read_only=True)
    file_path = serializers.CharField(read_only=True)

    class Meta:
        model = DownloadedFile
        fields = [
            'id', 'channel_id', 'channel_title', 'message_id',
            'stored_filename', 'file_type', 'file_size',
            'sha256_hash', 'telegram_date', 'downloaded_at',
            'file_path'
        ]
