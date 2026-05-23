"""
Audit app forms for channel configuration.
"""

from django import forms
from .models import ChannelConfig


class ChannelConfigForm(forms.ModelForm):
    """Form for editing channel download configuration."""

    # Hidden field to store the priority order as JSON
    file_type_priority_input = forms.CharField(
        required=False,
        widget=forms.HiddenInput()
    )

    class Meta:
        model = ChannelConfig
        fields = [
            'auto_download_enabled',
            'download_photos',
            'download_videos',
            'download_files',
            'download_thumbnails',
            'thumbnail_size',
            'priority',
            'deduplication_mode',
            'is_paused',
            'bypass_listener',
        ]
        widgets = {
            'priority': forms.Select(attrs={'class': 'form-select'}),
            'deduplication_mode': forms.Select(attrs={'class': 'form-select'}),
            'thumbnail_size': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Initialize the hidden field with current priority order
        if self.instance and self.instance.pk:
            priority_list = self.instance.get_effective_file_type_priority()
            self.fields['file_type_priority_input'].initial = ','.join(priority_list)

    def save(self, commit=True):
        instance = super().save(commit=False)

        # Parse the priority order from the hidden input
        priority_input = self.cleaned_data.get('file_type_priority_input', '')
        if priority_input:
            priority_list = [p.strip() for p in priority_input.split(',') if p.strip()]
            # Validate that all items are valid file types
            valid_types = {'photo', 'video', 'file'}
            priority_list = [p for p in priority_list if p in valid_types]
            # Ensure all types are present
            for ft in valid_types:
                if ft not in priority_list:
                    priority_list.append(ft)
            instance.file_type_priority = priority_list

        if commit:
            instance.save()
        return instance
