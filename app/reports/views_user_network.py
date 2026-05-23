"""
Per-user Network tab — JSON endpoints.

One module, three endpoints, one per panel on the user-detail Network tab:
  - api_user_lanes    swim-lane timeline by group
  - api_user_flow     sankey: inbound forward sources -> user -> outbound destinations
  - api_user_copost   co-poster vis.js graph

Intentionally separate from views_cib.py / views_activity.py because the
shape is different: every response is keyed to a single TelegramUser.
"""

from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from silk.profiling.profiler import silk_profile

from audit.models import TelegramChannel, TelegramUser, UserGroupMembership

from . import queries_user_network as qn


def _user_or_404(pk):
    """Mirror the access check used by audit.views.user_detail."""
    user = get_object_or_404(TelegramUser, pk=pk)
    visible = TelegramChannel.objects.from_active_accounts().all()
    if not UserGroupMembership.objects.filter(user=user, channel__in=visible).exists():
        raise Http404('User not visible to active accounts')
    return user


@login_required
@silk_profile(name='reports.api_user_lanes')
def api_user_lanes(request, pk):
    user = _user_or_404(pk)
    days = qn.parse_days(request)
    return JsonResponse(qn.get_group_lanes(user, days))


@login_required
@silk_profile(name='reports.api_user_flow')
def api_user_flow(request, pk):
    user = _user_or_404(pk)
    days = qn.parse_days(request)
    return JsonResponse(qn.get_content_flow(user, days))


@login_required
@silk_profile(name='reports.api_user_copost')
def api_user_copost(request, pk):
    user = _user_or_404(pk)
    days = qn.parse_days(request)
    window_seconds = qn.parse_window_seconds(request)
    min_overlap = qn.parse_min_overlap(request)
    return JsonResponse(qn.get_co_posters(user, days, window_seconds, min_overlap))
