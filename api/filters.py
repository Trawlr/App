import django_filters
from django.db.models import Q

from accounts.models import TelegramAccount
from audit.models import GlobalEntity, MessageEntity, TelegramChannel, TelegramUser
from downloads.models import ArchivedMessage, DownloadedFile, TaskRun


class TelegramAccountFilter(django_filters.FilterSet):
    """Filters for TelegramAccount."""
    isAuthenticated = django_filters.BooleanFilter(field_name='is_authenticated')
    isActive = django_filters.BooleanFilter(field_name='is_active')
    listenerStatus = django_filters.ChoiceFilter(field_name='listener_status', choices=TelegramAccount.LISTENER_STATUS_CHOICES)

    class Meta:
        model = TelegramAccount
        fields = ['isAuthenticated', 'isActive', 'listenerStatus']


class TelegramChannelFilter(django_filters.FilterSet):
    """Filters for TelegramChannel."""
    account = django_filters.NumberFilter(field_name='account_id')
    channelType = django_filters.ChoiceFilter(field_name='channel_type', choices=TelegramChannel.CHANNEL_TYPE_CHOICES)
    isPrivate = django_filters.BooleanFilter(field_name='is_private')
    availabilityStatus = django_filters.ChoiceFilter(field_name='availability_status', choices=TelegramChannel.AVAILABILITY_STATUS_CHOICES)
    search = django_filters.CharFilter(method='filter_search')
    tag = django_filters.NumberFilter(method='filter_by_tag')

    class Meta:
        model = TelegramChannel
        fields = ['account', 'channelType', 'isPrivate', 'availabilityStatus']

    def filter_search(self, queryset, name, value):
        return queryset.filter(
            Q(title__icontains=value) |
            Q(username__icontains=value)
        )

    def filter_by_tag(self, queryset, name, value):
        return queryset.filter(tags__id=value)

class DownloadedFileFilter(django_filters.FilterSet):
    """Filters for DownloadedFile."""
    channel = django_filters.NumberFilter(field_name='channel_id')
    fileType = django_filters.ChoiceFilter(field_name='file_type', choices=DownloadedFile.FILE_TYPE_CHOICES)
    downloadedAfter = django_filters.DateTimeFilter(field_name='downloaded_at', lookup_expr='gte')
    downloadedBefore = django_filters.DateTimeFilter(field_name='downloaded_at', lookup_expr='lte')
    sha256 = django_filters.CharFilter(field_name='sha256_hash', lookup_expr='icontains')

    class Meta:
        model = DownloadedFile
        fields = ['channel', 'fileType']


class ArchivedMessageFilter(django_filters.FilterSet):
    """Filters for ArchivedMessage."""
    channel_id = django_filters.NumberFilter(field_name='channel_id')
    sender_id = django_filters.NumberFilter(field_name='sender_id')
    hasMedia = django_filters.BooleanFilter(field_name='has_media')
    mediaType = django_filters.ChoiceFilter(field_name='media_type', choices=ArchivedMessage.MEDIA_TYPE_CHOICES)
    dateFrom = django_filters.DateTimeFilter(field_name='telegram_date', lookup_expr='gte')
    dateTo = django_filters.DateTimeFilter(field_name='telegram_date', lookup_expr='lte')
    isPinned = django_filters.BooleanFilter(field_name='is_pinned')
    isPost = django_filters.BooleanFilter(field_name='is_post')

    class Meta:
        model = ArchivedMessage
        fields = ['channel_id', 'sender_id', 'hasMedia', 'mediaType', 'isPinned', 'isPost']


class TelegramUserFilter(django_filters.FilterSet):
    """Filters for TelegramUser."""
    isFlagged = django_filters.BooleanFilter(field_name='is_flagged')
    isBot = django_filters.BooleanFilter(field_name='is_bot')
    isPremium = django_filters.BooleanFilter(field_name='is_premium')
    channel = django_filters.NumberFilter(method='filter_by_channel')
    tag = django_filters.NumberFilter(method='filter_by_tag')

    class Meta:
        model = TelegramUser
        fields = ['isFlagged', 'isBot', 'isPremium']

    def filter_by_channel(self, queryset, name, value):
        return queryset.filter(memberships__channel_id=value)

    def filter_by_tag(self, queryset, name, value):
        return queryset.filter(tags__id=value)

class GlobalEntityFilter(django_filters.FilterSet):
    """Filters for GlobalEntity (deduplicated message entity identity rows)."""
    entityType = django_filters.ChoiceFilter(field_name='entity_type', choices=MessageEntity.ENTITY_TYPE_CHOICES)
    text = django_filters.CharFilter(field_name='text', lookup_expr='icontains')
    url = django_filters.CharFilter(field_name='url', lookup_expr='icontains')
    userId = django_filters.NumberFilter(field_name='user_id')
    search = django_filters.CharFilter(method='filter_search')
    firstSeenAfter = django_filters.DateTimeFilter(field_name='first_seen_at', lookup_expr='gte')
    firstSeenBefore = django_filters.DateTimeFilter(field_name='first_seen_at', lookup_expr='lte')
    lastSeenAfter = django_filters.DateTimeFilter(field_name='last_seen_at', lookup_expr='gte')
    lastSeenBefore = django_filters.DateTimeFilter(field_name='last_seen_at', lookup_expr='lte')

    class Meta:
        model = GlobalEntity
        fields = ['entityType', 'userId']

    def filter_search(self, queryset, name, value):
        return queryset.filter(
            Q(text__icontains=value) | Q(url__icontains=value)
        )


class TaskRunFilter(django_filters.FilterSet):
    """Filters for TaskRun."""
    channel = django_filters.NumberFilter(field_name='channel_id')
    account = django_filters.NumberFilter(field_name='account_id')
    taskType = django_filters.ChoiceFilter(field_name='task_type', choices=TaskRun.TASK_TYPE_CHOICES)
    status = django_filters.ChoiceFilter(choices=TaskRun.STATUS_CHOICES)

    class Meta:
        model = TaskRun
        fields = ['channel', 'account', 'taskType', 'status']
