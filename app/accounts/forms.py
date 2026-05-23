"""
Accounts app forms.
"""

from django import forms
from django.core.validators import RegexValidator
from .models import GlobalSettings, TelegramAccount

class TelegramAccountForm(forms.ModelForm):
    """Form for adding a new Telegram account."""

    phone_number = forms.CharField(
        max_length=20,
        validators=[
            RegexValidator(
                regex=r'^\+\d{10,15}$',
                message='Enter a valid phone number with country code (e.g., +1234567890)'
            )
        ],
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': '+61491570006'
        })
    )

    api_id = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': '12345678'
        }),
        help_text='Get your API ID from my.telegram.org > API development tools'
    )

    api_hash = forms.CharField(
        max_length=64,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'abc123def456...'
        }),
        help_text='Get your API Hash from my.telegram.org > API development tools'
    )

    class Meta:
        model = TelegramAccount
        fields = ['phone_number', 'api_id', 'api_hash']


class TelegramCodeForm(forms.Form):
    """Form for entering the Telegram verification code."""

    code = forms.CharField(
        max_length=10,
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-lg text-center',
            'placeholder': '12345',
            'autocomplete': 'off',
            'autofocus': True,
        })
    )


class TelegramPasswordForm(forms.Form):
    """Form for entering the 2FA password."""

    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control form-control-lg',
            'placeholder': 'Enter your 2FA password',
            'autofocus': True,
        })
    )


class TelegramAccountSettingsForm(forms.ModelForm):
    """Form for editing Telegram account settings."""

    class Meta:
        model = TelegramAccount
        fields = ['display_name', 'is_active', 'max_concurrent_downloads', 'download_profile_photos']
        widgets = {
            'display_name': forms.TextInput(attrs={
                'class': 'form-control',
            }),
            'max_concurrent_downloads': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': 1,
                'max': 10,
            }),
        }
        help_texts = {
            'max_concurrent_downloads': 'Selecting a higher value will increase concurrency, however it will equally trigger Telegram rate limits quicker',
            'is_active': 'Disabled accounts will not sync channels or download files',
            'download_profile_photos': 'Download profile photos when scanning channel members. This increases the time taken to scan members.',
        }


class GlobalSettingsForm(forms.ModelForm):
    """Form for editing global settings."""

    class Meta:
        model = GlobalSettings
        fields = [
            'storage_provider',
            'storage_root',
            'filename_format',
            's3_endpoint_url',
            's3_access_key_id',
            's3_secret_access_key',
            's3_bucket_name',
            's3_region',
            's3_presigned_url_expiry',
            'azure_connection_string',
            'azure_container_name',
            'azure_sas_expiry',
            'auto_discover_sources',
            'default_retry_count',
            'download_queue_interval',
            'channel_sync_interval',
            'channel_stats_interval',
            'media_counts_interval',
            'member_sync_interval',
            'dialog_cache_limit',
            'event_processing_enabled',
            'event_processor_batch_size',
            'event_processor_retry_count',
            'event_processor_retry_backoff_min',
            'event_processor_retry_backoff_max',
            'store_raw_events',
            'stream_raw_events',
        ]
        widgets = {
            'storage_provider': forms.Select(attrs={'class': 'form-select', 'id': 'storage_provider'}),
            'storage_root': forms.TextInput(attrs={'class': 'form-control'}),
            'filename_format': forms.Select(attrs={'class': 'form-select'}),
            's3_endpoint_url': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'https://s3.amazonaws.com',
            }),
            's3_access_key_id': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'AKIAIOSFODNN7EXAMPLE',
            }),
            's3_secret_access_key': forms.PasswordInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter secret key',
            }, render_value=True),
            's3_bucket_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'my-trawlr-bucket',
            }),
            's3_region': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'us-east-1',
            }),
            's3_presigned_url_expiry': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': 60,
                'max': 86400,
                'style': 'max-width: 120px;',
            }),
            'azure_connection_string': forms.PasswordInput(attrs={
                'class': 'form-control',
                'placeholder': 'DefaultEndpointsProtocol=https;AccountName=...',
            }, render_value=True),
            'azure_container_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'trawlr-media',
            }),
            'azure_sas_expiry': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': 60,
                'max': 86400,
                'style': 'max-width: 120px;',
            }),
            'default_retry_count': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': 0,
                'max': 10,
            }),
            'download_queue_interval': forms.Select(attrs={'class': 'form-select'}),
            'channel_sync_interval': forms.Select(attrs={'class': 'form-select'}),
            'channel_stats_interval': forms.Select(attrs={'class': 'form-select'}),
            'media_counts_interval': forms.Select(attrs={'class': 'form-select'}),
            'event_processor_batch_size': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': 1,
                'max': 500,
                'style': 'max-width: 120px;',
            }),
            'event_processor_retry_count': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': 0,
                'max': 10,
                'style': 'max-width: 120px;',
            }),
            'event_processor_retry_backoff_min': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': 1,
                'max': 300,
                'style': 'max-width: 120px;',
            }),
            'event_processor_retry_backoff_max': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': 10,
                'max': 3600,
                'style': 'max-width: 120px;',
            }),
        }
        help_texts = {
            'storage_provider': 'Where to store downloaded media files',
            'storage_root': 'Root directory for local filesystem storage',
            'filename_format': 'How downloaded files should be named',
            's3_endpoint_url': 'S3 endpoint URL. Use https://s3.amazonaws.com for AWS, or your MinIO/Wasabi URL',
            's3_access_key_id': 'S3 access key ID',
            's3_secret_access_key': 'S3 secret access key',
            's3_bucket_name': 'S3 bucket name',
            's3_region': 'S3 region (e.g. us-east-1). Leave blank for non-AWS providers.',
            's3_presigned_url_expiry': 'How long presigned URLs are valid (seconds)',
            'azure_connection_string': 'Azure Blob Storage connection string',
            'azure_container_name': 'Azure Blob container name',
            'azure_sas_expiry': 'How long SAS tokens are valid (seconds)',
            'default_retry_count': 'Number of times to retry failed downloads',
            'download_queue_interval': 'How often to check for and process pending downloads',
            'channel_sync_interval': 'How often to sync channel lists from Telegram',
            'channel_stats_interval': 'How often to update channel statistics (member counts, etc.)',
            'media_counts_interval': 'How often to refresh media file counts for channels',
            'event_processor_batch_size': 'Number of events to process per batch (1-500)',
            'event_processor_retry_count': 'Number of retry attempts for failed event processing',
            'event_processor_retry_backoff_min': 'Minimum seconds to wait between retries',
            'event_processor_retry_backoff_max': 'Maximum seconds to wait between retries (exponential backoff)',
        }
