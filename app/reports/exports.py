"""
Export functions for reports (CSV and JSON).
"""

import csv
import json
from datetime import date
from django.http import StreamingHttpResponse, JsonResponse
from django.utils import timezone

from . import queries


class Echo:
    """An object that implements just the write method of the file-like interface."""
    def write(self, value):
        return value


def _format_date(dt):
    """Format datetime for export."""
    if dt:
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    return ''


def _get_filename(report_type, format_type, days_label):
    """Generate export filename."""
    date_str = date.today().strftime('%Y%m%d')
    return f"trawlr_{report_type}_{days_label}d_{date_str}.{format_type}"


# =============================================================================
# Content Analytics Export
# =============================================================================

def export_content_csv(start_date, end_date, days_label):
    """Export content analytics as CSV."""

    def generate():
        writer = csv.writer(Echo())

        # Header info
        yield writer.writerow(['Trawlr Content Analytics Report'])
        yield writer.writerow([f'Period: {start_date.date()} to {end_date.date()}'])
        yield writer.writerow([])

        # Engagement stats
        stats = queries.get_engagement_stats(start_date, end_date)
        yield writer.writerow(['=== Summary ==='])
        yield writer.writerow(['Metric', 'Value'])
        yield writer.writerow(['Total Messages', stats['total_messages']])
        yield writer.writerow(['With Media', stats['with_media']])
        yield writer.writerow(['Total Views', stats['total_views'] or 0])
        yield writer.writerow(['Total Forwards', stats['total_forwards'] or 0])
        yield writer.writerow(['Edited Messages', stats['edited_count']])
        yield writer.writerow(['Pinned Messages', stats['pinned_count']])
        yield writer.writerow(['Deleted Messages', stats['deleted_count']])
        yield writer.writerow([])

        # Media breakdown
        yield writer.writerow(['=== Media Distribution ==='])
        yield writer.writerow(['Type', 'Count'])
        for item in queries.get_media_distribution(start_date, end_date):
            yield writer.writerow([item['media_type'], item['count']])
        yield writer.writerow([])

        # Message volume by day
        yield writer.writerow(['=== Daily Message Volume ==='])
        yield writer.writerow(['Date', 'Count'])
        for item in queries.get_message_volume(start_date, end_date):
            yield writer.writerow([item['date'].strftime('%Y-%m-%d'), item['count']])
        yield writer.writerow([])

        # Top URLs
        yield writer.writerow(['=== Top URLs ==='])
        yield writer.writerow(['URL', 'Count'])
        for item in queries.get_top_urls(start_date, end_date):
            yield writer.writerow([item['url'], item['count']])
        yield writer.writerow([])

        # Top Hashtags
        yield writer.writerow(['=== Top Hashtags ==='])
        yield writer.writerow(['Hashtag', 'Count'])
        for item in queries.get_top_entities(start_date, end_date, 'hashtag'):
            yield writer.writerow([item['text'], item['count']])
        yield writer.writerow([])

        # Top Mentions
        yield writer.writerow(['=== Top Mentions ==='])
        yield writer.writerow(['Mention', 'Count'])
        for item in queries.get_top_entities(start_date, end_date, 'mention'):
            yield writer.writerow([item['text'], item['count']])
        yield writer.writerow([])

        # Forward Sources
        yield writer.writerow(['=== Top Forward Sources ==='])
        yield writer.writerow(['Source', 'Type', 'Username', 'Count'])
        for item in queries.get_top_forward_sources(start_date, end_date):
            yield writer.writerow([
                item['source_title'], item['source_type'],
                item['source_username'] or '', item['count']
            ])

    response = StreamingHttpResponse(generate(), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{_get_filename("content", "csv", days_label)}"'
    return response


def export_content_json(start_date, end_date, days_label):
    """Export content analytics as JSON."""
    stats = queries.get_engagement_stats(start_date, end_date)

    data = {
        'report': 'content_analytics',
        'generated_at': timezone.now().isoformat(),
        'period': {
            'start': start_date.isoformat(),
            'end': end_date.isoformat(),
        },
        'summary': stats,
        'media_distribution': list(queries.get_media_distribution(start_date, end_date)),
        'daily_volume': [
            {'date': item['date'].isoformat(), 'count': item['count']}
            for item in queries.get_message_volume(start_date, end_date)
        ],
        'top_urls': list(queries.get_top_urls(start_date, end_date)),
        'top_hashtags': list(queries.get_top_entities(start_date, end_date, 'hashtag')),
        'top_mentions': list(queries.get_top_entities(start_date, end_date, 'mention')),
        'top_forward_sources': list(queries.get_top_forward_sources(start_date, end_date)),
    }

    response = JsonResponse(data, json_dumps_params={'indent': 2, 'ensure_ascii': False})
    response['Content-Disposition'] = f'attachment; filename="{_get_filename("content", "json", days_label)}"'
    return response


# =============================================================================
# User Intelligence Export
# =============================================================================

def export_users_csv(start_date, end_date, days_label):
    """Export user intelligence as CSV."""

    def generate():
        writer = csv.writer(Echo())

        # Header info
        yield writer.writerow(['Trawlr User Intelligence Report'])
        yield writer.writerow([f'Period: {start_date.date()} to {end_date.date()}'])
        yield writer.writerow([])

        # User stats
        stats = queries.get_user_activity_stats(start_date, end_date)
        yield writer.writerow(['=== Summary ==='])
        yield writer.writerow(['Metric', 'Value'])
        yield writer.writerow(['Total Users Tracked', stats['total_users']])
        yield writer.writerow(['New Users (period)', stats['new_users']])
        yield writer.writerow(['Active Users (period)', stats['active_users']])
        yield writer.writerow(['Flagged Users', stats['flagged_users']])
        yield writer.writerow(['Premium Users', stats['premium_users']])
        yield writer.writerow(['Verified Users', stats['verified_users']])
        yield writer.writerow(['Bots', stats['bot_count']])
        yield writer.writerow(['Scam Accounts', stats['scam_count']])
        yield writer.writerow(['Fake Accounts', stats['fake_count']])
        yield writer.writerow(['Restricted Accounts', stats['restricted_count']])
        yield writer.writerow([])

        # Churn stats
        churn = queries.get_user_churn(start_date, end_date)
        yield writer.writerow(['=== Churn Analysis ==='])
        yield writer.writerow(['Total Memberships', churn['total_memberships']])
        yield writer.writerow(['Churned', churn['churned_count']])
        yield writer.writerow(['Churn Rate', f"{churn['churn_rate']}%"])
        yield writer.writerow([])

        # New users by day
        yield writer.writerow(['=== New Users by Day ==='])
        yield writer.writerow(['Date', 'Count'])
        for item in queries.get_new_users_by_day(start_date, end_date):
            yield writer.writerow([item['date'].strftime('%Y-%m-%d'), item['count']])
        yield writer.writerow([])

        # Top posters
        yield writer.writerow(['=== Top Posters ==='])
        yield writer.writerow(['Username', 'Name', 'Message Count', 'Premium', 'Verified'])
        for user in queries.get_top_posters():
            yield writer.writerow([
                f"@{user.username}" if user.username else '',
                f"{user.first_name} {user.last_name}".strip(),
                user.message_count,
                'Yes' if user.is_premium else 'No',
                'Yes' if user.is_verified else 'No',
            ])
        yield writer.writerow([])

        # Cross-channel users
        yield writer.writerow(['=== Cross-Channel Users ==='])
        yield writer.writerow(['Username', 'Name', 'Channel Count'])
        for user in queries.get_cross_channel_users():
            yield writer.writerow([
                f"@{user.username}" if user.username else '',
                f"{user.first_name} {user.last_name}".strip(),
                user.channel_count,
            ])
        yield writer.writerow([])

        # Flagged users
        yield writer.writerow(['=== Flagged Users ==='])
        yield writer.writerow(['Username', 'Name', 'Reason', 'Flagged At'])
        for user in queries.get_flagged_users():
            yield writer.writerow([
                f"@{user.username}" if user.username else '',
                f"{user.first_name} {user.last_name}".strip(),
                user.flagged_reason,
                _format_date(user.flagged_at),
            ])

    response = StreamingHttpResponse(generate(), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{_get_filename("users", "csv", days_label)}"'
    return response


def export_users_json(start_date, end_date, days_label):
    """Export user intelligence as JSON."""
    stats = queries.get_user_activity_stats(start_date, end_date)
    churn = queries.get_user_churn(start_date, end_date)

    data = {
        'report': 'user_intelligence',
        'generated_at': timezone.now().isoformat(),
        'period': {
            'start': start_date.isoformat(),
            'end': end_date.isoformat(),
        },
        'summary': stats,
        'churn': churn,
        'new_users_by_day': [
            {'date': item['date'].isoformat(), 'count': item['count']}
            for item in queries.get_new_users_by_day(start_date, end_date)
        ],
        'top_posters': [
            {
                'id': u.id,
                'telegram_id': u.telegram_id,
                'username': u.username,
                'name': f"{u.first_name} {u.last_name}".strip(),
                'message_count': u.message_count,
                'is_premium': u.is_premium,
                'is_verified': u.is_verified,
            }
            for u in queries.get_top_posters()
        ],
        'cross_channel_users': [
            {
                'id': u.id,
                'telegram_id': u.telegram_id,
                'username': u.username,
                'name': f"{u.first_name} {u.last_name}".strip(),
                'channel_count': u.channel_count,
            }
            for u in queries.get_cross_channel_users()
        ],
        'flagged_users': [
            {
                'id': u.id,
                'telegram_id': u.telegram_id,
                'username': u.username,
                'name': f"{u.first_name} {u.last_name}".strip(),
                'reason': u.flagged_reason,
                'flagged_at': _format_date(u.flagged_at),
            }
            for u in queries.get_flagged_users()
        ],
    }

    response = JsonResponse(data, json_dumps_params={'indent': 2, 'ensure_ascii': False})
    response['Content-Disposition'] = f'attachment; filename="{_get_filename("users", "json", days_label)}"'
    return response


# =============================================================================
# Source Analytics Export
# =============================================================================

def export_sources_csv(start_date, end_date, days_label):
    """Export source analytics as CSV."""

    def generate():
        writer = csv.writer(Echo())

        # Header info
        yield writer.writerow(['Trawlr Source Analytics Report'])
        yield writer.writerow([f'Period: {start_date.date()} to {end_date.date()}'])
        yield writer.writerow([])

        # Source stats
        stats = queries.get_source_stats(start_date, end_date)
        yield writer.writerow(['=== Summary ==='])
        yield writer.writerow(['Metric', 'Value'])
        yield writer.writerow(['Total Sources', stats['total_sources']])
        yield writer.writerow(['Active Sources', stats['active_sources']])
        yield writer.writerow(['New Sources (period)', stats['new_sources']])
        yield writer.writerow(['Forum Channels', stats['forum_count']])
        yield writer.writerow(['Verified Channels', stats['verified_count']])
        yield writer.writerow(['Scam Channels', stats['scam_count']])
        yield writer.writerow(['Fake Channels', stats['fake_count']])
        yield writer.writerow([])

        # Channel health
        yield writer.writerow(['=== Channel Health ==='])
        yield writer.writerow(['Status', 'Count'])
        for item in queries.get_channel_health():
            yield writer.writerow([item['availability_status'], item['count']])
        yield writer.writerow([])

        # Channel types
        yield writer.writerow(['=== Channel Types ==='])
        yield writer.writerow(['Type', 'Count'])
        for item in queries.get_channel_type_breakdown():
            yield writer.writerow([item['channel_type'], item['count']])
        yield writer.writerow([])

        # Content volume by source
        yield writer.writerow(['=== Top Sources by Content ==='])
        yield writer.writerow(['Channel', 'Message Count'])
        for item in queries.get_content_volume_by_source(start_date, end_date):
            yield writer.writerow([item['channel__title'], item['count']])
        yield writer.writerow([])

        # Download progress
        progress = queries.get_download_progress()
        yield writer.writerow(['=== Download Progress ==='])
        yield writer.writerow(['Total Channels', progress['total_channels']])
        yield writer.writerow(['Complete', progress['complete']])
        yield writer.writerow(['Incomplete', progress['incomplete']])
        yield writer.writerow(['Average Progress', f"{progress['avg_progress']}%"])
        yield writer.writerow([])

        # Incomplete downloads
        yield writer.writerow(['=== Incomplete Downloads ==='])
        yield writer.writerow(['Channel', 'Progress', 'Downloaded', 'Total'])
        for item in progress['incomplete_list']:
            yield writer.writerow([
                item['channel__title'],
                f"{item['download_progress_percent']}%",
                item['downloaded_messages'],
                item['total_messages'],
            ])

    response = StreamingHttpResponse(generate(), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{_get_filename("sources", "csv", days_label)}"'
    return response


def export_sources_json(start_date, end_date, days_label):
    """Export source analytics as JSON."""
    stats = queries.get_source_stats(start_date, end_date)
    progress = queries.get_download_progress()

    data = {
        'report': 'source_analytics',
        'generated_at': timezone.now().isoformat(),
        'period': {
            'start': start_date.isoformat(),
            'end': end_date.isoformat(),
        },
        'summary': stats,
        'channel_health': list(queries.get_channel_health()),
        'channel_types': list(queries.get_channel_type_breakdown()),
        'content_volume': list(queries.get_content_volume_by_source(start_date, end_date)),
        'download_progress': progress,
        'forum_activity': list(queries.get_forum_activity()),
    }

    response = JsonResponse(data, json_dumps_params={'indent': 2, 'ensure_ascii': False})
    response['Content-Disposition'] = f'attachment; filename="{_get_filename("sources", "json", days_label)}"'
    return response


# =============================================================================
# Investigation Export
# =============================================================================

def export_investigation_csv(start_date, end_date, days_label):
    """Export investigation report as CSV."""

    def generate():
        writer = csv.writer(Echo())

        # Header info
        yield writer.writerow(['Trawlr Investigation Report'])
        yield writer.writerow([f'Period: {start_date.date()} to {end_date.date()}'])
        yield writer.writerow([])

        # Report stats
        report_stats = queries.get_report_stats(start_date, end_date)
        yield writer.writerow(['=== Telegram Reports Submitted ==='])
        yield writer.writerow(['Metric', 'Value'])
        yield writer.writerow(['Total Reports', report_stats['total']])
        yield writer.writerow(['Successful', report_stats['successful']])
        yield writer.writerow(['Failed', report_stats['failed']])
        yield writer.writerow(['Success Rate', f"{report_stats['success_rate']}%"])
        yield writer.writerow([])

        # Reports by type
        yield writer.writerow(['=== Reports by Type ==='])
        yield writer.writerow(['Type', 'Count'])
        for item in report_stats['by_type']:
            yield writer.writerow([item['report_type'], item['count']])
        yield writer.writerow([])

        # Reports by reason
        yield writer.writerow(['=== Reports by Reason ==='])
        yield writer.writerow(['Reason', 'Count'])
        for item in report_stats['by_reason']:
            yield writer.writerow([item['reason'], item['count']])
        yield writer.writerow([])

        # Exclusion stats
        exclusion_stats = queries.get_exclusion_stats()
        yield writer.writerow(['=== Exclusion Rules ==='])
        yield writer.writerow(['Metric', 'Value'])
        yield writer.writerow(['Total Active Rules', exclusion_stats['total_rules']])
        yield writer.writerow(['Global Rules', exclusion_stats['global_rules']])
        yield writer.writerow(['Source-Specific Rules', exclusion_stats['source_specific']])
        yield writer.writerow(['Total Triggers', exclusion_stats['total_triggers']])
        yield writer.writerow([])

        # Top excluded users
        yield writer.writerow(['=== Top Excluded Users ==='])
        yield writer.writerow(['Username', 'Name', 'Rules', 'Triggers'])
        for item in exclusion_stats['top_excluded']:
            name = f"{item.get('telegram_user__first_name', '')} {item.get('telegram_user__last_name', '')}".strip()
            yield writer.writerow([
                f"@{item['telegram_user__username']}" if item['telegram_user__username'] else '',
                name,
                item['rule_count'],
                item['total_triggers'],
            ])
        yield writer.writerow([])

        # Notes count
        notes_count = queries.get_user_notes_count(start_date, end_date)
        yield writer.writerow(['=== User Notes ==='])
        yield writer.writerow(['Notes Created (period)', notes_count])

    response = StreamingHttpResponse(generate(), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{_get_filename("investigation", "csv", days_label)}"'
    return response


def export_investigation_json(start_date, end_date, days_label):
    """Export investigation report as JSON."""
    data = {
        'report': 'investigation',
        'generated_at': timezone.now().isoformat(),
        'period': {
            'start': start_date.isoformat(),
            'end': end_date.isoformat(),
        },
        'telegram_reports': queries.get_report_stats(start_date, end_date),
        'reports_by_day': [
            {
                'date': item['date'].isoformat(),
                'count': item['count'],
                'successful': item['successful'],
            }
            for item in queries.get_reports_by_day(start_date, end_date)
        ],
        'exclusions': queries.get_exclusion_stats(),
        'notes_created': queries.get_user_notes_count(start_date, end_date),
    }

    response = JsonResponse(data, json_dumps_params={'indent': 2, 'ensure_ascii': False})
    response['Content-Disposition'] = f'attachment; filename="{_get_filename("investigation", "json", days_label)}"'
    return response


# =============================================================================
# Media Inventory Export
# =============================================================================

def _media_inventory_filename(format_type, days_label, media_type):
    """Filename for the media inventory export — picks up the media-type lens."""
    date_str = date.today().strftime('%Y%m%d')
    suffix = f"_{media_type}" if media_type else ''
    return f"trawlr_media_inventory{suffix}_{days_label}d_{date_str}.{format_type}"


def _inventory_row_for_export(row):
    """Flatten a media inventory row into a dict suitable for CSV/JSON export."""
    ch = row['channel']
    account = ch.account
    return {
        'channel_id': ch.id,
        'channel_title': ch.title or '',
        'channel_username': ch.username or '',
        'channel_type': ch.channel_type or '',
        'account_id': account.id if account else None,
        'account_name': (account.display_name or account.phone_number) if account else '',
        'photo_count': row['photo_count'],
        'photo_size_bytes': row['photo_size'],
        'video_count': row['video_count'],
        'video_size_bytes': row['video_size'],
        'file_count': row['file_count'],
        'file_size_bytes': row['file_size'],
        'total_size_bytes': row['total_size'],
        'last_downloaded': _format_date(row['last_downloaded']),
        'configured_only': row['configured_only'],
    }


def export_media_inventory_csv(start_date, end_date, days_label,
                               media_type=None, size_band=None, account_id=None):
    """Export per-source media inventory as CSV (respects current page filters)."""
    rows = queries.get_source_media_inventory(
        media_type=media_type, size_band=size_band, account_id=account_id,
    )

    columns = [
        ('channel_id', 'channel_id'),
        ('channel_title', 'channel_title'),
        ('channel_username', 'channel_username'),
        ('channel_type', 'channel_type'),
        ('account_id', 'account_id'),
        ('account_name', 'account_name'),
        ('photo_count', 'photo_count'),
        ('photo_size_bytes', 'photo_size_bytes'),
        ('video_count', 'video_count'),
        ('video_size_bytes', 'video_size_bytes'),
        ('file_count', 'file_count'),
        ('file_size_bytes', 'file_size_bytes'),
        ('total_size_bytes', 'total_size_bytes'),
        ('last_downloaded', 'last_downloaded'),
        ('configured_only', 'configured_only'),
    ]

    def generate():
        writer = csv.writer(Echo())
        yield writer.writerow([header for header, _ in columns])
        for row in rows:
            r = _inventory_row_for_export(row)
            yield writer.writerow([r[key] if r[key] is not None else '' for _, key in columns])

    response = StreamingHttpResponse(generate(), content_type='text/csv')
    response['Content-Disposition'] = (
        f'attachment; filename="{_media_inventory_filename("csv", days_label, media_type)}"'
    )
    return response


def export_media_inventory_json(start_date, end_date, days_label,
                                media_type=None, size_band=None, account_id=None):
    """Export per-source media inventory as JSON (respects current page filters)."""
    rows = queries.get_source_media_inventory(
        media_type=media_type, size_band=size_band, account_id=account_id,
    )
    totals = queries.summarize_inventory_totals(rows, media_type=media_type)

    data = {
        'report': 'media_inventory',
        'generated_at': timezone.now().isoformat(),
        'period': {
            'start': start_date.isoformat(),
            'end': end_date.isoformat(),
        },
        'filters': {
            'media_type': media_type,
            'size_band': size_band,
            'account_id': account_id,
        },
        'totals': totals,
        'sources': [_inventory_row_for_export(row) for row in rows],
    }

    response = JsonResponse(data, json_dumps_params={'indent': 2, 'ensure_ascii': False})
    response['Content-Disposition'] = (
        f'attachment; filename="{_media_inventory_filename("json", days_label, media_type)}"'
    )
    return response
