"""Add indexes on (id, history_date) to historical tables.

simple_history only creates an index on history_date.  Queries like
``obj.history.all()`` filter on ``id`` (the original model PK) and
ORDER BY history_date DESC, which causes a full backward scan of the
history_date index.  A composite index on (id, -history_date) lets
PostgreSQL do a direct lookup.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0036_add_perf_indexes_entity_forward"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="historicaltelegramchannel",
            index=models.Index(
                fields=["id", "-history_date"],
                name="hist_tgchannel_id_date_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="historicaltelegramuser",
            index=models.Index(
                fields=["id", "-history_date"],
                name="hist_tguser_id_date_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="historicalusergroupmembership",
            index=models.Index(
                fields=["id", "-history_date"],
                name="hist_membership_id_date_idx",
            ),
        ),
    ]
