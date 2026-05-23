# Fix deleted_from_disk to have database-level default

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('downloads', '0010_downloadedfile_deleted_from_disk'),
    ]

    operations = [
        migrations.AlterField(
            model_name='downloadedfile',
            name='deleted_from_disk',
            field=models.BooleanField(default=False, db_default=False),
        ),
    ]
