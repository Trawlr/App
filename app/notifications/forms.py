"""
Watchlist entry form. ModelForm with Bootstrap-friendly widgets.

The operator picks an entity_type (dropdown) and types the entity_value
(free-form text, lowercased on save). Webhook target_config is SSRF-validated
at save time.
"""

from django import forms

from .delivery.webhook import SSRFValidationError, validate_webhook_url
from .models import (
    TARGET_RABBITMQ,
    TARGET_WEBHOOK,
    WatchlistEntry,
)


_BOOTSTRAP_INPUT = {'class': 'form-control'}
_BOOTSTRAP_SELECT = {'class': 'form-select'}


class WatchlistEntryForm(forms.ModelForm):
    """
    Operator-facing form. Target config is split into separate fields for
    each target_type and recombined into the JSON column on save.
    """

    # --- Webhook fields ---
    webhook_url = forms.URLField(
        required=False,
        widget=forms.URLInput(attrs={**_BOOTSTRAP_INPUT, 'placeholder': 'https://example.com/webhook'}),
        help_text='URL of the webhook endpoint. Must accept POST requests with application/json body.',
    )
    webhook_secret = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={**_BOOTSTRAP_INPUT, 'autocomplete': 'off'}),
        help_text='Optional shared secret. If set, requests carry X-Trawlr-Signature: sha256=<hex>.',
    )

    # --- RabbitMQ fields ---
    rabbitmq_queue = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={**_BOOTSTRAP_INPUT, 'placeholder': 'trawlr.alerts'}),
    )
    rabbitmq_exchange = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={**_BOOTSTRAP_INPUT, 'placeholder': '(default exchange)'}),
        help_text='Leave blank to use the default exchange with queue name as routing key.',
    )
    rabbitmq_declare = forms.BooleanField(
        required=False,
        help_text='Auto-declare the queue (durable) if it does not exist.',
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )

    class Meta:
        model = WatchlistEntry
        fields = [
            'name', 'description', 'is_active',
            'mode', 'entity_type', 'entity_value',
            'target_type',
            'cooldown_seconds',
        ]
        widgets = {
            'name': forms.TextInput(attrs=_BOOTSTRAP_INPUT),
            'description': forms.Textarea(attrs={**_BOOTSTRAP_INPUT, 'rows': 2}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
            'mode': forms.Select(attrs=_BOOTSTRAP_SELECT),
            'entity_type': forms.Select(attrs=_BOOTSTRAP_SELECT),
            'entity_value': forms.TextInput(attrs={**_BOOTSTRAP_INPUT, 'placeholder': 'google.com.au'}),
            'target_type': forms.Select(attrs=_BOOTSTRAP_SELECT),
            'cooldown_seconds': forms.NumberInput(attrs={**_BOOTSTRAP_INPUT, 'min': 0}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            cfg = self.instance.target_config or {}
            if self.instance.target_type == TARGET_WEBHOOK:
                self.fields['webhook_url'].initial = cfg.get('url', '')
                self.fields['webhook_secret'].initial = cfg.get('secret', '')
            elif self.instance.target_type == TARGET_RABBITMQ:
                self.fields['rabbitmq_queue'].initial = cfg.get('queue', '')
                self.fields['rabbitmq_exchange'].initial = cfg.get('exchange', '')
                self.fields['rabbitmq_declare'].initial = bool(cfg.get('declare', False))

    def clean_entity_value(self):
        value = (self.cleaned_data.get('entity_value') or '').strip().lower()
        if not value:
            raise forms.ValidationError('Entity value is required.')
        return value

    def clean(self):
        cleaned = super().clean()

        target_type = cleaned.get('target_type')
        if target_type == TARGET_WEBHOOK:
            url = (cleaned.get('webhook_url') or '').strip()
            if not url:
                self.add_error('webhook_url', 'A webhook URL is required for webhook targets.')
            else:
                try:
                    validate_webhook_url(url)
                except SSRFValidationError as e:
                    self.add_error('webhook_url', str(e))
        elif target_type == TARGET_RABBITMQ:
            queue = (cleaned.get('rabbitmq_queue') or '').strip()
            if not queue:
                self.add_error('rabbitmq_queue', 'A queue name is required for RabbitMQ targets.')
        return cleaned

    def save(self, commit=True):
        entry = super().save(commit=False)
        if entry.target_type == TARGET_WEBHOOK:
            entry.target_config = {
                'url': (self.cleaned_data.get('webhook_url') or '').strip(),
                'secret': (self.cleaned_data.get('webhook_secret') or '').strip(),
            }
        elif entry.target_type == TARGET_RABBITMQ:
            entry.target_config = {
                'queue': (self.cleaned_data.get('rabbitmq_queue') or '').strip(),
                'exchange': (self.cleaned_data.get('rabbitmq_exchange') or '').strip(),
                'declare': bool(self.cleaned_data.get('rabbitmq_declare')),
            }
        if commit:
            entry.save()
        return entry
