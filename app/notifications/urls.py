from django.urls import path

from . import views

app_name = 'notifications'

urlpatterns = [
    path('', views.list_entries, name='list'),
    path('add/', views.add_entry, name='add'),
    path('<int:pk>/edit/', views.edit_entry, name='edit'),
    path('<int:pk>/delete/', views.delete_entry, name='delete'),
    path('<int:pk>/test/', views.test_entry, name='test'),
    path('<int:pk>/deliveries/', views.entry_deliveries, name='deliveries'),
]
