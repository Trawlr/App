"""
URL configuration for trawlr project.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponse
from django.urls import include, path

urlpatterns = [
    # Health check (unauthenticated, for Docker healthchecks)
    path('ping', lambda request: HttpResponse('pong', content_type='text/plain')),

    # Admin
    path('admin/', admin.site.urls),

    # Authentication
    path('login/', auth_views.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),

    # App URLs
    path('', include('dashboard.urls')),
    path('', include('accounts.urls')),
    path('', include('audit.urls')),
    path('', include('downloads.urls')),
    path('search/', include('search.urls')),
    path('reports/', include('reports.urls')),
    path('map/', include('graph.urls')),
    path('ops/', include('ops.urls')),
    path('notifications/', include('notifications.urls')),

    # REST API
    path('api/v1/', include('api.urls')),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += [path('silk/', include('silk.urls', namespace='silk'))]
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    # Static files are handled automatically by django.contrib.staticfiles in DEBUG mode
