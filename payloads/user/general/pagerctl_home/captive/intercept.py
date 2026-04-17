"""intercept.py - nftables transparent HTTP intercept for captive portal.

Uses OpenWrt's `fw4` nftables table. Adds redirect rules to the
existing `dstnat` chain that catch port 80 traffic coming IN on the
captive AP interfaces (wlan0mgmt, wlan0wpa) and bend it to the local
HTTP server. This is what makes captive-portal probes from iOS/
Android/Windows actually hit our server even when the phone isn't
using our DNS (DoH bypass etc.).

Rules are tagged with comment "pagerctl_captive" so we can find and
remove them on stop without disturbing fw4's other rules.
"""

import os
import re
import subprocess


COMMENT_TAG = 'pagerctl_captive'
KILL_TAG = 'pagerctl_captive_killnet'


def _run(cmd):
    """Run nft, return stdout. Silent on failure."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                                timeout=4).stdout or ''
    except Exception:
        return ''


def enable(https=False):
    """Install the redirect rules. Idempotent — removes any
    existing pagerctl_captive rules first to avoid duplicates.

    Filters by `iifname "br-lan"` (not `wlan0mgmt`) because
    br_netfilter isn't loaded on this kernel — the inet/IP nft
    hooks see bridged routed packets as arriving from the bridge.

    Args:
      https: also redirect tcp 443 to the local TLS server. This
        breaks iOS's HTTPS connectivity checks (apple.com, push
        services) because every TLS handshake fails cert validation
        against our self-signed cert, so iOS reports "no internet".
        Only enable for explicit HTTPS-spoof testing.
    """
    disable()
    rules = [
        ['nft', 'add', 'rule', 'inet', 'fw4', 'dstnat',
         'iifname', 'br-lan', 'tcp', 'dport', '80',
         'counter', 'redirect', 'to', ':80', 'comment', COMMENT_TAG],
        # Plain DNS catch-all (DoH bypass not handled here)
        ['nft', 'add', 'rule', 'inet', 'fw4', 'dstnat',
         'iifname', 'br-lan', 'udp', 'dport', '53',
         'counter', 'redirect', 'to', ':53', 'comment', COMMENT_TAG],
        ['nft', 'add', 'rule', 'inet', 'fw4', 'dstnat',
         'iifname', 'br-lan', 'tcp', 'dport', '53',
         'counter', 'redirect', 'to', ':53', 'comment', COMMENT_TAG],
    ]
    if https:
        rules.append([
            'nft', 'add', 'rule', 'inet', 'fw4', 'dstnat',
            'iifname', 'br-lan', 'tcp', 'dport', '443',
            'counter', 'redirect', 'to', ':443',
            'comment', COMMENT_TAG,
        ])
    for cmd in rules:
        _run(cmd)
    return True


def disable():
    """Find every dstnat rule tagged with pagerctl_captive and
    delete them by handle. Also clear any internet-kill rule
    so stopping captive doesn't leave clients permanently
    disconnected."""
    listing = _run(['nft', '-a', 'list', 'chain', 'inet', 'fw4', 'dstnat'])
    handles = []
    for line in listing.split('\n'):
        if COMMENT_TAG not in line:
            continue
        m = re.search(r'#\s*handle\s+(\d+)', line)
        if m:
            handles.append(m.group(1))
    for h in handles:
        _run(['nft', 'delete', 'rule', 'inet', 'fw4', 'dstnat',
              'handle', h])
    _kill_disable()
    return True


def is_active():
    listing = _run(['nft', 'list', 'chain', 'inet', 'fw4', 'dstnat'])
    return COMMENT_TAG in listing


# ----------------------------------------------------------------------
# Internet kill switch
# ----------------------------------------------------------------------

def set_internet(allowed):
    """Toggle real internet for br-lan clients.

    allowed=True: remove any kill-net drop rules, normal routing.
    allowed=False: install a forward-chain drop rule that blocks
    every br-lan → wlan0cli packet, killing real internet for the
    captive AP. The pager itself stays reachable (input chain, not
    forward), so the captive portal/server keep working — clients
    can only see the spoof library and any cached assets.

    Side effect: this is a blanket br-lan → wan drop, so any other
    br-lan client (USB ethernet laptop, etc.) also loses internet
    while the kill is on. Only enable during a live demo.
    """
    _kill_disable()  # always start clean
    if not allowed:
        _run(['nft', 'add', 'rule', 'inet', 'fw4', 'forward',
              'iifname', 'br-lan', 'oifname', 'wlan0cli',
              'counter', 'drop', 'comment', KILL_TAG])
    return True


def _kill_disable():
    listing = _run(['nft', '-a', 'list', 'chain', 'inet', 'fw4', 'forward'])
    for line in listing.split('\n'):
        if KILL_TAG not in line:
            continue
        m = re.search(r'#\s*handle\s+(\d+)', line)
        if m:
            _run(['nft', 'delete', 'rule', 'inet', 'fw4', 'forward',
                  'handle', m.group(1)])


def internet_blocked():
    listing = _run(['nft', 'list', 'chain', 'inet', 'fw4', 'forward'])
    return KILL_TAG in listing
