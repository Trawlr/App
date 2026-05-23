"""
Storage backend factory and utilities.
"""

import logging

from storage.backends import AzureBlobStorageBackend, LocalStorageBackend, S3StorageBackend, StorageBackend

logger = logging.getLogger(__name__)

# Module-level cache to avoid recreating backends on every call
_cached_backend = None
_cached_provider = None
_cached_config_hash = None


def _config_hash(settings) -> str:
    """Generate a hash of storage-related settings to detect changes."""
    return (
        f"{settings.storage_provider}:{settings.storage_root}:"
        f"{settings.s3_endpoint_url}:{settings.s3_bucket_name}:{settings.s3_access_key_id}:"
        f"{settings.azure_container_name}"
    )


def get_storage_backend(settings=None) -> StorageBackend:
    """
    Get the configured storage backend from GlobalSettings.

    Uses a module-level cache that invalidates when storage config changes.
    Pass settings explicitly to avoid a DB query when you already have the object.
    """
    global _cached_backend, _cached_provider, _cached_config_hash

    if settings is None:
        from accounts.models import GlobalSettings
        settings = GlobalSettings.get_settings()

    current_hash = _config_hash(settings)

    if _cached_backend is not None and _cached_config_hash == current_hash:
        return _cached_backend

    provider = settings.storage_provider

    if provider == 's3':
        backend = S3StorageBackend(
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            bucket_name=settings.s3_bucket_name,
            region=settings.s3_region,
            presigned_url_expiry=settings.s3_presigned_url_expiry,
        )
    elif provider == 'azure':
        backend = AzureBlobStorageBackend(
            connection_string=settings.azure_connection_string,
            container_name=settings.azure_container_name,
            sas_expiry=settings.azure_sas_expiry,
        )
    else:
        backend = LocalStorageBackend(
            storage_root=settings.storage_root,
        )

    _cached_backend = backend
    _cached_provider = provider
    _cached_config_hash = current_hash

    logger.info(f"Initialized storage backend: {provider}")
    return backend


def invalidate_backend_cache():
    """Invalidate the cached backend. Call after storage settings change."""
    global _cached_backend, _cached_provider, _cached_config_hash
    _cached_backend = None
    _cached_provider = None
    _cached_config_hash = None


def is_cloud_backend(settings=None) -> bool:
    """Check if the current storage backend is a cloud provider (S3 or Azure)."""
    if settings is None:
        from accounts.models import GlobalSettings
        settings = GlobalSettings.get_settings()
    return settings.storage_provider in ('s3', 'azure')
