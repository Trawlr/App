from rest_framework import serializers
from audit.models import TelegramUser, UserGroupMembership
from api.serializers.tags import TagCompactSerializer


class TelegramUserListSerializer(serializers.ModelSerializer):
    """Lighter serializer for user list views."""
    display_name = serializers.CharField(read_only=True)
    telegram_link = serializers.CharField(read_only=True)

    class Meta:
        model = TelegramUser
        fields = [
            'id', 'telegram_id', 'first_name', 'last_name', 'username',
            'display_name', 'telegram_link',
            'is_bot', 'is_flagged', 'message_count', 'first_seen', 'last_seen'
        ]

class TelegramUserDetailSerializer(serializers.ModelSerializer):
    """Full serializer for user detail view."""
    display_name = serializers.CharField(read_only=True)
    telegram_link = serializers.CharField(read_only=True)
    tags = TagCompactSerializer(many=True, read_only=True)

    class Meta:
        model = TelegramUser
        fields = [
            'id', 'telegram_id', 'first_name', 'last_name', 'username', 'phone',
            'display_name', 'telegram_link',
            'is_bot', 'is_verified', 'is_premium', 'is_scam', 'is_fake', 'is_restricted',
            'is_deleted', 'is_support', 'is_contact', 'is_mutual_contact', 'is_close_friend',
            'lang_code', 'stories_hidden', 'stories_unavailable', 'emoji_status',
            'photo_path', 'profile_photo_updated_at',
            'bio', 'birthday', 'private_forward_name', 'personal_channel_id', 'common_chats_count',
            'phone_calls_available', 'video_calls_available', 'voice_messages_forbidden', 'contact_require_premium',
            'is_blocked', 'business_intro', 'business_location', 'business_work_hours',
            'has_pinned_stories', 'has_scheduled_messages', 'pinned_message_id',
            'full_profile_fetched_at',
            'first_seen', 'last_seen', 'message_count',
            'is_flagged', 'flagged_reason', 'flagged_notes', 'flagged_at',
            'reported_to_telegram', 'reported_at',
            'notes', 'tags'
        ]


class UserGroupMembershipSerializer(serializers.ModelSerializer):
    """Serializer for UserGroupMembership."""
    channel_id = serializers.IntegerField(source='channel.id', read_only=True)
    channel_title = serializers.CharField(source='channel.title', read_only=True)

    class Meta:
        model = UserGroupMembership
        fields = [
            'id', 'channel_id', 'channel_title',
            'first_seen', 'last_seen', 'last_message_date',
            'is_admin', 'is_creator', 'admin_title',
            'message_count'
        ]


class UserFlagSerializer(serializers.Serializer):
    """Serializer for flagging a user."""
    reason = serializers.ChoiceField(
        choices=[
            ('spam', 'Spam'),
            ('violence', 'Violence'),
            ('pornography', 'Adult Content'),
            ('child_abuse', 'Child Abuse'),
            ('copyright', 'Copyright Violation'),
            ('fake', 'Fake Account / Scam'),
            ('illegal_drugs', 'Illegal Drugs'),
            ('personal_details', 'Personal Data Exposure'),
            ('other', 'Other'),
        ],
        required=True
    )
    notes = serializers.CharField(required=False, allow_blank=True)
