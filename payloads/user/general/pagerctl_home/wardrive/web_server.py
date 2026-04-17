"""web_server.py - Pagerctl Home web UI.

Generic pager management server — lives here under wardrive/ for legacy
import compatibility but is not wardrive-specific. Serves the modular
SPA in pagerctl_home/web/ and exposes APIs for live framebuffer mirror,
button injection, shell exec, captive portal control, wardrive control,
loot downloads, settings management, and system actions.

Adapted from loki's webapp.py pattern: plain HTTPServer + custom handler,
gzipped static file serving, client-side rendering (no Flask/WS).
"""

import os
import io
import base64
import gzip
import json
import queue
import shutil
import struct
import subprocess
import threading
import time
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, unquote

from wardrive.config import (LOOT_DIR, EXPORT_DIR, CAPTURE_DIR, DB_PATH,
                              load_config, save_config)


# Resolved at start_web_ui time
WEB_DIR = None
HOME_DIR = None  # pagerctl_home root

# Shared virtual-button queue. The web Control panel writes to it, and
# every UI loop drains it each frame so a press works regardless of
# which screen the user is on.
virt_buttons = queue.Queue()


def set_button_queue(q):
    """Legacy shim — no-op."""
    pass


def drain_virt_button():
    """Pop the next queued virtual button name, or None if empty."""
    try:
        return virt_buttons.get_nowait()
    except queue.Empty:
        return None


def wait_any_button(pager, poll_ms=30):
    """Block until a physical or virtual button press."""
    import time as _t
    try:
        from pagerctl import PAGER_EVENT_PRESS
    except Exception:
        PAGER_EVENT_PRESS = 1
    virt_map = {
        'up':    pager.BTN_UP,
        'down':  pager.BTN_DOWN,
        'left':  pager.BTN_LEFT,
        'right': pager.BTN_RIGHT,
        'a':     pager.BTN_A,
        'b':     pager.BTN_B,
        'power': pager.BTN_POWER,
    }
    while True:
        while pager.has_input_events():
            event = pager.get_input_event()
            if not event:
                break
            button, event_type, _ = event
            if event_type != PAGER_EVENT_PRESS:
                continue
            return button
        name = drain_virt_button()
        if name:
            return virt_map.get(name, 0)
        _t.sleep(poll_ms / 1000.0)


_MIME = {
    '.html': 'text/html; charset=utf-8',
    '.css':  'text/css; charset=utf-8',
    '.js':   'application/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png':  'image/png',
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif':  'image/gif',
    '.ico':  'image/x-icon',
    '.svg':  'image/svg+xml',
    '.ttf':  'font/ttf',
    '.woff': 'font/woff',
    '.woff2':'font/woff2',
    '.txt':  'text/plain; charset=utf-8',
    '.log':  'text/plain; charset=utf-8',
    '.csv':  'text/csv; charset=utf-8',
    '.pcap': 'application/vnd.tcpdump.pcap',
}

_BTN_NAMES = {'up', 'down', 'left', 'right', 'a', 'b', 'power'}


# ----------------------------------------------------------------------
# Helper: resolve captive dirs lazily
# ----------------------------------------------------------------------

def _captive_dirs():
    return {
        'home': HOME_DIR,
        'portal_templates': os.path.join(HOME_DIR, 'captive', 'templates', 'portal'),
        'spoof': os.path.join(HOME_DIR, 'captive', 'templates', 'spoof'),
        'captures': os.path.join(HOME_DIR, 'captive', 'captures'),
    }


def _safe_join(base, rel):
    """Join base + rel but refuse if rel escapes base."""
    full = os.path.realpath(os.path.join(base, rel))
    base_real = os.path.realpath(base)
    if not full.startswith(base_real + os.sep) and full != base_real:
        return None
    return full


class PagerHandler(BaseHTTPRequestHandler):

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def log_message(self, format, *args):
        pass

    def _check_auth(self):
        try:
            pwd = load_config().get('web_password', '') or ''
        except Exception:
            pwd = ''
        if not pwd:
            return True
        header = self.headers.get('Authorization', '')
        if not header.startswith('Basic '):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode('utf-8', errors='replace')
            _user, _, password = raw.partition(':')
        except Exception:
            return False
        return password == pwd

    def _send_auth_challenge(self):
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Pagerctl Home"')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _send_bytes(self, status, ctype, body, headers=None, gzip_ok=False):
        if gzip_ok and isinstance(body, (bytes, bytearray)) and self._accepts_gzip():
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=6) as gz:
                gz.write(body)
            body = buf.getvalue()
            extra_gzip = True
        else:
            extra_gzip = False
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        if extra_gzip:
            self.send_header('Content-Encoding', 'gzip')
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self._send_bytes(status, 'application/json', body,
                         headers={'Access-Control-Allow-Origin': '*'},
                         gzip_ok=True)

    def _accepts_gzip(self):
        return 'gzip' in self.headers.get('Accept-Encoding', '')

    def _read_json_body(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            return json.loads(body or '{}')
        except Exception:
            return {}

    def _read_raw_body(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            return self.rfile.read(length)
        except Exception:
            return b''

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_GET(self):
        if not self._check_auth():
            return self._send_auth_challenge()
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # Static SPA shell
        if path in ('/', '/index.html'):
            return self._serve_static('index.html')
        if path.startswith('/web/'):
            return self._serve_static(path[5:])
        if path.startswith('/screen.png'):
            return self._serve_framebuffer()

        # Dashboard
        if path == '/api/sysinfo':
            return self._send_json(self._gather_sysinfo())

        # Captive
        if path == '/api/captive/state':
            return self._send_json(self._captive_state())
        if path == '/api/captive/templates':
            return self._send_json(self._list_portal_templates())
        if path.startswith('/api/captive/template/'):
            rel = unquote(path[len('/api/captive/template/'):])
            return self._serve_portal_template_file(rel)
        if path == '/api/captive/cache':
            return self._send_json(self._list_cache())
        if path.startswith('/api/captive/cache_tree/'):
            host = unquote(path[len('/api/captive/cache_tree/'):])
            return self._send_json(self._cache_tree(host))
        if path.startswith('/api/captive/cache_file/'):
            rel = unquote(path[len('/api/captive/cache_file/'):])
            return self._serve_cache_file(rel)
        if path == '/api/captive/credentials':
            return self._send_json(self._list_credentials())
        if path.startswith('/api/captive/credential/'):
            name = unquote(path[len('/api/captive/credential/'):])
            return self._serve_credential(name)

        # Wardrive
        if path == '/api/wardrive/state':
            return self._send_json(self._wardrive_state())
        if path == '/api/wardrive/networks':
            return self._send_json(self._wardrive_networks(qs))
        if path == '/api/wardrive/stats':
            return self._send_json(self._wardrive_stats())
        if path == '/api/stats':  # legacy
            return self._send_json(self._wardrive_stats())

        # Loot
        if path == '/api/loot/list':
            return self._send_json(self._loot_index())
        if path.startswith('/api/loot/download/'):
            rel = path[len('/api/loot/download/'):]
            return self._serve_loot_download(unquote(rel))
        if path == '/api/files':  # legacy
            return self._send_json(self._list_loot_files())
        if path.startswith('/download/'):
            return self._serve_legacy_download(path[10:])

        # Settings
        if path == '/api/settings':
            return self._send_json(self._full_settings())

        self.send_error(404)

    def do_POST(self):
        if not self._check_auth():
            return self._send_auth_challenge()
        parsed = urlparse(self.path)
        path = parsed.path

        # Button injection
        if path.startswith('/api/button/'):
            return self._post_button(path[len('/api/button/'):])

        # Terminal
        if path == '/api/terminal':
            return self._post_terminal()

        # Captive
        if path == '/api/captive/start':
            return self._post_captive_action('start')
        if path == '/api/captive/stop':
            return self._post_captive_action('stop')
        if path == '/api/captive/config':
            return self._post_captive_config()
        if path == '/api/captive/clear_cache':
            return self._post_captive_clear_cache()
        if path == '/api/captive/clear_creds':
            return self._post_captive_clear_creds()
        if path == '/api/captive/delete_cache_host':
            return self._post_captive_delete_host()
        if path == '/api/captive/upload_portal':
            return self._post_captive_upload_portal()
        if path == '/api/captive/upload_cache':
            return self._post_captive_upload_cache()
        if path == '/api/captive/delete_credential':
            return self._post_captive_delete_credential()

        # Wardrive
        if path == '/api/wardrive/start':
            return self._post_wardrive_start()
        if path == '/api/wardrive/stop':
            return self._post_wardrive_stop()
        if path == '/api/wardrive/clear':
            return self._post_wardrive_clear()
        if path == '/api/wardrive/upload_wigle':
            return self._post_wardrive_upload_wigle()

        # Loot
        if path == '/api/loot/delete':
            return self._post_loot_delete()

        # Settings
        if path == '/api/settings':
            return self._post_settings()

        # System
        if path == '/api/system/reboot':
            return self._post_system_reboot()
        if path == '/api/system/shutdown':
            return self._post_system_shutdown()
        if path == '/api/system/restart_ui':
            return self._post_system_restart_ui()

        self.send_error(404)

    def do_DELETE(self):
        if not self._check_auth():
            return self._send_auth_challenge()
        # Same dispatch as POST for convenience
        return self.do_POST()

    # ------------------------------------------------------------------
    # Static
    # ------------------------------------------------------------------

    def _serve_static(self, rel):
        if '..' in rel or rel.startswith('/'):
            self.send_error(403); return
        full = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(full):
            self.send_error(404); return
        ext = os.path.splitext(full)[1].lower()
        ctype = _MIME.get(ext, 'application/octet-stream')
        try:
            with open(full, 'rb') as f:
                body = f.read()
        except Exception:
            self.send_error(500); return
        gzip_ok = ctype.startswith('text/') or 'javascript' in ctype or 'json' in ctype or 'svg' in ctype
        self._send_bytes(200, ctype, body, gzip_ok=gzip_ok,
                         headers={'Cache-Control': 'no-cache'})

    # ------------------------------------------------------------------
    # Framebuffer
    # ------------------------------------------------------------------

    def _serve_framebuffer(self):
        fb_path = '/dev/fb0'
        fb_width = 222
        fb_height = 480
        fb_size = fb_width * fb_height * 2
        rotation = 270
        try:
            with open(fb_path, 'rb') as fb:
                raw = fb.read(fb_size)
            header = struct.pack('<HHH', fb_width, fb_height, rotation)
            body = header + raw
            self._send_bytes(200, 'application/octet-stream', body,
                             headers={'Cache-Control': 'no-cache, no-store'})
        except Exception:
            self.send_error(500)

    # ------------------------------------------------------------------
    # Button
    # ------------------------------------------------------------------

    def _post_button(self, name):
        if name not in _BTN_NAMES:
            return self._send_json({'error': 'unknown button'}, 400)
        try:
            virt_buttons.put_nowait(name)
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)
        return self._send_json({'status': 'ok', 'button': name})

    # ------------------------------------------------------------------
    # Terminal
    # ------------------------------------------------------------------

    def _post_terminal(self):
        params = self._read_json_body()
        cmd = (params.get('command') or '').strip()
        cwd = params.get('cwd') or '/mmc/root'
        if not os.path.isdir(cwd):
            cwd = '/mmc/root'
        if not cmd:
            return self._send_json({'error': 'empty command'}, 400)

        blocked = ['rm -rf /', 'mkfs', 'dd if=/dev/zero', '> /dev/sda',
                   'chmod -R 777 /', ':(){ :|:&};:']
        low = cmd.lower()
        for pat in blocked:
            if pat in low:
                return self._send_json({
                    'command': cmd, 'cwd': cwd,
                    'output': 'Command blocked for safety.',
                    'exit_code': -1,
                }, 403)

        try:
            r = subprocess.run(cmd, shell=True, capture_output=True,
                               text=True, timeout=30, cwd=cwd)
            out = (r.stdout or '') + (r.stderr or '')
            return self._send_json({
                'command': cmd, 'cwd': cwd,
                'output': out,
                'exit_code': r.returncode,
            })
        except subprocess.TimeoutExpired:
            return self._send_json({
                'command': cmd, 'cwd': cwd,
                'output': 'Command timed out (30s).',
                'exit_code': -1,
            })
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)

    # ------------------------------------------------------------------
    # System info
    # ------------------------------------------------------------------

    def _gather_sysinfo(self):
        import glob
        s = {}
        try:
            with open('/proc/meminfo') as f:
                info = {}
                for line in f:
                    p = line.split()
                    if len(p) >= 2:
                        info[p[0].rstrip(':')] = int(p[1])
            total = info.get('MemTotal', 0) // 1024
            free = info.get('MemAvailable', info.get('MemFree', 0)) // 1024
            used = total - free
            s['mem'] = f"{used}/{total}M"
            s['mem_pct'] = int(used * 100 / total) if total else 0
        except Exception:
            s['mem'] = '?'; s['mem_pct'] = 0
        try:
            with open('/proc/loadavg') as f:
                s['cpu'] = f.read().split()[0]
        except Exception:
            s['cpu'] = '?'
        val = None
        for path in glob.glob('/sys/class/ieee80211/phy*/hwmon*/temp1_input') + \
                    glob.glob('/sys/class/hwmon/hwmon*/temp1_input'):
            try:
                with open(path) as f:
                    raw = int(f.read().strip())
                val = raw // 1000 if raw > 1000 else raw
                break
            except Exception:
                continue
        s['temp'] = f"{val}C" if val is not None else '?'
        try:
            r = subprocess.run(['df', '-h', '/mmc'], capture_output=True, text=True, timeout=2)
            parts = r.stdout.strip().split('\n')[1].split()
            s['disk'] = f"{parts[2]} {parts[4]}"
        except Exception:
            s['disk'] = '?'
        try:
            with open('/proc/uptime') as f:
                secs = int(float(f.read().split()[0]))
            s['uptime'] = f"{secs // 3600}h {(secs % 3600) // 60}m"
        except Exception:
            s['uptime'] = '?'
        try:
            s['procs'] = str(sum(1 for n in os.listdir('/proc') if n.isdigit()))
        except Exception:
            s['procs'] = '?'
        try:
            for p in glob.glob('/sys/class/power_supply/*/capacity'):
                with open(p) as f:
                    s['battery'] = f.read().strip() + '%'
                break
            else:
                s['battery'] = '?'
        except Exception:
            s['battery'] = '?'
        try:
            with open('/proc/sys/kernel/hostname') as f:
                s['hostname'] = f.read().strip()
        except Exception:
            s['hostname'] = '?'
        try:
            with open('/proc/version') as f:
                s['kernel'] = f.read().split()[2]
        except Exception:
            s['kernel'] = '?'
        ifaces = []
        try:
            r = subprocess.run(['ip', '-4', '-o', 'addr'], capture_output=True, text=True, timeout=2)
            for line in r.stdout.strip().split('\n'):
                parts = line.split()
                if len(parts) >= 4:
                    name = parts[1]
                    ip = parts[3].split('/')[0]
                    if ip != '127.0.0.1':
                        ifaces.append([name, ip])
        except Exception:
            pass
        s['interfaces'] = ifaces
        exclude_kw = ['host controller', 'hub', 'uart', 'spi', 'i2c',
                      'jtag', 'wireless_device', 'ehci', 'xhci', 'ohci',
                      'root hub']
        usb = []
        try:
            for prod in glob.glob('/sys/bus/usb/devices/*/product'):
                try:
                    with open(prod) as f:
                        name = f.read().strip()
                except Exception:
                    continue
                if not name:
                    continue
                low = name.lower()
                if any(kw in low for kw in exclude_kw):
                    continue
                usb.append(name)
        except Exception:
            pass
        s['usb'] = usb
        return s

    # ------------------------------------------------------------------
    # CAPTIVE PORTAL
    # ------------------------------------------------------------------

    def _captive_state(self):
        out = {}
        cfg = load_config()
        out['config'] = {
            'ssid': cfg.get('captive_ssid', ''),
            'ap_open': cfg.get('captive_ap_open', True),
            'password': cfg.get('captive_password', ''),
            'portal_enabled': cfg.get('captive_portal_enabled', True),
            'spoof_enabled': cfg.get('captive_spoof_enabled', False),
            'mitm_enabled': cfg.get('captive_mitm_enabled', False),
            'mirror_banner': cfg.get('captive_mirror_banner', True),
            'internet_allowed': cfg.get('captive_internet_allowed', True),
            'dns_hijack': cfg.get('captive_dns_hijack', False),
            'portal_template': cfg.get('captive_portal_template', 'default'),
        }
        try:
            from captive import server as cap_server
            out['running'] = cap_server.is_running()
        except Exception:
            out['running'] = False
        try:
            from captive import ap_control
            out['ap_marker'] = ap_control.marker_exists()
        except Exception:
            out['ap_marker'] = False
        try:
            from captive import captures as cap_captures
            out['cred_count'] = cap_captures.count()
        except Exception:
            out['cred_count'] = 0
        try:
            from captive import mirror
            out['cache_count'] = mirror.cache_count()
        except Exception:
            out['cache_count'] = 0
        try:
            from captive import intercept
            out['intercept_active'] = intercept.is_active()
            out['internet_blocked'] = intercept.internet_blocked()
        except Exception:
            out['intercept_active'] = False
            out['internet_blocked'] = False
        # Live associated clients
        clients = []
        try:
            r = subprocess.run(['iw', 'dev', 'wlan0mgmt', 'station', 'dump'],
                                capture_output=True, text=True, timeout=2)
            cur = None
            for line in r.stdout.split('\n'):
                stripped = line.strip()
                if line.startswith('Station '):
                    if cur:
                        clients.append(cur)
                    mac = line.split()[1]
                    cur = {'mac': mac, 'signal': '', 'connected': ''}
                elif cur and stripped.startswith('signal:'):
                    cur['signal'] = stripped.split(':', 1)[1].strip()
                elif cur and stripped.startswith('connected time:'):
                    cur['connected'] = stripped.split(':', 1)[1].strip()
            if cur:
                clients.append(cur)
        except Exception:
            pass
        out['clients'] = clients
        return out

    def _list_portal_templates(self):
        d = _captive_dirs()['portal_templates']
        out = []
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                tdir = os.path.join(d, name)
                if not os.path.isdir(tdir):
                    continue
                files = []
                for root, _, fns in os.walk(tdir):
                    for fn in fns:
                        full = os.path.join(root, fn)
                        rel = os.path.relpath(full, tdir)
                        try:
                            size = os.path.getsize(full)
                        except Exception:
                            size = 0
                        files.append({'path': rel, 'size': size})
                out.append({'name': name, 'files': files})
        return {'templates': out}

    def _serve_portal_template_file(self, rel):
        d = _captive_dirs()['portal_templates']
        full = _safe_join(d, rel)
        if not full or not os.path.isfile(full):
            return self.send_error(404)
        try:
            with open(full, 'rb') as f:
                body = f.read()
        except Exception:
            return self.send_error(500)
        ext = os.path.splitext(full)[1].lower()
        ctype = _MIME.get(ext, 'application/octet-stream')
        self._send_bytes(200, ctype, body,
                         headers={'Content-Disposition':
                                  f'attachment; filename="{os.path.basename(full)}"'})

    def _list_cache(self):
        try:
            from captive import mirror
            rows = mirror.list_cached_hosts()
        except Exception:
            rows = []
        return {
            'hosts': [
                {'host': h, 'files': n, 'bytes': b} for h, n, b in rows
            ]
        }

    def _cache_tree(self, host):
        d = _captive_dirs()['spoof']
        host_dir = _safe_join(d, host)
        out = []
        if not host_dir or not os.path.isdir(host_dir):
            return {'host': host, 'files': out}
        for root, _, files in os.walk(host_dir):
            for fn in files:
                if fn.endswith('.ctype'):
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, host_dir)
                try:
                    size = os.path.getsize(full)
                except Exception:
                    size = 0
                out.append({'path': rel, 'size': size})
        out.sort(key=lambda x: x['path'])
        return {'host': host, 'files': out}

    def _serve_cache_file(self, rel):
        d = _captive_dirs()['spoof']
        full = _safe_join(d, rel)
        if not full or not os.path.isfile(full):
            return self.send_error(404)
        try:
            with open(full, 'rb') as f:
                body = f.read()
        except Exception:
            return self.send_error(500)
        ext = os.path.splitext(full)[1].lower()
        ctype = _MIME.get(ext, 'application/octet-stream')
        self._send_bytes(200, ctype, body, headers={
            'Content-Disposition': f'attachment; filename="{os.path.basename(full)}"'
        })

    def _list_credentials(self):
        try:
            from captive import captures as cap_captures
            rows = cap_captures.list_recent(200)
        except Exception:
            rows = []
        d = _captive_dirs()['captures']
        files = []
        try:
            for name in sorted(os.listdir(d), reverse=True):
                if not name.endswith('.json'):
                    continue
                try:
                    size = os.path.getsize(os.path.join(d, name))
                except Exception:
                    size = 0
                files.append({'name': name, 'size': size})
        except Exception:
            pass
        return {'entries': rows, 'files': files}

    def _serve_credential(self, name):
        if '..' in name or '/' in name or not name.endswith('.json'):
            return self.send_error(400)
        full = os.path.join(_captive_dirs()['captures'], name)
        if not os.path.isfile(full):
            return self.send_error(404)
        try:
            with open(full, 'rb') as f:
                body = f.read()
        except Exception:
            return self.send_error(500)
        self._send_bytes(200, 'application/json', body, headers={
            'Content-Disposition': f'attachment; filename="{name}"'
        })

    def _post_captive_action(self, action):
        # Import inside the call to avoid heavy imports at module load
        try:
            from captive import server as cap_server
            from captive import dns_hijack
            from captive import ap_control
            from captive import intercept
        except Exception as e:
            return self._send_json({'error': f'import: {e}'}, 500)
        cfg = load_config()
        if action == 'start':
            ssid = cfg.get('captive_ssid', '')
            if not ssid:
                return self._send_json({'error': 'set SSID first'}, 400)
            ap_open = cfg.get('captive_ap_open', True)
            password = cfg.get('captive_password', '')
            try:
                ok = ap_control.start_ap(ssid, open_ap=ap_open, password=password)
                if not ok:
                    return self._send_json({'error': 'ap start failed'}, 500)
                intercept.enable()
                intercept.set_internet(cfg.get('captive_internet_allowed', True))
                if cfg.get('captive_dns_hijack', False):
                    dns_hijack.enable()
                cap_server.start(port=80)
                cap_server.update_state(
                    portal_enabled=cfg.get('captive_portal_enabled', True),
                    spoof_enabled=cfg.get('captive_spoof_enabled', False),
                    mitm_enabled=cfg.get('captive_mitm_enabled', False),
                    mirror_banner=cfg.get('captive_mirror_banner', True),
                    portal_template=cfg.get('captive_portal_template', 'default'),
                )
            except Exception as e:
                return self._send_json({'error': str(e)}, 500)
            return self._send_json({'status': 'started'})
        if action == 'stop':
            try:
                cap_server.stop()
                intercept.disable()
                dns_hijack.disable()
                ap_control.stop_ap()
            except Exception as e:
                return self._send_json({'error': str(e)}, 500)
            return self._send_json({'status': 'stopped'})
        return self._send_json({'error': 'unknown action'}, 400)

    def _post_captive_config(self):
        data = self._read_json_body()
        cfg = load_config()
        # Whitelist of captive config keys
        keys = {
            'captive_ssid', 'captive_ap_open', 'captive_password',
            'captive_portal_enabled', 'captive_spoof_enabled',
            'captive_mitm_enabled', 'captive_mirror_banner',
            'captive_internet_allowed', 'captive_dns_hijack',
            'captive_portal_template',
        }
        for k in keys:
            if k in data:
                cfg[k] = data[k]
        save_config(cfg)
        # Push runtime changes for currently-running server
        try:
            from captive import server as cap_server
            from captive import intercept
            from captive import dns_hijack
            if cap_server.is_running():
                cap_server.update_state(
                    portal_enabled=cfg.get('captive_portal_enabled', True),
                    spoof_enabled=cfg.get('captive_spoof_enabled', False),
                    mitm_enabled=cfg.get('captive_mitm_enabled', False),
                    mirror_banner=cfg.get('captive_mirror_banner', True),
                    portal_template=cfg.get('captive_portal_template', 'default'),
                )
                if 'captive_internet_allowed' in data:
                    intercept.set_internet(cfg['captive_internet_allowed'])
                if 'captive_dns_hijack' in data:
                    if data['captive_dns_hijack']:
                        dns_hijack.enable()
                    else:
                        dns_hijack.disable()
        except Exception:
            pass
        return self._send_json({'status': 'ok', 'config': {k: cfg.get(k) for k in keys}})

    def _post_captive_clear_cache(self):
        try:
            from captive import mirror
            n = mirror.clear_cache()
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)
        return self._send_json({'status': 'ok', 'removed': n})

    def _post_captive_clear_creds(self):
        try:
            from captive import captures as cap_captures
            n = cap_captures.clear_all()
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)
        return self._send_json({'status': 'ok', 'removed': n})

    def _post_captive_delete_host(self):
        data = self._read_json_body()
        host = data.get('host', '')
        if not host:
            return self._send_json({'error': 'host required'}, 400)
        try:
            from captive import mirror
            ok = mirror.remove_host(host)
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)
        return self._send_json({'status': 'ok' if ok else 'not found'})

    def _post_captive_delete_credential(self):
        data = self._read_json_body()
        name = data.get('name', '')
        if not name or '..' in name or '/' in name or not name.endswith('.json'):
            return self._send_json({'error': 'bad name'}, 400)
        full = os.path.join(_captive_dirs()['captures'], name)
        try:
            os.unlink(full)
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)
        return self._send_json({'status': 'ok'})

    def _post_captive_upload_portal(self):
        """POST raw file bytes with headers:
        X-Pager-Template: template name (creates/overwrites dir)
        X-Pager-Filename: filename inside template dir (e.g. index.html)
        Body: raw file bytes
        """
        template = self.headers.get('X-Pager-Template', '').strip()
        filename = self.headers.get('X-Pager-Filename', '').strip()
        if not template or not filename:
            return self._send_json({'error': 'X-Pager-Template and X-Pager-Filename headers required'}, 400)
        if '..' in template or '/' in template or not template:
            return self._send_json({'error': 'bad template name'}, 400)
        if '..' in filename:
            return self._send_json({'error': 'bad filename'}, 400)
        body = self._read_raw_body()
        d = os.path.join(_captive_dirs()['portal_templates'], template)
        try:
            os.makedirs(os.path.dirname(os.path.join(d, filename)) or d, exist_ok=True)
            with open(os.path.join(d, filename), 'wb') as f:
                f.write(body)
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)
        return self._send_json({'status': 'ok', 'template': template,
                                 'filename': filename, 'size': len(body)})

    def _post_captive_upload_cache(self):
        """POST raw file bytes for a cached spoof host.
        X-Pager-Host: hostname (e.g. gmail.com)
        X-Pager-Filename: relative path inside cache dir (e.g. index.html)
        Body: raw file bytes
        """
        host = self.headers.get('X-Pager-Host', '').strip()
        filename = self.headers.get('X-Pager-Filename', '').strip()
        if not host or not filename:
            return self._send_json({'error': 'X-Pager-Host and X-Pager-Filename headers required'}, 400)
        if '..' in host or '/' in host or not host:
            return self._send_json({'error': 'bad host'}, 400)
        if '..' in filename:
            return self._send_json({'error': 'bad filename'}, 400)
        body = self._read_raw_body()
        d = os.path.join(_captive_dirs()['spoof'], host)
        try:
            sub_d = os.path.dirname(os.path.join(d, filename))
            if sub_d and not os.path.isdir(sub_d):
                os.makedirs(sub_d, exist_ok=True)
            if not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, filename), 'wb') as f:
                f.write(body)
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)
        return self._send_json({'status': 'ok', 'host': host,
                                 'filename': filename, 'size': len(body)})

    # ------------------------------------------------------------------
    # WARDRIVE
    # ------------------------------------------------------------------

    def _wardrive_state(self):
        cfg = load_config()
        out = {
            'config': {
                'capture_interface': cfg.get('capture_interface', 'wlan1mon'),
                'channel_hop': cfg.get('channel_hop', True),
                'wigle_enabled': cfg.get('wigle_enabled', False),
                'pcap_enabled': cfg.get('pcap_enabled', False),
                'gps_enabled': cfg.get('gps_enabled', True),
                'gps_device': cfg.get('gps_device', ''),
            }
        }
        # Running state
        try:
            r = subprocess.run(['pgrep', '-f', 'airodump-ng'],
                                capture_output=True, text=True, timeout=1)
            out['running'] = bool(r.stdout.strip())
        except Exception:
            out['running'] = False
        # GPS
        gps = {'fix': False, 'lat': None, 'lon': None, 'sats': 0}
        try:
            from wardrive import gps_utils
            fix = gps_utils.get_gps_fix()
            if fix:
                gps = fix
        except Exception:
            pass
        out['gps'] = gps
        return out

    def _wardrive_networks(self, qs):
        import sqlite3
        limit = int((qs.get('limit', ['50'])[0]) or 50)
        rows = []
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.execute(
                'SELECT bssid,ssid,encryption,auth_mode,signal,channel,frequency,'
                'first_seen,last_seen,handshake,lat,lon '
                'FROM access_points ORDER BY last_seen DESC LIMIT ?',
                (limit,)
            )
            for r in cur:
                rows.append({
                    'bssid': r[0], 'ssid': r[1], 'encryption': r[2],
                    'auth_mode': r[3], 'signal': r[4], 'channel': r[5],
                    'frequency': r[6], 'first_seen': r[7], 'last_seen': r[8],
                    'handshake': bool(r[9]), 'lat': r[10], 'lon': r[11],
                })
            conn.close()
        except Exception:
            pass
        return {'networks': rows}

    def _wardrive_stats(self):
        import sqlite3
        stats = {'total': 0, 'open': 0, 'wep': 0, 'wpa': 0, 'wpa3': 0, 'handshakes': 0}
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute('''SELECT COUNT(*),
                SUM(CASE WHEN encryption='Open' THEN 1 ELSE 0 END),
                SUM(CASE WHEN encryption='WEP' THEN 1 ELSE 0 END),
                SUM(CASE WHEN encryption IN ('WPA','WPA2') THEN 1 ELSE 0 END),
                SUM(CASE WHEN encryption='WPA3' THEN 1 ELSE 0 END),
                SUM(handshake)
                FROM access_points''').fetchone()
            stats = {
                'total': row[0] or 0, 'open': row[1] or 0, 'wep': row[2] or 0,
                'wpa': row[3] or 0, 'wpa3': row[4] or 0, 'handshakes': row[5] or 0,
            }
            conn.close()
        except Exception:
            pass
        return stats

    def _post_wardrive_start(self):
        try:
            import wardrive_ui
            ui = wardrive_ui.get_wardrive()
            ui.start_scan()
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)
        return self._send_json({'status': 'started'})

    def _post_wardrive_stop(self):
        try:
            import wardrive_ui
            ui = wardrive_ui.get_wardrive()
            ui.stop_scan()
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)
        return self._send_json({'status': 'stopped'})

    def _post_wardrive_clear(self):
        import sqlite3
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute('DELETE FROM access_points')
            conn.commit()
            conn.close()
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)
        return self._send_json({'status': 'ok'})

    def _post_wardrive_upload_wigle(self):
        """Upload a single wigle CSV file to WiGLE via the credentials
        stored in the config. Accepts one of:
          - JSON body with {"name": "<file in EXPORT_DIR>"}
          - raw file bytes with X-Pager-Filename header
        """
        name = self.headers.get('X-Pager-Filename', '').strip()
        body = None
        if name:
            if '..' in name or '/' in name:
                return self._send_json({'error': 'bad filename'}, 400)
            body = self._read_raw_body()
            # Save incoming file into EXPORT_DIR first
            target = os.path.join(EXPORT_DIR, name)
            try:
                os.makedirs(EXPORT_DIR, exist_ok=True)
                with open(target, 'wb') as f:
                    f.write(body)
            except Exception as e:
                return self._send_json({'error': f'save: {e}'}, 500)
        else:
            data = self._read_json_body()
            picked = data.get('name', '')
            if not picked:
                return self._send_json({'error': 'filename required'}, 400)
            target = os.path.join(EXPORT_DIR, picked)
            if '..' in picked or not os.path.isfile(target):
                return self._send_json({'error': 'file not found'}, 404)

        cfg = load_config()
        user = cfg.get('wigle_username', '')
        token = cfg.get('wigle_api_token', '')
        if not user or not token:
            return self._send_json({'error': 'wigle_username and wigle_api_token required in settings'}, 400)

        # Push via the wardrive module's uploader if available; else
        # fall back to a direct curl.
        try:
            from wardrive import wigle_upload
            ok, msg = wigle_upload.upload_file(target, user, token)
            return self._send_json({'status': 'ok' if ok else 'error',
                                     'message': msg})
        except Exception:
            pass
        try:
            auth = base64.b64encode(f'{user}:{token}'.encode()).decode()
            r = subprocess.run([
                'curl', '-s', '-X', 'POST',
                '-H', f'Authorization: Basic {auth}',
                '-F', f'file=@{target}',
                'https://api.wigle.net/api/v2/file/upload',
            ], capture_output=True, text=True, timeout=60)
            return self._send_json({
                'status': 'ok' if r.returncode == 0 else 'error',
                'message': (r.stdout or r.stderr or '').strip()[:500],
            })
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)

    # ------------------------------------------------------------------
    # LOOT
    # ------------------------------------------------------------------

    def _loot_index(self):
        """Unified file index for the Loot tab.

        Categories:
          credentials: captive portal JSON captures
          wigle: wardrive CSV exports
          pcap: handshake captures
          logs: log files under /tmp/pagerctl_*.log
        """
        def scan(dir_path, exts=None, recursive=False):
            out = []
            if not os.path.isdir(dir_path):
                return out
            walker = os.walk(dir_path) if recursive else [(dir_path, [],
                       [f for f in os.listdir(dir_path)
                        if os.path.isfile(os.path.join(dir_path, f))])]
            for root, _, files in walker:
                for name in files:
                    if exts and not any(name.lower().endswith(e) for e in exts):
                        continue
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, dir_path)
                    try:
                        size = os.path.getsize(full)
                        mtime = int(os.path.getmtime(full))
                    except Exception:
                        size, mtime = 0, 0
                    out.append({'name': rel, 'size': size, 'mtime': mtime})
            out.sort(key=lambda x: x['mtime'], reverse=True)
            return out

        dirs = _captive_dirs()
        creds = scan(dirs['captures'], exts=['.json'])
        wigle = scan(EXPORT_DIR, exts=['.csv'])
        pcap = scan(CAPTURE_DIR, exts=['.pcap', '.cap'])

        # System and app logs. Some are actual files on disk, others
        # are generated on-the-fly by a command — we mark those with
        # `virtual: True` so the download endpoint knows to exec the
        # command instead of cat'ing a file.
        logs = []
        for name, path in self._LOG_FILE_PATHS.items():
            if os.path.isfile(path):
                try:
                    size = os.path.getsize(path)
                    mtime = int(os.path.getmtime(path))
                except Exception:
                    size, mtime = 0, 0
                logs.append({'name': name, 'size': size, 'mtime': mtime})
        for name in self._VIRTUAL_LOGS:
            logs.append({'name': name, 'size': 0, 'mtime': 0, 'virtual': True})
        return {
            'credentials': creds,
            'wigle': wigle,
            'pcap': pcap,
            'logs': logs,
        }

    # Static lookup for virtual log sources (generated on demand).
    _VIRTUAL_LOGS = {
        'dmesg.txt':              ['dmesg'],
        'logread.txt':            ['logread'],
        'logread.kernel.txt':     ['sh', '-c', 'logread | grep -i kern'],
        'logread.hostapd.txt':    ['sh', '-c', 'logread | grep hostapd'],
        'logread.dnsmasq.txt':    ['sh', '-c', 'logread | grep dnsmasq'],
        'dmesg.wifi.txt':         ['sh', '-c', 'dmesg | grep -iE "wlan|hostapd|mt76|ieee80211"'],
        'dmesg.usb.txt':          ['sh', '-c', 'dmesg | grep -i usb'],
    }
    _LOG_FILE_PATHS = {
        'pagerctl_captive.log':   '/tmp/pagerctl_captive.log',
        'wardrive.log':            '/tmp/wardrive.log',
        'pagerctl_home.log':       '/tmp/pagerctl_home.log',
        'pagerctl_wardrive.log':   '/tmp/pagerctl_wardrive.log',
        'pagerctl_bootloader.log': '/tmp/pagerctl_bootloader.log',
        'airodump.log':            '/tmp/airodump.log',
        'hostapd.log':             '/tmp/hostapd.log',
        'dnsmasq.log':             '/tmp/dnsmasq.log',
        'syslog':                  '/var/log/syslog',
        'messages':                '/var/log/messages',
        'kern.log':                '/var/log/kern.log',
    }

    def _serve_loot_download(self, rel):
        """rel looks like 'kind/path' e.g. 'wigle/scan_2026.csv'
        or 'credentials/20260412_230154_captive.apple.com.json'."""
        if '/' not in rel:
            return self.send_error(400)
        kind, name = rel.split('/', 1)
        dirs = _captive_dirs()
        base_map = {
            'credentials': dirs['captures'],
            'wigle': EXPORT_DIR,
            'pcap': CAPTURE_DIR,
        }
        if kind == 'logs':
            # Virtual logs are generated on demand via subprocess
            if name in self._VIRTUAL_LOGS:
                try:
                    r = subprocess.run(self._VIRTUAL_LOGS[name],
                                       capture_output=True, text=True, timeout=8)
                    body = (r.stdout or '').encode('utf-8') + \
                           (r.stderr or '').encode('utf-8')
                except Exception as e:
                    body = f'[error running {name}: {e}]'.encode('utf-8')
                self._send_bytes(200, 'text/plain; charset=utf-8', body,
                                 headers={'Content-Disposition':
                                          f'attachment; filename="{name}"'})
                return
            cand = self._LOG_FILE_PATHS.get(name)
            if not cand or not os.path.isfile(cand):
                return self.send_error(404)
            full = cand
        else:
            base = base_map.get(kind)
            if not base:
                return self.send_error(404)
            full = _safe_join(base, name)
            if not full or not os.path.isfile(full):
                return self.send_error(404)
        try:
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition',
                             f'attachment; filename="{os.path.basename(full)}"')
            self.send_header('Content-Length', str(size))
            self.end_headers()
            with open(full, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk: break
                    try:
                        self.wfile.write(chunk)
                    except BrokenPipeError:
                        return
        except Exception:
            self.send_error(500)

    def _post_loot_delete(self):
        data = self._read_json_body()
        kind = data.get('kind', '')
        name = data.get('name', '')
        if not kind or not name:
            return self._send_json({'error': 'kind and name required'}, 400)
        dirs = _captive_dirs()
        base_map = {
            'credentials': dirs['captures'],
            'wigle': EXPORT_DIR,
            'pcap': CAPTURE_DIR,
        }
        base = base_map.get(kind)
        if not base:
            return self._send_json({'error': 'bad kind'}, 400)
        full = _safe_join(base, name)
        if not full or not os.path.isfile(full):
            return self._send_json({'error': 'not found'}, 404)
        try:
            os.unlink(full)
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)
        return self._send_json({'status': 'ok'})

    # Legacy endpoints (preserved for older clients)

    def _list_loot_files(self):
        def _list(d, ext):
            out = []
            if not os.path.isdir(d):
                return out
            for name in sorted(os.listdir(d), reverse=True):
                if name.endswith(ext):
                    try:
                        size = os.path.getsize(os.path.join(d, name))
                    except Exception:
                        size = 0
                    out.append({'name': name, 'size': self._fmt_size(size)})
            return out
        return {
            'wigle': _list(EXPORT_DIR, '.csv'),
            'pcap': _list(CAPTURE_DIR, '.pcap'),
        }

    def _fmt_size(self, n):
        if n < 1024: return f'{n}B'
        if n < 1024 * 1024: return f'{n // 1024}KB'
        return f'{n // (1024*1024)}MB'

    def _serve_legacy_download(self, rel):
        if '..' in rel:
            self.send_error(403); return
        full = os.path.realpath(os.path.join(LOOT_DIR, rel))
        if not full.startswith(os.path.realpath(LOOT_DIR)) or not os.path.isfile(full):
            self.send_error(404); return
        try:
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition',
                             f'attachment; filename="{os.path.basename(full)}"')
            self.send_header('Content-Length', str(size))
            self.end_headers()
            with open(full, 'rb') as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk: break
                    try:
                        self.wfile.write(chunk)
                    except BrokenPipeError:
                        return
        except Exception:
            self.send_error(500)

    # ------------------------------------------------------------------
    # SETTINGS
    # ------------------------------------------------------------------

    def _full_settings(self):
        cfg = load_config()
        return {'config': cfg}

    def _post_settings(self):
        data = self._read_json_body()
        cfg = load_config()
        cfg.update(data)
        save_config(cfg)
        return self._send_json({'status': 'ok', 'config': cfg})

    # ------------------------------------------------------------------
    # SYSTEM
    # ------------------------------------------------------------------

    def _post_system_reboot(self):
        try:
            subprocess.Popen(['sh', '-c', 'sleep 1; reboot'])
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)
        return self._send_json({'status': 'rebooting'})

    def _post_system_shutdown(self):
        try:
            subprocess.Popen(['sh', '-c', 'sleep 1; poweroff'])
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)
        return self._send_json({'status': 'shutting down'})

    def _post_system_restart_ui(self):
        """Kill the pagerctl_home process so the launcher respawns it.
        Using SIGKILL because the in-process signal handlers ignored
        SIGTERM during testing."""
        try:
            subprocess.Popen(['sh', '-c', 'sleep 1; pkill -9 -f "python3 pagerctl_home.py"'])
        except Exception as e:
            return self._send_json({'error': str(e)}, 500)
        return self._send_json({'status': 'restarting'})


# ----------------------------------------------------------------------
# Thread wrapper
# ----------------------------------------------------------------------

class WebServer(threading.Thread):
    def __init__(self, port=1337):
        super().__init__(daemon=True)
        self.port = port
        self.server = None

    def run(self):
        try:
            self.server = ThreadingHTTPServer(('0.0.0.0', self.port), PagerHandler)
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


# Module-level singleton
_singleton = None


def start_web_ui(port=1337):
    global _singleton, WEB_DIR, HOME_DIR
    if WEB_DIR is None:
        here = os.path.dirname(os.path.abspath(__file__))
        HOME_DIR = os.path.dirname(here)
        WEB_DIR = os.path.join(HOME_DIR, 'web')
    if _singleton is not None and _singleton.is_alive():
        return _singleton
    try:
        _singleton = WebServer(port=port)
        _singleton.start()
    except Exception:
        _singleton = None
    return _singleton


def stop_web_ui():
    global _singleton
    if _singleton is not None:
        try:
            _singleton.stop()
        except Exception:
            pass
        _singleton = None


def is_running():
    return _singleton is not None and _singleton.is_alive()
