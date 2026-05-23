from rest_framework import serializers
from downloads.models import ArchivedMessage
from audit.models import MessageEntity, ForwardSource


class MessageEntitySerializer(serializers.ModelSerializer):
    """Serializer for MessageEntity.

    After the GlobalEntity split the identity fields (entity_type, text, url,
    user_id, custom_emoji_id, language) live on the related GlobalEntity.
    Exposed here via ``source='entity.*'`` so the JSON shape stays identical
    for API consumers.
    """

    entity_type = serializers.CharField(source='entity.entity_type', read_only=True)
    text = serializers.CharField(source='entity.text', read_only=True)
    url = serializers.CharField(source='entity.url', read_only=True)
    user_id = serializers.IntegerField(source='entity.user_id', read_only=True, allow_null=True)
    custom_emoji_id = serializers.IntegerField(source='entity.custom_emoji_id', read_only=True, allow_null=True)
    language = serializers.CharField(source='entity.language', read_only=True)

    class Meta:
        model = MessageEntity
        fields = [
            'id', 'entity_type', 'offset', 'length', 'text',
            'url', 'user_id', 'custom_emoji_id', 'language'
        ]


class ForwardSourceSerializer(serializers.ModelSerializer):
    """Serializer for ForwardSource."""

    class Meta:
        model = ForwardSource
        fields = [
            'source_type', 'source_telegram_id', 'source_title',
            'source_username', 'original_message_id', 'original_date',
            'original_author', 'from_name',
            'source_is_verified', 'source_is_scam', 'source_is_fake'
        ]


class ArchivedMessageListSerializer(serializers.ModelSerializer):
    """Lighter serializer for message list views."""
    channel_id = serializers.IntegerField(source='channel.id', read_only=True)
    channel_title = serializers.CharField(source='channel.title', read_only=True)
    is_downloaded = serializers.BooleanField(read_only=True)
    media_id = serializers.SerializerMethodField()

    class Meta:
        model = ArchivedMessage
        fields = [
            'id', 'channel_id', 'channel_title', 'message_id',
            'text', 'has_media', 'media_type',
            'sender_id', 'sender_name', 'sender_username',
            'telegram_date', 'is_deleted', 'is_downloaded', 'media_id'
        ]

    def get_media_id(self, obj):
        if obj.downloaded_file_id:
            return obj.downloaded_file_id
        return None


class ArchivedMessageDetailSerializer(serializers.ModelSerializer):
    """Full serializer for message detail view."""
    channel_id = serializers.IntegerField(source='channel.id', read_only=True)
    channel_title = serializers.CharField(source='channel.title', read_only=True)
    thumbnail_url = serializers.CharField(read_only=True)
    is_downloaded = serializers.BooleanField(read_only=True)
    media_id = serializers.SerializerMethodField()
    entities = MessageEntitySerializer(many=True, read_only=True)
    forward_source = ForwardSourceSerializer(read_only=True)

    class Meta:
        model = ArchivedMessage
        fields = [
            'id', 'channel_id', 'channel_title', 'message_id',
            'text', 'is_pinned', 'is_post', 'noforwards', 'is_silent', 'is_deleted',
            'grouped_id', 'post_author', 'ttl_period',
            'has_media', 'media_type', 'telegram_file_id',
            'original_filename', 'file_size', 'mime_type',
            'thumbnail_path', 'media_unavailable',
            'is_downloaded', 'media_id',
            'sender_id', 'sender_name', 'sender_username',
            'reply_to_message_id',
            'views', 'forwards', 'reactions',
            'telegram_date', 'edited_date', 'archived_at',
            'thumbnail_url', 'entities', 'forward_source'
        ]

    def get_media_id(self, obj):
        if obj.downloaded_file_id:
            return obj.downloaded_file_id
        return None
