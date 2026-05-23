from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from audit.models import Tag
from api.serializers.tags import TagSerializer


class TagViewSet(viewsets.ModelViewSet):
    """CRUD for tags."""
    queryset = Tag.objects.all()
    serializer_class = TagSerializer
    permission_classes = [IsAuthenticated]
    search_fields = ['name']
