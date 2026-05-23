from rest_framework.permissions import BasePermission


class IsOwner(BasePermission):
    """Ensure user can only access their own resources"""

    def has_object_permission(self, request, view, obj):
        # For objects with direct user FK
        if hasattr(obj, 'user'):
            return obj.user == request.user
        # For TelegramAccount
        if hasattr(obj, 'account'):
            return obj.account.user == request.user
        # For TelegramChannel (via telegram_account)
        if hasattr(obj, 'telegram_account'):
            return obj.telegram_account.user == request.user
        return False
