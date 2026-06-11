"""
Pagination helpers for search views.
"""

from django.core.paginator import Paginator
from django.utils.functional import cached_property


class CappedCountPaginator(Paginator):
    """
    Paginator that stops counting at ``count_cap`` rows instead of running an
    unbounded COUNT(*) over the whole result set. For large result sets the
    exact total costs more than the search itself, and nobody pages that
    deep — the UI shows "10,000+" instead (see ``is_capped``).
    """

    count_cap = 10_000

    @cached_property
    def _probe_count(self):
        qs = self.object_list
        if hasattr(qs, 'order_by'):
            # Ordering is irrelevant to the count and forces a sort before
            # the LIMIT kicks in.
            qs = qs.order_by()
        try:
            return qs[:self.count_cap + 1].count()
        except (AttributeError, TypeError):
            return len(self.object_list)

    @cached_property
    def count(self):
        return min(self._probe_count, self.count_cap)

    @cached_property
    def is_capped(self):
        return self._probe_count > self.count_cap
