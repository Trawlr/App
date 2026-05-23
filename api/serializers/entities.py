from rest_framework import serializers

from audit.models import GlobalEntity


class GlobalEntityListSerializer(serializers.ModelSerializer):
    """List view of GlobalEntity with occurrence count."""
    occurrence_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = GlobalEntity
        fields = [
            'id', 'entity_type', 'text', 'url', 'user_id',
            'custom_emoji_id', 'language',
            'first_seen_at', 'last_seen_at', 'occurrence_count',
        ]


class GlobalEntityDetailSerializer(serializers.ModelSerializer):
    """Detail view of GlobalEntity (includes content_hash)."""
    occurrence_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = GlobalEntity
        fields = [
            'id', 'entity_type', 'text', 'url', 'user_id',
            'custom_emoji_id', 'language', 'content_hash',
            'first_seen_at', 'last_seen_at', 'occurrence_count',
        ]
