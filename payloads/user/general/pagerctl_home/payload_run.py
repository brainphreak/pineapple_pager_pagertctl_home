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

import theme_utils

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


THEME_DIR_FALLBACK = os.environ.get('PAGERCTL_THEME',
    '/root/payloads/user/general/pagerctl_home/themes/Circuitry')

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

    def _spinner_cfg(self):
        """Lazy-load spinner.json once per render instance."""
        cfg = getattr(self, '_spinner_cfg_cache', None)
        if cfg is not None:
            return cfg
        cfg = _load_json(self.theme_dir, 'components/spinner.json') or {}
        self._spinner_cfg_cache = cfg
        return cfg

    def _render_spinner(self, text):
        """Draw the spinner from components/spinner.json — animation
        frames, tick interval, and text area all come from the theme.
        Fallbacks preserve the original hardcoded layout if the JSON
        is missing or truncated."""
        self._load_spinner_frames()
        p = self.pager
        try:
            p.clear(0)
            cfg = self._spinner_cfg()

            # Animation + interval + per-frame position
            interval = 0.4
            anim = None
            for item in (cfg.get('menu_items') or []):
                if item.get('id') == 'spinner' and item.get('animation'):
                    anim = item.get('animation')
                    interval = float(item.get('animation_interval', interval))
                    break

            now = time.time()
            if now - self._spinner_last_tick >= interval:
                self._spinner_frame_idx = (
                    (self._spinner_frame_idx + 1)
                    % max(1, len(self._spinner_frames)))
                self._spinner_last_tick = now
            if self._spinner_frames:
                frame = self._spinner_frames[self._spinner_frame_idx]
                if frame is not None:
                    if anim and self._spinner_frame_idx < len(anim):
                        fx = int(anim[self._spinner_frame_idx].get('x', 140))
                        fy = int(anim[self._spinner_frame_idx].get('y', 40))
                    else:
                        fx, fy = 140, 40
                    p.draw_image(fx, fy, frame)

            # Text area — bounds/font/color/wrap from JSON
            if text:
                ta = cfg.get('text_area') or {}
                tx = int(ta.get('x', 165))
                ty = int(ta.get('y', 62))
                tw = int(ta.get('w', 150))
                th = int(ta.get('h', 61))
                max_chars = int(ta.get('max_chars', 16))
                max_lines = int(ta.get('max_lines', 4))
                line_h = int(ta.get('line_height', 14))
                fs = int(ta.get('font_size', 12))
                font = FONT_TITLE if ta.get('font') == 'title' else FONT_MENU
                text_color = theme_utils.get_color(
                    p, ta.get('color', 'warning_accent'))

                lines = []
                cur = ''
                for word in text.split():
                    if len(cur) + 1 + len(word) > max_chars:
                        if cur:
                            lines.append(cur)
                        cur = word
                    else:
                        cur = (cur + ' ' + word).strip()
                if cur:
                    lines.append(cur)
                lines = lines[:max_lines]
                total_h = len(lines) * line_h
                start_y = ty + (th - total_h) // 2
                for i, line in enumerate(lines):
                    try:
                        lw = p.ttf_width(line, font, fs)
                    except Exception:
                        lw = len(line) * 7
                    lx = tx + (tw - lw) // 2
                    p.draw_ttf(lx, start_y + i * line_h, line,
                               text_color, font, fs)
            p.flip()
        except Exception:
            pass

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

    def _load_spinner_frames(self):
        """Load spinner frames listed in spinner.json's animation array.
        Falls back to the legacy `assets/spinner/spinner{1..4}.png`
        sequence if the JSON is missing or has no frames."""
        if hasattr(self, '_spinner_frames'):
            return
        self._spinner_frames = []
        cfg = self._spinner_cfg()
        anim = None
        for item in (cfg.get('menu_items') or []):
            if item.get('id') == 'spinner' and item.get('animation'):
                anim = item.get('animation')
                break
        if anim:
            frame_paths = [f.get('image_path') for f in anim if f.get('image_path')]
        else:
            frame_paths = [f'assets/spinner/spinner{i}.png' for i in range(1, 5)]
        for rel in frame_paths:
            path = os.path.join(self.theme_dir, rel)
            try:
                if os.path.isfile(path):
                    self._spinner_frames.append(self.pager.load_image(path))
                else:
                    self._spinner_frames.append(None)
            except Exception:
                self._spinner_frames.append(None)
        self._spinner_frame_idx = 0
        self._spinner_last_tick = 0.0

    def render(self, title, status):
        self._load_assets()
        p = self.pager

        # If a spinner is active, draw it instead of the normal log.
        try:
            import api_server as _api
            if _api.spinner_state.get('active'):
                self._render_spinner(_api.spinner_state.get('text', ''))
                return
        except Exception:
            pass

        try:
            if self.bg_handle:
                p.draw_image(0, 0, self.bg_handle)
            else:
                p.clear(0)

            accent = theme_utils.get_color(p, 'warning_accent')
            log_c = theme_utils.get_color(p, 'pale_blue')

            # Title in yellow, above the cyan separator line in the bg.
            if title:
                p.draw_ttf(TITLE_X, TITLE_Y, title[:40],
                           accent, FONT_TITLE, TITLE_FONT_SIZE)

            # Log body — each cached line in readable TTF below the line.
            visible = self.snapshot()[-LOG_MAX_LINES:]
            y = LOG_START_Y
            for line in visible:
                p.draw_ttf(LOG_START_X, y, line[:LOG_MAX_CHARS],
                           log_c, FONT_MENU, LOG_FONT_SIZE)
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
                           accent, FONT_MENU, 12)

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
        if kind in ('alert', 'warning', 'error', 'prompt'):
            self._alert(req)
        elif kind == 'confirm':
            self._confirm(req)
        elif kind == 'list':
            self._list(req)
        elif kind in ('number', 'ip', 'mac', 'string'):
            self._keyboard(req)
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

        # Block until A or B only — directional presses are ignored
        # so accidental dpad taps don't dismiss the alert.
        accept = self.pager.BTN_A | self.pager.BTN_B
        self._wait_button(accept_mask=accept)
        req.response = {'success': True, 'button': 'A'}

    def _confirm(self, req):
        raw = _load_json(self.theme_dir,
                         'components/dialogs/confirmation_dialog.json')
        message = (req.data.get('message') or req.data.get('text') or
                   req.data.get('prompt') or 'Are you sure?')

        try:
            import api_server
            api_server._payload_log.add(
                f'[CONFIRM] {message}', 'yellow')
        except Exception:
            pass

        _play_alert_sound(self.pager, 'alert')

        confirmed = False
        pressed_b = False

        if raw is None:
            self._simple_modal('Confirm', message + '\n\nA = Yes    B = No')
            accept = self.pager.BTN_A | self.pager.BTN_B
            btn = self._wait_button(accept_mask=accept)
            confirmed = bool(btn & self.pager.BTN_A)
            pressed_b = bool(btn & self.pager.BTN_B)
        else:
            _sub_vars(raw, {
                '$_INPUT': message,
                '$_INPUT_NAME': message,
                '$_PAYLOAD_DESCRIPTION': message,
                '$_CONFIRMATION_TEXT': message,
            })
            items = raw.get('menu_items') or []
            if not items:
                for page in raw.get('pages') or []:
                    items.extend(page.get('menu_items') or [])
            num = len(items) if items else 2

            sel, btn = self._navigate_dialog(raw, num)
            pressed_b = bool(btn & self.pager.BTN_B)
            if pressed_b:
                confirmed = False
            else:
                sel_item = items[sel] if sel < len(items) else {}
                sel_target = sel_item.get('target', '')
                confirmed = (sel_target == 'confirm')

        try:
            import api_server
            label = 'accepted' if confirmed else ('cancelled' if pressed_b else 'denied')
            api_server._payload_log.add(f'  -> {label}',
                'green' if confirmed else 'red')
        except Exception:
            pass

        # hak5cmd response field mapping:
        #   {accepted: true}  → stdout "1" (USER_CONFIRMED), exit 0
        #   {accepted: false} → stdout "0" (USER_DENIED), exit 0
        #   {cancelled: true} → exit 2 (CANCELLED), no stdout
        if pressed_b:
            req.response = {'cancelled': True, 'success': True}
        else:
            req.response = {'accepted': confirmed, 'success': True}

    def _list(self, req):
        """LIST_PICKER / TEXT_PICKER — show a scrollable list of
        options using pager_dialogs.popup_menu. Returns the selected
        item's text in {selected: "..."}."""
        import pager_dialogs

        title = req.data.get('title') or req.data.get('message') or 'Select'
        opts = req.data.get('items') or req.data.get('options') or []
        default = req.data.get('default') or ''

        if not opts:
            req.response = {'cancelled': True, 'success': True}
            return

        try:
            import api_server
            api_server._payload_log.add(
                f'[LIST] {title} ({len(opts)} items)', 'cyan')
        except Exception:
            pass

        # Build (label, value) pairs for popup_menu.
        items = [(str(o), str(o)) for o in opts]

        def bg_drawer():
            try:
                self.pager.clear(0)
            except Exception:
                pass

        result = pager_dialogs.popup_menu(
            self.pager, title, items, bg_drawer=bg_drawer)

        if result is None:
            req.response = {'cancelled': True, 'success': True}
        else:
            req.response = {'selected': result, 'success': True}

    def _keyboard(self, req):
        """Handle PROMPT, IP_PICKER, MAC_PICKER, NUMBER_PICKER,
        TEXT_PICKER — any command that needs the user to type a value.
        Uses our existing pager_dialogs.edit_string on-screen keyboard.
        The themed per-type keyboards (edit_ip_dialog, edit_mac_dialog,
        etc.) can replace this later for a polished look; edit_string
        is functional and universal for now."""
        import pager_dialogs

        prompt = (req.data.get('title') or req.data.get('message') or
                  req.data.get('prompt') or 'Enter value')
        # hak5cmd sends the default value under a type-specific key:
        # ip_picker sends "ip", mac_picker sends "mac", number_picker
        # sends "number", prompt/text sends "default" or "text".
        default = (req.data.get('ip') or req.data.get('mac') or
                   req.data.get('number') or req.data.get('default') or
                   req.data.get('text') or '')

        # Per-type keyboard JSON from the Circuitry theme.
        kb_json_map = {
            'ip':     'components/keyboards/ui_keyboard_ip.json',
            'mac':    'components/keyboards/ui_keyboard_hex.json',
            'number': 'components/keyboards/ui_keyboard_numeric.json',
            'prompt': 'components/keyboards/ui_keyboard.json',
            'string': 'components/keyboards/ui_keyboard.json',
        }
        kb_rel = kb_json_map.get(req.kind, 'components/keyboards/ui_keyboard.json')
        kb_path = os.path.join(self.theme_dir, kb_rel)

        try:
            import api_server
            api_server._payload_log.add(
                f'[{req.kind.upper()}] {prompt}', 'cyan')
        except Exception:
            pass

        result = pager_dialogs.themed_keyboard(
            self.pager, prompt, default,
            keyboard_json_path=kb_path,
            theme_dir=self.theme_dir,
        )

        if result is None:
            req.response = {'cancelled': True, 'success': True}
        else:
            # hak5cmd pickers echo the value back using the SAME
            # field name the request sent the default in:
            #   IP_PICKER  → {ip: "..."}
            #   MAC_PICKER → {mac: "..."}
            #   NUMBER     → {number: "..."}
            #   PROMPT     → {text: "..."}
            #   TEXT/STRING → {text: "..."}
            field_map = {
                'ip': 'ip',
                'mac': 'mac',
                'number': 'number',
            }
            field = field_map.get(req.kind, 'text')
            value = result
            # NUMBER_PICKER: hak5cmd expects an integer, not a string.
            if req.kind == 'number':
                try:
                    value = int(result)
                except (ValueError, TypeError):
                    try:
                        value = float(result)
                    except (ValueError, TypeError):
                        value = 0
            req.response = {field: value, 'success': True}

    # --------------------------------------------------------------
    # Rendering helpers
    # --------------------------------------------------------------

    def _render_raw_screen(self, raw):
        """Render a theme JSON tree one-shot. Fire-and-forget — no
        animations, no status bar widgets. Good enough for alert
        modals and confirmation dialogs.

        Handles both layouts: top-level `menu_items` (alert dialogs)
        and `pages[].menu_items` (confirmation dialog, etc.)."""
        p = self.pager
        try:
            bg = (raw.get('background') or {})
            for layer in bg.get('layers') or []:
                self._draw_layer(layer)

            # Collect items from either top-level or pages array.
            items = raw.get('menu_items') or []
            if not items:
                for page in raw.get('pages') or []:
                    items.extend(page.get('menu_items') or [])

            for idx, item in enumerate(items):
                # Show all items visible — use selected_layers for
                # the first item as the visual default selection.
                layers = (item.get('selected_layers')
                          if idx == 0 else item.get('layers'))
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
            sz_map = {n: float(theme_utils.get_size(n))
                      for n in ('small', 'medium', 'large')}
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

    def _simple_modal_cfg(self):
        """Lazy-load simple_modal.json once per runner."""
        cfg = getattr(self, '_simple_modal_cfg_cache', None)
        if cfg is not None:
            return cfg
        cfg = _load_json(self.theme_dir,
                         'components/dialogs/simple_modal.json') or {}
        self._simple_modal_cfg_cache = cfg
        return cfg

    def _simple_modal(self, title, body, color='alert'):
        """Fallback modal when no theme component for this dialog
        kind exists. All geometry, colors, and fonts come from
        components/dialogs/simple_modal.json. Hardcoded defaults kick
        in only if that component is missing or malformed."""
        p = self.pager
        try:
            cfg = self._simple_modal_cfg()
            box = cfg.get('box') or {}
            tcfg = cfg.get('title') or {}
            bcfg = cfg.get('body') or {}
            fcfg = cfg.get('footer') or {}

            w = int(box.get('w', 360))
            h = int(box.get('h', 140))
            x = int(box.get('x', (480 - w) // 2))
            y = int(box.get('y', (222 - h) // 2))
            bw = int(box.get('border_width', 3))
            fill = theme_utils.get_color(p, box.get('fill', 'navy'))
            edge = theme_utils.get_color(
                p, box.get('border_color', 'warning_accent'))

            p.fill_rect(x, y, w, h, fill)
            p.fill_rect(x, y, w, bw, edge)
            p.fill_rect(x, y + h - bw, w, bw, edge)
            p.fill_rect(x, y, bw, h, edge)
            p.fill_rect(x + w - bw, y, bw, h, edge)

            title_font = FONT_TITLE if tcfg.get('font') == 'title' else FONT_MENU
            title_fs = int(tcfg.get('font_size', 20))
            title_c = theme_utils.get_color(
                p, tcfg.get('color', 'warning_accent'))
            tmax = int(tcfg.get('max_chars', 32))
            tx = x + int(tcfg.get('x_offset', 12))
            ty = y + int(tcfg.get('y_offset', 10))
            p.draw_ttf(tx, ty, title[:tmax], title_c, title_font, title_fs)

            body_font = FONT_TITLE if bcfg.get('font') == 'title' else FONT_MENU
            body_fs = int(bcfg.get('font_size', 16))
            body_c = theme_utils.get_color(
                p, bcfg.get('color', 'modal_body'))
            body_max = int(bcfg.get('max_chars', 40))
            line_h = int(bcfg.get('line_height', 20))
            bottom_pad = int(bcfg.get('bottom_pad', 30))
            bx = x + int(bcfg.get('x_offset', 12))
            by = y + int(bcfg.get('y_offset', 40))

            line = ''
            ly = by
            for word in body.split():
                if len(line) + 1 + len(word) > body_max:
                    p.draw_ttf(bx, ly, line, body_c, body_font, body_fs)
                    ly += line_h
                    if ly > y + h - bottom_pad:
                        break
                    line = word
                else:
                    line = (line + ' ' + word).strip()
            if line and ly <= y + h - bottom_pad:
                p.draw_ttf(bx, ly, line, body_c, body_font, body_fs)

            if fcfg:
                ftxt = fcfg.get('text', 'Press any key')
                footer_font = FONT_TITLE if fcfg.get('font') == 'title' else FONT_MENU
                footer_fs = int(fcfg.get('font_size', 12))
                footer_c = theme_utils.get_color(
                    p, fcfg.get('color', 'warning_accent'))
                fx = x + int(fcfg.get('x_offset', 12))
                fy = y + h - int(fcfg.get('y_from_bottom', 22))
                p.draw_ttf(fx, fy, ftxt, footer_c, footer_font, footer_fs)

            p.flip()
        except Exception:
            pass

    def _wait_button(self, accept_mask=None):
        """Block until a qualifying physical button press and return
        the mask. If `accept_mask` is given, only those buttons are
        accepted — directional presses are silently swallowed."""
        p = self.pager
        p.clear_input_events()
        while True:
            while p.has_input_events():
                ev = p.get_input_event()
                if not ev:
                    break
                btn, etype, _ = ev
                if etype != PAGER_EVENT_PRESS:
                    continue
                if accept_mask is None or (btn & accept_mask):
                    return btn
            time.sleep(0.03)

    def _navigate_dialog(self, raw, num_items):
        """Full navigation loop for a dialog with multiple selectable
        items. Renders the dialog, handles left/right/up/down to
        switch selected item, and returns (selected_index, button)
        when A or B is pressed.

        selected_index is 0-based into the items list.
        button is the raw pager button mask (BTN_A or BTN_B)."""
        p = self.pager
        sel = 0
        p.clear_input_events()
        self._render_dialog_with_selection(raw, sel)

        while True:
            while p.has_input_events():
                ev = p.get_input_event()
                if not ev:
                    break
                btn, etype, _ = ev
                if etype != PAGER_EVENT_PRESS:
                    continue
                if btn & p.BTN_A:
                    return sel, btn
                if btn & p.BTN_B:
                    return sel, btn
                if btn & (p.BTN_LEFT | p.BTN_UP):
                    sel = (sel - 1) % num_items
                    self._render_dialog_with_selection(raw, sel)
                elif btn & (p.BTN_RIGHT | p.BTN_DOWN):
                    sel = (sel + 1) % num_items
                    self._render_dialog_with_selection(raw, sel)
            time.sleep(0.03)

    def _render_dialog_with_selection(self, raw, selected_index):
        """Render a theme dialog with the given item highlighted."""
        p = self.pager
        try:
            bg = (raw.get('background') or {})
            for layer in bg.get('layers') or []:
                self._draw_layer(layer)

            items = raw.get('menu_items') or []
            if not items:
                for page in raw.get('pages') or []:
                    items.extend(page.get('menu_items') or [])

            for idx, item in enumerate(items):
                layers = (item.get('selected_layers')
                          if idx == selected_index
                          else item.get('layers'))
                for layer in layers or []:
                    self._draw_layer(layer)
            p.flip()
        except Exception:
            pass


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
