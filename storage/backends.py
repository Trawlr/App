"""
Storage backend abstraction for media files and thumbnails.

Supports local filesystem, S3-compatible storage, and Azure Blob Storage.
The active backend is determined by GlobalSettings.storage_provider.
"""

import io
import logging
import mimetypes
import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO

from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.utils.encoding import escape_uri_path

logger = logging.getLogger(__name__)


class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    def save_file(self, relative_path: str, local_file_path: str) -> None:
        """Upload/move a local file to storage at the given relative path."""

    @abstractmethod
    def delete_file(self, relative_path: str) -> None:
        """Delete a file from storage."""

    @abstractmethod
    def file_exists(self, relative_path: str) -> bool:
        """Check if a file exists in storage."""

    @abstractmethod
    def open_file(self, relative_path: str) -> BinaryIO:
        """Open a file for reading. Caller is responsible for closing."""

    @abstractmethod
    def get_serve_response(self, relative_path: str, content_type: str, filename: str = '') -> HttpResponse:
        """Return an HttpResponse that serves the file to the client."""

    def makedirs(self, relative_path: str) -> None:
        """Ensure parent directory exists. No-op for cloud backends."""


class LocalStorageBackend(StorageBackend):
    """Local filesystem storage backend. Uses nginx X-Accel-Redirect for serving."""

    def __init__(self, storage_root: str):
        self.storage_root = Path(storage_root)

    def _full_path(self, relative_path: str) -> Path:
        return self.storage_root / relative_path

    def save_file(self, relative_path: str, local_file_path: str) -> None:
        dest = self._full_path(relative_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # If source and dest are the same (direct download to storage_root), no-op
        if Path(local_file_path).resolve() == dest.resolve():
            return
        shutil.move(local_file_path, str(dest))

    def delete_file(self, relative_path: str) -> None:
        path = self._full_path(relative_path)
        if path.exists():
            os.remove(path)

    def file_exists(self, relative_path: str) -> bool:
        return self._full_path(relative_path).exists()

    def open_file(self, relative_path: str) -> BinaryIO:
        return open(self._full_path(relative_path), 'rb')

    def get_serve_response(self, relative_path: str, content_type: str, filename: str = '') -> HttpResponse:
        # Guard against path traversal
        full_path = self._full_path(relative_path)
        resolved = full_path.resolve()
        if not str(resolved).startswith(str(self.storage_root.resolve())):
            raise Http404("Invalid path")

        if not full_path.exists():
            raise Http404("File not found on disk")

        response = HttpResponse()
        response['X-Accel-Redirect'] = f'/protected-media/{relative_path}'
        response['Content-Type'] = content_type
        if filename:
            safe_name = escape_uri_path(filename)
            response['Content-Disposition'] = f"inline; filename*=UTF-8''{safe_name}"
        return response

    def makedirs(self, relative_path: str) -> None:
        self._full_path(relative_path).mkdir(parents=True, exist_ok=True)


class S3StorageBackend(StorageBackend):
    """S3-compatible storage backend (AWS S3, MinIO, Wasabi, etc.)."""

    def __init__(self, endpoint_url: str, access_key_id: str, secret_access_key: str,
                 bucket_name: str, region: str = '', presigned_url_expiry: int = 3600):
        import boto3
        from botocore.config import Config

        client_kwargs = {
            'aws_access_key_id': access_key_id,
            'aws_secret_access_key': secret_access_key,
            'config': Config(signature_version='s3v4'),
        }
        if endpoint_url:
            client_kwargs['endpoint_url'] = endpoint_url
        if region:
            client_kwargs['region_name'] = region

        self.client = boto3.client('s3', **client_kwargs)
        self.bucket_name = bucket_name
        self.presigned_url_expiry = presigned_url_expiry

    def _key(self, relative_path: str) -> str:
        """Normalize path separators to forward slashes for S3 keys."""
        return relative_path.replace('\\', '/')

    def save_file(self, relative_path: str, local_file_path: str) -> None:
        key = self._key(relative_path)
        content_type, _ = mimetypes.guess_type(local_file_path)
        extra_args = {}
        if content_type:
            extra_args['ContentType'] = content_type
        self.client.upload_file(local_file_path, self.bucket_name, key, ExtraArgs=extra_args)
        logger.info(f"Uploaded to S3: {key}")

    def delete_file(self, relative_path: str) -> None:
        key = self._key(relative_path)
        self.client.delete_object(Bucket=self.bucket_name, Key=key)
        logger.info(f"Deleted from S3: {key}")

    def file_exists(self, relative_path: str) -> bool:
        from botocore.exceptions import ClientError
        key = self._key(relative_path)
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError:
            return False

    def open_file(self, relative_path: str) -> BinaryIO:
        key = self._key(relative_path)
        response = self.client.get_object(Bucket=self.bucket_name, Key=key)
        return response['Body']

    def get_serve_response(self, relative_path: str, content_type: str, filename: str = '') -> HttpResponse:
        key = self._key(relative_path)
        params = {
            'Bucket': self.bucket_name,
            'Key': key,
        }
        if content_type:
            params['ResponseContentType'] = content_type
        if filename:
            safe_name = escape_uri_path(filename)
            params['ResponseContentDisposition'] = f"inline; filename*=UTF-8''{safe_name}"

        url = self.client.generate_presigned_url(
            'get_object',
            Params=params,
            ExpiresIn=self.presigned_url_expiry,
        )
        return HttpResponseRedirect(url)


class AzureBlobStorageBackend(StorageBackend):
    """Azure Blob Storage backend."""

    def __init__(self, connection_string: str, container_name: str, sas_expiry: int = 3600):
        from azure.storage.blob import BlobServiceClient

        self.blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        self.container_name = container_name
        self.container_client = self.blob_service_client.get_container_client(container_name)
        self.sas_expiry = sas_expiry
        # Extract account name and key for SAS generation
        self._connection_string = connection_string

    def _blob_name(self, relative_path: str) -> str:
        """Normalize path separators to forward slashes for blob names."""
        return relative_path.replace('\\', '/')

    def save_file(self, relative_path: str, local_file_path: str) -> None:
        blob_name = self._blob_name(relative_path)
        blob_client = self.container_client.get_blob_client(blob_name)
        content_type, _ = mimetypes.guess_type(local_file_path)

        from azure.storage.blob import ContentSettings
        content_settings = ContentSettings(content_type=content_type) if content_type else None

        with open(local_file_path, 'rb') as data:
            blob_client.upload_blob(data, overwrite=True, content_settings=content_settings)
        logger.info(f"Uploaded to Azure Blob: {blob_name}")

    def delete_file(self, relative_path: str) -> None:
        blob_name = self._blob_name(relative_path)
        blob_client = self.container_client.get_blob_client(blob_name)
        try:
            blob_client.delete_blob()
            logger.info(f"Deleted from Azure Blob: {blob_name}")
        except Exception as e:
            if 'BlobNotFound' in str(e):
                logger.debug(f"Blob not found (already deleted): {blob_name}")
            else:
                raise

    def file_exists(self, relative_path: str) -> bool:
        blob_name = self._blob_name(relative_path)
        blob_client = self.container_client.get_blob_client(blob_name)
        try:
            blob_client.get_blob_properties()
            return True
        except Exception:
            return False

    def open_file(self, relative_path: str) -> BinaryIO:
        blob_name = self._blob_name(relative_path)
        blob_client = self.container_client.get_blob_client(blob_name)
        stream = blob_client.download_blob()
        return io.BytesIO(stream.readall())

    def get_serve_response(self, relative_path: str, content_type: str, filename: str = '') -> HttpResponse:
        from datetime import datetime, timedelta, timezone

        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        blob_name = self._blob_name(relative_path)
        blob_client = self.container_client.get_blob_client(blob_name)

        # Parse account name and key from connection string for SAS generation
        account_name = None
        account_key = None
        for part in self._connection_string.split(';'):
            part = part.strip()
            key_value = part.split('=', 1)
            if len(key_value) == 2:
                key = key_value[0].strip().lower()
                if key == 'accountname':
                    account_name = key_value[1].strip()
                elif key == 'accountkey':
                    account_key = key_value[1].strip()

        if account_name and account_key:
            # Standard connection string with account key — generate SAS token
            sas_token = generate_blob_sas(
                account_name=account_name,
                container_name=self.container_name,
                blob_name=blob_name,
                account_key=account_key,
                permission=BlobSasPermissions(read=True),
                expiry=datetime.now(timezone.utc) + timedelta(seconds=self.sas_expiry),
                content_type=content_type,
                content_disposition=f"inline; filename*=UTF-8''{escape_uri_path(filename)}" if filename else None,
            )
            url = f"{blob_client.url}?{sas_token}"
        else:
            # SAS-based or other connection string — the blob client URL already contains auth
            url = blob_client.url

        return HttpResponseRedirect(url)
