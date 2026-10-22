"""Shared automatic-research channel switches; historical rows are unaffected."""
import json
from pathlib import Path
from urllib.parse import urlsplit


CONFIG_PATH = Path(__file__).resolve().parents[2] / 'config' / 'research_channels.json'
CHANNELS = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))


def blocked_reason(channel_id=None, url=None):
    host = (urlsplit(url).hostname or '').lower().rstrip('.') if url else ''
    for key, policy in CHANNELS.items():
        matches = key == channel_id or (host and any(
            host == domain or host.endswith('.' + domain) for domain in policy.get('hosts', ())))
        if matches and not policy['enabled']:
            return 'CHANNEL_DISABLED: ' + key + ': ' + policy['reason']
    return None


def require_enabled(channel_id=None, url=None):
    reason = blocked_reason(channel_id, url)
    if reason:
        raise ValueError(reason)


def summary():
    # AVAILABLE describes configuration only, not successful collection or body access.
    return [{'channel_id': key, 'enabled': policy['enabled'],
             'status': 'AVAILABLE' if policy['enabled'] else 'UNAVAILABLE',
             'reason': policy['reason'], 'evidence': policy.get('evidence')}
            for key, policy in CHANNELS.items()]
