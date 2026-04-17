"""settings_ui.py - Theme-driven settings menu for pagerctl_home.

Reads the category/item schema from a theme JSON so that users can
edit/add/remove settings entries without touching Python.

Schema path (relative to active theme dir):
    components/dashboards/settings_dashboard.json

Takes over rendering while active and returns to the home screen on B.
Power exits and requests the power menu.
"""

import os
import json
import time
import subprocess

from wardrive.config import (load_config, save_config, SETTINGS_FILE,
                              backup_settings, list_backups, restore_backup)
from wardrive.web_server import wait_any_button
from wardrive.gps_utils import (detect_gps_devices, get_device_name,
                                 short_device_label, get_gpsd_baud)
from wardrive import wifi_utils


FONT_MENU = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wardrive', 'fonts', 'menu.ttf')
FONT_TITLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wardrive', 'fonts', 'title.TTF')
SCREEN_W = 480
SCREEN_H = 222

LAYOUT_REL = 'components/dashboards/settings_dashboard.json'


class SettingsUI:
    """Two-pane settings navigator driven by a theme JSON schema."""

    def __init__(self):
        self.bg_handle = None
        self.pager = None
        self.engine = None
        self.config = None
        self._layout = None
        self._layout_path = None
        self._layout_mtime = 0

    def run(self, pager, engine=None):
        self.pager = pager
        self.engine = engine
        self.config = load_config()
        self._load_layout()

        if not self._layout:
            return None

        # Load background via theme-relative path
        self.bg_handle = None
        bg_rel = self._layout.get('bg_image')
        if bg_rel:
            theme_dir = engine.theme_dir if engine else os.path.join(
                os.path.dirname(__file__), 'themes', 'Circuitry')
            bg_path = os.path.join(theme_dir, bg_rel)
            if os.path.isfile(bg_path):
                self.bg_handle = pager.load_image(bg_path)

        pager.clear_input_events()
        return self._category_loop()

    def _load_layout(self):
        theme_dir = self.engine.theme_dir if self.engine else os.path.join(
            os.path.dirname(__file__), 'themes', 'Circuitry')
        path = os.path.join(theme_dir, LAYOUT_REL)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            self._layout = None
            return
        if self._layout is not None and path == self._layout_path and mtime == self._layout_mtime:
            return
        try:
            with open(path) as f:
                self._layout = json.load(f)
            self._layout_path = path
            self._layout_mtime = mtime
        except Exception:
            self._layout = None

    # -- Color helpers --

    def _color(self, name):
        c = self._layout.get('colors', {}).get(name, [255, 255, 255])
        return self.pager.rgb(*c)

    def _drain_until_released(self, btn_mask):
        """Wait for a button to be released and clear the event queue.
        Prevents stale events from leaking to the main loop after we
        return (the "press B twice to go back" bug)."""
        p = self.pager
        deadline = time.time() + 0.5
        while time.time() < deadline:
            try:
                held, _, _ = p.poll_input()
            except Exception:
                break
            if not (held & btn_mask):
                break
            time.sleep(0.01)
        p.clear_input_events()

    # -- Drawing primitives --

    def _draw_bg(self):
        p = self.pager
        if self.bg_handle:
            p.draw_image(0, 0, self.bg_handle)
        else:
            p.clear(0)

    def _draw_title(self, text):
        p = self.pager
        size = self._layout.get('title_size', 24)
        tw = p.ttf_width(text, FONT_TITLE, size)
        p.draw_ttf((SCREEN_W - tw) // 2, 12, text, self._color('title'), FONT_TITLE, size)

    def _draw_widgets(self):
        if self.engine:
            for w in self.engine.widgets:
                try:
                    w.render(self.pager, self.engine.renderer)
                except Exception:
                    pass

    def _draw_list(self, items, selected, x, y_start, row_h, fs,
                   color_normal, color_selected):
        p = self.pager
        for i, label in enumerate(items):
            y = y_start + i * row_h
            c = color_selected if i == selected else color_normal
            p.draw_ttf(x, y, label, c, FONT_MENU, fs)

    # -- Main navigation loop --

    def _category_loop(self):
        """Left pane: category list. A selects, B exits, power → power menu."""
        cfg = self._layout
        categories = cfg.get('categories', [])
        if not categories:
            return None
        fs = cfg.get('font_size', 18)
        row_h = cfg.get('row_height', 22)
        left_x = cfg.get('left_x', 20)
        y_start = cfg.get('y_start', 52)

        selected = 0
        while True:
            self._draw_bg()

            # Category list on left
            labels = [c.get('label', c.get('id', '?')) for c in categories]
            self._draw_list(labels, selected, left_x, y_start, row_h, fs,
                            self._color('category'), self._color('category_selected'))

            # Preview of selected category items on right (no values yet)
            cat = categories[selected]
            right_x = cfg.get('right_x', 180)
            preview_items = [i.get('label', '?') for i in cat.get('items', [])]
            for i, label in enumerate(preview_items[:6]):
                y = y_start + i * row_h
                self.pager.draw_ttf(right_x, y, label, self._color('dim'), FONT_MENU, fs)

            self._draw_widgets()
            self.pager.flip()

            btn = wait_any_button(self.pager)
            if btn & self.pager.BTN_UP:
                selected = (selected - 1) % len(categories)
            elif btn & self.pager.BTN_DOWN:
                selected = (selected + 1) % len(categories)
            elif btn & self.pager.BTN_A or btn & self.pager.BTN_RIGHT:
                result = self._item_loop(categories, selected)
                if result == 'power':
                    self.pager.clear_input_events()
                    return 'power'
            elif btn & self.pager.BTN_POWER:
                self._drain_until_released(self.pager.BTN_POWER)
                return 'power'
            elif btn & self.pager.BTN_B or btn & self.pager.BTN_LEFT:
                self._drain_until_released(self.pager.BTN_B)
                return None

    def _item_loop(self, categories, cat_index):
        """Right pane: items within a category, showing live values.
        Left pane: category list stays visible with the active category
        highlighted (dim, not selectable while we're drilling into items).

        Scrolls when the list is longer than the visible window. A small
        up/down arrow indicator is drawn when more items exist above or
        below the current view."""
        cfg = self._layout
        category = categories[cat_index]
        items = category.get('items', [])
        if not items:
            return
        fs = cfg.get('font_size', 18)
        row_h = cfg.get('row_height', 22)
        left_x = cfg.get('left_x', 20)
        right_x = cfg.get('right_x', 180)
        y_start = cfg.get('y_start', 52)

        # How many rows fit between y_start and the status bar / bottom
        bottom_limit = 190
        max_visible = max(1, (bottom_limit - y_start) // row_h)

        selected = 0
        scroll = 0
        while True:
            # Adjust scroll so `selected` is always on screen
            if selected < scroll:
                scroll = selected
            elif selected >= scroll + max_visible:
                scroll = selected - max_visible + 1

            self._draw_bg()

            # Left pane: category list (always short so no scroll needed)
            for i, c in enumerate(categories):
                y = y_start + i * row_h
                label = c.get('label', '?')
                color = self._color('category_selected') if i == cat_index else self._color('dim')
                self.pager.draw_ttf(left_x, y, label, color, FONT_MENU, fs)

            # Visible slice of items
            visible_end = min(scroll + max_visible, len(items))
            for draw_row, i in enumerate(range(scroll, visible_end)):
                item = items[i]
                y = y_start + draw_row * row_h
                label = item.get('label', '?')
                value = self._format_item_value(item)
                is_sel = (i == selected)
                label_c = self._color('category_selected' if is_sel else 'category')
                value_c = self._color('value_selected' if is_sel else 'value')
                # Only show "Label:" when there's a value to follow it.
                # Pure-action rows (no value) just show the label.
                if value:
                    label_text = label + ':'
                    self.pager.draw_ttf(right_x, y, label_text, label_c, FONT_MENU, fs)
                    lw = self.pager.ttf_width(label_text, FONT_MENU, fs)
                    self.pager.draw_ttf(right_x + lw + 6, y, value, value_c, FONT_MENU, fs)
                else:
                    self.pager.draw_ttf(right_x, y, label, label_c, FONT_MENU, fs)

            # Scroll indicators: ^ if more above, v if more below
            dim = self._color('dim')
            if scroll > 0:
                self.pager.draw_ttf(460, y_start - 2, '^', dim, FONT_MENU, fs)
            if visible_end < len(items):
                self.pager.draw_ttf(460, y_start + (max_visible - 1) * row_h, 'v', dim, FONT_MENU, fs)

            self._draw_widgets()
            self.pager.flip()

            btn = wait_any_button(self.pager)
            if btn & self.pager.BTN_UP:
                selected = (selected - 1) % len(items)
            elif btn & self.pager.BTN_DOWN:
                selected = (selected + 1) % len(items)
            elif btn & self.pager.BTN_A:
                self._activate_item(items[selected])
            elif btn & self.pager.BTN_POWER:
                self._drain_until_released(self.pager.BTN_POWER)
                return 'power'
            elif btn & self.pager.BTN_B or btn & self.pager.BTN_LEFT:
                self._drain_until_released(self.pager.BTN_B)
                return None

    # -- Item value formatting / activation --

    def _format_item_value(self, item):
        t = item.get('type')
        if t == 'toggle':
            return 'ON' if self.config.get(item['key'], False) else 'OFF'
        if t == 'cycle':
            val = self.config.get(item['key'])
            # Special case: GPS baud "auto" → show detected value
            if item.get('key') == 'gps_baud' and (val == 'auto' or val == 0):
                detected = get_gpsd_baud(self.config.get('gps_device', ''))
                return f'auto ({detected})' if detected else 'auto'
            return self._format_scalar(val, item)
        if t == 'action':
            # Actions can optionally show a live value beside the
            # label (e.g. Hotspot action shows Enabled/Disabled).
            src = item.get('value_source')
            if src:
                return self._resolve_live_value(src)
            return ''
        if t == 'service':
            run, en = self._service_state(item.get('service_name', ''),
                                          item.get('process_name'))
            run_s = 'Run' if run else 'Stop'
            en_s = 'Auto' if en else 'Man'
            return f'{run_s}/{en_s}'
        if t == 'edit_string':
            val = self.config.get(item.get('key', ''), '')
            if item.get('secret'):
                return 'set' if val else 'not set'
            return str(val) if val else '-'
        if t == 'live_value':
            return self._resolve_live_value(item.get('value_source', ''))
        return str(self.config.get(item.get('key'), ''))

    def _resolve_live_value(self, source):
        """Resolve a live_value item's `value_source` to a display string.
        Add new sources here as needed."""
        if source == 'wifi_ssid':
            try:
                ssid = wifi_utils.get_client_status() or wifi_utils.get_client_ssid()
                return ssid or 'Not connected'
            except Exception:
                return '?'
        if source == 'wifi_hotspot_state':
            # wlan0mgmt is shared between the user's hotspot and the
            # captive AP. uci `disabled=0` can't tell them apart, so
            # defer to the captive marker: if captive owns the AP
            # right now, report hotspot as Disabled regardless of
            # the uci state.
            try:
                from captive import ap_control
                if ap_control.marker_exists():
                    return 'Disabled'
            except Exception:
                pass
            try:
                enabled, _, _ = wifi_utils.get_hotspot_state()
                return 'Enabled' if enabled else 'Disabled'
            except Exception:
                return '?'
        if source == 'wifi_pineap_state':
            try:
                return 'Enabled' if wifi_utils.get_pineap_state() else 'Disabled'
            except Exception:
                return '?'
        if source == 'gps_device':
            dev = self.config.get('gps_device') or ''
            if not dev:
                return 'auto'
            # short_device_label already returns 'u-blox (ttyACM2)'
            return short_device_label(dev)
        return ''

    def _format_scalar(self, val, item):
        if val is None or val == '':
            if item.get('include_none'):
                return 'None'
            return '?'
        fmt = item.get('formatter')
        if fmt == 'seconds':
            try:
                n = int(val)
                if n == 0:
                    return 'Never'
                if n < 60:
                    return f'{n}s'
                return f'{n // 60}m'
            except Exception:
                return str(val)
        # For file-backed cycles, show the basename without extension.
        if item.get('source_dir'):
            ext = item.get('extension', '')
            name = str(val)
            if ext and name.endswith(ext):
                name = name[:-len(ext)]
            return name
        suffix = item.get('suffix', '')
        return f'{val}{suffix}'

    def _resolve_options(self, item):
        """Return the option list for a cycle item — static options[] or
        files from source_dir (+extension, + optional 'None' sentinel)."""
        static = item.get('options')
        if static:
            return static
        src_dir = item.get('source_dir')
        if not src_dir:
            return []
        ext = item.get('extension', '')
        try:
            files = sorted(
                f for f in os.listdir(src_dir)
                if not ext or f.endswith(ext)
            )
        except Exception:
            files = []
        if item.get('include_none'):
            files = ['None'] + files
        return files

    def _activate_item(self, item):
        t = item.get('type')
        if t == 'toggle':
            key = item['key']
            self.config[key] = not self.config.get(key, False)
            save_config(self.config)
            self._apply_side_effect(item)
        elif t == 'cycle':
            self._cycle_item(item)
        elif t == 'action':
            self._run_action(item)
        elif t == 'service':
            self._service_picker(item)
        elif t == 'edit_string':
            self._edit_string(item)

    # -- On-screen keyboard for text input --

    _KB_ROWS = [
        ['1','2','3','4','5','6','7','8','9','0','BK'],
        ['q','w','e','r','t','y','u','i','o','p','SP'],
        ['a','s','d','f','g','h','j','k','l','.','OK'],
        ['z','x','c','v','b','n','m','-','_','@','X'],
    ]
    _KB_ACTIONS = {'BK', 'SP', 'OK', 'X'}

    def _edit_string(self, item):
        """On-screen keyboard dialog — returns when user hits OK, X, or B."""
        p = self.pager
        key = item.get('key', '')
        buf = str(self.config.get(key, '') or '')
        secret = bool(item.get('secret'))
        label = item.get('label', 'Edit')
        maxlen = int(item.get('max_length', 48))

        rows = len(self._KB_ROWS)
        cols = len(self._KB_ROWS[0])
        cell_w = 40
        cell_h = 28
        grid_x0 = 20
        grid_y0 = 80
        sel_row, sel_col = 0, 0

        c_label = self._color('category')
        c_val = self._color('category_selected')
        c_dim = self._color('dim')
        c_cell = self._color('category')
        c_cell_sel = self._color('category_selected')
        fs = 18
        cell_fs = 16

        while True:
            self._draw_bg()

            # Header: label + current buffer (masked if secret)
            p.draw_ttf(20, 40, f'{label}:', c_label, FONT_MENU, fs)
            lw = p.ttf_width(f'{label}:', FONT_MENU, fs)
            display = ('*' * len(buf)) if secret else buf
            p.draw_ttf(20 + lw + 8, 40, display + '_', c_val, FONT_MENU, fs)

            # Keyboard grid
            for r in range(rows):
                for c in range(cols):
                    ch = self._KB_ROWS[r][c]
                    x = grid_x0 + c * cell_w
                    y = grid_y0 + r * cell_h
                    is_sel = (r == sel_row and c == sel_col)
                    if is_sel:
                        p.fill_rect(x - 2, y - 2, cell_w - 4, cell_h - 4, p.rgb(40, 40, 40))
                    color = c_cell_sel if is_sel else c_cell
                    # Offset text for readability; multi-char labels smaller
                    tfs = cell_fs if len(ch) == 1 else cell_fs - 2
                    p.draw_ttf(x + 6, y + 2, ch, color, FONT_MENU, tfs)

            self._draw_widgets()
            p.flip()

            btn = wait_any_button(p)
            if btn & p.BTN_UP:
                sel_row = (sel_row - 1) % rows
            elif btn & p.BTN_DOWN:
                sel_row = (sel_row + 1) % rows
            elif btn & p.BTN_LEFT:
                sel_col = (sel_col - 1) % cols
            elif btn & p.BTN_RIGHT:
                sel_col = (sel_col + 1) % cols
            elif btn & p.BTN_A:
                ch = self._KB_ROWS[sel_row][sel_col]
                if ch == 'BK':
                    buf = buf[:-1]
                elif ch == 'SP':
                    if len(buf) < maxlen:
                        buf += ' '
                elif ch == 'OK':
                    self.config[key] = buf
                    save_config(self.config)
                    self._apply_side_effect(item)
                    self._flash('Saved')
                    return
                elif ch == 'X':
                    return
                else:
                    if len(buf) < maxlen:
                        buf += ch
            elif btn & p.BTN_B:
                return

    def _service_state(self, name, process_name=None):
        """Return (running, enabled) tuple for an init.d service.

        Values are cached forever once loaded. Cache is cleared only
        when the user triggers a start/stop/enable/disable via
        _service_action(), so navigating the list re-uses the same
        dict lookups with zero subprocess spawns.

        First-time load uses a batched shell probe that queries every
        service the theme knows about in a SINGLE subprocess — MIPS
        fork+exec is expensive so one spawn beats 14.
        """
        if not name:
            return (False, False)
        if not hasattr(self, '_svc_cache'):
            self._svc_cache = {}
        proc = process_name or name
        key = (name, proc)
        if key in self._svc_cache:
            return self._svc_cache[key]
        # Cache miss — do a batch load of every service in the Services
        # category so subsequent navigation is instant.
        self._load_service_cache_batch()
        return self._svc_cache.get(key, (False, False))

    def _load_service_cache_batch(self):
        """Batch-query running/enabled state for every service in the
        Services category via a single shell subprocess."""
        if not hasattr(self, '_svc_cache'):
            self._svc_cache = {}
        cfg = self._layout or {}
        services = []
        for cat in cfg.get('categories', []):
            if cat.get('id') == 'services':
                for item in cat.get('items', []):
                    if item.get('type') == 'service':
                        name = item.get('service_name', '')
                        proc = item.get('process_name', name)
                        if name:
                            services.append((name, proc))
                break
        if not services:
            return

        # Shell script: for each (service, proc), emit a line
        #   name|proc|run_rc|en_rc
        lines = []
        for svc, proc in services:
            lines.append(
                'name="{0}"; proc="{1}"; '
                'if [ -f /etc/init.d/$name ]; then '
                'pidof $proc >/dev/null 2>&1; r=$?; '
                '/etc/init.d/$name enabled >/dev/null 2>&1; e=$?; '
                'echo "$name|$proc|$r|$e"; '
                'else echo "$name|$proc|1|1"; fi'.format(svc, proc)
            )
        script = '; '.join(lines)
        try:
            r = subprocess.run(
                ['sh', '-c', script], capture_output=True, text=True, timeout=8
            )
            for line in r.stdout.strip().split('\n'):
                parts = line.split('|')
                if len(parts) != 4:
                    continue
                svc, proc, rc_run, rc_en = parts
                run = rc_run == '0'
                en = rc_en == '0'
                self._svc_cache[(svc, proc)] = (run, en)
        except Exception:
            # On failure, populate with False so we don't retry forever
            for svc, proc in services:
                self._svc_cache[(svc, proc)] = (False, False)

    def _rc(self, *args):
        try:
            return subprocess.run(list(args), capture_output=True,
                                  timeout=3).returncode
        except Exception:
            return -1

    def _service_action(self, name, action):
        path = f'/etc/init.d/{name}'
        if not os.path.isfile(path):
            return False
        try:
            subprocess.run([path, action], capture_output=True, timeout=10)
            # Invalidate cache so the next render shows fresh state
            if hasattr(self, '_svc_cache'):
                self._svc_cache.clear()
            return True
        except Exception:
            return False

    def _service_picker(self, item):
        """Start/Stop/Restart/Enable/Disable picker for an init.d service."""
        name = item.get('service_name', '')
        p = self.pager
        if not name:
            return
        cfg = self._layout
        fs = cfg.get('font_size', 18)
        row_h = cfg.get('row_height', 22)
        right_x = cfg.get('right_x', 180)
        y_start = cfg.get('y_start', 52)

        selected = 0
        while True:
            run, en = self._service_state(name, item.get('process_name'))
            actions = []
            if run:
                actions.append(('Stop', 'stop'))
                actions.append(('Restart', 'restart'))
            else:
                actions.append(('Start', 'start'))
            if en:
                actions.append(('Disable Autostart', 'disable'))
            else:
                actions.append(('Enable Autostart', 'enable'))

            self._draw_bg()
            # Header showing service + state on the left pane area
            state = f'{name}: {"Running" if run else "Stopped"} / {"Auto" if en else "Manual"}'
            p.draw_ttf(20, y_start, state, self._color('category_selected'),
                       FONT_MENU, fs)
            # Action list on the right
            for i, (label, _) in enumerate(actions):
                y = y_start + (i + 2) * row_h
                color = self._color('category_selected' if i == selected else 'category')
                p.draw_ttf(right_x, y, label, color, FONT_MENU, fs)
            self._draw_widgets()
            p.flip()

            btn = wait_any_button(p)
            if btn & p.BTN_UP:
                selected = (selected - 1) % len(actions)
            elif btn & p.BTN_DOWN:
                selected = (selected + 1) % len(actions)
            elif btn & p.BTN_A:
                _, act = actions[selected]
                ok = self._service_action(name, act)
                self._flash(f'{name} {act}' + ('' if ok else ' failed'))
                if act == 'stop':
                    selected = 0
            elif btn & p.BTN_POWER:
                return
            elif btn & p.BTN_B:
                return

    def _cycle_item(self, item):
        """Advance to the next option. Options are either static (item.options)
        or dynamically resolved from source_dir."""
        key = item['key']
        options = self._resolve_options(item)
        if not options:
            return
        current = self.config.get(key)
        try:
            idx = options.index(current)
        except ValueError:
            idx = -1
        new_val = options[(idx + 1) % len(options)]
        self.config[key] = new_val
        save_config(self.config)
        self._apply_side_effect(item)

    def _apply_side_effect(self, item):
        """Settings that need immediate hardware action when changed."""
        apply = item.get('apply')
        key = item.get('key')
        if apply == 'brightness':
            try:
                self.pager.set_brightness(int(self.config.get('brightness', 80)))
            except Exception:
                pass
        if key == 'web_server':
            from wardrive.web_server import start_web_ui, stop_web_ui
            if self.config.get('web_server', False):
                port = self.config.get('web_port', 1337)
                start_web_ui(port=port)
                self._flash(f"Web UI on :{port}")
            else:
                stop_web_ui()
                self._flash('Web UI stopped')
        if key == 'web_port':
            # Bounce the web server so it rebinds on the new port
            from wardrive.web_server import start_web_ui, stop_web_ui, is_running
            if is_running():
                stop_web_ui()
                port = self.config.get('web_port', 1337)
                start_web_ui(port=port)
                self._flash(f"Web UI on :{port}")

    def _do_vibrate(self, ms=500):
        """Pulse the vibration motor.

        On this hardware the buzzer/PWM subsystem the vibrator
        shares goes into power-save when idle. The first vibrate
        call after a long pause is silently dropped while the
        subsystem wakes back up. Workaround: fire a short, very
        quiet beep first to wake the subsystem, then run the
        100/100/200 ms vibrate trio (matching the reference
        demo at .../examples/demo.py).
        """
        if not self.config.get('vibrate_enabled', True):
            return
        try:
            # Subsystem wake-up — short, near-inaudible beep
            self.pager.beep(20, 5)
            time.sleep(0.02)
            self.pager.vibrate(100)
            time.sleep(0.10)
            self.pager.vibrate(100)
            time.sleep(0.10)
            self.pager.vibrate(200)
        except Exception:
            pass

    def _pick_gps_device(self):
        """Scan for GPS devices and let the user pick one — modal popup."""
        self._flash('Scanning for GPS...', 0.4)
        devices = detect_gps_devices()
        if not devices:
            self._flash('No GPS devices found')
            return

        items = [(short_device_label(d), d) for d in devices]
        items.append(("Back", None))
        self._popup_menu("Select GPS", items, on_select=self._apply_gps_device)

    def _apply_gps_device(self, dev):
        if dev is None:
            return
        self.config['gps_device'] = dev
        save_config(self.config)
        self._flash(f"Set: {os.path.basename(dev)}")

    def _popup_menu(self, title, items, on_select=None):
        """Generic modal menu: dark bordered box centered on screen
        showing a title + selectable items. items is a list of
        (label, value) tuples. on_select(value) is called when the
        user presses A on a row. B/Power exit without calling.
        Background settings remain visible underneath."""
        p = self.pager
        fs = 18
        row_h = 22
        title_h = 26
        # Compute widest label for box width
        widest = max(p.ttf_width(lbl, FONT_MENU, fs) for lbl, _ in items)
        widest = max(widest, p.ttf_width(title, FONT_MENU, fs))
        box_w = min(SCREEN_W - 40, widest + 40)
        box_h = title_h + len(items) * row_h + 16
        box_h = min(box_h, SCREEN_H - 30)
        bx = (SCREEN_W - box_w) // 2
        by = (SCREEN_H - box_h) // 2

        edge = self._color('title') if 'title' in self._layout.get('colors', {}) \
            else p.rgb(100, 200, 255)
        title_c = edge
        sel_c = self._color('category_selected')
        norm_c = self._color('category')

        selected = 0
        scroll = 0
        max_visible = max(1, (box_h - title_h - 16) // row_h)

        while True:
            # Don't redraw background — keep settings visible underneath.
            # Only redraw the popup box.
            p.fill_rect(bx, by, box_w, box_h, p.rgb(0, 0, 0))
            # Border (4 thin rects)
            p.fill_rect(bx, by, box_w, 1, edge)
            p.fill_rect(bx, by + box_h - 1, box_w, 1, edge)
            p.fill_rect(bx, by, 1, box_h, edge)
            p.fill_rect(bx + box_w - 1, by, 1, box_h, edge)

            # Title
            tw = p.ttf_width(title, FONT_MENU, fs)
            p.draw_ttf(bx + (box_w - tw) // 2, by + 6, title, title_c, FONT_MENU, fs)
            # Divider
            p.fill_rect(bx + 4, by + title_h, box_w - 8, 1, edge)

            # Adjust scroll
            if selected < scroll:
                scroll = selected
            elif selected >= scroll + max_visible:
                scroll = selected - max_visible + 1

            # Items
            visible_end = min(scroll + max_visible, len(items))
            for draw_row, i in enumerate(range(scroll, visible_end)):
                label, _ = items[i]
                y = by + title_h + 8 + draw_row * row_h
                color = sel_c if i == selected else norm_c
                lw = p.ttf_width(label, FONT_MENU, fs)
                p.draw_ttf(bx + (box_w - lw) // 2, y, label, color, FONT_MENU, fs)

            self._draw_widgets()
            p.flip()

            btn = wait_any_button(p)
            if btn & p.BTN_UP:
                selected = (selected - 1) % len(items)
            elif btn & p.BTN_DOWN:
                selected = (selected + 1) % len(items)
            elif btn & p.BTN_A:
                _, value = items[selected]
                if on_select:
                    on_select(value)
                return value
            elif btn & p.BTN_POWER:
                return None
            elif btn & p.BTN_B:
                return None

    def _restart_gpsd(self):
        """Restart gpsd with the currently configured device + baud."""
        dev = self.config.get('gps_device') or '/dev/ttyACM0'
        baud = self.config.get('gps_baud', 'auto')
        baud_arg = '' if baud in ('auto', 0, '0') else f'-s {baud} '
        os.system(f'killall gpsd 2>/dev/null; sleep 1; gpsd {baud_arg}{dev} 2>/dev/null &')
        self._flash('gpsd restarted')

    # -- WiFi scan + connect --

    def _wifi_scan_connect(self):
        """Run a WiFi scan, let the user pick a network, prompt for the
        password via the on-screen keyboard, and commit the uci config."""
        self._flash('Scanning WiFi...', 0.4)
        networks = wifi_utils.scan_networks()
        if not networks:
            self._flash('No networks found')
            return

        cfg = self._layout
        fs = cfg.get('font_size', 18)
        row_h = cfg.get('row_height', 22)
        right_x = cfg.get('right_x', 220)
        y_start = cfg.get('y_start', 52)

        # Show top 8 for readability
        visible = networks[:10]
        items = []
        for n in visible:
            ssid = n['ssid']
            sig = n.get('signal', -100)
            enc = n.get('enc', 'open')
            label = f'{ssid[:14]} {sig}dBm {enc}'
            items.append((label, n))
        items.append(('Cancel', None))

        selected = 0
        while True:
            self._draw_bg()
            for i, (label, _) in enumerate(items):
                y = y_start + i * row_h
                if y > 190: break
                color = self._color('category_selected' if i == selected else 'category')
                self.pager.draw_ttf(right_x, y, label, color, FONT_MENU, fs)
            self._draw_widgets()
            self.pager.flip()

            btn = wait_any_button(self.pager)
            if btn & self.pager.BTN_UP:
                selected = (selected - 1) % len(items)
            elif btn & self.pager.BTN_DOWN:
                selected = (selected + 1) % len(items)
            elif btn & self.pager.BTN_A:
                net = items[selected][1]
                if net is None:
                    return
                # Ask for password via on-screen keyboard
                enc = net.get('enc', 'open')
                if enc == 'open':
                    password = ''
                else:
                    tmp_item = {
                        'key': '_wifi_tmp_password',
                        'label': f"Password for {net['ssid'][:12]}",
                        'secret': True,
                    }
                    # Pre-seed with empty so _edit_string starts clean
                    self.config['_wifi_tmp_password'] = ''
                    self._edit_string(tmp_item)
                    password = self.config.get('_wifi_tmp_password', '')
                    # Don't persist the temp key in settings
                    self.config.pop('_wifi_tmp_password', None)
                    save_config(self.config)

                self._flash('Connecting...', 0.5)
                enc_uci = 'psk2' if enc in ('wpa2', 'wpa') else ('none' if enc == 'open' else 'psk2')
                ok = wifi_utils.connect_network(net['ssid'], password, enc_uci)
                self._flash('Connected' if ok else 'Connect failed')
                return
            elif btn & self.pager.BTN_B:
                return

    def _stop_captive(self):
        """Tear down a running captive portal attack from any context."""
        try:
            from captive import server as cap_server
            from captive import dns_hijack
            from captive import ap_control
            cap_server.stop()
            dns_hijack.disable()
            ap_control.stop_ap()
        except Exception:
            pass

    def _wifi_pineap_toggle(self):
        """Enable/disable the PineAP/Karma capture interface.
        Mutually exclusive with the hotspot and captive portal."""
        currently = wifi_utils.get_pineap_state()
        target = not currently
        if target:
            mode = wifi_utils.get_active_wifi_mode()
            if mode == 'captive':
                if not self._popup_menu(
                        'Captive is on. Disable it?',
                        [('No, cancel', False), ('YES, disable', True)]):
                    return
                self._stop_captive()
            elif mode == 'hotspot':
                if not self._popup_menu(
                        'Hotspot is on. Disable it?',
                        [('No, cancel', False), ('YES, disable', True)]):
                    return
                wifi_utils.set_hotspot(False)
            choice = self._popup_menu(
                'Enable PineAP capture?',
                [('No, cancel', False), ('YES, enable', True)],
            )
            if not choice:
                return
            self._flash('Starting PineAP...', 0.4)
            wifi_utils.set_pineap_capture(True)
            self._flash('PineAP enabled')
        else:
            choice = self._popup_menu(
                'Disable PineAP capture?',
                [('No, keep on', False), ('YES, disable', True)],
            )
            if not choice:
                return
            self._flash('Stopping PineAP...', 0.3)
            wifi_utils.set_pineap_capture(False)
            self._flash('PineAP disabled')

    def _wifi_fix_net(self):
        """Restore wireless/network/firewall/dhcp from the baseline
        snapshot and reload all services. One-button recovery if the
        user breaks the network config."""
        # Confirm via popup (destructive)
        items = [('No, cancel', False), ('YES, reset network', True)]
        choice = self._popup_menu('Reset network to defaults?', items)
        if not choice:
            return
        self._flash('Resetting network...', 0.4)
        ok, n, msg = wifi_utils.fix_net()
        self._flash(msg if msg else ('Done' if ok else 'Failed'), 1.5)

    def _wifi_hotspot_toggle(self):
        """Enable/disable the WiFi hotspot, with confirmation prompt."""
        enabled, cur_ssid, cur_key = wifi_utils.get_hotspot_state()
        target_enabled = not enabled

        if target_enabled:
            # Mutual exclusion: offer to stop the conflicting mode
            mode = wifi_utils.get_active_wifi_mode()
            if mode == 'captive':
                if not self._popup_menu(
                        'Captive is on. Disable it?',
                        [('No, cancel', False), ('YES, disable', True)]):
                    return
                self._stop_captive()
            elif mode == 'pineap':
                if not self._popup_menu(
                        'PineAP is on. Disable it?',
                        [('No, cancel', False), ('YES, disable', True)]):
                    return
                wifi_utils.set_pineap_capture(False)

            ssid = self.config.get('hotspot_ssid', '') or cur_ssid or 'pager'
            password = self.config.get('hotspot_password', '') or cur_key
            if not password or len(password) < 8:
                self._flash('Set 8+ char password first')
                return
            # Confirm before enabling
            choice = self._popup_menu(
                f'Enable hotspot {ssid}?',
                [('No, cancel', False), ('YES, enable', True)],
            )
            if not choice:
                return
            self._flash('Starting hotspot...', 0.5)
            wifi_utils.set_hotspot(True, ssid=ssid, password=password)
            self._flash(f'Hotspot on: {ssid}')
        else:
            # Confirm before disabling too
            choice = self._popup_menu(
                'Disable hotspot?',
                [('No, keep on', False), ('YES, disable', True)],
            )
            if not choice:
                return
            self._flash('Stopping hotspot...', 0.3)
            wifi_utils.set_hotspot(False)
            self._flash('Hotspot off')

    # Map named RTTTL presets → melody strings or None (silent)
    _RTTTL_PRESETS = {
        'None': None,
        'Tetris': None,      # resolved dynamically from Pager class
        'Level Up': None,
        'Game Over': None,
    }

    def _resolve_rtttl(self, name):
        """Look up a preset name on the Pager class (filled lazily)."""
        mapping = {
            'Tetris': 'RTTTL_TETRIS',
            'Level Up': 'RTTTL_LEVEL_UP',
            'Game Over': 'RTTTL_GAME_OVER',
        }
        attr = mapping.get(name)
        if not attr:
            return None
        return getattr(type(self.pager), attr, None)

    def _run_action(self, item):
        action = item.get('action')
        if action == 'backup':
            path = backup_settings()
            self._flash('Backed up' if path else 'Backup failed')
        elif action == 'restore':
            self._restore_picker()
        elif action == 'test_vibrate':
            self._do_vibrate(800)
            self._flash('Vibrate')
        elif action == 'gps_pick_device':
            self._pick_gps_device()
        elif action == 'gps_restart':
            self._restart_gpsd()
        elif action == 'wifi_status':
            ssid = wifi_utils.get_client_status() or wifi_utils.get_client_ssid()
            self._flash(f'SSID: {ssid}' if ssid else 'Not connected')
        elif action == 'wifi_scan_connect':
            self._wifi_scan_connect()
        elif action == 'wifi_hotspot_toggle':
            self._wifi_hotspot_toggle()
        elif action == 'wifi_pineap_toggle':
            self._wifi_pineap_toggle()
        elif action == 'wifi_fix_net':
            self._wifi_fix_net()
        elif action == 'test_beep':
            if not self.config.get('sound_enabled', True):
                self._flash('Sound is off')
                return
            try:
                self.pager.beep(1000, 200)
            except Exception:
                pass
            self._flash('Beep')
        elif action == 'preview':
            if not self.config.get('sound_enabled', True):
                self._flash('Sound is off')
                return
            src = item.get('source')
            name = self.config.get(src, 'None') if src else 'None'
            if not name or name == 'None':
                self._flash('(silent)')
                return
            melody = self._load_rtttl_file(name)
            if melody:
                try:
                    self.pager.play_rtttl(melody)
                except Exception:
                    pass
                display = name[:-6] if name.endswith('.rtttl') else name
                self._flash(f'Playing: {display}')
            else:
                self._flash('Not found')

    def _load_rtttl_file(self, filename, source_dir='/lib/pager/ringtones'):
        """Read an RTTTL file from disk. Returns the melody string or None."""
        path = os.path.join(source_dir, filename)
        try:
            with open(path) as f:
                return f.read().strip()
        except Exception:
            return None

    def _restore_picker(self):
        files = list_backups()
        if not files:
            self._flash('No backups found')
            return
        selected = 0
        cfg = self._layout
        fs = cfg.get('font_size', 18)
        row_h = cfg.get('row_height', 22)
        right_x = cfg.get('right_x', 180)
        y_start = cfg.get('y_start', 52)

        while True:
            self._draw_bg()
            for i, fname in enumerate(files[:8]):
                y = y_start + i * row_h
                c = self._color('category_selected' if i == selected else 'category')
                # Show "20260412_120000" → "2026-04-12 12:00"
                shown = fname.replace('settings_', '').replace('.bak.json', '')
                self.pager.draw_ttf(right_x, y, shown, c, FONT_MENU, fs)
            self._draw_widgets()
            self.pager.flip()

            btn = wait_any_button(self.pager)
            if btn & self.pager.BTN_UP:
                selected = (selected - 1) % len(files)
            elif btn & self.pager.BTN_DOWN:
                selected = (selected + 1) % len(files)
            elif btn & self.pager.BTN_A:
                ok = restore_backup(files[selected])
                self._flash('Restored' if ok else 'Restore failed')
                if ok:
                    self.config = load_config()
                return
            elif btn & self.pager.BTN_B:
                return

    def _flash(self, msg, duration=1.0):
        p = self.pager
        self._draw_bg()
        fs = 18
        tw = p.ttf_width(msg, FONT_MENU, fs)
        box_w = max(tw + 40, 220)
        box_h = 50
        bx = (SCREEN_W - box_w) // 2
        by = (SCREEN_H - box_h) // 2
        p.fill_rect(bx, by, box_w, box_h, p.rgb(0, 0, 0))
        edge = self._color('title')
        p.fill_rect(bx, by, box_w, 1, edge)
        p.fill_rect(bx, by + box_h - 1, box_w, 1, edge)
        p.fill_rect(bx, by, 1, box_h, edge)
        p.fill_rect(bx + box_w - 1, by, 1, box_h, edge)
        p.draw_ttf(bx + (box_w - tw) // 2, by + (box_h - fs) // 2, msg,
                   p.rgb(255, 255, 255), FONT_MENU, fs)
        p.flip()
        time.sleep(duration)
        p.clear_input_events()


_instance = None

def get_settings():
    global _instance
    if _instance is None:
        _instance = SettingsUI()
    return _instance
