"""payload_run.py - On-screen payload log + run-lifecycle screen.

Called from pagerctl_home's main loop when a payload is launched from
the Payloads menu. Owns the display while the payload is running:
renders the scrolling log, handles Stop (B), waits for the user to
press B one more time on completion before returning to the UI.

Uses the Circuitry `payload_log.json` visual layout (title at top,
14-line scrolling text area, status indicator bar at the bottom)
but renders directly rather than going through theme_engine so we
can stream output lines into it as they arrive.
"""

import json
import os
import queue as _queue
import time

from pagerctl import PAGER_EVENT_PRESS

import duckyctl
from wardrive.config import FONT_TITLE, FONT_MENU, load_config

# Stock pager ringtone library. These are the files the original
# Hak5 firmware ships and match the ALERT/ERROR sounds users expect.
_STOCK_RTTTL_DIR = '/lib/pager/ringtones'
_DEFAULT_RTTTL_FALLBACK = (
    'alert:d=16,o=6,b=180:c,d,e,c,d,e'
)


def _load_rtttl(name):
    """Return the contents of a stock rtttl file as a single-line
    string, or None if not available. Accepts either `bonus` or
    `bonus.rtttl` — the Settings picker stores values with the
    extension, so we strip it if present before joining with the
    ringtones dir.
    """
    if not name:
        return None
    stem = name[:-6] if name.lower().endswith('.rtttl') else name
    path = os.path.join(_STOCK_RTTTL_DIR, f'{stem}.rtttl')
    try:
        with open(path) as f:
            raw = f.read()
    except Exception:
        return None
    # Strip comments + collapse whitespace. Most files are single-line
    # but a few have `# comment` headers.
    lines = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith('#'):
            continue
        lines.append(ln)
    return ''.join(lines).replace(' ', '')


THEME_DIR_FALLBACK = '/root/payloads/user/general/pagerctl_home/themes/Circuitry'

# Layout matches the Circuitry payload_log_bg.png. The background has
# a cyan separator line at y≈20 — title goes above it, log body below.
TITLE_X = 8
TITLE_Y = 2
TITLE_FONT_SIZE = 18

LOG_START_X = 8
LOG_START_Y = 32
LOG_LINE_H = 18
LOG_FONT_SIZE = 16
LOG_MAX_LINES = 8
LOG_MAX_CHARS = 48
STATUS_Y = 179

# status → asset filename (all under assets/payloadlog/)
_STATUS_ASSETS = {
    'running':  'payload_running_indicator.png',
    'complete': 'payload_complete_indicator.png',
    'stopped':  'payload_stopped_indicator.png',
    'error':    'payload_error_indicator.png',
}


class _LogRender:
    """Draws the payload log screen. Does NOT store its own log lines —
    reads them from api_server._payload_log which is the single source
    of truth written to by every `LOG` / `ALERT` / `ERROR_DIALOG` call
    that hits the API socket. Stdout from bash (echo/printf) is also
    captured into the same list via `add_stdout_line()` below so the
    display is unified.
    """

    _STDOUT_COLOR = 'gray'

    def __init__(self, pager, theme_dir):
        self.pager = pager
        self.theme_dir = theme_dir
        self.bg_handle = None
        self.status_handles = {}
        self._loaded = False

    def _load_assets(self):
        if self._loaded:
            return
        try:
            bg = os.path.join(self.theme_dir, 'assets', 'payloadlog',
                              'payload_log_bg.png')
            if os.path.isfile(bg):
                self.bg_handle = self.pager.load_image(bg)
            for key, fname in _STATUS_ASSETS.items():
                p = os.path.join(self.theme_dir, 'assets', 'payloadlog', fname)
                if os.path.isfile(p):
                    self.status_handles[key] = self.pager.load_image(p)
        except Exception:
            pass
        self._loaded = True

    def add_stdout_line(self, text):
        """Called by duckyctl's on_line callback when bash prints via
        echo/printf. Routes into the shared api_server payload log so
        it gets rendered alongside LOG/ALERT lines."""
        try:
            import api_server
            api_server._payload_log.add(text[:LOG_MAX_CHARS], self._STDOUT_COLOR)
        except Exception:
            pass

    def snapshot(self):
        try:
            import api_server
            return [line for (line, _color) in api_server._payload_log.snapshot()]
        except Exception:
            return []

    def render(self, title, status):
        self._load_assets()
        p = self.pager
        try:
            if self.bg_handle:
                p.draw_image(0, 0, self.bg_handle)
            else:
                p.clear(0)

            # Title in yellow, above the cyan separator line in the bg.
            if title:
                p.draw_ttf(TITLE_X, TITLE_Y, title[:40],
                           p.rgb(255, 220, 50), FONT_TITLE, TITLE_FONT_SIZE)

            # Log body — each cached line in readable TTF below the line.
            visible = self.snapshot()[-LOG_MAX_LINES:]
            y = LOG_START_Y
            for line in visible:
                p.draw_ttf(LOG_START_X, y, line[:LOG_MAX_CHARS],
                           p.rgb(200, 220, 255), FONT_MENU, LOG_FONT_SIZE)
                y += LOG_LINE_H

            # Status indicator bar
            ind = self.status_handles.get(status)
            if ind:
                p.draw_image(0, STATUS_Y, ind)

            # A "press B" hint on completion/error so it's obvious
            # the user needs to dismiss to return to the menu.
            if status in ('complete', 'stopped', 'error'):
                hint = {'complete': 'Done - press B',
                        'stopped':  'Stopped - press B',
                        'error':    'Error - press B'}[status]
                p.draw_ttf(LOG_START_X, 200, hint,
                           p.rgb(255, 220, 50), FONT_MENU, 12)

            p.flip()
        except Exception:
            pass


def _play_alert_sound(pager, kind='alert'):
    """Play the alert tone configured in Settings → Sound.

    The settings key is `alert_sound` and stores a ringtone NAME
    (the filename stem in /lib/pager/ringtones, e.g. "alert" or
    "hak5_the_planet"), NOT a raw RTTTL string. We load the matching
    file, strip comments/whitespace, and hand the single-line RTTTL
    to pager.play_rtttl. Falls through silently if the file is
    missing or the value is empty/None.
    """
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    name = cfg.get('alert_sound') or ''
    if not name or name.lower() in ('none', 'off', ''):
        # Nothing configured — use the stock alert.rtttl as a safe default
        name = {'alert': 'alert', 'error': 'error',
                'warning': 'alert'}.get(kind, 'alert')
    rtttl = _load_rtttl(name)
    if not rtttl:
        rtttl = _DEFAULT_RTTTL_FALLBACK
    try:
        mode = getattr(pager, 'RTTTL_SOUND_ONLY', 0)
        pager.play_rtttl(rtttl, mode)
    except Exception as e:
        try:
            with open('/tmp/pagerctl_home_crash.log', 'a') as f:
                f.write(f'[alert-sound] play_rtttl failed: {e}\n')
                f.write(f'  name={name!r} rtttl={rtttl[:80]!r}\n')
        except Exception:
            pass


def _load_json(theme_dir, rel_path):
    try:
        with open(os.path.join(theme_dir, rel_path)) as f:
            return json.load(f)
    except Exception:
        return None


def _sub_vars(node, subs):
    """Walk a theme JSON tree and rewrite $_ variable_name layers into
    text layers with the value baked in. Mirrors theme_engine's
    `_substitute_variables` for use inside dialog rendering."""
    if isinstance(node, dict):
        vname = node.get('variable_name')
        if isinstance(vname, str) and vname in subs:
            node['text'] = subs[vname]
            node.pop('variable_name', None)
            tmpl = node.get('string_template') or {}
            if 'text_size' in tmpl and 'text_size' not in node:
                node['text_size'] = tmpl['text_size']
            if 'text_color_palette' in tmpl and 'text_color_palette' not in node:
                node['text_color_palette'] = tmpl['text_color_palette']
        for v in list(node.values()):
            _sub_vars(v, subs)
    elif isinstance(node, list):
        for it in node:
            _sub_vars(it, subs)


class _DialogRunner:
    """Handles a single DialogRequest. Loads the matching Circuitry
    component JSON, populates variables, plays a sound, renders the
    modal, reads input, fills in the request's response, sets the
    event. Falls back to an in-log notice if the component is missing.
    """

    _DIALOG_COMPONENTS = {
        'alert':   ('components/alerts/alert_info_dialog.json',   'alert'),
        'warning': ('components/alerts/alert_warning_dialog.json','warning'),
        'error':   ('components/alerts/alert_error_dialog.json',  'error'),
        'confirm': ('components/dialogs/confirmation_dialog.json','alert'),
    }

    def __init__(self, pager, theme_dir, log):
        self.pager = pager
        self.theme_dir = theme_dir
        self.log = log            # shared _LogRender instance
        self._bg_cache = {}
        self._img_cache = {}

    def handle(self, req):
        """Dispatch a DialogRequest to the right handler. On return the
        request's response/event are already set."""
        kind = req.kind
        if kind in ('alert', 'warning', 'error'):
            self._alert(req)
        elif kind == 'confirm':
            self._confirm(req)
        elif kind in ('list', 'string'):
            self._list(req)
        elif kind in ('prompt', 'number', 'ip', 'mac'):
            self._prompt(req)
        elif kind == 'spinner_start':
            req.response = {'id': '1', 'success': True}
        elif kind == 'spinner_stop':
            req.response = {'success': True}
        elif kind == 'wait_button':
            btn = self._wait_button()
            req.response = {'button': btn}
        else:
            req.response = {'success': True}
        req.response_event.set()

    # --------------------------------------------------------------
    # Individual dialog renderers
    # --------------------------------------------------------------

    def _alert(self, req):
        """Alert/Warning/Error single-button dialog."""
        rel, sound_kind = self._DIALOG_COMPONENTS.get(
            req.kind, self._DIALOG_COMPONENTS['alert'])
        raw = _load_json(self.theme_dir, rel)
        message = (req.data.get('message') or req.data.get('text') or
                   req.data.get('title') or '')
        title = req.data.get('title') or {
            'alert': 'Alert', 'warning': 'Warning', 'error': 'Error'
        }.get(req.kind, 'Alert')

        # Push into the shared log so it's visible behind the modal and
        # after dismissal.
        try:
            import api_server
            api_server._payload_log.add(
                f'[{req.kind.upper()}] {message}', req.kind)
        except Exception:
            pass

        _play_alert_sound(self.pager, sound_kind)

        if raw is None:
            # No theme component — render a simple overlay ourselves.
            self._simple_modal(title, message, color=req.kind)
        else:
            subs = {
                '$_INPUT': message, '$_INPUT_NAME': title,
                '$_PAYLOAD_TITLE': title,
                '$_ALERT_MESSAGE': message,
                '$_ALERT_TITLE': title,
            }
            _sub_vars(raw, subs)
            self._render_raw_screen(raw)

        # Block until any button press.
        self._wait_button()
        req.response = {'success': True, 'button': 'A'}

    def _confirm(self, req):
        raw = _load_json(self.theme_dir,
                         'components/dialogs/confirmation_dialog.json')
        message = (req.data.get('message') or req.data.get('text') or
                   req.data.get('prompt') or 'Are you sure?')
        _play_alert_sound(self.pager, 'alert')
        if raw is None:
            self._simple_modal('Confirm', message + '  [A=yes  B=no]')
        else:
            _sub_vars(raw, {
                '$_INPUT': message, '$_INPUT_NAME': message,
                '$_PAYLOAD_DESCRIPTION': message,
            })
            self._render_raw_screen(raw)
        btn = self._wait_button()
        confirmed = bool(btn & self.pager.BTN_A)
        req.response = {'confirmed': confirmed, 'button': 'A' if confirmed else 'B'}

    def _list(self, req):
        # Minimal for now — return the default / first item.
        opts = req.data.get('options') or req.data.get('items') or []
        default = req.data.get('default') or (opts[0] if opts else '')
        try:
            import api_server
            api_server._payload_log.add(
                f'[LIST_PICKER] (stub) returning {default!r}', 'yellow')
        except Exception:
            pass
        req.response = {'selected': default, 'text': default}

    def _prompt(self, req):
        default = req.data.get('default', '')
        try:
            import api_server
            api_server._payload_log.add(
                f'[PROMPT] (stub) returning {default!r}', 'yellow')
        except Exception:
            pass
        req.response = {'text': default}

    # --------------------------------------------------------------
    # Rendering helpers
    # --------------------------------------------------------------

    def _render_raw_screen(self, raw):
        """Render a theme JSON tree one-shot. Fire-and-forget — no
        animations, no status bar widgets. Good enough for alert
        modals and confirmation dialogs."""
        p = self.pager
        try:
            bg = (raw.get('background') or {})
            for layer in bg.get('layers') or []:
                self._draw_layer(layer)
            for item in raw.get('menu_items') or []:
                # Unselected layers for every item, selected for item 0.
                layers = item.get('selected_layers') if (
                    raw.get('menu_items') or [None])[0] is item else item.get('layers')
                for layer in layers or []:
                    self._draw_layer(layer)
            p.flip()
        except Exception:
            pass

    def _draw_layer(self, layer):
        p = self.pager
        x = layer.get('x', 0)
        y = layer.get('y', 0)
        if 'image_path' in layer:
            ip = layer['image_path']
            handle = self._img_cache.get(ip)
            if handle is None:
                full = os.path.join(self.theme_dir, ip)
                if os.path.isfile(full):
                    try:
                        handle = p.load_image(full)
                    except Exception:
                        handle = None
                self._img_cache[ip] = handle
            if handle is not None:
                try:
                    p.draw_image(x, y, handle)
                except Exception:
                    pass
            return
        if 'text' in layer:
            text = str(layer['text'])
            size_name = layer.get('text_size', 'medium')
            sz_map = {'small': 14.0, 'medium': 18.0, 'large': 24.0}
            sz = sz_map.get(size_name, 18.0)
            color = self._layer_color(layer)
            try:
                p.draw_ttf(x, y, text[:60], color, FONT_MENU, sz)
            except Exception:
                pass

    def _layer_color(self, layer):
        p = self.pager
        tc = layer.get('text_color')
        if isinstance(tc, dict):
            try:
                return p.rgb(tc.get('r', 255), tc.get('g', 255), tc.get('b', 255))
            except Exception:
                pass
        # palette fallback — crude map for the common names
        pal = layer.get('text_color_palette') or ''
        palette = {
            'yellow': (255, 220, 50), 'green': (120, 220, 120),
            'light_green': (180, 255, 180), 'cyan': (100, 220, 255),
            'teal': (100, 200, 200), 'red': (255, 90, 90),
            'medium_gray': (180, 180, 180), 'blue': (120, 180, 255),
            'white': (255, 255, 255),
        }.get(pal, (255, 255, 255))
        try:
            return p.rgb(*palette)
        except Exception:
            return 0xFFFF

    def _simple_modal(self, title, body, color='alert'):
        """Fallback when no theme component is available: a plain
        centered black box with yellow border, title at top, body
        wrapped below."""
        p = self.pager
        try:
            w, h = 360, 140
            x, y = (480 - w) // 2, (222 - h) // 2
            p.fill_rect(x, y, w, h, p.rgb(10, 10, 30))
            p.fill_rect(x, y, w, 3, p.rgb(255, 220, 50))
            p.fill_rect(x, y + h - 3, w, 3, p.rgb(255, 220, 50))
            p.fill_rect(x, y, 3, h, p.rgb(255, 220, 50))
            p.fill_rect(x + w - 3, y, 3, h, p.rgb(255, 220, 50))
            p.draw_ttf(x + 12, y + 10, title[:32],
                       p.rgb(255, 220, 50), FONT_TITLE, 20)
            # Body — wrap at ~40 chars, max 4 lines
            line = ''
            ly = y + 40
            for word in body.split():
                if len(line) + 1 + len(word) > 40:
                    p.draw_ttf(x + 12, ly, line, p.rgb(220, 220, 220),
                               FONT_MENU, 16)
                    ly += 20
                    if ly > y + h - 30:
                        break
                    line = word
                else:
                    line = (line + ' ' + word).strip()
            if line and ly <= y + h - 30:
                p.draw_ttf(x + 12, ly, line, p.rgb(220, 220, 220),
                           FONT_MENU, 16)
            p.draw_ttf(x + 12, y + h - 22, 'Press any key',
                       p.rgb(255, 220, 50), FONT_MENU, 12)
            p.flip()
        except Exception:
            pass

    def _wait_button(self):
        """Block until any physical button press and return the mask."""
        p = self.pager
        p.clear_input_events()
        while True:
            while p.has_input_events():
                ev = p.get_input_event()
                if not ev:
                    break
                btn, etype, _ = ev
                if etype == PAGER_EVENT_PRESS:
                    return btn
            time.sleep(0.03)


def run(pager, info, theme_dir=None):
    """Run a payload with an on-screen log. Blocks until the payload
    exits AND the user presses B to return. Returns None.

    Args:
        pager: pagerctl Pager instance.
        info: payload_browser.PayloadInfo.
        theme_dir: path to the active Circuitry theme (defaults to a
                   hard-coded Circuitry path if missing).
    """
    theme_dir = theme_dir or THEME_DIR_FALLBACK

    # Wipe the shared payload log so previous runs don't bleed into
    # this one. This is the ONLY place that resets the log — API
    # handlers must not reset it mid-run.
    try:
        import api_server
        api_server._payload_log.reset()
        api_server._payload_log.add(f'Starting {info.title}...', 'green')
    except Exception:
        pass

    log = _LogRender(pager, theme_dir)
    dialog_runner = _DialogRunner(pager, theme_dir, log)

    runner = duckyctl.run_payload(
        info.payload_dir,
        on_line=log.add_stdout_line,
    )

    pager.clear_input_events()
    last_render = 0
    last_line_count = 0
    stopped_by_user = False
    post_done_redrawn = False

    # Cached handle so we only need to import api_server once.
    import api_server as _api

    while True:
        # -- Dialog servicer: one request per iteration, blocking. --
        try:
            req = _api.dialog_queue.get_nowait()
        except _queue.Empty:
            req = None
        if req is not None and not req.cancelled:
            try:
                dialog_runner.handle(req)
            except Exception:
                req.response = {'success': False}
                req.response_event.set()
            # After a modal closes, force a full repaint of the log
            # screen so the dialog background is erased.
            last_render = 0
            last_line_count = -1  # force re-render regardless of count
            pager.clear_input_events()


        now = time.time()
        status = 'running'
        if runner.exit_code is not None:
            status = ('complete' if runner.exit_code == 0
                      else 'stopped' if stopped_by_user
                      else 'error')

        # Redraw when:
        #  - new lines have arrived in the shared log
        #  - ~4 Hz baseline refresh
        #  - payload just finished (force one final frame)
        cur_lines = len(log.snapshot())
        needs_render = (
            cur_lines != last_line_count
            or now - last_render >= 0.25
            or (status != 'running' and not post_done_redrawn)
        )
        if needs_render:
            log.render(info.title, status)
            last_render = now
            last_line_count = cur_lines
            if status != 'running':
                post_done_redrawn = True

        # Drain input events
        while pager.has_input_events():
            ev = pager.get_input_event()
            if not ev:
                break
            btn, etype, _ = ev
            if etype != PAGER_EVENT_PRESS:
                continue
            if btn & pager.BTN_B:
                if runner.is_running():
                    try:
                        _api._payload_log.add('[stopping...]', 'yellow')
                    except Exception:
                        pass
                    stopped_by_user = True
                    runner.stop()
                    # Unblock any in-flight interact handler so the
                    # bash child can exit cleanly when we SIGTERM it.
                    try:
                        _api.cancel_all_dialogs()
                    except Exception:
                        pass
                else:
                    # Finished and user acknowledged — leave.
                    try:
                        _api.cancel_all_dialogs()
                    except Exception:
                        pass
                    return

        time.sleep(0.05)
