# Generated migration for adding download_thumbnails to ChannelConfig

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('audit', '0004_telegramchannel_is_favourite'),
    ]

    operations = [
        migrations.AddField(
            model_name='channelconfig',
            name='download_thumbnails',
            field=models.BooleanField(
                default=True,
                help_text='Always download thumbnails for media posts (enables browsing without full downloads)'
            ),
        ),
    ]
