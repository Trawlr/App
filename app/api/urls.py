from django.urls import path, include, re_path
from rest_framework.routers import DefaultRouter
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

app_name = 'api'
from .views.accounts import TelegramAccountViewSet
from .views.auth import LoginView
from .views.channels import TelegramChannelViewSet
from .views.entities import GlobalEntityViewSet
from .views.files import DownloadedFileViewSet
from .views.messages import ArchivedMessageViewSet
from .views.users import TelegramUserViewSet
from .views.settings import GlobalSettingsView, StatsView
from .views.tags import TagViewSet
from .views.resolve import resolve_link

# Router without trailing slashes
router = DefaultRouter(trailing_slash=False)
router.register(r'accounts', TelegramAccountViewSet, basename='account')
router.register(r'channels', TelegramChannelViewSet, basename='channel')
router.register(r'entities', GlobalEntityViewSet, basename='entity')
router.register(r'files', DownloadedFileViewSet, basename='file')
router.register(r'messages', ArchivedMessageViewSet, basename='message')
router.register(r'users', TelegramUserViewSet, basename='telegram-user')
router.register(r'tags', TagViewSet, basename='tag')

urlpatterns = [
    # Authentication (username/password -> token exchange for mobile client)
    path('auth/login/', LoginView.as_view(), name='auth-login'),

    # API routes
    path('', include(router.urls)),

    # Global settings and stats (support both with and without trailing slash)
    re_path(r'^settings/?$', GlobalSettingsView.as_view(), name='settings'),
    re_path(r'^stats/?$', StatsView.as_view(), name='stats'),

    # Telegram link resolution
    re_path(r'^resolve/?$', resolve_link, name='resolve-link'),

    # OpenAPI schema (support both with and without trailing slash)
    re_path(r'^schema/?$', SpectacularAPIView.as_view(), name='schema'),
    re_path(r'^docs/?$', SpectacularSwaggerView.as_view(url_name='api:schema'), name='swagger-ui'),
    re_path(r'^redoc/?$', SpectacularRedocView.as_view(url_name='api:schema'), name='redoc'),
]
