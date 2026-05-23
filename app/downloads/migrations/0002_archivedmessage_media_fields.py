# Generated migration for adding media fields to ArchivedMessage

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('downloads', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='archivedmessage',
            name='has_media',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='archivedmessage',
            name='media_type',
            field=models.CharField(
                blank=True,
                choices=[('photo', 'Photo'), ('video', 'Video'), ('file', 'File')],
                max_length=20
            ),
        ),
        migrations.AddField(
            model_name='archivedmessage',
            name='telegram_file_id',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='archivedmessage',
            name='original_filename',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='archivedmessage',
            name='file_size',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='archivedmessage',
            name='mime_type',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='archivedmessage',
            name='thumbnail_path',
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name='archivedmessage',
            name='downloaded_file',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='archived_messages',
                to='downloads.downloadedfile'
            ),
        ),
        migrations.AddIndex(
            model_name='archivedmessage',
            index=models.Index(fields=['channel', 'has_media'], name='downloads_a_channel_b8d2a4_idx'),
        ),
        migrations.AddIndex(
            model_name='archivedmessage',
            index=models.Index(fields=['channel', 'media_type'], name='downloads_a_channel_c47f12_idx'),
        ),
    ]
