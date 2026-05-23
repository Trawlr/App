"""
Thin RabbitMQ Management API client.

Wraps the handful of calls ops/views.py needs: list queues and connections,
peek messages, purge queue contents, force-close a connection. Credentials
and base URL come from Django settings.
"""

import json
from urllib.parse import quote

import requests
from django.conf import settings


TIMEOUT = 5
VHOST = '/'


def _auth():
    return (settings.RABBITMQ_USER, settings.RABBITMQ_PASSWORD)


def _url(*parts):
    return settings.RABBITMQ_MANAGEMENT_URL + ''.join(parts)


def list_queues():
    r = requests.get(_url('/queues'), auth=_auth(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def list_consumers():
    r = requests.get(_url('/consumers'), auth=_auth(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def list_connections():
    r = requests.get(_url('/connections'), auth=_auth(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def peek_messages(queue_name, count=20):
    """
    Browse up to ``count`` READY messages non-destructively (they are rejected
    and requeued, so the queue state is unchanged). Does NOT see unacked
    messages — those must be released first via close_connection().
    """
    body = {
        'count': count,
        'ackmode': 'reject_requeue_true',
        'encoding': 'auto',
        'truncate': 50000,
    }
    r = requests.post(
        _url('/queues/', quote(VHOST, safe=''), '/', quote(queue_name, safe=''), '/get'),
        auth=_auth(), timeout=TIMEOUT, json=body,
    )
    r.raise_for_status()
    messages = r.json()
    decoded = []
    for m in messages:
        entry = {'raw': m}
        try:
            body = json.loads(m.get('payload') or '{}')
            entry['actor'] = body.get('actor_name')
            entry['args'] = body.get('args')
            entry['kwargs'] = body.get('kwargs')
            entry['message_id'] = body.get('message_id')
            entry['queue'] = body.get('queue_name')
            entry['options'] = body.get('options')
        except Exception as e:
            entry['decode_error'] = str(e)
        decoded.append(entry)
    return decoded


def purge_queue(queue_name):
    r = requests.delete(
        _url('/queues/', quote(VHOST, safe=''), '/', quote(queue_name, safe=''), '/contents'),
        auth=_auth(), timeout=TIMEOUT,
    )
    return r.status_code == 204


def close_connection(connection_name, reason='closed from ops panel'):
    r = requests.delete(
        _url('/connections/', quote(connection_name, safe='')),
        auth=_auth(), timeout=TIMEOUT,
        headers={'X-Reason': reason},
    )
    return r.status_code == 204
