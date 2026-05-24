"""
Watchlist UI views. Single-user; everything is `@login_required`.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .forms import WatchlistEntryForm
from .models import NotificationDelivery, WatchlistEntry


@login_required
def list_entries(request):
    """The list lives as a tab on /settings/. Keep the URL for back-compat."""
    return redirect(reverse('accounts:settings') + '#notifications')


@login_required
def add_entry(request):
    if request.method == 'POST':
        form = WatchlistEntryForm(request.POST)
        if form.is_valid():
            entry = form.save()
            messages.success(request, f'Notification "{entry.name}" created.')
            return redirect(reverse('accounts:settings') + '#notifications')
    else:
        form = WatchlistEntryForm()
    return render(request, 'notifications/form.html', {'form': form, 'mode': 'add'})


@login_required
def edit_entry(request, pk):
    entry = get_object_or_404(WatchlistEntry, pk=pk)
    if request.method == 'POST':
        form = WatchlistEntryForm(request.POST, instance=entry)
        if form.is_valid():
            form.save()
            messages.success(request, f'Notification "{entry.name}" updated.')
            return redirect(reverse('accounts:settings') + '#notifications')
    else:
        form = WatchlistEntryForm(instance=entry)
    return render(request, 'notifications/form.html', {'form': form, 'entry': entry, 'mode': 'edit'})


@login_required
@require_POST
def delete_entry(request, pk):
    entry = get_object_or_404(WatchlistEntry, pk=pk)
    name = entry.name
    entry.delete()
    messages.success(request, f'Deleted notification "{name}".')
    return redirect(reverse('accounts:settings') + '#notifications')


@login_required
@require_POST
def test_entry(request, pk):
    """Synthesize a sample payload and fire one delivery to validate config."""
    from .matcher import build_test_delivery
    from .tasks import deliver_notification

    entry = get_object_or_404(WatchlistEntry, pk=pk)
    delivery = build_test_delivery(entry)
    deliver_notification.send(delivery.pk)
    messages.success(request, f'Test delivery #{delivery.pk} queued for "{entry.name}".')
    return HttpResponseRedirect(reverse('notifications:deliveries', args=[entry.pk]))


@login_required
def entry_deliveries(request, pk):
    entry = get_object_or_404(WatchlistEntry, pk=pk)
    deliveries = NotificationDelivery.objects.filter(entry=entry).order_by('-created_at')
    paginator = Paginator(deliveries, 25)
    page = paginator.get_page(request.GET.get('page'))
    return render(request, 'notifications/deliveries.html', {
        'entry': entry,
        'page_obj': page,
    })
