"""Search URL configuration."""

from django.urls import path
from . import views

app_name = 'search'

urlpatterns = [
    path('', views.search_view, name='search'),
    path('autocomplete/', views.search_autocomplete, name='autocomplete'),
    path('global/', views.global_search, name='global_search'),
    path('aggregate/', views.aggregate_view, name='aggregate'),
    path('aggregate/export/', views.aggregate_export, name='aggregate_export'),
]
