from rest_framework import serializers

from audit.models import Tag


class TagSerializer(serializers.ModelSerializer):
    """Full serializer for Tag CRUD."""
    channel_count = serializers.SerializerMethodField()
    user_count = serializers.SerializerMethodField()

    class Meta:
        model = Tag
        fields = ['id', 'name', 'colour', 'description', 'channel_count', 'user_count', 'created_at']
        read_only_fields = ['created_at']

    def get_channel_count(self, obj):
        return obj.channels.count()

    def get_user_count(self, obj):
        return obj.users.count()


class TagCompactSerializer(serializers.ModelSerializer):
    """Lightweight serializer for embedding tags in other responses."""

    class Meta:
        model = Tag
        fields = ['id', 'name', 'colour']
