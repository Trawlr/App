from django.core.paginator import Paginator
from django.db import connection


class EstimatedCountPaginator(Paginator):
    """
    Paginator that uses PostgreSQL's reltuples estimate for unfiltered querysets.
    Falls back to exact COUNT(*) when filters are applied.
    """

    def __init__(self, *args, is_filtered=False, **kwargs):
        self._is_filtered = is_filtered
        super().__init__(*args, **kwargs)

    @property
    def count(self):
        if self._is_filtered:
            return super().count

        # Use reltuples estimate for unfiltered queries
        try:
            db_table = self.object_list.model._meta.db_table
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT reltuples::bigint FROM pg_class WHERE relname = %s",
                    [db_table],
                )
                row = cursor.fetchone()
                if row and row[0] > 0:
                    return row[0]
        except Exception:
            pass

        return super().count
