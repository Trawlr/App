import mimetypes

from django.http import Http404
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from django_filters.rest_framework import DjangoFilterBackend

from api.filters import DownloadedFileFilter
from api.serializers.files import DownloadedFileSerializer, DownloadedFileListSerializer
from downloads.models import DownloadedFile
from storage.utils import get_storage_backend


class DownloadedFileViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Retrieves and manages files that have been downloaded using Trawlr.
    """
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = DownloadedFileFilter
    ordering_fields = ['downloaded_at', 'telegram_date']
    ordering = ['-downloaded_at']

    def get_queryset(self):
        return DownloadedFile.objects.all().select_related('channel')

    def get_serializer_class(self):
        if self.action == 'list':
            return DownloadedFileListSerializer
        return DownloadedFileSerializer

    @action(detail=True, methods=['get'])
    def download(self, request, pk=None):
        """Download the file content via the storage backend."""
        file_obj = self.get_object()

        if file_obj.deleted_from_disk or not file_obj.file_path:
            raise Http404('File not found')

        # If it's a duplicate, serve the original file
        if file_obj.is_duplicate and file_obj.original_file:
            file_obj = file_obj.original_file

        content_type = file_obj.mime_type or 'application/octet-stream'
        # Override with extension-based type for reliability
        guessed_type, _ = mimetypes.guess_type(file_obj.file_path)
        if guessed_type:
            content_type = guessed_type

        backend = get_storage_backend()
        return backend.get_serve_response(
            file_obj.file_path,
            content_type,
            filename=file_obj.original_filename,
        )
